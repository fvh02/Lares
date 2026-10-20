"""Remote access: settings, the services for Tailscale and cloudflared, and the two gateways."""

import io
import json
import stat
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

from lares_core.api import make_app
from lares_core.gateway import Gateway
from lares_core.manifest import parse
from lares_core.remote import Remote, check_code, hash_code
from lares_core.runtime import ContainerRuntime
from lares_core.vault import Vault


class RemoteSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.vault = Vault(self.dir)
        self.remote = Remote(self.dir, self.vault, "lares-core", 8000, 8001)

    def tearDown(self):
        self.tmp.cleanup()

    def test_codes(self):
        stored = hash_code("geheimer-code")
        self.assertTrue(check_code("geheimer-code", stored))
        self.assertFalse(check_code("falsch", stored))
        self.assertNotIn("geheimer-code", stored)

    def test_tailscale_settings_and_service(self):
        self.remote.set_tailscale({"hostname": "Lares-Haus", "routen": ["192.168.178.7/24"], "exit_node": True,
                                   "aktiv": True})
        self.assertEqual(self.remote.data["tailscale"]["routen"], ["192.168.178.0/24"])
        for bad in ({"hostname": "mit leerzeichen"}, {"routen": ["kein netz"]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.remote.set_tailscale(bad)
        service = self.remote.tailscale_service()
        self.assertNotIn("TS_AUTHKEY", service.env)
        self.vault.set("tailscale_authkey", "tskey-auth-abc")
        service = self.remote.tailscale_service()
        self.assertEqual(service.env["TS_AUTHKEY"], "tskey-auth-abc")
        self.assertEqual(service.env["TS_HOSTNAME"], "lares-haus")
        self.assertEqual(service.env["TS_ROUTES"], "192.168.178.0/24")
        self.assertEqual(service.env["TS_EXTRA_ARGS"], "--advertise-exit-node")
        serve = json.loads(service.files["serve.json"])
        self.assertEqual(serve["Web"]["${TS_CERT_DOMAIN}:443"]["Handlers"]["/"]["Proxy"], "http://lares-core:8000")
        again = Remote(self.dir, self.vault, "lares-core", 8000, 8001)
        self.assertTrue(again.data["tailscale"]["aktiv"])

    def test_cloudflared_needs_token(self):
        with self.assertRaises(ValueError):
            self.remote.cloudflared_service()
        self.vault.set("cloudflare_tunnel_token", "eyJh\nbGc")
        service = self.remote.cloudflared_service()
        self.assertEqual(service.env, {"TUNNEL_TOKEN": "eyJhbGc"})
        self.assertEqual(service.command, ["tunnel", "--no-autoupdate", "run"])

    def test_release(self):
        self.assertEqual(self.remote.release("spiele", {"host": "Spiele.Example.de.", "zugangscode": "12345678"}),
                         {"host": "spiele.example.de", "zugangscode": True})
        self.assertEqual(self.remote.room_for_host("spiele.example.de:443"), "spiele")
        with self.assertRaises(ValueError):
            self.remote.release("karte", {"host": "spiele.example.de"})
        with self.assertRaises(ValueError):
            self.remote.release("karte", {"zugangscode": "kurz"})
        cookie = self.remote.cookie_for("spiele")
        self.remote.release("spiele", {"zugangscode": "anderer-code"})
        self.assertNotEqual(cookie, self.remote.cookie_for("spiele"))
        self.remote.release("spiele", None)
        self.assertIsNone(self.remote.room_for_host("spiele.example.de"))


FAKE = """#!/bin/sh
echo "$@" >> "{log}"
if [ "$1" = run ]; then cat > "{log}.stdin"; fi
if [ "$1" = inspect ]; then echo running; fi
"""


class ContainerCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_command(self):
        from lares_core.runtime import ContainerError
        runtime = ContainerRuntime("/gibt/es/nicht/docker", "http://x", Path("/srv"))
        with self.assertRaises(ContainerError) as ctx:
            await runtime.remove("lares-room-x")
        self.assertIn("fehlt", str(ctx.exception))

    async def test_services_get_keys_through_stdin(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            fake = d / "docker"
            fake.write_text(FAKE.format(log=d / "log"))
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            vault = Vault(d)
            vault.set("tailscale_authkey", "tskey-auth-geheim")
            remote = Remote(d, vault, "lares-core", 8000, 8001)
            runtime = ContainerRuntime(str(fake), "http://lares-core:8080/api/v1", Path("/srv/lares"), d / "daten")
            await runtime.start_service(remote.tailscale_service())
            log = (d / "log").read_text()
            self.assertNotIn("tskey-auth-geheim", log)
            self.assertIn("--env-file /dev/stdin", log)
            self.assertIn("--name lares-tailscale", log)
            self.assertIn("-v /srv/lares/system/tailscale:/var/lib/tailscale", log)
            self.assertIn("TS_AUTHKEY=tskey-auth-geheim", (d / "log.stdin").read_text())
            self.assertTrue((d / "daten/system/tailscale/serve.json").exists())
            self.assertEqual(await runtime.status("lares-tailscale"), "running")


ROOMS = """
id: web
name: Web
version: 1.0.0
oberflaeche: {{port: {port}, oeffentlich: true}}
---
id: privat
name: Privat
version: 1.0.0
oberflaeche: {{port: {port}}}
"""


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runners = []
        room = web.Application(client_max_size=10 * 1024 ** 2)

        async def echo(request):
            return web.json_response({"pfad": request.path_qs, "kopf": dict(request.headers),
                                      "laenge": len(await request.read())})

        async def weiter(request):
            raise web.HTTPFound("/ziel")

        async def ws(request):
            sock = web.WebSocketResponse(protocols=["spiel"])
            await sock.prepare(request)
            async for msg in sock:
                await sock.send_str("echo:" + msg.data)
            return sock

        room.router.add_get("/ws", ws)
        room.router.add_get("/weiter", weiter)
        room.router.add_route("*", "/{x:.*}", echo)
        room_port = await self.serve(room)
        self.core = make_app(Path(self.tmp.name), background=False)
        for text in ROOMS.format(port=room_port).split("---"):
            self.core["registry"].install(parse(text))
        await self.serve(self.core)
        self.home = f"http://127.0.0.1:{await self.serve(Gateway(self.core, public=False).app())}"
        self.public = f"http://127.0.0.1:{await self.serve(Gateway(self.core, public=True).app())}"
        self.core["accounts"].create("Fabian", "langes-passwort", "admin")
        token = self.core["accounts"].login("Fabian", "langes-passwort")
        self.http = aiohttp.ClientSession(cookies={"lares_sitzung": token})

    async def asyncTearDown(self):
        await self.http.close()
        for runner in reversed(self.runners):
            await runner.cleanup()
        self.tmp.cleanup()

    async def serve(self, app) -> int:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.runners.append(runner)
        return site._server.sockets[0].getsockname()[1]

    async def get(self, url, **kw):
        async with self.http.get(url, allow_redirects=False, **kw) as resp:
            return resp.status, resp.headers, await resp.text()

    async def test_home_gateway(self):
        status, headers, _ = await self.get(self.home + "/r/web")
        self.assertEqual((status, headers["Location"]), (302, "/r/web/"))
        status, _, body = await self.get(self.home + "/r/privat/a/b?x=1", headers={"X-Lares-Zugang": "extern",
                                                                                   "X-Ingress-Path": "/boese"})
        data = json.loads(body)
        self.assertEqual(data["pfad"], "/a/b?x=1")
        self.assertEqual(data["kopf"]["X-Lares-Zugang"], "heim")
        self.assertEqual(data["kopf"]["X-Ingress-Path"], "/r/privat")
        self.assertEqual(data["kopf"]["X-Lares-Prefix"], "/r/privat")
        self.assertEqual((data["kopf"]["X-Lares-Benutzer"], data["kopf"]["X-Lares-Rolle"]), ("Fabian", "admin"))
        status, headers, _ = await self.get(self.home + "/r/web/weiter")
        self.assertEqual(headers["Location"], "/r/web/ziel")
        async with self.http.post(self.home + "/r/web/hoch", data=io.BytesIO(b"x" * 3_000_000)) as resp:
            self.assertEqual((await resp.json())["laenge"], 3_000_000)
        self.assertEqual((await self.get(self.home + "/r/fehlt/"))[0], 404)

    async def test_websocket(self):
        async with self.http.ws_connect(self.home + "/r/web/ws", protocols=["spiel"]) as sock:
            self.assertEqual(sock.protocol, "spiel")
            await sock.send_str("hallo")
            self.assertEqual((await sock.receive()).data, "echo:hallo")

    async def test_public_gateway(self):
        self.assertEqual((await self.get(self.public + "/r/web/"))[0], 404)       # not released yet
        self.assertEqual((await self.get(self.public + "/"))[0], 404)
        remote = self.core["remote"]
        remote.release("web", {"host": "spiele.example.de"})
        status, _, body = await self.get(self.public + "/x", headers={"Host": "spiele.example.de",
                                                                      "CF-Connecting-IP": "203.0.113.9"})
        data = json.loads(body)
        self.assertEqual((status, data["pfad"]), (200, "/x"))
        self.assertEqual(data["kopf"]["X-Lares-Zugang"], "extern")
        self.assertEqual(data["kopf"]["X-Lares-Prefix"], "")
        self.assertEqual(data["kopf"]["X-Forwarded-For"], "203.0.113.9")
        remote.release("privat", {})
        self.assertEqual((await self.get(self.public + "/r/privat/"))[0], 404)    # room.yaml: not public

    async def test_access_code(self):
        self.core["remote"].release("web", {"host": "spiele.example.de", "zugangscode": "richtig-123"})
        host = {"Host": "spiele.example.de"}
        status, _, body = await self.get(self.public + "/seite?a=1", headers=host)
        self.assertEqual(status, 401)
        self.assertIn('name="weiter" value="/seite?a=1"', body)
        async with self.http.post(self.public + "/_lares/zugang", headers=host, allow_redirects=False,
                                  data={"code": "falsch", "weiter": "/seite"}) as resp:
            self.assertEqual(resp.status, 401)
            self.assertIn("stimmt nicht", await resp.text())
        async with self.http.post(self.public + "/_lares/zugang", headers=host, allow_redirects=False,
                                  data={"code": "richtig-123", "weiter": "//boese.example"}) as resp:
            self.assertEqual((resp.status, resp.headers["Location"]), (303, "/"))
            cookie = resp.cookies["lares_zugang_web"]
            self.assertTrue(cookie["httponly"] and cookie["secure"])
        status, _, body = await self.get(self.public + "/seite", headers={
            **host, "Cookie": f"lares_zugang_web={cookie.value}"})
        self.assertEqual((status, json.loads(body)["pfad"]), (200, "/seite"))
        self.assertEqual((await self.get(self.public + "/seite", headers={
            **host, "Cookie": "lares_zugang_web=gefaelscht"}))[0], 401)

    async def test_admin_release_endpoint(self):
        token = self.core["admin_token"]
        port = self.runners[1].addresses[0][1]
        url = f"http://127.0.0.1:{port}/api/v1/admin"
        auth = {"Authorization": f"Bearer {token}"}
        async with self.http.put(f"{url}/rooms/privat/freigabe", json={}, headers=auth) as resp:
            self.assertEqual(resp.status, 400)
        async with self.http.put(f"{url}/rooms/web/freigabe", json={"zugangscode": "12345678"}, headers=auth) as r:
            self.assertEqual(await r.json(), {"host": "", "zugangscode": True})
        async with self.http.get(f"{url}/fernzugriff", headers=auth) as resp:
            data = await resp.json()
        self.assertEqual(data["freigaben"], {"web": {"host": "", "zugangscode": True}})
        self.assertEqual(data["cloudflared"]["ziel"], "http://lares-core:8001")
        self.assertFalse(data["tailscale"]["aktiv"])
        async with self.http.put(f"{url}/fernzugriff/cloudflared", json={"aktiv": True}, headers=auth) as resp:
            self.assertEqual(resp.status, 400)        # no tunnel token yet
        self.assertNotIn("12345678", (Path(self.tmp.name) / "core/fernzugriff.json").read_text())


if __name__ == "__main__":
    unittest.main()

"""HTTPS at home: the ACME client against Pebble (Let's Encrypt's test server), Cloudflare DNS, hot swap, redirect.

The Pebble test runs when LARES_PEBBLE_DIR points at a folder with the binaries pebble and pebble-challtestsrv
(https://github.com/letsencrypt/pebble/releases); in CI the workflow fetches them.
"""

import asyncio
import datetime
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from lares_core import zertifikat
from lares_core.api import make_app
from lares_core.gateway import Gateway

PEBBLE = Path(os.environ.get("LARES_PEBBLE_DIR", "/nonexistent"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def localhost_cert(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = directory / "cert.pem", directory / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(zertifikat.key_pem(key))
    return cert_file, key_file


def wait_port(port: int, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"Port {port} kommt nicht hoch")


@unittest.skipUnless((PEBBLE / "pebble").exists(), "Pebble nicht vorhanden (LARES_PEBBLE_DIR)")
class PebbleTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.pebble_cert, key = localhost_cert(d)
        cls.acme_port, cls.mgmt_port, cls.dns_port, cls.chall_port = (free_port() for _ in range(4))
        (d / "pebble.json").write_text(json.dumps({"pebble": {
            "listenAddress": f"127.0.0.1:{cls.acme_port}", "managementListenAddress": f"127.0.0.1:{cls.mgmt_port}",
            "certificate": str(cls.pebble_cert), "privateKey": str(key), "httpPort": free_port(),
            "tlsPort": free_port(), "ocspResponderURL": "", "externalAccountBindingRequired": False}}))
        env = {**os.environ, "PEBBLE_VA_NOSLEEP": "1", "PEBBLE_WFE_NONCEREJECT": "20", "PEBBLE_AUTHZREUSE": "0"}
        cls.procs = [
            subprocess.Popen([str(PEBBLE / "pebble-challtestsrv"), "-dns01", f"127.0.0.1:{cls.dns_port}",
                              "-management", f"127.0.0.1:{cls.chall_port}", "-http01", "", "-https01", "",
                              "-tlsalpn01", "", "-doh", "", "-defaultIPv4", "127.0.0.1", "-defaultIPv6", ""],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
            subprocess.Popen([str(PEBBLE / "pebble"), "-config", str(d / "pebble.json"), "-dnsserver",
                              f"127.0.0.1:{cls.dns_port}"], env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)]
        wait_port(cls.acme_port)
        wait_port(cls.chall_port)

    @classmethod
    def tearDownClass(cls):
        for proc in cls.procs:
            proc.terminate()
            proc.wait(10)
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.saved = (zertifikat.LETS_ENCRYPT, os.environ.get("LARES_ACME_TESTDNS"))
        zertifikat.LETS_ENCRYPT = f"https://127.0.0.1:{self.acme_port}/dir"
        os.environ["LARES_ACME_TESTDNS"] = f"http://127.0.0.1:{self.chall_port}"
        self.core = make_app(Path(self.data.name), background=False)
        self.core["acme_ssl"] = ssl.create_default_context(cafile=str(self.pebble_cert))
        self.runners = []
        api_port = await self.serve(self.core)
        self.api = f"http://127.0.0.1:{api_port}/api/v1/admin"
        self.auth = {"Authorization": f"Bearer {self.core['admin_token']}"}

    async def asyncTearDown(self):
        zertifikat.LETS_ENCRYPT = self.saved[0]
        if self.saved[1] is None:
            os.environ.pop("LARES_ACME_TESTDNS", None)
        for runner in reversed(self.runners):
            await runner.cleanup()
        self.data.cleanup()

    async def serve(self, app, ssl_context=None, runner=None) -> int:
        if runner is None:
            runner = web.AppRunner(app)
            await runner.setup()
            self.runners.append(runner)
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
        await site.start()
        return site._server.sockets[0].getsockname()[1]

    async def test_certificate_hot_swap_and_redirect(self):
        home = Gateway(self.core, public=False).app()
        runner = web.AppRunner(home)
        await runner.setup()
        self.runners.append(runner)
        http_port = await self.serve(home, runner=runner)
        tls_ctx = self.core["https"].context()                   # listens from the start, with the placeholder
        tls_port = await self.serve(home, ssl_context=tls_ctx, runner=runner)
        self.core["https_port"] = tls_port

        async with aiohttp.ClientSession(headers=self.auth) as http:
            async with http.post(self.api + "/https/holen") as r:
                self.assertEqual(r.status, 400)                  # no address yet
            async with http.put(self.api + "/https", json={"domain": "lares.test", "ip": "192.168.178.20"}) as r:
                self.assertEqual(r.status, 200)
            async with http.post(self.api + "/https/holen") as r:
                self.assertEqual(r.status, 202)
            for _ in range(120):
                async with http.get(self.api + "/https") as r:
                    status = await r.json()
                if not status["laeuft"]:
                    break
                await asyncio.sleep(0.5)
        self.assertEqual(status["fehler"], "", status)
        self.assertTrue(status["aktiv"])
        self.assertEqual(status["zertifikat"]["fuer"], ["lares.test"])
        self.assertIn("Pebble", status["zertifikat"]["aussteller"])
        self.assertTrue(any("zeigt auf 192.168.178.20" in s for s in status["schritte"]))
        self.assertIn("Fertig – HTTPS ist aktiv", status["schritte"])

        # the running server now presents the real certificate for lares.test
        async with aiohttp.ClientSession() as plain:
            async with plain.get(f"https://127.0.0.1:{self.mgmt_port}/intermediates/0",
                                 ssl=self.core["acme_ssl"]) as r:
                intermediate = await r.text()
            async with plain.get(f"https://127.0.0.1:{self.mgmt_port}/roots/0", ssl=self.core["acme_ssl"]) as r:
                root = await r.text()
        trust = ssl.create_default_context(cadata=root + intermediate)
        reader, writer = await asyncio.open_connection("127.0.0.1", tls_port, ssl=trust, server_hostname="lares.test")
        writer.write(b"GET /atrium/zustand HTTP/1.1\r\nHost: lares.test\r\nConnection: close\r\n\r\n")
        response = await reader.read()
        writer.close()
        self.assertTrue(response.startswith(b"HTTP/1.1 200"), response[:100])

        # plain http for the address goes to https; other names (lares.local, the IP) stay on http
        async with aiohttp.ClientSession() as plain:
            async with plain.get(f"http://127.0.0.1:{http_port}/r/x/?a=1", headers={"Host": "lares.test"},
                                 allow_redirects=False) as r:
                self.assertEqual((r.status, r.headers["Location"]), (301, f"https://lares.test:{tls_port}/r/x/?a=1"))
            async with plain.get(f"http://127.0.0.1:{http_port}/atrium/zustand", headers={"Host": "lares.local"},
                                 allow_redirects=False) as r:
                self.assertEqual(r.status, 200)


class CloudflareTest(unittest.IsolatedAsyncioTestCase):
    async def test_records(self):
        records, calls = [], []
        app = web.Application()

        async def zones(request):
            calls.append(("zones", request.query["name"]))
            found = request.query["name"] == "example.de"
            return web.json_response({"success": True, "result": [{"id": "z1"}] if found else []})

        async def dns(request):
            self.assertEqual(request.headers["Authorization"], "Bearer cf-token")
            if request.method == "GET":
                return web.json_response({"success": True, "result": [
                    r for r in records if r["type"] == request.query["type"] and r["name"] == request.query["name"]]})
            body = await request.json()
            records.append({**body, "id": f"r{len(records)}"})
            return web.json_response({"success": True, "result": records[-1]})

        async def one(request):
            rid = request.match_info["rid"]
            if request.method == "DELETE":
                records[:] = [r for r in records if r["id"] != rid]
            else:
                body = await request.json()
                for r in records:
                    if r["id"] == rid:
                        r.update(body)
            return web.json_response({"success": True, "result": {}})
        app.router.add_get("/zones", zones)
        app.router.add_route("*", "/zones/z1/dns_records", dns)
        app.router.add_route("*", "/zones/z1/dns_records/{rid}", one)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        saved = zertifikat.CLOUDFLARE
        zertifikat.CLOUDFLARE = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with aiohttp.ClientSession() as http:
                cf = zertifikat.Cloudflare(http, "cf-token")
                await cf.set_txt("_acme-challenge.lares.example.de", "wert")
                self.assertEqual(calls[:3], [("zones", "acme-challenge.lares.example.de".join(["_", ""])),
                                             ("zones", "lares.example.de"), ("zones", "example.de")])
                self.assertEqual([(r["type"], r["content"]) for r in records], [("TXT", "wert")])
                await cf.clear_txt("_acme-challenge.lares.example.de", "wert")
                self.assertEqual(records, [])
                self.assertTrue(await cf.set_a("lares.example.de", "192.168.178.20"))
                self.assertFalse(await cf.set_a("lares.example.de", "192.168.178.20"))   # unchanged
                self.assertTrue(await cf.set_a("lares.example.de", "192.168.178.21"))
                self.assertEqual([(r["type"], r["content"], r["proxied"]) for r in records],
                                 [("A", "192.168.178.21", False)])
                with self.assertRaises(zertifikat.CertError):
                    await zertifikat.Cloudflare(http, "cf-token").zone("andere.org")
        finally:
            zertifikat.CLOUDFLARE = saved
            await runner.cleanup()


class SettingsTest(unittest.TestCase):
    def test_validation_and_placeholder(self):
        with tempfile.TemporaryDirectory() as d:
            from lares_core.vault import Vault
            https = zertifikat.Https(Path(d), Vault(Path(d)))
            for bad in ({"domain": "kein domain"}, {"ip": "8.8.8.8"}, {"email": "x"}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    https.update(bad)
            self.assertTrue(https.needs_renewal() is False)                 # no address: nothing to do
            https.update({"domain": "Lares.Example.de."})
            self.assertEqual(https.settings["domain"], "lares.example.de")
            self.assertTrue(https.needs_renewal())
            self.assertIsInstance(https.context(), ssl.SSLContext)          # placeholder until the real one
            self.assertFalse(https.status()["aktiv"])
            with self.assertRaises(zertifikat.CertError):
                https.dns(None)                                             # no Cloudflare key


if __name__ == "__main__":
    unittest.main()



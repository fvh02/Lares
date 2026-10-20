"""The Atrium: setup, login with two factors, sessions, roles and the protection of the home gateway."""

import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import unquote

import aiohttp
from aiohttp import web

from lares_core.accounts import AccountError, Accounts, check_totp, totp
from lares_core import weather
from lares_core.api import make_app
from lares_core.gateway import Gateway
from lares_core.manifest import parse


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts = Accounts(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_setup_needs_code(self):
        code = self.accounts.setup_code()
        self.assertRegex(code, r"^[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}$")
        with self.assertRaises(AccountError):
            self.accounts.setup("FALSCH", "Fabian", "langes-passwort")
        self.accounts.setup(code.lower(), "Fabian", "langes-passwort")
        self.assertFalse(self.accounts.needs_setup)
        self.assertFalse((Path(self.tmp.name) / "setup.code").exists())
        with self.assertRaises(AccountError):
            self.accounts.setup(code, "Zweiter", "langes-passwort")

    def test_passwords_sessions_roles(self):
        self.accounts.create("Fabian", "langes-passwort", "admin")
        with self.assertRaises(AccountError):
            self.accounts.create("fabian", "langes-passwort", "bewohner")     # names ignore case
        with self.assertRaises(AccountError):
            self.accounts.create("Kurz", "kurz", "bewohner")
        self.assertNotIn("langes-passwort", (Path(self.tmp.name) / "konten.json").read_text())
        with self.assertRaises(AccountError):
            self.accounts.login("Fabian", "falsch")
        token = self.accounts.login("FABIAN", "langes-passwort")
        self.assertEqual(self.accounts.session(token), {"name": "Fabian", "rolle": "admin"})
        self.assertNotIn(token, (Path(self.tmp.name) / "sitzungen.json").read_text())
        self.assertEqual(Accounts(Path(self.tmp.name)).session(token)["name"], "Fabian")
        with self.assertRaises(AccountError):
            self.accounts.set_role("Fabian", "bewohner")                      # the last admin stays
        with self.assertRaises(AccountError):
            self.accounts.delete("Fabian")
        self.accounts.create("Gast", "noch-ein-passwort", "bewohner")
        guest = self.accounts.login("Gast", "noch-ein-passwort")
        self.accounts.reset_password("Gast", "ganz-neues-passwort")
        self.assertIsNone(self.accounts.session(guest))
        self.accounts.logout(token)
        self.assertIsNone(self.accounts.session(token))

    def test_two_factors(self):
        self.accounts.create("Fabian", "langes-passwort", "admin")
        secret = self.accounts.start_totp("Fabian")["geheimnis"]
        with self.assertRaises(AccountError):
            self.accounts.confirm_totp("Fabian", "000000" if totp(secret) != "000000" else "111111")
        self.accounts.confirm_totp("Fabian", totp(secret))
        with self.assertRaises(AccountError) as ctx:
            self.accounts.login("Fabian", "langes-passwort")
        self.assertEqual(str(ctx.exception), "2fa")
        self.assertTrue(self.accounts.login("Fabian", "langes-passwort", totp(secret)))
        self.assertTrue(check_totp(secret, totp(secret, time.time() - 30)))
        self.assertFalse(check_totp(secret, totp(secret, time.time() - 120)))

    def test_known_totp_vector(self):
        # RFC 6238, SHA-1, secret "12345678901234567890", T = 59 s → 94287082 (last 6 digits)
        self.assertEqual(totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59), "287082")


class AtriumTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runners = []
        room = web.Application()
        room.router.add_get("/{x:.*}", lambda r: web.json_response({"benutzer": r.headers.get("X-Lares-Benutzer")}))
        room_port = await self.serve(room)
        self.core = make_app(Path(self.tmp.name), background=False)
        self.core["registry"].install(parse(f"id: web\nname: Web\nversion: 1.0.0\noberflaeche: {{port: {room_port}}}"))
        await self.serve(self.core)
        self.url = f"http://127.0.0.1:{await self.serve(Gateway(self.core, public=False).app())}"
        self.origin = {"Origin": self.url}
        self.http = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))

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

    async def req(self, method, path, json=None, headers=None):
        async with self.http.request(method, self.url + path, json=json, allow_redirects=False,
                                     headers={**self.origin, **(headers or {})}) as resp:
            try:
                return resp.status, await resp.json(content_type=None), resp.headers
            except ValueError:
                return resp.status, None, resp.headers

    async def test_flow(self):
        status, page, _ = await self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual((await self.req("GET", "/atrium/zustand"))[1], {"eingerichtet": False, "konto": None,
                                                                  "anbieter": [], "darstellung": None})
        # before the login nothing behind the gateway is reachable
        status, _, headers = await self.req("GET", "/r/web/seite", headers={"Accept": "text/html"})
        self.assertEqual((status, unquote(headers["Location"])), (302, "/?weiter=/r/web/seite"))
        self.assertEqual((await self.req("GET", "/r/web/api"))[0], 401)
        self.assertEqual((await self.req("GET", "/api/v1/admin/rooms"))[0], 401)
        self.assertEqual((await self.req("GET", "/atrium/rooms"))[0], 401)
        # setup with the code from the log
        code = self.core["accounts"].setup_code()
        self.assertEqual((await self.req("POST", "/atrium/einrichten", {"code": "x", "name": "Fabian",
                                                                         "passwort": "langes-passwort"}))[0], 400)
        status, _, _ = await self.req("POST", "/atrium/einrichten", {"code": code, "name": "Fabian",
                                                                     "passwort": "langes-passwort"})
        self.assertEqual(status, 200)
        self.assertEqual((await self.req("GET", "/atrium/zustand"))[1]["konto"], {"name": "Fabian", "rolle": "admin"})
        self.assertEqual((await self.req("GET", "/r/web/x"))[1], {"benutzer": "Fabian"})
        self.assertEqual([r["id"] for r in (await self.req("GET", "/atrium/rooms"))[1]], ["web"])
        self.assertEqual((await self.req("GET", "/api/v1/admin/rooms"))[0], 200)
        # changing requests from another page are refused
        status, _, _ = await self.req("POST", "/api/v1/admin/konten", {"name": "Böse", "passwort": "x" * 12},
                                      headers={"Origin": "http://boese.example"})
        self.assertEqual(status, 403)
        status, accounts, _ = await self.req("POST", "/api/v1/admin/konten", {"name": "Gast", "passwort": "gast-passwort",
                                                                             "rolle": "bewohner"})
        self.assertEqual([a["name"] for a in accounts], ["Fabian", "Gast"])
        # logout, then a resident: rooms yes, administration no
        await self.req("POST", "/atrium/abmelden")
        self.assertEqual((await self.req("GET", "/r/web/x"))[0], 401)
        self.assertEqual((await self.req("POST", "/atrium/anmelden", {"name": "Gast", "passwort": "falsch"}))[0], 401)
        self.assertEqual((await self.req("POST", "/atrium/anmelden", {"name": "gast", "passwort": "gast-passwort"}))[0],
                         200)
        self.assertEqual((await self.req("GET", "/r/web/x"))[1], {"benutzer": "Gast"})
        self.assertEqual((await self.req("GET", "/api/v1/admin/rooms"))[0], 403)
        # two factors for the resident
        secret = (await self.req("POST", "/atrium/zwei-faktor/start"))[1]["geheimnis"]
        self.assertEqual((await self.req("POST", "/atrium/zwei-faktor/bestaetigen", {"code": totp(secret)}))[0], 200)
        await self.req("POST", "/atrium/abmelden")
        status, data, _ = await self.req("POST", "/atrium/anmelden", {"name": "Gast", "passwort": "gast-passwort"})
        self.assertEqual((status, data), (401, {"zwei_faktor": True}))
        status, _, _ = await self.req("POST", "/atrium/anmelden", {"name": "Gast", "passwort": "gast-passwort",
                                                                   "code": totp(secret)})
        self.assertEqual(status, 200)

    async def test_appearance(self):
        self.core["accounts"].create("Fabian", "langes-passwort", "admin")
        await self.req("POST", "/atrium/anmelden", {"name": "Fabian", "passwort": "langes-passwort"})
        self.assertEqual((await self.req("GET", "/atrium/zustand"))[1]["darstellung"],
                         {"design": "klassisch", "modus": "auto"})
        status, data, _ = await self.req("PUT", "/atrium/darstellung", {"design": "himmel"})
        self.assertEqual((status, data), (200, {"design": "himmel", "modus": "auto"}))
        self.assertEqual((await self.req("PUT", "/atrium/darstellung", {"modus": "dunkel"}))[1],
                         {"design": "himmel", "modus": "dunkel"})
        self.assertEqual((await self.req("PUT", "/atrium/darstellung", {"design": "neon"}))[0], 400)
        self.assertEqual(self.core["accounts"].appearance("Fabian"), {"design": "himmel", "modus": "dunkel"})

    async def test_weather(self):
        calls = []
        meteo = web.Application()

        async def forecast(request):
            calls.append(dict(request.query))
            return web.json_response({"current": {"temperature_2m": 12.4, "weather_code": 61, "cloud_cover": 90,
                                                  "precipitation": 1.2, "wind_speed_10m": 14},
                                      "daily": {"sunrise": [1760938800], "sunset": [1760976000]}})
        meteo.router.add_get("/v1/forecast", forecast)
        self.addCleanup(setattr, weather, "OPEN_METEO", weather.OPEN_METEO)
        weather.OPEN_METEO = f"http://127.0.0.1:{await self.serve(meteo)}/v1/forecast"
        self.assertEqual((await self.req("GET", "/atrium/wetter"))[0], 404)          # no place known yet
        status, data, _ = await self.req("GET", "/atrium/wetter?lat=52.52437&lon=13.41053")
        self.assertEqual((status, data["code"], data["wolken"], data["quelle"]), (200, 61, 90, "geraet"))
        self.assertEqual((calls[0]["latitude"], calls[0]["longitude"]), ("52.52", "13.41"))   # rounded
        await self.req("GET", "/atrium/wetter?lat=52.5241&lon=13.4091")                     # same km: cache
        self.assertEqual(len(calls), 1)
        self.core["system"].update({"wohnort": {"name": "Zuhause", "lat": 50.94, "lon": 6.96}})
        status, data, _ = await self.req("GET", "/atrium/wetter")
        self.assertEqual((data["quelle"], data["ort"]), ("wohnort", {"lat": 50.94, "lon": 6.96}))
        self.assertEqual((await self.req("GET", "/atrium/wetter?lat=95&lon=0"))[0], 503)

    async def test_login_rate_limit(self):
        self.core["accounts"].create("Fabian", "langes-passwort", "admin")
        statuses = [(await self.req("POST", "/atrium/anmelden", {"name": "Fabian", "passwort": "falsch"}))[0]
                    for _ in range(11)]
        self.assertEqual(statuses[-1], 429)


if __name__ == "__main__":
    unittest.main()

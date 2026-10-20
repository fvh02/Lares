"""End to end: Lares Core in a thread, the example room "hallo" as its own process, and the SDK in this process."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from aiohttp import web

from lares_core.api import make_app

ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT / "sdk" / "python"
sys.path.insert(0, str(SDK))

from lares import LaresFehler, room  # noqa: E402

TESTER = """
id: tester
name: Tester
version: 1.0.0
rechte:
  - befehle
  - ki
  - benachrichtigen
  - ereignisse: {senden: [person.*], empfangen: [hallo.*]}
  - schluessel: [test_key]
"""


class CoreThread:
    def __init__(self, data: Path):
        self.loop = asyncio.new_event_loop()
        self.app = make_app(data)
        self.sent: list[tuple[str, str]] = []
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            runner = web.AppRunner(self.app)
            self.loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, "127.0.0.1", 0)
            self.loop.run_until_complete(site.start())
            self.port = site._server.sockets[0].getsockname()[1]

            async def fake_imessage(phone, text, image=None, filename=""):
                self.sent.append((phone, text))
            self.app["notifier"]._imessage = fake_imessage
            ready.set()
            self.loop.run_forever()
            self.loop.run_until_complete(runner.cleanup())

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(10)
        self.url = f"http://127.0.0.1:{self.port}/api/v1"

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


def call(url: str, method: str = "GET", body=None, token: str = ""):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def wait_for(check, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Zeit abgelaufen")


class LaresEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.core = CoreThread(Path(cls.tmp.name))
        cls.admin = cls.core.app["admin_token"]
        a = cls.admin_call
        status, out = a("/admin/rooms", "POST", {"room_yaml": (ROOT / "examples/hallo/room.yaml").read_text()})
        assert status == 409 and out["bestaetigen"], out
        status, out = a("/admin/rooms", "POST", {"room_yaml": (ROOT / "examples/hallo/room.yaml").read_text(),
                                                 "rechte_bestaetigt": True})
        assert status == 200, out
        env = {**os.environ, "LARES_URL": cls.core.url, "LARES_TOKEN": out["token"], "PYTHONPATH": str(SDK)}
        cls.hallo = subprocess.Popen([sys.executable, str(ROOT / "examples/hallo/main.py")], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status, out = a("/admin/rooms", "POST", {"room_yaml": TESTER, "rechte_bestaetigt": True})
        assert status == 200, out
        os.environ["LARES_URL"], os.environ["LARES_TOKEN"] = cls.core.url, out["token"]
        wait_for(lambda: any(r["id"] == "hallo" and r["verbunden"] for r in a("/admin/rooms")[1]))

    @classmethod
    def tearDownClass(cls):
        cls.hallo.terminate()
        cls.hallo.wait(10)
        cls.core.stop()
        cls.tmp.cleanup()

    @classmethod
    def admin_call(cls, path, method="GET", body=None):
        return call(cls.core.url + path, method, body, cls.admin)

    def test_identity_and_unknown_token(self):
        self.assertEqual(room.ich()["id"], "tester")
        self.assertEqual(call(self.core.url + "/ich", token="falsch")[0], 401)
        self.assertEqual(call(self.core.url + "/admin/rooms", token=os.environ["LARES_TOKEN"])[0], 401)

    def test_command_with_settings(self):
        self.assertEqual(room.ausfuehren("hallo", "gruessen", name="Fabian"), "Hallo, Fabian!")
        status, _ = self.admin_call("/admin/rooms/hallo/einstellungen", "PUT", {"gruss": "Moin"})
        self.assertEqual(status, 200)
        self.assertEqual(room.ausfuehren("hallo", "gruessen", name="Fabian"), "Moin, Fabian!")
        with self.assertRaises(LaresFehler) as ctx:
            room.ausfuehren("hallo", "gruessen", alter=3)
        self.assertEqual(ctx.exception.status, 400)
        listed = room.befehle()
        self.assertEqual([c["name"] for c in listed[0]["befehle"]], ["gruessen"])

    def test_events_between_rooms(self):
        nr = room.ereignis("person.angekommen", {"name": "Fabian"})

        def answered():
            status, events = call(f"{self.core.url}/ereignisse?seit={nr}", token=os.environ["LARES_TOKEN"])
            return [e for e in events if e["name"] == "hallo.begruesst"]
        self.assertEqual(wait_for(answered)[0]["daten"], {"person": "Fabian"})
        with self.assertRaises(LaresFehler) as ctx:
            room.ereignis("hallo.gefaelscht")
        self.assertEqual(ctx.exception.status, 403)

    def test_keys_and_rights(self):
        self.assertIsNone(room.schluessel("test_key"))
        self.admin_call("/admin/schluessel/test_key", "PUT", {"wert": "geheim"})
        self.assertEqual(room.schluessel("test_key"), "geheim")
        with self.assertRaises(LaresFehler) as ctx:
            room.schluessel("inkbox_api_key")
        self.assertEqual(ctx.exception.status, 403)
        status, keys = self.admin_call("/admin/schluessel")
        self.assertNotIn("geheim", json.dumps(keys))
        self.assertIn({"name": "test_key", "eingetragen": True, "rooms": ["tester"]}, keys)
        with self.assertRaises(LaresFehler) as ctx:
            room.standort()
        self.assertEqual(ctx.exception.status, 403)

    def test_ai_needs_a_provider(self):
        with self.assertRaises(LaresFehler) as ctx:
            room.ki("Hallo?")
        self.assertEqual(ctx.exception.status, 503)

    def test_ai_through_provider_room(self):
        yaml = "id: kibot\nname: KI-Bot\nversion: 1.0.0\nrechte: [ki_anbieter]\n"
        status, out = self.admin_call("/admin/rooms", "POST", {"room_yaml": yaml, "rechte_bestaetigt": True})
        self.assertEqual(status, 200)
        script = ("from lares import room\n"
                  "room.ki_anbieten(lambda frage, schema, von: {'echo': frage, 'von': von, 'schema': schema})\n"
                  "room.laufen()\n")
        env = {**os.environ, "LARES_TOKEN": out["token"], "PYTHONPATH": str(SDK)}
        bot = subprocess.Popen([sys.executable, "-c", script], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(lambda: self.core.app["bus"].connected("kibot"))
            answer = room.ki("Wie spät?", schema={"type": "object"})
            self.assertEqual(answer, {"echo": "Wie spät?", "von": "tester", "schema": {"type": "object"}})
            self.assertEqual(self.admin_call("/admin/ki")[1]["tester"]["anfragen"], 1)
        finally:
            bot.terminate()
            bot.wait(10)
            self.admin_call("/admin/rooms/kibot", "DELETE")

    def test_notify_and_quiet_hours(self):
        self.admin_call("/admin/system", "PUT", {"bewohner": [{"name": "Fabian", "telefon": "+491701234567"}],
                                                 "ruhezeit": None})
        self.assertEqual(room.melden("Test"), {"gesendet": True})
        self.assertIn(("+491701234567", "Tester: Test"), self.core.sent)
        hour = self.core.app["system"].now().hour
        self.admin_call("/admin/system", "PUT", {"ruhezeit": {"von": hour, "bis": (hour + 1) % 24}})
        self.assertEqual(room.melden("Später"), {"wartet": True})
        self.assertEqual(room.melden("Jetzt", dringend=True), {"gesendet": True})
        self.admin_call("/admin/system", "PUT", {"ruhezeit": None})

    def test_schedule_wakes_room(self):
        self.admin_call("/admin/system", "PUT", {"bewohner": [{"name": "Fabian", "telefon": "+491701234567"}],
                                                 "ruhezeit": None})
        self.admin_call("/admin/rooms/hallo/einstellungen", "PUT", {"morgens_melden": True, "gruss": "Hallo"})
        self.core.loop.call_soon_threadsafe(self.core.app["bus"].wake, "hallo", "morgens")
        wait_for(lambda: any(t.endswith("guten Morgen!") for _, t in self.core.sent))


if __name__ == "__main__":
    unittest.main()

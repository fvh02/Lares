"""Sessions, warnings about new devices and failed logins, and USB devices for rooms."""

import asyncio
import stat
import tempfile
import time
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

from lares_core import usb
from lares_core.accounts import Accounts
from lares_core.api import make_app
from lares_core.atrium import device_label, network_of
from lares_core.gateway import Gateway
from lares_core.manifest import parse
from lares_core.runtime import ContainerRuntime

IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile Safari/604.1"
WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36 Edg/131.0"


class HelpersTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(device_label(IPHONE), "Safari auf iPhone")
        self.assertEqual(device_label(WINDOWS), "Edge auf Windows")
        self.assertEqual(device_label(""), "Unbekanntes Gerät")
        self.assertEqual(network_of("192.168.178.23"), "Heimnetz")
        self.assertEqual(network_of("100.101.2.3"), "Tailscale")
        self.assertEqual(network_of("93.184.216.34"), "Internet")
        self.assertEqual(network_of("127.0.0.1"), "dieses Gerät")

    def test_sessions_and_block_token(self):
        with tempfile.TemporaryDirectory() as d:
            a = Accounts(Path(d))
            a.create("Fabian", "langes-passwort", "admin")
            t1 = a.new_session("Fabian", {"geraet": "Safari auf iPhone", "ip": "192.168.178.23", "netz": "Heimnetz",
                                          "methode": "Passkey"})
            t2 = a.new_session("Fabian", {"geraet": "Edge auf Windows"})
            listed = a.list_sessions("Fabian", t1)
            self.assertEqual((listed[0]["aktuell"], listed[0]["geraet"], listed[0]["methode"]),
                             (True, "Safari auf iPhone", "Passkey"))
            self.assertNotIn(t1, str(listed))
            self.assertTrue(a.end_session("Fabian", listed[1]["id"]))
            self.assertIsNone(a.session(t2))
            a.new_session("Fabian")
            self.assertEqual(a.end_other_sessions("Fabian", t1), 1)
            self.assertIsNotNone(a.session(t1))
            token = a.block_token("Fabian")
            self.assertEqual(a.use_block_token(token), "Fabian")
            self.assertIsNone(a.session(t1))
            self.assertIsNone(a.use_block_token(token))                       # only once
            old = a.block_token("Fabian")
            for key in a.accounts["Fabian"]["sperren"]:
                a.accounts["Fabian"]["sperren"][key] = time.time() - 1
            self.assertIsNone(a.use_block_token(old))                         # expired


class WarningTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runners = []
        self.core = make_app(Path(self.tmp.name), background=False)
        self.core["accounts"].create("Fabian", "langes-passwort", "admin")
        self.core["system"].update({"bewohner": [{"name": "Fabian", "telefon": "+491701234567"}]})
        await self.serve(self.core)
        self.sent = []

        async def fake(phone, text, image=None, filename=""):
            self.sent.append((phone, text))
        self.core["notifier"]._imessage = fake
        self.url = f"http://127.0.0.1:{await self.serve(Gateway(self.core, public=False).app())}"

    async def asyncTearDown(self):
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

    def browser(self, ua=IPHONE):
        return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                                     headers={"User-Agent": ua, "Origin": self.url})

    async def login(self, http, password="langes-passwort"):
        async with http.post(self.url + "/atrium/anmelden", json={"name": "Fabian", "passwort": password}) as r:
            return r.status

    async def settle(self):
        await asyncio.sleep(0.2)

    async def test_new_device_is_reported_known_device_not(self):
        async with self.browser() as phone:
            self.assertEqual(await self.login(phone), 200)                    # first login ever: no warning
            await self.settle()
            self.assertEqual(self.sent, [])
            await phone.post(self.url + "/atrium/abmelden")
            self.assertEqual(await self.login(phone), 200)                    # same device again: no warning
            await self.settle()
            self.assertEqual(self.sent, [])
            async with phone.get(self.url + "/atrium/sitzungen") as r:
                mine = await r.json()
        self.assertEqual(mine[0]["geraet"], "Safari auf iPhone")
        self.assertEqual(mine[0]["methode"], "Passwort")
        async with self.browser(WINDOWS) as other:
            self.assertEqual(await self.login(other), 200)                    # an unknown device
            await self.settle()
            self.assertEqual(len(self.sent), 1)
            phone_no, text = self.sent[0]
            self.assertEqual(phone_no, "+491701234567")
            self.assertIn("Edge auf Windows", text)
            link = text.split("beenden: ")[1].strip()
            self.assertTrue(link.startswith(self.url + "/atrium/sperren/"))
            # opening the link (a link preview, too) only shows the button …
            async with aiohttp.ClientSession() as preview:
                async with preview.get(link) as r:
                    self.assertIn("Alle Sitzungen beenden", await r.text())
            async with other.get(self.url + "/atrium/sitzungen") as r:
                self.assertEqual(r.status, 200)
            # … the button ends all sessions of the account
            async with aiohttp.ClientSession(headers={"Origin": self.url}) as owner:
                async with owner.post(link) as r:
                    self.assertEqual(r.status, 200)
                async with owner.post(link) as r:
                    self.assertEqual(r.status, 410)
            async with other.get(self.url + "/atrium/sitzungen") as r:
                self.assertEqual(r.status, 401)

    async def test_failed_logins_warn_once(self):
        async with self.browser() as http:
            for _ in range(6):
                self.assertEqual(await self.login(http, "falsch-falsch"), 401)
        await self.settle()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("falsche Anmeldeversuche", self.sent[0][1])


# -- USB ------------------------------------------------------------------------------------------
def write(path: Path, files: dict):
    path.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        (path / name).write_text(value + "\n")


def fake_sysfs(root: Path, with_zigbee=True):
    if with_zigbee:
        write(root / "1-2", {"idVendor": "10c4", "idProduct": "ea60", "product": "Sonoff Zigbee 3.0 USB Dongle Plus",
                             "manufacturer": "ITead", "serial": "a1b2", "bDeviceClass": "00", "busnum": "1",
                             "devnum": "5"})
        write(root / "1-2" / "1-2:1.0", {"bInterfaceClass": "ff"})
        (root / "1-2" / "1-2:1.0" / "ttyUSB0" / "tty" / "ttyUSB0").mkdir(parents=True)
    write(root / "1-1", {"idVendor": "05e3", "idProduct": "0610", "product": "USB2.0 Hub", "bDeviceClass": "09",
                         "busnum": "1", "devnum": "2"})
    write(root / "2-1", {"idVendor": "0781", "idProduct": "5581", "product": "Ultra", "bDeviceClass": "00",
                         "busnum": "2", "devnum": "3"})
    write(root / "2-1" / "2-1:1.0", {"bInterfaceClass": "08"})
    write(root / "usb1", {"idVendor": "1d6b", "idProduct": "0002", "bDeviceClass": "09"})


class UsbTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = usb.SYSFS
        usb.SYSFS = self.root / "sys"
        fake_sysfs(usb.SYSFS)

    async def asyncTearDown(self):
        usb.SYSFS = self.saved
        self.tmp.cleanup()

    def test_scan(self):
        devices = {d["name"]: d for d in usb.scan()}
        self.assertEqual(set(devices), {"Sonoff Zigbee 3.0 USB Dongle Plus", "Ultra"})     # no hubs
        zigbee = devices["Sonoff Zigbee 3.0 USB Dongle Plus"]
        self.assertEqual(zigbee["schluessel"], "10c4:ea60:a1b2")
        self.assertEqual(zigbee["knoten"], ["/dev/ttyUSB0", "/dev/bus/usb/001/005"])
        self.assertTrue(zigbee["art"].startswith("Seriell"))
        self.assertFalse(devices["Ultra"]["zuweisbar"])                                      # mass storage

    def test_assign_and_changes(self):
        u = usb.Usb(self.root / "core")
        self.assertEqual(u.changes(), set())
        u.assign("10c4:ea60:a1b2", "zigbee")
        self.assertEqual(u.nodes_for("zigbee"), ["/dev/ttyUSB0", "/dev/bus/usb/001/005"])
        self.assertEqual(usb.Usb(self.root / "core").assigned, {"10c4:ea60:a1b2": "zigbee"})
        u.changes()
        import shutil
        shutil.rmtree(usb.SYSFS / "1-2")                                                     # unplugged
        self.assertEqual(u.changes(), {"zigbee"})
        self.assertEqual(u.nodes_for("zigbee"), [])
        fake_sysfs(usb.SYSFS)                                                                # plugged in again
        self.assertEqual(u.changes(), {"zigbee"})
        u.forget_room("zigbee")
        self.assertEqual(u.assigned, {})

    async def test_container_gets_only_its_devices(self):
        log = self.root / "docker.log"
        fake = self.root / "docker"
        fake.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\ncat > /dev/null\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        runtime = ContainerRuntime(str(fake), "http://lares-core:8080/api/v1", Path("/srv/lares"))
        core = make_app(self.root / "data", runtime=runtime, background=False)
        for text in ("id: zigbee\nname: Zigbee\nversion: 1.0.0\nimage: x/zigbee\nrechte: [usb]",
                     "id: serien\nname: Serien\nversion: 1.0.0\nimage: x/serien"):
            core["registry"].install(parse(text))
        core["usb"].assign("10c4:ea60:a1b2", "zigbee")
        from lares_core.api import start_room
        await start_room(core, core["registry"].get("zigbee"))
        await start_room(core, core["registry"].get("serien"))
        runs = [line for line in log.read_text().splitlines() if line.startswith("run ")]
        self.assertIn("--device /dev/ttyUSB0 --device /dev/bus/usb/001/005", runs[0])
        self.assertNotIn("--device", runs[1])

    async def test_api_needs_the_usb_right(self):
        core = make_app(self.root / "data", background=False)
        core["registry"].install(parse("id: serien\nname: Serien\nversion: 1.0.0"))
        core["registry"].install(parse("id: zigbee\nname: Zigbee\nversion: 1.0.0\nrechte: [usb]"))
        runner = web.AppRunner(core)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/api/v1/admin/usb"
        auth = {"Authorization": f"Bearer {core['admin_token']}"}
        try:
            async with aiohttp.ClientSession(headers=auth) as http:
                async with http.get(url) as r:
                    data = await r.json()
                self.assertEqual([r["id"] for r in data["rooms"]], ["zigbee"])
                async with http.put(url, json={"schluessel": "10c4:ea60:a1b2", "room": "serien"}) as r:
                    self.assertEqual(r.status, 400)
                async with http.put(url, json={"schluessel": "0781:5581:2-1", "room": "zigbee"}) as r:
                    self.assertEqual(r.status, 400)                                        # storage
                async with http.put(url, json={"schluessel": "10c4:ea60:a1b2", "room": "zigbee"}) as r:
                    self.assertEqual(r.status, 200)
                async with http.get(url) as r:
                    data = await r.json()
                zigbee = next(d for d in data["geraete"] if d["schluessel"] == "10c4:ea60:a1b2")
                self.assertEqual(zigbee["room"], "zigbee")
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()

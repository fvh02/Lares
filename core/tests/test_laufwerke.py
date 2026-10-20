"""Where the rooms live on Lares OS: disks, the choice, and moving between stick and disk."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

from lares_core import laufwerke
from lares_core.api import make_app
from lares_core.gateway import Gateway

GB = 1024 ** 3


def lsblk(hdd_lares: bool = True) -> str:
    return json.dumps({"blockdevices": [
        {"name": "sda", "path": "/dev/sda", "size": 32 * GB, "type": "disk", "tran": "usb", "rm": True, "rota": False,
         "model": "SanDisk Ultra", "mountpoints": [None], "children": [
             {"name": "sda1", "path": "/dev/sda1", "size": GB // 2, "type": "part", "fstype": "vfat", "partlabel": "esp",
              "mountpoints": ["/efi"]},
             {"name": "sda2", "path": "/dev/sda2", "size": 4 * GB, "type": "part", "fstype": "ext4",
              "label": "lares-system", "mountpoints": ["/"]},
             {"name": "sda3", "path": "/dev/sda3", "size": 27 * GB, "type": "part", "fstype": "ext4",
              "partlabel": "lares-data", "mountpoints": ["/data"]}]},
        {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 512 * GB, "type": "disk", "tran": "nvme", "rm": False,
         "rota": False, "model": "Samsung SSD 980", "mountpoints": [None], "children": [
             {"name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "size": 512 * GB, "type": "part", "fstype": "ntfs",
              "label": "Windows", "uuid": "AAAA", "mountpoints": [None]}]},
        {"name": "sdb", "path": "/dev/sdb", "size": 1000 * GB, "type": "disk", "tran": "sata", "rm": False,
         "rota": True, "model": "WD Blue", "mountpoints": [None], "children": [
             {"name": "sdb1", "path": "/dev/sdb1", "size": 1000 * GB, "type": "part", "fstype": "ext4",
              "label": "lares-rooms" if hdd_lares else "daten", "uuid": "uuid-hdd", "mountpoints": [None]}]},
        {"name": "sdc", "path": "/dev/sdc", "size": 8 * GB, "type": "disk", "tran": "sata", "rm": False,
         "rota": False, "model": "Tiny", "mountpoints": [None], "children": []},
        {"name": "loop0", "path": "/dev/loop0", "size": GB, "type": "loop"},
        {"name": "zram0", "path": "/dev/zram0", "size": GB, "type": "disk"},
    ]})


class DiskTest(unittest.TestCase):
    def test_parse(self):
        disks = {d.path: d for d in laufwerke.parse_lsblk(lsblk())}
        self.assertEqual(set(disks), {"/dev/sda", "/dev/nvme0n1", "/dev/sdb", "/dev/sdc"})
        self.assertTrue(disks["/dev/sda"].system)
        self.assertEqual(disks["/dev/sda"].kind, "usb")
        self.assertEqual((disks["/dev/nvme0n1"].kind, disks["/dev/sdb"].kind, disks["/dev/sdc"].kind),
                         ("nvme", "hdd", "ssd"))
        self.assertEqual(disks["/dev/sdb"].lares_partition, {"path": "/dev/sdb1", "uuid": "uuid-hdd"})
        public = {p: d.public() for p, d in disks.items()}
        self.assertFalse(public["/dev/sda"]["geeignet"])          # the stick itself
        self.assertFalse(public["/dev/sdc"]["geeignet"])          # too small
        self.assertTrue(public["/dev/nvme0n1"]["geeignet"])
        self.assertEqual(public["/dev/nvme0n1"]["partitionen"][0]["dateisystem"], "ntfs")


class FakeSystem:
    """Stands in for lsblk, mount, mkfs … – and really copies and moves the folders."""

    def __init__(self, hdd_lares=True):
        self.mounted: set[str] = set()
        self.calls: list[tuple] = []
        self.hdd_lares = hdd_lares

    async def run(self, *args, check=True):
        self.calls.append(args)
        if args[0] == "lsblk" and "-J" in args:
            return lsblk(self.hdd_lares)
        if args[0] == "lsblk":
            return "/dev/x\n/dev/x1"
        if args[0] == "blkid":
            return "uuid-neu"
        if args[0] == "cp":
            shutil.copytree(args[2].rstrip("/."), args[3], dirs_exist_ok=True, symlinks=True)
        if args[0] == "mv":
            shutil.move(args[1], args[2])
        if args[0] == "rm":
            shutil.rmtree(args[2], ignore_errors=True)
        if args[0] == "mount":
            self.mounted.add(args[-1])
        if args[0] == "umount":
            self.mounted.discard(args[-1])
        return ""

    def names(self):
        return [c[0] for c in self.calls]


class MoveTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data, self.mount = root / "data", root / "srv-lares-rooms"
        self.saved = (laufwerke.MOUNT, laufwerke.RUN, dict(laufwerke.BINDS), laufwerke.run, laufwerke.is_mounted)
        laufwerke.MOUNT, laufwerke.RUN = self.mount, root / "run"
        laufwerke.BINDS.update(docker=root / "var-lib-docker", rooms=root / "var-lib-lares-rooms")
        (self.data / "rooms" / "serien").mkdir(parents=True)
        (self.data / "rooms" / "serien" / "liste.json").write_text('["Severance"]')
        (self.data / "docker" / "overlay2").mkdir(parents=True)
        (self.data / "docker" / "overlay2" / "schicht").write_bytes(b"x" * 1000)
        self.events = []
        self.mounted = set()          # what the fake system has mounted

    async def asyncTearDown(self):
        laufwerke.MOUNT, laufwerke.RUN, binds, laufwerke.run, laufwerke.is_mounted = self.saved
        laufwerke.BINDS.clear()
        laufwerke.BINDS.update(binds)
        self.tmp.cleanup()

    def move(self, system, target, wipe=False):
        system.mounted = self.mounted
        laufwerke.run = system.run
        laufwerke.is_mounted = lambda p: str(p) in self.mounted

        async def stop():
            self.events.append("stop")

        async def start(base):
            self.events.append(("start", base))

        async def systemctl(*args):
            self.events.append(("systemctl",) + args)
        return laufwerke.Move(self.data, target, wipe, stop, start, systemctl)

    async def test_to_new_disk_and_back(self):
        system = FakeSystem(hdd_lares=False)
        move = self.move(system, "/dev/nvme0n1")
        await move()
        self.assertTrue(move.done, move.error)
        names = system.names()
        self.assertIn("wipefs", names)
        self.assertIn("mkfs.ext4", names)
        self.assertEqual(laufwerke.load_choice(self.data)["uuid"], "uuid-neu")
        self.assertEqual(laufwerke.load_choice(self.data)["modell"], "Samsung SSD 980")
        self.assertEqual(json.loads((self.mount / "rooms" / "serien" / "liste.json").read_text()), ["Severance"])
        self.assertTrue((self.mount / "docker" / "overlay2" / "schicht").exists())
        self.assertFalse((self.data / "rooms").exists())                    # old data moved aside …
        self.assertFalse((self.data / "docker").exists())                   # … old images removed
        self.assertTrue(list((self.data / "alt").glob("*/rooms/serien/liste.json")))
        self.assertEqual(self.events[0], "stop")
        self.assertIn(("systemctl", "stop", "docker.socket", "docker"), self.events)
        self.assertEqual(self.events[-1], ("start", self.mount))
        binds = [c for c in system.calls if c[:2] == ("mount", "--bind")]
        self.assertEqual({c[2] for c in binds}, {str(self.mount / "docker"), str(self.mount / "rooms")})

        system, self.events = FakeSystem(hdd_lares=False), []
        back = self.move(system, "usb")
        await back()
        self.assertTrue(back.done, back.error)
        self.assertEqual(laufwerke.load_choice(self.data), {"ort": "usb"})
        self.assertEqual(json.loads((self.data / "rooms" / "serien" / "liste.json").read_text()), ["Severance"])
        self.assertNotIn("wipefs", system.names())

    async def test_existing_lares_disk_is_kept(self):
        system = FakeSystem(hdd_lares=True)
        move = self.move(system, "/dev/sdb")
        await move()
        self.assertTrue(move.done, move.error)
        self.assertNotIn("wipefs", system.names())
        self.assertNotIn("mkfs.ext4", system.names())
        self.assertEqual(laufwerke.load_choice(self.data)["uuid"], "uuid-hdd")
        self.assertTrue(any("übernehmen" in s for s in move.steps))

    async def test_refuses_the_stick_and_restarts_on_error(self):
        system = FakeSystem()
        move = self.move(system, "/dev/sda")
        await move()
        self.assertIn("Lares-Stick", move.error)
        self.assertFalse(move.done)
        self.assertIsNone(laufwerke.load_choice(self.data))
        self.assertEqual(self.events[-1], ("start", self.data))                # back where it was

    async def test_boot_falls_back_to_the_stick(self):
        system = FakeSystem()

        async def failing(*args, check=True):
            if args[0] == "mount" and "--bind" not in args:
                raise laufwerke.LaufwerkError("special device UUID=weg does not exist")
            return await system.run(*args)
        laufwerke.run = failing
        laufwerke.storage.save(laufwerke.config_file(self.data), {"ort": "laufwerk", "uuid": "weg", "modell": "WD"})
        base = await laufwerke.boot(self.data)
        self.assertEqual(base, self.data)
        self.assertIn("WD", (laufwerke.RUN / "speicher-fehler").read_text())


class StorageApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = laufwerke.run
        self.system = FakeSystem(hdd_lares=False)
        laufwerke.run = self.system.run
        self.core = make_app(Path(self.tmp.name), background=False, lares_os=True)
        self.core["accounts"].create("Fabian", "langes-passwort", "admin")
        self.runners = []
        await self.serve(self.core)
        self.url = f"http://127.0.0.1:{await self.serve(Gateway(self.core, public=False).app())}"
        self.http = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        await self.req("POST", "/atrium/anmelden", {"name": "Fabian", "passwort": "langes-passwort"})

    async def asyncTearDown(self):
        laufwerke.run = self.saved
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

    async def req(self, method, path, json=None):
        async with self.http.request(method, self.url + path, json=json, headers={"Origin": self.url}) as resp:
            return resp.status, await resp.json(content_type=None)

    async def test_choice(self):
        self.assertTrue((await self.req("GET", "/atrium/zustand"))[1]["speicher_waehlen"])
        status, data = await self.req("GET", "/api/v1/admin/speicher")
        self.assertEqual((status, data["lares_os"], data["gewaehlt"]), (200, True, False))
        self.assertEqual([d["geraet"] for d in data["laufwerke"] if d["geeignet"]], ["/dev/nvme0n1", "/dev/sdb"])
        # a disk with data needs the explicit confirmation
        status, data = await self.req("POST", "/api/v1/admin/speicher", {"ziel": "/dev/nvme0n1", "loeschen": True})
        self.assertEqual(status, 409)
        self.assertEqual((await self.req("POST", "/api/v1/admin/speicher", {"ziel": "/dev/sda"}))[0], 400)
        # the stick: nothing to move, chosen at once
        status, data = await self.req("POST", "/api/v1/admin/speicher", {"ziel": "usb"})
        self.assertEqual((status, data["fertig"]), (200, True))
        self.assertFalse((await self.req("GET", "/atrium/zustand"))[1]["speicher_waehlen"])

    async def test_not_on_other_systems(self):
        self.core["lares_os"] = False
        self.assertEqual((await self.req("GET", "/api/v1/admin/speicher"))[1], {"lares_os": False})
        self.assertFalse((await self.req("GET", "/atrium/zustand"))[1]["speicher_waehlen"])


if __name__ == "__main__":
    unittest.main()



"""Where the rooms live on Lares OS: on the USB stick (next to the system) or on an internal HDD/SSD.

Lares itself – system, Lares Core, keys, accounts – always stays on the stick (/data). The rooms (their data folders)
and the container images go wherever the owner chooses:

  usb        /data/rooms, /data/docker                      – nothing to set up, the stick only
  laufwerk   /srv/lares-rooms/{rooms,docker} on a disk      – formatted once as "lares-rooms" (ext4)

The choice is kept in /data/core/speicher.json. At every start `python -m lares_core.laufwerke boot` mounts the disk
and binds its folders to the fixed places Docker and Lares Core use – /var/lib/docker and /var/lib/lares/rooms – so
neither needs other settings and the system partition can stay read-only.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import storage

log = logging.getLogger("lares.laufwerke")
LABEL = "lares-rooms"
MOUNT = Path(os.environ.get("LARES_ROOMS_MOUNT", "/srv/lares-rooms"))
RUN = Path(os.environ.get("LARES_RUN", "/run/lares"))
BINDS = {"docker": Path(os.environ.get("LARES_DOCKER_DIR", "/var/lib/docker")),
         "rooms": Path(os.environ.get("LARES_ROOMS_BIND", "/var/lib/lares/rooms"))}
MIN_SIZE = 16 * 1024 ** 3
LSBLK = "NAME,PATH,SIZE,TYPE,TRAN,RM,ROTA,MODEL,PARTLABEL,LABEL,FSTYPE,MOUNTPOINTS,UUID"


class LaufwerkError(RuntimeError):
    pass


def is_mounted(path) -> bool:
    return os.path.ismount(path)


async def run(*args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    text = out.decode(errors="replace").strip()
    if check and proc.returncode:
        raise LaufwerkError(f"{args[0]}: {text[-300:]}")
    return text


# -- what is there --------------------------------------------------------------------------------
@dataclass
class Disk:
    path: str
    size: int
    model: str
    kind: str                       # usb, ssd, nvme, hdd
    system: bool                    # the stick Lares runs from
    lares_partition: dict | None    # an existing "lares-rooms" partition: {path, uuid}
    partitions: list[dict] = field(default_factory=list)

    def public(self) -> dict:
        return {"geraet": self.path, "groesse": self.size, "modell": self.model, "typ": self.kind,
                "system": self.system, "lares_speicher": bool(self.lares_partition),
                "partitionen": [{"name": p["path"], "groesse": p["size"], "dateisystem": p["fstype"] or "",
                                 "name_label": p["label"] or ""} for p in self.partitions],
                "geeignet": not self.system and self.size >= MIN_SIZE}


def parse_lsblk(output: str) -> list[Disk]:
    """Reads `lsblk -J -b -o …`; only real disks (no loop, zram or CD)."""
    disks = []
    for dev in json.loads(output).get("blockdevices", []):
        if dev.get("type") != "disk" or dev.get("name", "").startswith(("loop", "zram", "ram", "sr")):
            continue
        parts = [{"path": c.get("path"), "size": int(c.get("size") or 0), "label": c.get("label"),
                  "partlabel": c.get("partlabel"), "fstype": c.get("fstype"), "uuid": c.get("uuid"),
                  "mounts": [m for m in (c.get("mountpoints") or []) if m]} for c in dev.get("children") or []]
        mounts = {m for p in parts for m in p["mounts"]} | set(m for m in dev.get("mountpoints") or [] if m)
        system = bool(mounts & {"/", "/data", "/usr", "/boot", "/efi"}) or any(
            p["partlabel"] == "lares-data" for p in parts)
        tran = (dev.get("tran") or "").lower()
        kind = "usb" if tran == "usb" or str(dev.get("rm")) in ("1", "True", "true") else \
            "nvme" if tran == "nvme" or dev["name"].startswith("nvme") else \
            "hdd" if str(dev.get("rota")) in ("1", "True", "true") else "ssd"
        lares = next(({"path": p["path"], "uuid": p["uuid"]} for p in parts
                      if p["label"] == LABEL and p["fstype"] == "ext4" and p["uuid"]), None)
        disks.append(Disk(dev.get("path") or f"/dev/{dev['name']}", int(dev.get("size") or 0),
                          (dev.get("model") or "").strip() or dev["name"], kind, system, lares, parts))
    return disks


async def list_disks() -> list[Disk]:
    return parse_lsblk(await run("lsblk", "-J", "-b", "-o", LSBLK))


# -- the choice -----------------------------------------------------------------------------------
def config_file(data_dir: Path) -> Path:
    return data_dir / "core" / "speicher.json"


def load_choice(data_dir: Path) -> dict | None:
    return storage.load(config_file(data_dir), None)


def base_for(choice: dict | None, data_dir: Path) -> Path:
    """Where rooms and container images live for this choice."""
    return MOUNT if choice and choice.get("ort") == "laufwerk" else data_dir


def free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def used_bytes(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


# -- at boot --------------------------------------------------------------------------------------
async def mount_disk(uuid: str) -> None:
    MOUNT.mkdir(parents=True, exist_ok=True)
    if is_mounted(MOUNT):
        return
    await run("mount", "-o", "noatime", f"UUID={uuid}", str(MOUNT))


async def bind(base: Path) -> None:
    """Puts <base>/docker on /var/lib/docker and <base>/rooms on /var/lib/lares/rooms (Docker must be stopped)."""
    for name, target in BINDS.items():
        (base / name).mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
        while is_mounted(target):
            await run("umount", str(target))
        await run("mount", "--bind", str(base / name), str(target))


async def boot(data_dir: Path) -> Path:
    """Runs before Docker and Lares Core. If the disk is missing, Lares still starts – from the stick – and says so."""
    choice = load_choice(data_dir)
    base = base_for(choice, data_dir)
    problem = ""
    if choice and choice.get("ort") == "laufwerk":
        try:
            await mount_disk(choice["uuid"])
        except (LaufwerkError, KeyError) as err:
            problem = f"Das Laufwerk für die Rooms ({choice.get('modell', '?')}) fehlt: {err}"
            log.error(problem)
            base = data_dir
    await bind(base)
    RUN.mkdir(parents=True, exist_ok=True)
    if problem:
        (RUN / "speicher-fehler").write_text(problem)
    else:
        (RUN / "speicher-fehler").unlink(missing_ok=True)
    return base


# -- moving the rooms -----------------------------------------------------------------------------
class Move:
    """One move of the rooms between stick and disk, step by step; the Atrium shows the progress."""

    def __init__(self, data_dir: Path, target: str, wipe: bool, stop_all, start_all, systemctl=None):
        self.data_dir, self.target, self.wipe = data_dir, target, wipe
        self.stop_all, self.start_all = stop_all, start_all
        self.systemctl = systemctl or (lambda *a: run("systemctl", *a))
        self.steps: list[str] = []
        self.done = False
        self.error = ""
        self.started = time.time()

    def public(self) -> dict:
        return {"ziel": self.target, "schritte": self.steps, "fertig": self.done, "fehler": self.error}

    def step(self, text: str) -> None:
        log.info("Speicher: %s", text)
        self.steps.append(text)

    async def __call__(self) -> None:
        try:
            await self._move()
            self.done = True
        except Exception as err:  # noqa: BLE001 – every failure ends up in the Atrium
            self.error = str(err) or type(err).__name__
            log.exception("Umzug der Rooms fehlgeschlagen")
            self.step("Fehler – Lares startet die Rooms wieder am alten Ort")
            try:
                await bind(base_for(load_choice(self.data_dir), self.data_dir))
                await self.systemctl("start", "docker")
                await self.start_all(base_for(load_choice(self.data_dir), self.data_dir))
            except Exception:  # noqa: BLE001
                log.exception("Rückweg fehlgeschlagen")

    async def _move(self) -> None:
        old_choice = load_choice(self.data_dir)
        old = base_for(old_choice, self.data_dir)
        disks = await list_disks()
        if self.target == "usb":
            new_choice, new = {"ort": "usb"}, self.data_dir
        else:
            disk = next((d for d in disks if d.path == self.target), None)
            if disk is None:
                raise LaufwerkError("Laufwerk nicht gefunden")
            if disk.system:
                raise LaufwerkError("Das ist der Lares-Stick selbst")
            if disk.size < MIN_SIZE:
                raise LaufwerkError("Laufwerk zu klein (mindestens 16 GB)")
            new_choice = {"ort": "laufwerk", "geraet": disk.path, "modell": disk.model, "typ": disk.kind}
            new = MOUNT
        if new_choice == old_choice or (new == old and self.target == "usb"):
            self.step("Die Rooms sind schon dort")
            return
        need = used_bytes(old / "rooms") + used_bytes(old / "docker")

        self.step("Rooms und Dienste anhalten")
        await self.stop_all()
        await self.systemctl("stop", "docker.socket", "docker")

        if self.target != "usb":
            if disk.lares_partition and not self.wipe:
                self.step(f"Vorhandenen Lares-Speicher auf {disk.model} übernehmen")
                uuid = disk.lares_partition["uuid"]
            else:
                self.step(f"{disk.model} vorbereiten (alle Daten darauf werden gelöscht)")
                for part in disk.partitions:
                    for mount in part["mounts"]:
                        await run("umount", mount)
                await run("wipefs", "-a", disk.path)
                await run("sh", "-c", f"echo 'label: gpt\n,,L,\n' | sfdisk --quiet {disk.path}")
                await run("udevadm", "settle")
                part = (await run("lsblk", "-n", "-o", "PATH", "-r", disk.path)).split()[-1]
                await run("mkfs.ext4", "-F", "-q", "-L", LABEL, "-m", "1", part)
                await run("udevadm", "settle")
                uuid = await run("blkid", "-s", "UUID", "-o", "value", part)
            new_choice["uuid"] = uuid
            if is_mounted(MOUNT):
                await run("umount", str(MOUNT))
            await mount_disk(uuid)
        elif old == MOUNT and not is_mounted(MOUNT):
            raise LaufwerkError("Das bisherige Laufwerk ist nicht eingehängt")

        if free_bytes(new) < need * 1.1:
            raise LaufwerkError(f"Zu wenig Platz: {need // 1024 ** 2} MB nötig, {free_bytes(new) // 1024 ** 2} MB frei")
        for folder in ("rooms", "docker"):
            source, target = old / folder, new / folder
            if not source.exists():
                continue
            self.step(f"{'Rooms' if folder == 'rooms' else 'Container-Images'} kopieren")
            target.mkdir(parents=True, exist_ok=True)
            await run("cp", "-a", f"{source}/.", f"{target}/")

        self.step("Neuen Ort merken")
        storage.save(config_file(self.data_dir), new_choice)
        await bind(new)
        await self.systemctl("start", "docker")

        self.step("Rooms und Dienste starten")
        await self.start_all(new)
        # the old room data stays, out of the way, until the owner removes it; images can always be loaded again
        backup = old / "alt" / time.strftime("%Y%m%d-%H%M")
        if (old / "rooms").exists():
            backup.mkdir(parents=True, exist_ok=True)
            await run("mv", str(old / "rooms"), str(backup / "rooms"))
        if (old / "docker").exists():
            await run("rm", "-rf", str(old / "docker"))
        if old == MOUNT and new != MOUNT:
            await run("umount", str(MOUNT), check=False)
        self.step(f"Fertig – die alte Kopie liegt unter {backup}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if sys.argv[1:] != ["boot"]:
        print("Aufruf: python -m lares_core.laufwerke boot", file=sys.stderr)
        sys.exit(2)
    base = asyncio.run(boot(Path(os.environ.get("LARES_DATA", "/data"))))
    print(f"Rooms und Container-Images: {base}")


if __name__ == "__main__":
    main()

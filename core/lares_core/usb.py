"""USB devices of the machine – Zigbee and Z-Wave sticks, cameras, printers, … – each given to exactly one room.

Lares reads them from sysfs. A room only gets a device the admin assigned to it (and only if its room.yaml has the
right "usb"); the container then sees the device nodes. When an assigned device is plugged in or out, the room
starts again with the devices that are there.
"""

import os
from pathlib import Path

from . import storage

SYSFS = Path(os.environ.get("LARES_USB_SYSFS", "/sys/bus/usb/devices"))
DEV = Path(os.environ.get("LARES_DEV", "/dev"))
KINDS = {"01": "Audio", "02": "Netzwerk/Modem", "03": "Eingabegerät", "06": "Kamera (Bilder)", "07": "Drucker",
         "08": "Speicher", "0a": "Daten", "0b": "Kartenleser", "0e": "Kamera", "e0": "Funk (Bluetooth)",
         "ef": "USB-Gerät", "ff": "USB-Gerät"}
SKIP_CLASSES = {"09"}          # hubs
SKIP_KINDS_FOR_ASSIGN = {"08"}  # mass storage belongs to the storage settings, not to a room


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _nodes(device_dir: Path) -> list[str]:
    """Device nodes below a USB device: serial ports, video, sound, printer – plus the raw USB node."""
    nodes = []
    for sub in sorted(device_dir.rglob("*")):
        name = sub.name
        parent = sub.parent.name
        if parent in ("tty", "video4linux", "usbmisc", "hidraw") and \
                name.startswith(("ttyUSB", "ttyACM", "video", "lp", "hidraw")):
            nodes.append(str(DEV / name))
        if parent == "sound" and name.startswith("card"):
            nodes.append(str(DEV / "snd"))
    bus, num = _read(device_dir / "busnum"), _read(device_dir / "devnum")
    if bus and num:
        nodes.append(str(DEV / "bus" / "usb" / f"{int(bus):03d}" / f"{int(num):03d}"))
    return sorted(set(nodes), key=nodes.index)


def scan() -> list[dict]:
    devices = []
    if not SYSFS.exists():
        return devices
    for entry in sorted(SYSFS.iterdir()):
        if ":" in entry.name or entry.name.startswith("usb") or not (entry / "idVendor").exists():
            continue                                    # interfaces and root hubs
        cls = _read(entry / "bDeviceClass").lower()
        if cls in SKIP_CLASSES:
            continue
        if cls in ("00", ""):                          # class given per interface
            cls = next((_read(i / "bInterfaceClass").lower() for i in sorted(entry.glob(f"{entry.name}:*"))
                        if _read(i / "bInterfaceClass")), "ff")
        vendor, product = _read(entry / "idVendor"), _read(entry / "idProduct")
        serial = _read(entry / "serial")
        name = _read(entry / "product") or f"USB-Gerät {vendor}:{product}"
        nodes = _nodes(entry)
        kind = "Seriell (z. B. Zigbee/Z-Wave)" if any("tty" in n for n in nodes) else KINDS.get(cls, "USB-Gerät")
        devices.append({"schluessel": f"{vendor}:{product}:{serial or entry.name}", "name": name,
                        "hersteller": _read(entry / "manufacturer"), "art": kind, "port": entry.name,
                        "knoten": nodes, "zuweisbar": cls not in SKIP_KINDS_FOR_ASSIGN})
    return devices


class Usb:
    def __init__(self, core_dir: Path):
        self.file = core_dir / "usb.json"
        self.assigned: dict[str, str] = storage.load(self.file, {})
        self.present: set[str] = set()

    def assign(self, key: str, room_id: str | None) -> None:
        if room_id:
            self.assigned[key] = room_id
        else:
            self.assigned.pop(key, None)
        storage.save(self.file, self.assigned)

    def forget_room(self, room_id: str) -> None:
        for key in [k for k, r in self.assigned.items() if r == room_id]:
            del self.assigned[key]
        storage.save(self.file, self.assigned)

    def nodes_for(self, room_id: str, devices: list[dict] | None = None) -> list[str]:
        devices = scan() if devices is None else devices
        return [n for d in devices if self.assigned.get(d["schluessel"]) == room_id for n in d["knoten"]]

    def changes(self) -> set[str]:
        """Rooms whose assigned devices were plugged in or out since the last call."""
        devices = scan()
        now = {d["schluessel"] for d in devices}
        changed = now ^ self.present
        self.present = now
        return {self.assigned[k] for k in changed if k in self.assigned}

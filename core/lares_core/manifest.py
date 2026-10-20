"""room.yaml: the description of a room – what it is, what it may do, which settings and schedules it has."""

import re
from dataclasses import dataclass, field

import yaml

ROOM_ID = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$")
NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
EVENT = re.compile(r"^[a-z0-9_*]+(\.[a-z0-9_*]+)*$")
DEVICE = re.compile(r"^[a-z_*]+\.[a-z0-9_*]+$")
INTERVAL = re.compile(r"^(\d+)(s|m|h|d)$")
CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
RAM = re.compile(r"^\d+[KMG]$")
ARCHES = {"amd64", "aarch64", "armv7"}
SETTING_TYPES = {"bool", "int", "float", "text", "auswahl"}
FLAGS = ("benachrichtigen", "ki", "ki_anbieter", "standort", "befehle")


class ManifestError(ValueError):
    """room.yaml is not valid; the message says what to fix."""


@dataclass
class Rights:
    benachrichtigen: bool = False
    ki: bool = False             # may ask the AI
    ki_anbieter: bool = False    # answers the AI questions of the other rooms (e.g. the assistant)
    standort: bool = False
    befehle: bool = False        # may use the commands of the other rooms (e.g. the assistant)
    geraete: list[str] = field(default_factory=list)
    ereignisse_senden: list[str] = field(default_factory=list)
    ereignisse_empfangen: list[str] = field(default_factory=list)
    schluessel: list[str] = field(default_factory=list)

    def summary(self) -> list[str]:
        """The rights in words, for the confirmation on installation."""
        out = []
        words = {"benachrichtigen": "dir Nachrichten schicken", "ki": "die KI fragen",
                 "ki_anbieter": "KI-Fragen der anderen Rooms beantworten", "standort": "deinen Standort sehen",
                 "befehle": "Befehle der anderen Rooms nutzen"}
        out += [words[f] for f in FLAGS if getattr(self, f)]
        if self.geraete:
            out.append("Geräte: " + ", ".join(self.geraete))
        if self.ereignisse_senden:
            out.append("Ereignisse senden: " + ", ".join(self.ereignisse_senden))
        if self.ereignisse_empfangen:
            out.append("Ereignisse empfangen: " + ", ".join(self.ereignisse_empfangen))
        if self.schluessel:
            out.append("Schlüssel: " + ", ".join(self.schluessel))
        return out


@dataclass
class Setting:
    name: str
    typ: str
    standard: object = None
    titel: str = ""
    min: float | None = None
    max: float | None = None
    werte: list[str] = field(default_factory=list)

    def check(self, value):
        """Returns the value in its type, or raises ManifestError."""
        if self.typ == "bool":
            if isinstance(value, bool):
                return value
            if str(value).lower() in ("true", "1", "ja", "an"):
                return True
            if str(value).lower() in ("false", "0", "nein", "aus", ""):
                return False
            raise ManifestError(f"{self.titel or self.name}: ja oder nein")
        if self.typ in ("int", "float"):
            try:
                number = int(value) if self.typ == "int" else float(value)
            except (TypeError, ValueError):
                raise ManifestError(f"{self.titel or self.name}: bitte eine Zahl") from None
            if isinstance(value, bool) or (self.typ == "int" and isinstance(value, float) and not value.is_integer()):
                raise ManifestError(f"{self.titel or self.name}: bitte eine ganze Zahl")
            if self.min is not None and number < self.min or self.max is not None and number > self.max:
                raise ManifestError(f"{self.titel or self.name}: zwischen {self.min} und {self.max}")
            return number
        if self.typ == "auswahl":
            if str(value) not in self.werte:
                raise ManifestError(f"{self.titel or self.name}: eins von {', '.join(self.werte)}")
            return str(value)
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ManifestError(f"{self.titel or self.name}: bitte Text")
        return str(value)[:2000]


@dataclass
class Schedule:
    name: str
    alle: int | None = None      # seconds
    um: str | None = None        # "HH:MM", every day in the system's time zone


@dataclass
class Command:
    name: str
    beschreibung: str
    argumente: list[str] = field(default_factory=list)


@dataclass
class Manifest:
    id: str
    name: str
    version: str
    beschreibung: str = ""
    symbol: str = ""
    image: str = ""
    arch: list[str] = field(default_factory=list)
    port: int | None = None
    kachel: bool = False
    oeffentlich: bool = False
    rechte: Rights = field(default_factory=Rights)
    einstellungen: dict[str, Setting] = field(default_factory=dict)
    zeitplaene: list[Schedule] = field(default_factory=list)
    befehle: list[Command] = field(default_factory=list)
    ram: str = ""
    raw: dict = field(default_factory=dict)

    def defaults(self) -> dict:
        return {k: s.standard for k, s in self.einstellungen.items()}

    def public(self) -> dict:
        """What the Atrium and the other rooms may see of this room."""
        return {"id": self.id, "name": self.name, "version": self.version, "beschreibung": self.beschreibung,
                "symbol": self.symbol, "kachel": self.kachel, "oeffentlich": self.oeffentlich,
                "befehle": [vars(c) for c in self.befehle]}


def _str_list(value, what: str, pattern: re.Pattern | None = None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{what}: bitte eine Liste")
    if pattern:
        for v in value:
            if not pattern.match(v):
                raise ManifestError(f"{what}: „{v}“ ist ungültig")
    return value


def _rights(value) -> Rights:
    rights = Rights()
    if value is None:
        return rights
    if not isinstance(value, list):
        raise ManifestError("rechte: bitte eine Liste")
    for item in value:
        if isinstance(item, str):
            if item not in FLAGS:
                raise ManifestError(f"rechte: „{item}“ gibt es nicht")
            setattr(rights, item, True)
        elif isinstance(item, dict) and len(item) == 1:
            (key, arg), = item.items()
            if key == "geraete":
                rights.geraete = _str_list(arg, "rechte.geraete", DEVICE)
            elif key == "schluessel":
                rights.schluessel = _str_list(arg, "rechte.schluessel", NAME)
            elif key == "ereignisse":
                if not isinstance(arg, dict) or set(arg) - {"senden", "empfangen"}:
                    raise ManifestError("rechte.ereignisse: senden und/oder empfangen")
                rights.ereignisse_senden = _str_list(arg.get("senden"), "rechte.ereignisse.senden", EVENT)
                rights.ereignisse_empfangen = _str_list(arg.get("empfangen"), "rechte.ereignisse.empfangen", EVENT)
            else:
                raise ManifestError(f"rechte: „{key}“ gibt es nicht")
        else:
            raise ManifestError("rechte: jeder Eintrag ist ein Name oder „name: werte“")
    return rights


def _settings(value) -> dict[str, Setting]:
    out = {}
    if value is None:
        return out
    if not isinstance(value, dict):
        raise ManifestError("einstellungen: bitte „name: {typ: …}“")
    for key, spec in value.items():
        if not isinstance(key, str) or not NAME.match(key):
            raise ManifestError(f"einstellungen: „{key}“ ist kein gültiger Name")
        if not isinstance(spec, dict) or spec.get("typ") not in SETTING_TYPES:
            raise ManifestError(f"einstellungen.{key}: typ ist eins von {', '.join(sorted(SETTING_TYPES))}")
        setting = Setting(name=key, typ=spec["typ"], titel=str(spec.get("name") or ""),
                          min=spec.get("min"), max=spec.get("max"),
                          werte=_str_list(spec.get("werte"), f"einstellungen.{key}.werte"))
        if setting.typ == "auswahl" and not setting.werte:
            raise ManifestError(f"einstellungen.{key}: auswahl braucht werte")
        if "standard" in spec:
            setting.standard = setting.check(spec["standard"])
        out[key] = setting
    return out


def _schedules(value) -> list[Schedule]:
    out = []
    for item in value or []:
        if not isinstance(item, dict) or not NAME.match(str(item.get("name", ""))):
            raise ManifestError("zeitplaene: jeder Eintrag braucht einen name")
        alle, um = item.get("alle"), item.get("um")
        if (alle is None) == (um is None):
            raise ManifestError(f"zeitplaene.{item['name']}: entweder alle (z. B. 30m) oder um (z. B. 17:00)")
        seconds = None
        if alle is not None:
            match = INTERVAL.match(str(alle))
            if not match:
                raise ManifestError(f"zeitplaene.{item['name']}: alle wie 45s, 30m, 2h oder 1d")
            seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
            if seconds < 60:
                raise ManifestError(f"zeitplaene.{item['name']}: höchstens einmal pro Minute")
        if um is not None and not CLOCK.match(str(um)):
            raise ManifestError(f"zeitplaene.{item['name']}: um wie 07:30")
        out.append(Schedule(item["name"], seconds, str(um) if um is not None else None))
    if len({s.name for s in out}) != len(out):
        raise ManifestError("zeitplaene: Namen doppelt")
    return out


def _commands(value) -> list[Command]:
    out = []
    for item in value or []:
        if not isinstance(item, dict) or not NAME.match(str(item.get("name", ""))):
            raise ManifestError("befehle: jeder Eintrag braucht einen name")
        out.append(Command(item["name"], str(item.get("beschreibung") or ""),
                           _str_list(item.get("argumente"), f"befehle.{item['name']}.argumente", NAME)))
    if len({c.name for c in out}) != len(out):
        raise ManifestError("befehle: Namen doppelt")
    return out


def parse(source: str | dict) -> Manifest:
    """Reads a room.yaml (text or already loaded); raises ManifestError."""
    if isinstance(source, str):
        try:
            data = yaml.safe_load(source)
        except yaml.YAMLError as err:
            raise ManifestError(f"room.yaml ist kein gültiges YAML: {err}") from None
    else:
        data = source
    if not isinstance(data, dict):
        raise ManifestError("room.yaml: oben stehen id, name, version …")
    room_id = str(data.get("id") or "")
    if not ROOM_ID.match(room_id):
        raise ManifestError("id: Kleinbuchstaben, Ziffern, - und _, 2–32 Zeichen, beginnt mit einem Buchstaben")
    if not str(data.get("name") or "").strip():
        raise ManifestError("name fehlt")
    if not VERSION.match(str(data.get("version") or "")):
        raise ManifestError("version wie 1.0.0")
    ui = data.get("oberflaeche") or {}
    if not isinstance(ui, dict):
        raise ManifestError("oberflaeche: bitte port, kachel, oeffentlich")
    port = ui.get("port")
    if port is not None and (not isinstance(port, int) or not 1 <= port <= 65535):
        raise ManifestError("oberflaeche.port: 1–65535")
    arch = _str_list(data.get("arch"), "arch")
    if set(arch) - ARCHES:
        raise ManifestError(f"arch: nur {', '.join(sorted(ARCHES))}")
    ram = str((data.get("ressourcen") or {}).get("ram") or "")
    if ram and not RAM.match(ram):
        raise ManifestError("ressourcen.ram wie 128M oder 1G")
    return Manifest(
        id=room_id, name=str(data["name"]).strip(), version=str(data["version"]),
        beschreibung=str(data.get("beschreibung") or ""), symbol=str(data.get("symbol") or ""),
        image=str(data.get("image") or ""), arch=arch, port=port,
        kachel=bool(ui.get("kachel", port is not None)), oeffentlich=bool(ui.get("oeffentlich", False)),
        rechte=_rights(data.get("rechte")), einstellungen=_settings(data.get("einstellungen")),
        zeitplaene=_schedules(data.get("zeitplaene")), befehle=_commands(data.get("befehle")),
        ram=ram, raw=data)

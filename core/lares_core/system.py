"""System settings: home, time zone, language, residents and quiet hours."""

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import storage

PHONE = re.compile(r"^\+[1-9][0-9]{6,14}$")
DEFAULTS = {"wohnort": None, "zeitzone": "Europe/Berlin", "sprache": "de", "bewohner": [],
            "ruhezeit": {"von": 22, "bis": 7}}


def phone(value: str) -> str:
    value = re.sub(r"[\s/()-]", "", str(value or ""))
    if value.startswith("00"):
        value = "+" + value[2:]
    if value and not PHONE.match(value):
        raise ValueError("Handynummer bitte im Format +4917012345678")
    return value


class System:
    def __init__(self, core_dir: Path):
        self.file = core_dir / "system.json"
        self.data = {**DEFAULTS, **storage.load(self.file, {})}

    def public(self) -> dict:
        """What every room may read (no phone numbers)."""
        return {"wohnort": self.data["wohnort"], "zeitzone": self.data["zeitzone"], "sprache": self.data["sprache"],
                "bewohner": [b["name"] for b in self.data["bewohner"]]}

    def update(self, values: dict) -> dict:
        """Checks and saves; raises ValueError with a message for the Atrium."""
        data = dict(self.data)
        if "wohnort" in values:
            place = values["wohnort"]
            if place is not None:
                try:
                    lat, lon = float(place["lat"]), float(place["lon"])
                except (TypeError, KeyError, ValueError):
                    raise ValueError("Wohnort: lat und lon als Zahlen") from None
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Wohnort: lat −90…90, lon −180…180")
                place = {"name": str(place.get("name") or "Zuhause")[:80], "lat": lat, "lon": lon}
            data["wohnort"] = place
        if "zeitzone" in values:
            try:
                ZoneInfo(str(values["zeitzone"]))
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Zeitzone unbekannt, z. B. Europe/Berlin") from None
            data["zeitzone"] = str(values["zeitzone"])
        if "sprache" in values:
            if not re.match(r"^[a-z]{2}$", str(values["sprache"])):
                raise ValueError("Sprache als Kürzel, z. B. de")
            data["sprache"] = str(values["sprache"])
        if "bewohner" in values:
            people = []
            for person in values["bewohner"] or []:
                name = str((person or {}).get("name") or "").strip()[:40]
                if not name:
                    raise ValueError("Jeder Bewohner braucht einen Namen")
                people.append({"name": name, "telefon": phone(person.get("telefon"))})
            if len({p["name"].lower() for p in people}) != len(people):
                raise ValueError("Namen der Bewohner doppelt")
            data["bewohner"] = people
        if "ruhezeit" in values:
            quiet = values["ruhezeit"]
            if quiet is not None:
                try:
                    quiet = {"von": int(quiet["von"]), "bis": int(quiet["bis"])}
                except (TypeError, KeyError, ValueError):
                    raise ValueError("Ruhezeit: von und bis als Stunden") from None
                if not all(0 <= h <= 23 for h in quiet.values()):
                    raise ValueError("Ruhezeit: Stunden 0–23")
            data["ruhezeit"] = quiet
        self.data = data
        storage.save(self.file, data)
        return data

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.data["zeitzone"])

    def now(self) -> datetime:
        return datetime.now(self.zone())

    def quiet(self, at: datetime | None = None) -> bool:
        quiet = self.data["ruhezeit"]
        if not quiet or quiet["von"] == quiet["bis"]:
            return False
        hour = (at or self.now()).hour
        start, end = quiet["von"], quiet["bis"]
        return start <= hour < end if start < end else hour >= start or hour < end

    def phone_of(self, name: str | None = None) -> str:
        """The phone of the named resident, or of the first one (the owner)."""
        for person in self.data["bewohner"]:
            if name is None or person["name"].lower() == name.lower():
                if person["telefon"] or name is not None:
                    return person["telefon"]
        return ""

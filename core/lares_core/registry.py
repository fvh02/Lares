"""The installed rooms: their room.yaml, settings, state and tokens."""

import hashlib
import secrets
import shutil
import time
from pathlib import Path

from . import storage
from .manifest import Manifest, ManifestError, parse


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Room:
    def __init__(self, manifest: Manifest, entry: dict):
        self.manifest, self.entry = manifest, entry

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def active(self) -> bool:
        return bool(self.entry.get("aktiv", True))

    def settings(self) -> dict:
        return {**self.manifest.defaults(), **{k: v for k, v in self.entry.get("einstellungen", {}).items()
                                              if k in self.manifest.einstellungen}}

    def info(self) -> dict:
        return {**self.manifest.public(), "aktiv": self.active, "installiert": self.entry.get("installiert"),
                "rechte": self.manifest.rechte.summary()}


class Registry:
    """Keeps rooms.json; only token hashes are stored – a room gets a fresh token on every start."""

    def __init__(self, core_dir: Path, rooms_dir: Path):
        self.file, self.rooms_dir = core_dir / "rooms.json", rooms_dir
        self.rooms: dict[str, Room] = {}
        self.by_token: dict[str, str] = {}
        for room_id, entry in storage.load(self.file, {}).items():
            try:
                room = Room(parse(entry["manifest"]), entry)
            except (ManifestError, KeyError, TypeError):
                continue
            self.rooms[room_id] = room
            if entry.get("token"):
                self.by_token[entry["token"]] = room_id

    def _save(self) -> None:
        storage.save(self.file, {rid: r.entry for rid, r in self.rooms.items()})

    def get(self, room_id: str) -> Room | None:
        return self.rooms.get(room_id)

    def install(self, manifest: Manifest) -> Room:
        """Installs or updates; settings, state and token of an existing room are kept."""
        old = self.rooms.get(manifest.id)
        entry = dict(old.entry) if old else {"aktiv": True, "installiert": int(time.time()), "einstellungen": {}}
        entry["manifest"] = manifest.raw
        room = Room(manifest, entry)
        self.rooms[manifest.id] = room
        self.data_dir(manifest.id).mkdir(parents=True, exist_ok=True)
        self._save()
        return room

    def remove(self, room_id: str, keep_data: bool = True) -> None:
        room = self.rooms.pop(room_id, None)
        if room is None:
            return
        self.by_token.pop(room.entry.get("token", ""), None)
        self._save()
        if not keep_data:
            shutil.rmtree(self.data_dir(room_id), ignore_errors=True)

    def set_active(self, room_id: str, active: bool) -> None:
        self.rooms[room_id].entry["aktiv"] = active
        self._save()

    def set_settings(self, room_id: str, values: dict) -> dict:
        """Checks the values against the room's room.yaml; raises ManifestError."""
        room = self.rooms[room_id]
        clean = {}
        for key, value in values.items():
            spec = room.manifest.einstellungen.get(key)
            if spec is None:
                raise ManifestError(f"Einstellung „{key}“ gibt es nicht")
            clean[key] = spec.check(value)
        room.entry["einstellungen"] = {**room.entry.get("einstellungen", {}), **clean}
        self._save()
        return room.settings()

    def issue_token(self, room_id: str) -> str:
        """A new token for the room; the old one stops working."""
        room = self.rooms[room_id]
        self.by_token.pop(room.entry.get("token", ""), None)
        token = secrets.token_urlsafe(32)
        room.entry["token"] = _hash(token)
        self.by_token[room.entry["token"]] = room_id
        self._save()
        return token

    def room_for_token(self, token: str) -> Room | None:
        room_id = self.by_token.get(_hash(token)) if token else None
        return self.rooms.get(room_id) if room_id else None

    def data_dir(self, room_id: str) -> Path:
        return self.rooms_dir / room_id

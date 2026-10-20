"""Accounts of the Atrium: password (scrypt), optional two-factor codes (TOTP) and sessions.

The first account is created with a one-time setup code that only the core's log and /data/core/setup.code show –
so nobody else in the home network can take over a fresh Lares.
"""

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from pathlib import Path
from urllib.parse import quote

from . import storage

NAME = re.compile(r"^[A-Za-zÄÖÜäöüß0-9 ._-]{2,32}$")
ROLES = ("admin", "bewohner")
DESIGNS = ("klassisch", "himmel")
MODES = ("auto", "hell", "dunkel")
SESSION_DAYS = 30
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


class AccountError(ValueError):
    pass


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, _ = stored.split("$")
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)), stored)
    except ValueError:
        return False


def totp(secret: str, at: float | None = None, step: int = 30) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int((at or time.time()) // step)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def check_totp(secret: str, code: str, at: float | None = None) -> bool:
    code = re.sub(r"\s", "", code or "")
    now = at or time.time()
    return bool(code) and any(hmac.compare_digest(totp(secret, now + d * 30), code) for d in (-1, 0, 1))


def check_strength(password: str) -> None:
    if len(password) < 10:
        raise AccountError("Passwort: mindestens 10 Zeichen")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, core_dir: Path):
        self.file, self.sessions_file = core_dir / "konten.json", core_dir / "sitzungen.json"
        self.setup_file = core_dir / "setup.code"
        self.accounts: dict[str, dict] = storage.load(self.file, {})
        self.sessions: dict[str, dict] = storage.load(self.sessions_file, {})

    # -- first start --------------------------------------------------------------------------------
    @property
    def needs_setup(self) -> bool:
        return not self.accounts

    def setup_code(self) -> str:
        """The one-time code for the first account (created on demand, removed after the setup)."""
        if self.setup_file.exists():
            return self.setup_file.read_text().strip()
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        storage.write(self.setup_file, code.encode())
        return code

    def setup(self, code: str, name: str, password: str) -> None:
        if not self.needs_setup:
            raise AccountError("Lares ist schon eingerichtet")
        if not hmac.compare_digest(code.strip().upper(), self.setup_code()):
            raise AccountError("Der Einrichtungscode stimmt nicht – er steht im Log von Lares Core und in "
                               "/data/core/setup.code")
        self.create(name, password, "admin")
        self.setup_file.unlink(missing_ok=True)

    # -- accounts -----------------------------------------------------------------------------------
    def _save(self) -> None:
        storage.save(self.file, self.accounts)

    def find(self, name: str) -> str | None:
        return next((n for n in self.accounts if n.lower() == name.strip().lower()), None)

    def create(self, name: str, password: str, role: str) -> None:
        name = name.strip()
        if not NAME.match(name):
            raise AccountError("Name: 2–32 Zeichen, Buchstaben, Ziffern, Leerzeichen, . _ -")
        if self.find(name):
            raise AccountError(f"{name} gibt es schon")
        if role not in ROLES:
            raise AccountError("Rolle: admin oder bewohner")
        check_strength(password)
        self.accounts[name] = {"passwort": hash_password(password), "rolle": role, "erstellt": int(time.time())}
        self._save()

    def delete(self, name: str) -> None:
        name = self.find(name) or ""
        if name not in self.accounts:
            raise AccountError("Konto gibt es nicht")
        if self.accounts[name]["rolle"] == "admin" and self.admins() == [name]:
            raise AccountError("Das letzte Admin-Konto bleibt")
        del self.accounts[name]
        self._save()
        self.end_sessions(name)

    def set_role(self, name: str, role: str) -> None:
        if role not in ROLES:
            raise AccountError("Rolle: admin oder bewohner")
        if role != "admin" and self.admins() == [name]:
            raise AccountError("Das letzte Admin-Konto bleibt Admin")
        self.accounts[name]["rolle"] = role
        self._save()

    def admins(self) -> list[str]:
        return [n for n, a in self.accounts.items() if a["rolle"] == "admin"]

    def public(self) -> list[dict]:
        return [{"name": n, "rolle": a["rolle"], "zwei_faktor": bool(a.get("totp")), "erstellt": a["erstellt"],
                 "passkeys": len(a.get("passkeys", [])), "verknuepft": sorted(a.get("verknuepft", {}))}
                for n, a in sorted(self.accounts.items())]

    def set_password(self, name: str, old: str, new: str) -> None:
        if not check_password(old, self.accounts[name]["passwort"]):
            raise AccountError("Das bisherige Passwort stimmt nicht")
        check_strength(new)
        self.accounts[name]["passwort"] = hash_password(new)
        self._save()

    def reset_password(self, name: str, new: str) -> None:
        """For an admin: a new password for someone else; their sessions end."""
        check_strength(new)
        self.accounts[name]["passwort"] = hash_password(new)
        self._save()
        self.end_sessions(name)

    # -- two factors --------------------------------------------------------------------------------
    def start_totp(self, name: str) -> dict:
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        self.accounts[name]["totp_neu"] = secret
        self._save()
        uri = f"otpauth://totp/Lares:{quote(name)}?secret={secret}&issuer=Lares&digits=6&period=30"
        return {"geheimnis": secret, "link": uri}

    def confirm_totp(self, name: str, code: str) -> None:
        secret = self.accounts[name].get("totp_neu")
        if not secret or not check_totp(secret, code):
            raise AccountError("Der Code stimmt nicht – Uhrzeit des Handys prüfen")
        self.accounts[name]["totp"] = self.accounts[name].pop("totp_neu")
        self._save()

    def disable_totp(self, name: str, password: str) -> None:
        if not check_password(password, self.accounts[name]["passwort"]):
            raise AccountError("Das Passwort stimmt nicht")
        self.accounts[name].pop("totp", None)
        self._save()

    # -- appearance ---------------------------------------------------------------------------------
    def appearance(self, name: str) -> dict:
        saved = self.accounts[name].get("darstellung") or {}
        return {"design": saved.get("design", "klassisch"), "modus": saved.get("modus", "auto")}

    def set_appearance(self, name: str, design: str | None, mode: str | None) -> dict:
        current = self.appearance(name)
        if design is not None:
            if design not in DESIGNS:
                raise AccountError(f"Design: {', '.join(DESIGNS)}")
            current["design"] = design
        if mode is not None:
            if mode not in MODES:
                raise AccountError(f"Modus: {', '.join(MODES)}")
            current["modus"] = mode
        self.accounts[name]["darstellung"] = current
        self._save()
        return current

    # -- passkeys -----------------------------------------------------------------------------------
    def webauthn_id(self, name: str) -> str:
        """The random user handle of the account for passkeys (never the name)."""
        account = self.accounts[name]
        if not account.get("webauthn_id"):
            account["webauthn_id"] = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")
            self._save()
        return account["webauthn_id"]

    def passkeys(self, name: str) -> list[dict]:
        return [{"id": p["id"], "name": p["name"], "erstellt": p["erstellt"], "zuletzt": p.get("zuletzt")}
                for p in self.accounts[name].get("passkeys", [])]

    def add_passkey(self, name: str, key: dict, label: str) -> None:
        if self.find_passkey(key["id"]):
            raise AccountError("Diesen Passkey gibt es schon")
        self.accounts[name].setdefault("passkeys", []).append(
            {**key, "name": (label or "Passkey").strip()[:40], "erstellt": int(time.time())})
        self._save()

    def remove_passkey(self, name: str, key_id: str) -> None:
        keys = self.accounts[name].get("passkeys", [])
        self.accounts[name]["passkeys"] = [p for p in keys if p["id"] != key_id]
        self._save()

    def find_passkey(self, key_id: str) -> tuple[str, dict] | None:
        for name, account in self.accounts.items():
            for key in account.get("passkeys", []):
                if key["id"] == key_id:
                    return name, key
        return None

    def used_passkey(self, key_id: str, count: int) -> None:
        found = self.find_passkey(key_id)
        if found:
            found[1].update(sign_count=count, zuletzt=int(time.time()))
            self._save()

    # -- Apple and Google -----------------------------------------------------------------------------
    def link(self, name: str, provider: str, sub: str, email: str) -> None:
        other = self.find_link(provider, sub)
        if other and other != name:
            raise AccountError(f"Dieses Konto ist schon mit {other} verbunden")
        self.accounts[name].setdefault("verknuepft", {})[provider] = {"sub": sub, "email": email,
                                                                      "seit": int(time.time())}
        self._save()

    def unlink(self, name: str, provider: str) -> None:
        self.accounts[name].get("verknuepft", {}).pop(provider, None)
        self._save()

    def find_link(self, provider: str, sub: str) -> str | None:
        return next((n for n, a in self.accounts.items()
                     if a.get("verknuepft", {}).get(provider, {}).get("sub") == sub), None)

    def links(self, name: str) -> dict:
        return {p: {"email": v["email"], "seit": v["seit"]} for p, v in self.accounts[name].get("verknuepft", {}).items()}

    # -- login and sessions -------------------------------------------------------------------------
    def login(self, name: str, password: str, code: str = "") -> str:
        """Returns a session token; raises AccountError ("2fa" when the code is missing)."""
        real = self.find(name)
        account = self.accounts.get(real or "")
        if account is None:
            hash_password(password)       # same time as for a real account
            raise AccountError("Name oder Passwort stimmt nicht")
        if not check_password(password, account["passwort"]):
            raise AccountError("Name oder Passwort stimmt nicht")
        if account.get("totp"):
            if not code:
                raise AccountError("2fa")
            if not check_totp(account["totp"], code):
                raise AccountError("Der Code stimmt nicht")
        return self.new_session(real)

    def new_session(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        self.sessions[_hash(token)] = {"name": name, "erstellt": now, "ablauf": now + SESSION_DAYS * 86400}
        self._save_sessions()
        return token

    def session(self, token: str) -> dict | None:
        """The account of a session ({name, rolle}) or None; extends the session while it is used."""
        if not token:
            return None
        entry = self.sessions.get(_hash(token))
        now = time.time()
        if entry is None or entry["ablauf"] < now or entry["name"] not in self.accounts:
            return None
        if entry["ablauf"] - now < (SESSION_DAYS - 1) * 86400:
            entry["ablauf"] = int(now) + SESSION_DAYS * 86400
            self._save_sessions()
        return {"name": entry["name"], "rolle": self.accounts[entry["name"]]["rolle"]}

    def logout(self, token: str) -> None:
        if self.sessions.pop(_hash(token), None):
            self._save_sessions()

    def end_sessions(self, name: str) -> None:
        self.sessions = {k: v for k, v in self.sessions.items() if v["name"] != name}
        self._save_sessions()

    def _save_sessions(self) -> None:
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v["ablauf"] > now}
        storage.save(self.sessions_file, self.sessions)

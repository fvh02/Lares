"""Remote access, built into the system: Tailscale (your devices, private) and Cloudflare Tunnel (the internet, only
for released rooms).

- Tailscale runs as the service lares-tailscale. It joins your tailnet with the key tailscale_authkey and serves
  the home gateway at https://<name>.<tailnet>.ts.net; optionally it routes your home network or is an exit node.
- cloudflared runs as the service lares-cloudflared with the key cloudflare_tunnel_token. In the Cloudflare
  dashboard every public hostname of the tunnel points to http://lares-core:8001 – the public gateway, which only
  lets through rooms you released (optionally behind an access code).
"""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
from pathlib import Path

from . import storage
from .runtime import Service

TAILSCALE_IMAGE = os.environ.get("LARES_TAILSCALE_IMAGE", "tailscale/tailscale:v1.86.2")
CLOUDFLARED_IMAGE = os.environ.get("LARES_CLOUDFLARED_IMAGE", "cloudflare/cloudflared:2025.8.0")
HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
DEFAULTS = {"tailscale": {"aktiv": False, "hostname": "lares", "routen": [], "exit_node": False},
            "cloudflared": {"aktiv": False}, "freigaben": {}}
ITERATIONS = 200_000


def hash_code(code: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f"{salt}${digest}"


def check_code(code: str, stored: str) -> bool:
    salt, _, _ = stored.partition("$")
    return hmac.compare_digest(hash_code(code, salt), stored)


class Remote:
    def __init__(self, core_dir: Path, vault, core_host: str, home_port: int, public_port: int):
        self.file, self.vault = core_dir / "fernzugriff.json", vault
        self.core_host, self.home_port, self.public_port = core_host, home_port, public_port
        data = storage.load(self.file, {})
        self.data = {k: (dict(v, **data.get(k, {})) if isinstance(v, dict) else data.get(k, v))
                     for k, v in DEFAULTS.items()}
        key_file = core_dir / "gateway.key"
        if not key_file.exists():
            storage.write(key_file, secrets.token_bytes(32))
        self.cookie_key = key_file.read_bytes()

    def _save(self) -> None:
        storage.save(self.file, self.data)

    # -- settings -----------------------------------------------------------------------------------
    def set_tailscale(self, values: dict) -> dict:
        ts = dict(self.data["tailscale"])
        if "hostname" in values:
            name = str(values["hostname"] or "").strip().lower()
            if not HOSTNAME.match(name):
                raise ValueError("Gerätename: Kleinbuchstaben, Ziffern und -, z. B. lares")
            ts["hostname"] = name
        if "routen" in values:
            routes = []
            for route in values["routen"] or []:
                try:
                    net = ipaddress.ip_network(str(route).strip(), strict=False)
                except ValueError:
                    raise ValueError(f"Route „{route}“ ist kein Netz wie 192.168.178.0/24") from None
                routes.append(str(net))
            ts["routen"] = routes
        if "exit_node" in values:
            ts["exit_node"] = bool(values["exit_node"])
        if "aktiv" in values:
            ts["aktiv"] = bool(values["aktiv"])
        self.data["tailscale"] = ts
        self._save()
        return ts

    def set_cloudflared(self, values: dict) -> dict:
        if "aktiv" in values:
            self.data["cloudflared"]["aktiv"] = bool(values["aktiv"])
        self._save()
        return self.data["cloudflared"]

    def release(self, room_id: str, values: dict | None) -> dict | None:
        """Releases a room for the internet ({host?, zugangscode?}) or withdraws it (None)."""
        if values is None:
            self.data["freigaben"].pop(room_id, None)
            self._save()
            return None
        entry = dict(self.data["freigaben"].get(room_id, {}))
        if "host" in values:
            host = str(values["host"] or "").strip().lower().rstrip(".")
            if host and not DOMAIN.match(host):
                raise ValueError("Adresse wie spiele.example.de")
            other = self.room_for_host(host) if host else None
            if other and other != room_id:
                raise ValueError(f"{host} ist schon für {other} eingetragen")
            entry["host"] = host
        if "zugangscode" in values:
            code = str(values["zugangscode"] or "")
            if code and len(code) < 8:
                raise ValueError("Zugangscode: mindestens 8 Zeichen")
            entry["code"] = hash_code(code) if code else ""
        self.data["freigaben"][room_id] = entry
        self._save()
        return self.public_release(room_id)

    def released(self, room_id: str) -> dict | None:
        return self.data["freigaben"].get(room_id)

    def public_release(self, room_id: str) -> dict | None:
        entry = self.released(room_id)
        return None if entry is None else {"host": entry.get("host", ""), "zugangscode": bool(entry.get("code"))}

    def room_for_host(self, host: str) -> str | None:
        host = host.split(":")[0].lower().rstrip(".")
        return next((rid for rid, e in self.data["freigaben"].items() if e.get("host") == host), None)

    def cookie_for(self, room_id: str) -> str:
        entry = self.released(room_id) or {}
        return hmac.new(self.cookie_key, f"{room_id}|{entry.get('code', '')}".encode(), hashlib.sha256).hexdigest()

    # -- services -----------------------------------------------------------------------------------
    def tailscale_service(self) -> Service:
        ts = self.data["tailscale"]
        serve = {"TCP": {"443": {"HTTPS": True}},
                 "Web": {"${TS_CERT_DOMAIN}:443": {"Handlers": {"/": {
                     "Proxy": f"http://{self.core_host}:{self.home_port}"}}}}}
        env = {"TS_HOSTNAME": ts["hostname"], "TS_STATE_DIR": "/var/lib/tailscale", "TS_USERSPACE": "true",
               "TS_AUTH_ONCE": "true", "TS_SERVE_CONFIG": "/var/lib/tailscale/serve.json"}
        key = self.vault.get("tailscale_authkey")
        if key:
            env["TS_AUTHKEY"] = key
        if ts["routen"]:
            env["TS_ROUTES"] = ",".join(ts["routen"])
        if ts["exit_node"]:
            env["TS_EXTRA_ARGS"] = "--advertise-exit-node"
        return Service("tailscale", TAILSCALE_IMAGE, env, mount="/var/lib/tailscale",
                       files={"serve.json": json.dumps(serve, indent=1)})

    def cloudflared_service(self) -> Service:
        token = self.vault.get("cloudflare_tunnel_token")
        if not token:
            raise ValueError("Schlüssel cloudflare_tunnel_token fehlt – im Cloudflare-Dashboard unter Zero Trust → "
                             "Networks → Tunnels einen Tunnel anlegen und das Token als Schlüssel eintragen")
        return Service("cloudflared", CLOUDFLARED_IMAGE, {"TUNNEL_TOKEN": token},
                       command=["tunnel", "--no-autoupdate", "run"])

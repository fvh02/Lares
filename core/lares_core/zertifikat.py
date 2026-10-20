"""HTTPS at home: a real certificate from Let's Encrypt for an own address such as lares.example.de.

- The proof for Let's Encrypt is a DNS entry (DNS-01) – Lares does not have to be reachable from the internet.
  The entry is made through the Cloudflare API with the key cloudflare_dns_token (Zone → DNS → Edit).
- If wanted, Lares also points the address (A record) at its own address in the home network and keeps it current.
- The certificate is renewed 30 days before it runs out and swapped while Lares keeps running.

The home gateway then listens on 443 as well; requests for the address over plain http are sent to https. Passkeys
work at home this way, without Tailscale.
"""

import asyncio
import base64
import datetime
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import time
from pathlib import Path

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

from . import storage

log = logging.getLogger("lares.https")
LETS_ENCRYPT = os.environ.get("LARES_ACME_URL", "https://acme-v02.api.letsencrypt.org/directory")
CLOUDFLARE = os.environ.get("LARES_CLOUDFLARE_API", "https://api.cloudflare.com/client/v4")
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
RENEW_DAYS = 30


class CertError(RuntimeError):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def home_ip() -> str:
    """The address of this machine in the home network (the interface towards the internet; nothing is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
        except OSError:
            return ""


# -- ACME (RFC 8555) ------------------------------------------------------------------------------
class Acme:
    def __init__(self, http: aiohttp.ClientSession, directory: str, account_key, ssl_ctx=None):
        self.http, self.directory_url, self.key, self.ssl = http, directory, account_key, ssl_ctx
        self.dir: dict = {}
        self.nonce = ""
        self.kid = ""
        numbers = account_key.public_key().public_numbers()
        self.jwk = {"crv": "P-256", "kty": "EC", "x": b64u(numbers.x.to_bytes(32, "big")),
                    "y": b64u(numbers.y.to_bytes(32, "big"))}
        self.thumbprint = b64u(hashlib.sha256(json.dumps(self.jwk, sort_keys=True, separators=(",", ":"))
                                              .encode()).digest())

    async def _get(self, url: str) -> dict:
        async with self.http.get(url, ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            return await resp.json(content_type=None)

    async def _nonce(self) -> str:
        if self.nonce:
            nonce, self.nonce = self.nonce, ""
            return nonce
        async with self.http.head(self.dir["newNonce"], ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=30)) as r:
            return r.headers["Replay-Nonce"]

    def _sign(self, url: str, payload, nonce: str) -> dict:
        protected = {"alg": "ES256", "nonce": nonce, "url": url}
        if self.kid:
            protected["kid"] = self.kid
        else:
            protected["jwk"] = self.jwk
        body = "" if payload is None else b64u(json.dumps(payload).encode())
        head = b64u(json.dumps(protected).encode())
        r, s = decode_dss_signature(self.key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
        return {"protected": head, "payload": body, "signature": b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}

    async def post(self, url: str, payload=None, raw: bool = False):
        """Signed request; payload None is a POST-as-GET. Returns (data, headers)."""
        # RFC 8555 §6.5: badNonce is retried with the fresh nonce from the error response.
        for attempt in range(1, 6):
            jws = self._sign(url, payload, await self._nonce())
            async with self.http.post(url, data=json.dumps(jws), ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=60),
                                      headers={"Content-Type": "application/jose+json"}) as resp:
                self.nonce = resp.headers.get("Replay-Nonce", "")
                text = await resp.text()
                if resp.status < 300:
                    return (text if raw else (json.loads(text) if text else {})), resp.headers
                try:
                    problem = json.loads(text)
                except ValueError:
                    problem = {"detail": text[:200]}
                if problem.get("type", "").endswith(":badNonce") and attempt < 5:
                    continue
                raise CertError(f"Let's Encrypt: {problem.get('detail') or problem.get('type') or resp.status}")
        raise CertError("Let's Encrypt antwortet nicht")

    async def account(self, email: str = "") -> None:
        self.dir = await self._get(self.directory_url)
        payload: dict = {"termsOfServiceAgreed": True}
        if email:
            payload["contact"] = [f"mailto:{email}"]
        _, headers = await self.post(self.dir["newAccount"], payload)
        self.kid = headers["Location"]

    async def poll(self, url: str, done: set, timeout: float = 180) -> dict:
        end = time.time() + timeout
        while True:
            data, _ = await self.post(url)
            if data.get("status") in done:
                return data
            if data.get("status") == "invalid":
                details = [c.get("error", {}).get("detail", "") for c in data.get("challenges", [])]
                raise CertError("Prüfung fehlgeschlagen: " + ("; ".join(d for d in details if d) or "ungültig"))
            if time.time() > end:
                raise CertError("Let's Encrypt hat nicht rechtzeitig geantwortet")
            await asyncio.sleep(2)

    async def certificate(self, domain: str, dns, step=lambda text: None) -> tuple[bytes, bytes]:
        """Returns (certificate chain PEM, private key PEM) for the domain."""
        step("Bei Let's Encrypt anmelden")
        order, headers = await self.post(self.dir["newOrder"], {"identifiers": [{"type": "dns", "value": domain}]})
        order_url = headers["Location"]
        records = []
        try:
            for authz_url in order["authorizations"]:
                authz, _ = await self.post(authz_url)
                if authz.get("status") == "valid":
                    continue
                challenge = next((c for c in authz["challenges"] if c["type"] == "dns-01"), None)
                if challenge is None:
                    raise CertError("Let's Encrypt bietet keine DNS-Prüfung an")
                value = b64u(hashlib.sha256(f"{challenge['token']}.{self.thumbprint}".encode()).digest())
                name = f"_acme-challenge.{authz['identifier']['value']}"
                step(f"DNS-Eintrag {name} setzen")
                await dns.set_txt(name, value)
                records.append((name, value))
                step("Warten, bis der Eintrag im DNS sichtbar ist")
                await dns.wait(name, value)
                await self.post(challenge["url"], {})
                step("Let's Encrypt prüft den Eintrag")
                await self.poll(authz_url, {"valid"})
        finally:
            for name, value in records:
                try:
                    await dns.clear_txt(name, value)
                except Exception as err:  # noqa: BLE001 – cleaning up must not hide the real result
                    log.warning("DNS-Eintrag %s nicht entfernt: %s", name, err)
        step("Zertifikat ausstellen lassen")
        key = new_key()
        csr = x509.CertificateSigningRequestBuilder().subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])).add_extension(
            x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False).sign(key, hashes.SHA256())
        await self.post(order["finalize"], {"csr": b64u(csr.public_bytes(serialization.Encoding.DER))})
        order = await self.poll(order_url, {"valid"})
        chain, _ = await self.post(order["certificate"], raw=True)
        return chain.encode(), key_pem(key)


# -- DNS ------------------------------------------------------------------------------------------
class Cloudflare:
    """TXT records for the proof and the A record of the address, through the Cloudflare API."""

    def __init__(self, http: aiohttp.ClientSession, token: str):
        self.http, self.headers = http, {"Authorization": f"Bearer {token}"}
        self.zones: dict[str, str] = {}

    async def _call(self, method: str, path: str, **kw) -> dict:
        try:
            async with self.http.request(method, CLOUDFLARE + path, headers=self.headers,
                                         timeout=aiohttp.ClientTimeout(total=30), **kw) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise CertError("Cloudflare nicht erreichbar") from None
        if not data.get("success"):
            errors = "; ".join(e.get("message", "") for e in data.get("errors", [])) or "unbekannter Fehler"
            raise CertError(f"Cloudflare: {errors}")
        return data

    async def zone(self, name: str) -> str:
        parts = name.rstrip(".").split(".")
        for i in range(len(parts) - 1):
            candidate = ".".join(parts[i:])
            if candidate in self.zones:
                return self.zones[candidate]
            data = await self._call("GET", "/zones", params={"name": candidate})
            if data["result"]:
                self.zones[candidate] = data["result"][0]["id"]
                return self.zones[candidate]
        raise CertError(f"Keine Cloudflare-Zone für {name} – liegt die Domain bei Cloudflare, und darf der "
                        "Schlüssel cloudflare_dns_token sie bearbeiten?")

    async def set_txt(self, name: str, value: str) -> None:
        zone = await self.zone(name)
        await self._call("POST", f"/zones/{zone}/dns_records", json={"type": "TXT", "name": name,
                                                                     "content": value, "ttl": 60})

    async def clear_txt(self, name: str, value: str) -> None:
        zone = await self.zone(name)
        data = await self._call("GET", f"/zones/{zone}/dns_records", params={"type": "TXT", "name": name})
        for record in data["result"]:
            if record.get("content", "").strip('"') == value:
                await self._call("DELETE", f"/zones/{zone}/dns_records/{record['id']}")

    async def wait(self, name: str, value: str, timeout: float = 180) -> None:
        """Until a public resolver (Cloudflare DNS over HTTPS) sees the TXT record."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                async with self.http.get("https://cloudflare-dns.com/dns-query", params={"name": name, "type": "TXT"},
                                         headers={"Accept": "application/dns-json"},
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    answers = (await resp.json(content_type=None)).get("Answer", [])
                if any(a.get("data", "").strip('"') == value for a in answers):
                    await asyncio.sleep(5)
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                pass
            await asyncio.sleep(5)

    async def set_a(self, name: str, ip: str) -> bool:
        """Points the address at the home network; True when something changed."""
        zone = await self.zone(name)
        data = await self._call("GET", f"/zones/{zone}/dns_records", params={"type": "A", "name": name})
        record = next(iter(data["result"]), None)
        if record and record.get("content") == ip and not record.get("proxied"):
            return False
        body = {"type": "A", "name": name, "content": ip, "ttl": 300, "proxied": False}
        if record:
            await self._call("PUT", f"/zones/{zone}/dns_records/{record['id']}", json=body)
        else:
            await self._call("POST", f"/zones/{zone}/dns_records", json=body)
        return True


class TestDns:
    """For tests against Pebble: pebble-challtestsrv serves the TXT records (LARES_ACME_TESTDNS)."""

    def __init__(self, http: aiohttp.ClientSession, url: str):
        self.http, self.url = http, url.rstrip("/")

    async def set_txt(self, name: str, value: str) -> None:
        await self.http.post(self.url + "/set-txt", json={"host": name + ".", "value": value})

    async def clear_txt(self, name: str, value: str) -> None:
        await self.http.post(self.url + "/clear-txt", json={"host": name + "."})

    async def wait(self, name: str, value: str) -> None:
        return None

    async def set_a(self, name: str, ip: str) -> bool:
        await self.http.post(self.url + "/add-a", json={"host": name + ".", "addresses": [ip]})
        return True


# -- the certificate of Lares ---------------------------------------------------------------------
class Https:
    def __init__(self, core_dir: Path, vault):
        self.dir, self.vault = core_dir / "https", vault
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cert_file, self.key_file = self.dir / "zertifikat.pem", self.dir / "schluessel.pem"
        self.info_file = self.dir / "einstellungen.json"
        self.settings = {"domain": "", "a_eintrag": True, "ip": "", "email": "",
                         **storage.load(self.info_file, {})}
        self.steps: list[str] = []
        self.error = ""
        self.busy = False
        self.contexts: list[ssl.SSLContext] = []
        self.ready = self.certificate() is not None

    # settings
    def update(self, values: dict) -> dict:
        settings = dict(self.settings)
        if "domain" in values:
            domain = str(values["domain"] or "").strip().lower().rstrip(".")
            if domain and not DOMAIN.match(domain):
                raise ValueError("Adresse wie lares.example.de")
            settings["domain"] = domain
        if "a_eintrag" in values:
            settings["a_eintrag"] = bool(values["a_eintrag"])
        if "ip" in values:
            ip = str(values["ip"] or "").strip()
            if ip:
                try:
                    if not ipaddress.ip_address(ip).is_private:
                        raise ValueError
                except ValueError:
                    raise ValueError("IP-Adresse im Heimnetz, z. B. 192.168.178.20") from None
            settings["ip"] = ip
        if "email" in values:
            email = str(values["email"] or "").strip()
            if email and "@" not in email:
                raise ValueError("E-Mail-Adresse ungültig")
            settings["email"] = email
        self.settings = settings
        storage.save(self.info_file, settings)
        return settings

    # the certificate
    def certificate(self) -> x509.Certificate | None:
        try:
            return x509.load_pem_x509_certificate(self.cert_file.read_bytes())
        except (OSError, ValueError):
            return None

    def status(self) -> dict:
        cert = self.certificate()
        names = []
        if cert:
            try:
                names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
                    x509.DNSName)
            except x509.ExtensionNotFound:
                pass
        until = cert.not_valid_after_utc.timestamp() if cert else None
        return {**self.settings, "zertifikat": {"fuer": names, "gueltig_bis": until,
                                                 "aussteller": cert.issuer.rfc4514_string() if cert else ""}
                if cert else None,
                "aktiv": bool(cert and self.settings["domain"] in names and until > time.time()),
                "heim_ip": self.settings["ip"] or home_ip(), "schluessel": bool(self.vault.get("cloudflare_dns_token")),
                "laeuft": self.busy, "schritte": self.steps, "fehler": self.error}

    def needs_renewal(self) -> bool:
        cert = self.certificate()
        if not self.settings["domain"]:
            return False
        if cert is None:
            return True
        try:
            names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
                x509.DNSName)
        except x509.ExtensionNotFound:
            names = []
        left = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
        return self.settings["domain"] not in names or left < datetime.timedelta(days=RENEW_DAYS)

    def dns(self, http: aiohttp.ClientSession):
        test = os.environ.get("LARES_ACME_TESTDNS")
        if test:
            return TestDns(http, test)
        token = self.vault.get("cloudflare_dns_token")
        if not token:
            raise CertError("Schlüssel cloudflare_dns_token fehlt – bei Cloudflare unter „Mein Profil → API-Token“ "
                            "ein Token mit „Zone → DNS → Bearbeiten“ anlegen und unter Schlüssel eintragen")
        return Cloudflare(http, token)

    async def obtain(self, http: aiohttp.ClientSession, ssl_ctx=None) -> None:
        """Gets a new certificate and puts it in use."""
        domain = self.settings["domain"]
        if not domain:
            raise CertError("Erst eine Adresse eintragen")
        self.busy, self.error = True, ""
        try:
            dns = self.dns(http)
            account_file = self.dir / "konto.pem"
            if not account_file.exists():
                storage.write(account_file, key_pem(new_key()))
            account = serialization.load_pem_private_key(account_file.read_bytes(), None)
            acme = Acme(http, LETS_ENCRYPT, account, ssl_ctx)
            await acme.account(self.settings["email"])
            chain, key = await acme.certificate(domain, dns, self.steps.append)
            storage.write(self.key_file, key)
            storage.write(self.cert_file, chain)
            self.reload()
            self.steps.append("Fertig – HTTPS ist aktiv")
            log.info("Zertifikat für %s erhalten", domain)
        except (CertError, aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as err:
            self.error = str(err) or type(err).__name__
            log.warning("Zertifikat für %s nicht erhalten: %s", domain, self.error)
            raise CertError(self.error) from None
        finally:
            self.busy = False

    async def keep_address(self, http: aiohttp.ClientSession) -> bool:
        """Points the address at the home network, if wanted; True when the record changed."""
        if not (self.settings["domain"] and self.settings["a_eintrag"]):
            return False
        ip = self.settings["ip"] or home_ip()
        if not ip:
            return False
        return await self.dns(http).set_a(self.settings["domain"], ip)

    # TLS for the home gateway
    def placeholder(self) -> tuple[Path, Path]:
        """A self-made certificate until the real one is there (the port must listen from the start)."""
        cert, key = self.dir / "platzhalter.pem", self.dir / "platzhalter.key"
        if not cert.exists():
            private = new_key()
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lares.local")])
            now = datetime.datetime.now(datetime.timezone.utc)
            certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                           .public_key(private.public_key()).serial_number(x509.random_serial_number())
                           .not_valid_before(now - datetime.timedelta(days=1))
                           .not_valid_after(now + datetime.timedelta(days=3650))
                           .add_extension(x509.SubjectAlternativeName([x509.DNSName("lares.local")]), critical=False)
                           .sign(private, hashes.SHA256()))
            storage.write(key, key_pem(private))
            storage.write(cert, certificate.public_bytes(serialization.Encoding.PEM))
        return cert, key

    def _load(self, ctx: ssl.SSLContext) -> None:
        if self.certificate() is not None and self.key_file.exists():
            ctx.load_cert_chain(self.cert_file, self.key_file)
        else:
            ctx.load_cert_chain(*self.placeholder())

    def context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self._load(ctx)
        self.contexts.append(ctx)
        return ctx

    def reload(self) -> None:
        """New certificate for the running server: the next connections get it."""
        self.ready = self.certificate() is not None
        for ctx in self.contexts:
            self._load(ctx)

    async def run(self, http: aiohttp.ClientSession) -> None:
        """Every six hours: renew if needed, keep the address pointing home."""
        while True:
            try:
                if self.settings["domain"]:
                    await self.keep_address(http)
                    if self.needs_renewal() and self.vault.get("cloudflare_dns_token") and not self.busy:
                        self.steps = ["Automatische Erneuerung"]
                        await self.obtain(http)
            except Exception as err:  # noqa: BLE001 – try again later
                log.warning("HTTPS-Pflege: %s", err)
            await asyncio.sleep(6 * 3600)

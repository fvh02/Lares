"""Login with Apple or Google (OpenID Connect).

Only accounts that were linked before can log in this way – a Google or Apple account alone never creates an account
in Lares. Both providers need an HTTPS address for the return (at home: the Tailscale address).

Keys in the vault:
  Google: google_client_id, google_client_secret       (Google Cloud → APIs → Credentials → OAuth client, Web)
  Apple:  apple_client_id (Services ID), apple_team_id, apple_key_id, apple_private_key (the .p8 file)
"""

import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlencode

import aiohttp
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

PROVIDERS = {
    "google": {"name": "Google", "auth": "https://accounts.google.com/o/oauth2/v2/auth",
               "token": "https://oauth2.googleapis.com/token", "jwks": "https://www.googleapis.com/oauth2/v3/certs",
               "issuers": ["https://accounts.google.com", "accounts.google.com"], "scope": "openid email",
               "keys": ["google_client_id", "google_client_secret"]},
    "apple": {"name": "Apple", "auth": "https://appleid.apple.com/auth/authorize",
              "token": "https://appleid.apple.com/auth/token", "jwks": "https://appleid.apple.com/auth/keys",
              "issuers": ["https://appleid.apple.com"], "scope": "email",
              "keys": ["apple_client_id", "apple_team_id", "apple_key_id", "apple_private_key"]},
}
PENDING_SECONDS = 600


class OIDCError(ValueError):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def configured(vault, provider: str) -> bool:
    return all(vault.get(k) for k in PROVIDERS[provider]["keys"])


def apple_client_secret(vault, now: float | None = None) -> str:
    """Apple wants a short JWT signed with the .p8 key instead of a fixed secret."""
    now = int(now or time.time())
    header = {"alg": "ES256", "kid": vault.get("apple_key_id")}
    claims = {"iss": vault.get("apple_team_id"), "iat": now, "exp": now + 300, "aud": "https://appleid.apple.com",
              "sub": vault.get("apple_client_id")}
    # The vault removes all spaces and line breaks from *_key values: rebuild the PEM around the base64 body.
    body = re.sub(r"-----(BEGIN|END)\s*PRIVATE\s*KEY-----|\s", "", vault.get("apple_private_key"))
    pem = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + \
        "\n-----END PRIVATE KEY-----\n"
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except ValueError:
        raise OIDCError("apple_private_key ist kein gültiger .p8-Schlüssel") from None
    signing = b64url(json.dumps(header).encode()) + "." + b64url(json.dumps(claims).encode())
    r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    return signing + "." + b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def jwk_to_key(jwk: dict):
    if jwk.get("kty") == "RSA":
        return rsa.RSAPublicNumbers(int.from_bytes(unb64url(jwk["e"]), "big"),
                                    int.from_bytes(unb64url(jwk["n"]), "big")).public_key()
    raise OIDCError("Schlüsseltyp nicht unterstützt")


def verify_id_token(token: str, jwks: dict, issuers: list[str], audience: str, nonce: str,
                    now: float | None = None) -> dict:
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        header, claims = json.loads(unb64url(head_b64)), json.loads(unb64url(body_b64))
    except ValueError:
        raise OIDCError("id_token nicht lesbar") from None
    if header.get("alg") != "RS256":
        raise OIDCError("id_token mit unerwarteter Signatur")
    jwk = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
    if jwk is None:
        raise OIDCError("Schlüssel des Anbieters unbekannt")
    try:
        jwk_to_key(jwk).verify(unb64url(sig_b64), f"{head_b64}.{body_b64}".encode(), padding.PKCS1v15(),
                               hashes.SHA256())
    except InvalidSignature:
        raise OIDCError("Signatur des Anbieters stimmt nicht") from None
    now = now or time.time()
    aud = claims.get("aud")
    if claims.get("iss") not in issuers:
        raise OIDCError("Falscher Aussteller")
    if audience not in (aud if isinstance(aud, list) else [aud]):
        raise OIDCError("Anmeldung ist für eine andere App")
    if float(claims.get("exp", 0)) < now - 60:
        raise OIDCError("Anmeldung abgelaufen")
    if claims.get("nonce") != nonce:
        raise OIDCError("Anmeldung passt nicht zu dieser Anfrage")
    if not claims.get("sub"):
        raise OIDCError("Anbieter nennt kein Konto")
    return claims


class OIDC:
    def __init__(self, vault, http: aiohttp.ClientSession, providers: dict | None = None):
        self.vault, self.http = vault, http
        self.providers = providers or PROVIDERS
        self.pending: dict[str, dict] = {}
        self.jwks: dict[str, tuple[float, dict]] = {}

    def start(self, provider: str, redirect_uri: str, mode: str, account: str | None) -> str:
        """Returns the address of the provider's login page."""
        conf = self.providers[provider]
        if not configured(self.vault, provider):
            raise OIDCError(f"Anmeldung mit {conf['name']} ist nicht eingerichtet")
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v["ablauf"] > now}
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        self.pending[state] = {"provider": provider, "nonce": nonce, "verifier": verifier, "mode": mode,
                               "account": account, "redirect": redirect_uri, "ablauf": now + PENDING_SECONDS}
        params = {"client_id": self.vault.get(f"{provider}_client_id"), "redirect_uri": redirect_uri,
                  "response_type": "code", "scope": conf["scope"], "state": state, "nonce": nonce}
        if provider == "apple":
            params["response_mode"] = "form_post"
        else:
            params.update(code_challenge=b64url(hashlib.sha256(verifier.encode()).digest()),
                          code_challenge_method="S256", prompt="select_account")
        return f"{conf['auth']}?{urlencode(params)}"

    async def finish(self, provider: str, state: str, code: str) -> dict:
        """Returns {provider, sub, email, mode, account}."""
        entry = self.pending.pop(state, None)
        if entry is None or entry["provider"] != provider or entry["ablauf"] < time.time():
            raise OIDCError("Anmeldung abgelaufen – bitte neu starten")
        if not code:
            raise OIDCError("Anmeldung abgebrochen")
        conf = self.providers[provider]
        client_id = self.vault.get(f"{provider}_client_id")
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": entry["redirect"],
                "client_id": client_id}
        if provider == "apple":
            form["client_secret"] = apple_client_secret(self.vault)
        else:
            form.update(client_secret=self.vault.get("google_client_secret"), code_verifier=entry["verifier"])
        try:
            async with self.http.post(conf["token"], data=form, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                status, data = resp.status, await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError, TimeoutError):
            raise OIDCError(f"{conf['name']} nicht erreichbar") from None
        if status >= 300 or not isinstance(data, dict) or "id_token" not in data:
            detail = (data.get("error_description") or data.get("error")) if isinstance(data, dict) else status
            raise OIDCError(f"{conf['name']} lehnt ab: {detail}")
        claims = verify_id_token(data["id_token"], await self._jwks(provider), conf["issuers"], client_id,
                                 entry["nonce"])
        return {"provider": provider, "sub": str(claims["sub"]), "email": str(claims.get("email") or ""),
                "mode": entry["mode"], "account": entry["account"]}

    async def _jwks(self, provider: str) -> dict:
        cached = self.jwks.get(provider)
        if cached and cached[0] > time.time():
            return cached[1]
        try:
            async with self.http.get(self.providers[provider]["jwks"], timeout=aiohttp.ClientTimeout(total=15)) as r:
                keys = await r.json(content_type=None)
        except (aiohttp.ClientError, ValueError, TimeoutError):
            raise OIDCError("Schlüssel des Anbieters nicht abrufbar") from None
        self.jwks[provider] = (time.time() + 3600, keys)
        return keys

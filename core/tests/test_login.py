"""Passkeys, login with Apple and Google, and installing rooms from a link."""

import base64
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp
from aiohttp import web
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from lares_core import oidc
from lares_core.api import fetch_room_yaml, make_app
from lares_core.gateway import Gateway
from lares_core.webauthn import WebAuthnError, b64url, cbor_decode, verify_assertion, verify_registration

ORIGIN, RP = "https://lares.test", "lares.test"


# -- a software passkey ---------------------------------------------------------------------------
def cbor(value) -> bytes:
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
    if isinstance(value, bool):
        return bytes([0xF5 if value else 0xF4])
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        return head(3, len(value.encode())) + value.encode()
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor(v) for v in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(value)


class SoftPasskey:
    def __init__(self, rp_id=RP):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = os.urandom(16)
        self.rp_hash = hashlib.sha256(rp_id.encode()).digest()
        self.count = 0

    def create(self, challenge: str, origin=ORIGIN) -> dict:
        numbers = self.key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        auth = self.rp_hash + bytes([0x45]) + (0).to_bytes(4, "big") + bytes(16) + len(self.id).to_bytes(2, "big") \
            + self.id + cbor(cose)
        client = json.dumps({"type": "webauthn.create", "challenge": challenge, "origin": origin}).encode()
        return {"id": b64url(self.id), "rawId": b64url(self.id), "type": "public-key",
                "response": {"clientDataJSON": b64url(client),
                             "attestationObject": b64url(cbor({"fmt": "none", "attStmt": {}, "authData": auth}))}}

    def get(self, challenge: str, origin=ORIGIN, count=None) -> dict:
        self.count = self.count + 1 if count is None else count
        auth = self.rp_hash + bytes([0x05]) + self.count.to_bytes(4, "big")
        client = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": origin}).encode()
        signature = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64url(self.id), "rawId": b64url(self.id), "type": "public-key",
                "response": {"clientDataJSON": b64url(client), "authenticatorData": b64url(auth),
                             "signature": b64url(signature)}}


class WebAuthnTest(unittest.TestCase):
    def test_cbor(self):
        value = {"a": [1, -5, b"\x00\x01", True, None], 3: {"x": 300, "y": 70000}}
        self.assertEqual(cbor_decode(cbor({"a": [1, -5, b"\x00\x01", True], 3: {"x": 300, "y": 70000}}))[0],
                         {"a": [1, -5, b"\x00\x01", True], 3: {"x": 300, "y": 70000}})
        self.assertTrue(value)

    def test_register_and_login(self):
        pk = SoftPasskey()
        key = verify_registration(pk.create("abc"), "abc", ORIGIN, RP)
        self.assertEqual(key["id"], b64url(pk.id))
        count = verify_assertion(pk.get("xyz"), "xyz", ORIGIN, RP, key["public_key"], 0)
        self.assertEqual(count, 1)
        bad = {
            "falsche Challenge": lambda: verify_assertion(pk.get("xyz"), "andere", ORIGIN, RP, key["public_key"], 1),
            "falscher Ursprung": lambda: verify_assertion(pk.get("xyz", "https://boese.test"), "xyz", ORIGIN, RP,
                                                          key["public_key"], 1),
            "falsche Adresse": lambda: verify_assertion(SoftPasskey("boese.test").get("xyz"), "xyz", ORIGIN, RP,
                                                        key["public_key"], 0),
            "Zähler zurück": lambda: verify_assertion(pk.get("xyz", count=1), "xyz", ORIGIN, RP, key["public_key"], 5),
            "fremder Schlüssel": lambda: verify_assertion(SoftPasskey().get("xyz"), "xyz", ORIGIN, RP,
                                                          key["public_key"], 0),
            "Registrierung mit falscher Challenge": lambda: verify_registration(pk.create("abc"), "zzz", ORIGIN, RP),
        }
        for name, attempt in bad.items():
            with self.subTest(name), self.assertRaises(WebAuthnError):
                attempt()


# -- a fake Google ---------------------------------------------------------------------------------
class FakeProvider:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes: dict[str, dict] = {}
        self.sub = "google-123"

    def id_token(self, nonce: str, aud: str) -> str:
        header = {"alg": "RS256", "kid": "k1"}
        claims = {"iss": "https://accounts.google.com", "aud": aud, "sub": self.sub, "email": "fabian@example.com",
                  "exp": time.time() + 300, "iat": time.time(), "nonce": nonce}
        signing = b64url(json.dumps(header).encode()) + "." + b64url(json.dumps(claims).encode())
        return signing + "." + b64url(self.key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256()))

    def app(self) -> web.Application:
        app = web.Application()

        async def token(request):
            form = await request.post()
            entry = self.codes.pop(form["code"], None)
            if entry is None or form["client_secret"] != "geheim":
                return web.json_response({"error": "invalid_grant"}, status=400)
            verifier = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
            if verifier.decode() != entry["challenge"]:
                return web.json_response({"error": "invalid_grant", "error_description": "PKCE"}, status=400)
            return web.json_response({"id_token": self.id_token(entry["nonce"], form["client_id"])})

        async def jwks(request):
            n = self.key.public_key().public_numbers()
            return web.json_response({"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256",
                                                "n": b64url(n.n.to_bytes(256, "big")), "e": b64url(n.e.to_bytes(3, "big"))}]})
        app.router.add_post("/token", token)
        app.router.add_get("/jwks", jwks)
        return app


class LoginFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runners = []
        self.fake = FakeProvider()
        fake_url = f"http://127.0.0.1:{await self.serve(self.fake.app())}"
        self.core = make_app(Path(self.tmp.name), background=False)
        self.core["oidc_providers"] = {**oidc.PROVIDERS, "google": {
            **oidc.PROVIDERS["google"], "auth": "https://accounts.example/auth", "token": fake_url + "/token",
            "jwks": fake_url + "/jwks"}}
        self.core["vault"].set("google_client_id", "lares-app")
        self.core["vault"].set("google_client_secret", "geheim")
        self.core["accounts"].create("Fabian", "langes-passwort", "admin")
        await self.serve(self.core)
        self.url = f"http://127.0.0.1:{await self.serve(Gateway(self.core, public=False).app())}"
        # as behind Tailscale Serve: HTTPS and the name in the browser
        self.https = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "lares.test", "Origin": ORIGIN}
        # the session cookie is Secure (HTTPS in the browser); the test talks plain HTTP to the gateway
        self.http = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True, treat_as_secure_origin=[self.url]))

    async def asyncTearDown(self):
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

    async def req(self, method, path, json=None, headers=None):
        async with self.http.request(method, self.url + path, json=json, allow_redirects=False,
                                     headers={**self.https, **(headers or {})}) as resp:
            try:
                return resp.status, await resp.json(content_type=None), resp.headers
            except ValueError:
                return resp.status, None, resp.headers

    async def login_password(self):
        status, _, _ = await self.req("POST", "/atrium/anmelden", {"name": "Fabian", "passwort": "langes-passwort"})
        self.assertEqual(status, 200)

    async def google(self, mode=""):
        status, _, headers = await self.req("GET", "/atrium/anmelden/google" + (f"?modus={mode}" if mode else ""))
        self.assertEqual(status, 302)
        query = parse_qs(urlsplit(headers["Location"]).query)
        self.assertEqual(query["redirect_uri"], ["https://lares.test/atrium/anmelden/google/zurueck"])
        self.fake.codes["c1"] = {"nonce": query["nonce"][0], "challenge": query["code_challenge"][0]}
        return await self.req("GET", f"/atrium/anmelden/google/zurueck?state={query['state'][0]}&code=c1")

    async def test_google_only_for_linked_accounts(self):
        status, _, headers = await self.google()
        self.assertEqual(status, 303)
        self.assertIn("fehler=", headers["Location"])          # not linked: no login, no new account
        self.assertIsNone((await self.req("GET", "/atrium/zustand"))[1]["konto"])
        await self.login_password()
        status, _, headers = await self.google("verbinden")
        self.assertEqual((status, headers["Location"]), (303, "/#konto"))
        links = (await self.req("GET", "/atrium/verknuepfungen"))[1]
        self.assertEqual(links["google"]["verbunden"]["email"], "fabian@example.com")
        await self.req("POST", "/atrium/abmelden")
        status, _, headers = await self.google()
        self.assertEqual((status, headers["Location"]), (303, "/"))
        self.assertEqual((await self.req("GET", "/atrium/zustand"))[1]["konto"]["name"], "Fabian")
        self.assertEqual((await self.req("GET", "/atrium/zustand"))[1]["anbieter"], ["google"])

    async def test_state_is_single_use_and_http_refused(self):
        await self.login_password()
        status, _, headers = await self.req("GET", "/atrium/anmelden/google")
        state = parse_qs(urlsplit(headers["Location"]).query)["state"][0]
        self.fake.codes["c2"] = {"nonce": "falsch", "challenge": "x"}
        status, _, headers = await self.req("GET", f"/atrium/anmelden/google/zurueck?state={state}&code=c2")
        self.assertIn("lehnt ab", headers["Location"].replace("%20", " ").replace("+", " "))
        status, _, headers = await self.req("GET", f"/atrium/anmelden/google/zurueck?state={state}&code=c2")
        self.assertIn("abgelaufen", headers["Location"].replace("%20", " ").replace("+", " "))
        status, _, headers = await self.req("GET", "/atrium/anmelden/google", headers={"X-Forwarded-Proto": "http"})
        self.assertIn("HTTPS", headers["Location"])

    async def test_passkeys(self):
        await self.login_password()
        pk = SoftPasskey()
        start = (await self.req("POST", "/atrium/passkey/neu/start"))[1]
        self.assertEqual(start["optionen"]["rp"]["id"], "lares.test")
        status, keys, _ = await self.req("POST", "/atrium/passkey/neu", {
            "vorgang": start["vorgang"], "credential": pk.create(start["optionen"]["challenge"]), "name": "iPhone"})
        self.assertEqual((status, keys[0]["name"]), (200, "iPhone"))
        await self.req("POST", "/atrium/abmelden")
        start = (await self.req("POST", "/atrium/passkey/anmelden/start"))[1]
        cred = pk.get(start["optionen"]["challenge"])
        self.assertEqual((await self.req("POST", "/atrium/passkey/anmelden", {"vorgang": start["vorgang"],
                                                                              "credential": cred}))[0], 200)
        self.assertEqual((await self.req("GET", "/atrium/zustand"))[1]["konto"]["name"], "Fabian")
        # the same answer again (replay) and a foreign passkey are refused
        await self.req("POST", "/atrium/abmelden")
        self.assertEqual((await self.req("POST", "/atrium/passkey/anmelden", {"vorgang": start["vorgang"],
                                                                              "credential": cred}))[0], 401)
        start = (await self.req("POST", "/atrium/passkey/anmelden/start"))[1]
        self.assertEqual((await self.req("POST", "/atrium/passkey/anmelden", {
            "vorgang": start["vorgang"], "credential": SoftPasskey().get(start["optionen"]["challenge"])}))[0], 401)
        # over plain http there are no passkeys
        status, data, _ = await self.req("POST", "/atrium/passkey/anmelden/start",
                                         headers={"X-Forwarded-Proto": "http", "Origin": "http://lares.test"})
        self.assertEqual(status, 400)
        self.assertIn("HTTPS", data["fehler"])


class AppleSecretTest(unittest.TestCase):
    def test_client_secret_from_flattened_p8(self):
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        with tempfile.TemporaryDirectory() as d:
            from lares_core.vault import Vault
            vault = Vault(Path(d))
            for name, value in {"apple_client_id": "de.example.lares", "apple_team_id": "TEAM123",
                                "apple_key_id": "KEY123", "apple_private_key": pem}.items():
                vault.set(name, value)
            self.assertNotIn("\n", vault.get("apple_private_key"))       # the vault flattens *_key values
            token = oidc.apple_client_secret(vault, now=1000)
        head, body, sig = token.split(".")
        claims = json.loads(oidc.unb64url(body))
        self.assertEqual((claims["iss"], claims["sub"], claims["exp"]), ("TEAM123", "de.example.lares", 1300))
        raw = oidc.unb64url(sig)
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        key.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                                f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))


class RoomLinkTest(unittest.IsolatedAsyncioTestCase):
    async def test_refuses_http_and_home_network(self):
        async with aiohttp.ClientSession() as http:
            for url, word in (("http://example.com/room.yaml", "https"), ("https://127.0.0.1/room.yaml", "Heimnetz"),
                              ("https://localhost/room.yaml", "Heimnetz")):
                with self.subTest(url), self.assertRaises(ValueError) as ctx:
                    await fetch_room_yaml(http, url)
                self.assertIn(word, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

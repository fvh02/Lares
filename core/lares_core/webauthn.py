"""Passkeys (WebAuthn): registration and login, verified with the standard library and cryptography.

Lares asks for no attestation ("none"): it only needs the public key of the passkey, not proof of the device maker.
Passkeys work only over HTTPS (or on localhost) – at home via the Tailscale address – and belong to that address.
"""

import base64
import hashlib
import json
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

FLAG_UP, FLAG_UV, FLAG_AT = 0x01, 0x04, 0x40


class WebAuthnError(ValueError):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64url(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(str(text) + "=" * (-len(str(text)) % 4))
    except (ValueError, TypeError):
        raise WebAuthnError("Daten nicht lesbar") from None


# -- CBOR (just enough for WebAuthn) --------------------------------------------------------------
def cbor_decode(data: bytes, pos: int = 0):
    """Returns (value, next position)."""
    if pos >= len(data):
        raise WebAuthnError("CBOR zu kurz")
    first = data[pos]
    major, info = first >> 5, first & 0x1F
    pos += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if pos + size > len(data):
            raise WebAuthnError("CBOR zu kurz")
        arg = int.from_bytes(data[pos:pos + size], "big")
        pos += size
    else:
        raise WebAuthnError("CBOR mit unbestimmter Länge wird nicht unterstützt")
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        if pos + arg > len(data):
            raise WebAuthnError("CBOR zu kurz")
        raw = data[pos:pos + arg]
        return (raw if major == 2 else raw.decode()), pos + arg
    if major == 4:
        items = []
        for _ in range(arg):
            item, pos = cbor_decode(data, pos)
            items.append(item)
        return items, pos
    if major == 5:
        out = {}
        for _ in range(arg):
            key, pos = cbor_decode(data, pos)
            out[key], pos = cbor_decode(data, pos)
        return out, pos
    if major == 6:
        return cbor_decode(data, pos)
    if info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], pos
    if info == 27:
        return struct.unpack(">d", arg.to_bytes(8, "big"))[0], pos
    raise WebAuthnError("CBOR-Typ nicht unterstützt")


# -- keys -----------------------------------------------------------------------------------------
def cose_to_public_key(cose: dict):
    kty, alg = cose.get(1), cose.get(3)
    if kty == 2 and alg == -7 and cose.get(-1) == 1:
        x, y = cose[-2], cose[-3]
        return ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"),
                                             ec.SECP256R1()).public_key()
    if kty == 3 and alg == -257:
        return rsa.RSAPublicNumbers(int.from_bytes(cose[-2], "big"), int.from_bytes(cose[-1], "big")).public_key()
    if kty == 1 and alg == -8 and cose.get(-1) == 6:
        return ed25519.Ed25519PublicKey.from_public_bytes(cose[-2])
    raise WebAuthnError("Diese Art von Passkey wird nicht unterstützt")


def public_pem(key) -> str:
    return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def verify_signature(pem: str, signature: bytes, data: bytes) -> bool:
    key = serialization.load_pem_public_key(pem.encode())
    try:
        if isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        elif isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        else:
            key.verify(signature, data)
    except InvalidSignature:
        return False
    return True


# -- checks ---------------------------------------------------------------------------------------
def _client_data(raw_b64: str, kind: str, challenge: str, origin: str) -> bytes:
    raw = unb64url(raw_b64)
    try:
        data = json.loads(raw)
    except ValueError:
        raise WebAuthnError("clientDataJSON nicht lesbar") from None
    if data.get("type") != kind:
        raise WebAuthnError("Falscher Vorgang")
    if data.get("challenge") != challenge:
        raise WebAuthnError("Anfrage abgelaufen – bitte neu versuchen")
    if data.get("origin") != origin:
        raise WebAuthnError(f"Passkey gehört zu {data.get('origin')}, nicht zu {origin}")
    return raw


def _auth_data(raw: bytes, rp_id: str) -> tuple[int, int, bytes]:
    if len(raw) < 37:
        raise WebAuthnError("authenticatorData zu kurz")
    if raw[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise WebAuthnError("Passkey gehört zu einer anderen Adresse")
    flags, count = raw[32], int.from_bytes(raw[33:37], "big")
    if not flags & FLAG_UP:
        raise WebAuthnError("Keine Bestätigung am Gerät")
    return flags, count, raw[37:]


def verify_registration(credential: dict, challenge: str, origin: str, rp_id: str) -> dict:
    """Checks a new passkey; returns {id, public_key (PEM), sign_count}."""
    response = credential.get("response") or {}
    _client_data(response.get("clientDataJSON", ""), "webauthn.create", challenge, origin)
    attestation, _ = cbor_decode(unb64url(response.get("attestationObject", "")))
    if not isinstance(attestation, dict) or not isinstance(attestation.get("authData"), bytes):
        raise WebAuthnError("attestationObject nicht lesbar")
    flags, count, rest = _auth_data(attestation["authData"], rp_id)
    if not flags & FLAG_AT or len(rest) < 18:
        raise WebAuthnError("Kein Schlüssel im Passkey")
    length = int.from_bytes(rest[16:18], "big")
    cred_id = rest[18:18 + length]
    cose, _ = cbor_decode(rest, 18 + length)
    if not cred_id or b64url(cred_id) != credential.get("id"):
        raise WebAuthnError("Passkey-ID passt nicht")
    return {"id": b64url(cred_id), "public_key": public_pem(cose_to_public_key(cose)), "sign_count": count}


def verify_assertion(credential: dict, challenge: str, origin: str, rp_id: str, public_key: str,
                     sign_count: int) -> int:
    """Checks a login with a passkey; returns the new signature counter."""
    response = credential.get("response") or {}
    client = _client_data(response.get("clientDataJSON", ""), "webauthn.get", challenge, origin)
    auth = unb64url(response.get("authenticatorData", ""))
    _, count, _ = _auth_data(auth, rp_id)
    if not verify_signature(public_key, unb64url(response.get("signature", "")),
                            auth + hashlib.sha256(client).digest()):
        raise WebAuthnError("Signatur stimmt nicht")
    if (count or sign_count) and count <= sign_count:
        raise WebAuthnError("Zähler des Passkeys stimmt nicht – womöglich kopiert")
    return count

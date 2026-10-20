"""The Atrium: login, start page with the tiles of the rooms, and the administration – served by the home gateway.

Everything behind the home gateway needs a session now: the Atrium itself, the rooms at /r/<room>/ and the admin
API (/api/v1/admin/…, for admins). Changing requests must come from the Atrium's own page (Origin check).
"""

import logging
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from aiohttp import web

from . import api
from .accounts import AccountError, Accounts
from .oidc import OIDC, PROVIDERS, OIDCError, configured
from .notify import RateLimit
from .weather import Weather, WeatherError
from .webauthn import WebAuthnError, b64url, verify_assertion, verify_registration

log = logging.getLogger("lares.atrium")
COOKIE = "lares_sitzung"
STATIC = Path(__file__).parent / "static"
SAFE = ("GET", "HEAD", "OPTIONS")
OPEN = ("/atrium/zustand", "/atrium/anmelden", "/atrium/einrichten", "/atrium/passkey/anmelden", "/atrium/wetter",
        "/atrium/static/")
CROSS_SITE = ("/atrium/anmelden/apple/zurueck",)      # Apple answers with a form POST from appleid.apple.com
CHALLENGE_SECONDS = 300

routes = web.RouteTableDef()


def attempts(request: web.Request) -> RateLimit:
    """Login attempts per address and name – at most 10 per minute."""
    return request.app["core"]["atrium_state"].setdefault("versuche", RateLimit(10))


def accounts_of(request: web.Request) -> Accounts:
    return request.app["core"]["accounts"]


def host_of(request: web.Request) -> str:
    """The address in the browser – also behind Tailscale Serve, which reports it as X-Forwarded-Host."""
    return request.headers.get("X-Forwarded-Host") or request.host or ""


def secure(request: web.Request) -> bool:
    return request.scheme == "https" or request.headers.get("X-Forwarded-Proto", "") == "https"


def same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if not origin:
        return False
    host = origin.split("://", 1)[-1].split("/", 1)[0]
    return host == host_of(request)


@web.middleware
async def sessions(request: web.Request, handler):
    """Finds the account of the session cookie; checks the origin of changing requests."""
    request["konto"] = accounts_of(request).session(request.cookies.get(COOKIE, ""))
    own = request.path.startswith(("/atrium/", "/api/"))
    if own and request.method not in SAFE and request.path not in CROSS_SITE and not same_origin(request):
        return web.json_response({"fehler": "Anfrage nicht vom Atrium"}, status=403)
    if request.path.startswith("/atrium/") and not request.path.startswith(OPEN) and request["konto"] is None:
        return web.json_response({"fehler": "Bitte anmelden"}, status=401)
    if request.path.startswith("/api/") and request["konto"] is None:
        return web.json_response({"fehler": "Bitte anmelden"}, status=401)
    return await handler(request)


def with_session(request: web.Request, response: web.Response, token: str) -> web.Response:
    response.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="Lax", secure=secure(request),
                        path="/")
    return response


async def body(request: web.Request) -> dict:
    return await api.body_of(request)


# -- pages ----------------------------------------------------------------------------------------
@routes.get("/")
async def index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC / "atrium.html", headers={
        "Cache-Control": "no-cache", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
                                   "img-src 'self' data:; frame-ancestors 'none'"})


@routes.get("/favicon.ico")
async def favicon(request: web.Request) -> web.Response:
    return web.Response(content_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"}, text=(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><path d="M4 14 16 5l12 9v13H4z" fill="#a0522d"/>'
        '<path d="M12 27v-8h8v8" fill="#f6f1e7"/></svg>'))


@routes.get("/atrium/zustand")
async def state(request: web.Request) -> web.Response:
    accounts = accounts_of(request)
    if accounts.needs_setup:
        accounts.setup_code()
    vault = request.app["core"]["vault"]
    account = request["konto"]
    return web.json_response({"eingerichtet": not accounts.needs_setup, "konto": account,
                              "anbieter": [p for p in PROVIDERS if configured(vault, p)],
                              "darstellung": accounts.appearance(account["name"]) if account else None})


@routes.put("/atrium/darstellung")
async def appearance(request: web.Request) -> web.Response:
    data = await body(request)
    try:
        return web.json_response(accounts_of(request).set_appearance(
            request["konto"]["name"], data.get("design"), data.get("modus")))
    except AccountError as err:
        return api.fail(str(err))


@routes.get("/atrium/wetter")
async def weather(request: web.Request) -> web.Response:
    """Weather for the background: at the position of the device, or else at home (System → Wohnort)."""
    if not attempts(request).ok("wetter:" + (request.remote or "?")):
        return api.fail("Zu viele Anfragen", 429)
    core = request.app["core"]
    try:
        lat, lon = float(request.query["lat"]), float(request.query["lon"])
        source = "geraet"
    except (KeyError, ValueError):
        home = core["system"].data.get("wohnort")
        if not home:
            return api.fail("Kein Ort – Standort freigeben oder unter System den Wohnort eintragen", 404)
        lat, lon, source = home["lat"], home["lon"], "wohnort"
    state = state_of(request)
    if "wetter" not in state:
        state["wetter"] = Weather(core["http"])
    try:
        data = await state["wetter"].at(lat, lon)
    except WeatherError as err:
        return api.fail(str(err), 503)
    return web.json_response({**data, "quelle": source}, headers={"Cache-Control": "private, max-age=300"})


# -- login ----------------------------------------------------------------------------------------
@routes.post("/atrium/einrichten")
async def setup(request: web.Request) -> web.Response:
    """The first account (an admin), with the setup code from the core's log."""
    if not attempts(request).ok("einrichten:" + (request.remote or "?")):
        return api.fail("Zu viele Versuche – bitte eine Minute warten", 429)
    data = await body(request)
    accounts = accounts_of(request)
    try:
        accounts.setup(str(data.get("code") or ""), str(data.get("name") or ""), str(data.get("passwort") or ""))
        token = accounts.login(str(data["name"]), str(data["passwort"]))
    except AccountError as err:
        return api.fail(str(err))
    log.info("Lares eingerichtet, erstes Konto: %s", data["name"])
    return with_session(request, web.json_response({"ok": True}), token)


@routes.post("/atrium/anmelden")
async def login(request: web.Request) -> web.Response:
    data = await body(request)
    name = str(data.get("name") or "")
    if not (attempts(request).ok("ip:" + (request.remote or "?")) and attempts(request).ok("name:" + name.lower())):
        return api.fail("Zu viele Versuche – bitte eine Minute warten", 429)
    try:
        token = accounts_of(request).login(name, str(data.get("passwort") or ""), str(data.get("code") or ""))
    except AccountError as err:
        if str(err) == "2fa":
            return web.json_response({"zwei_faktor": True}, status=401)
        log.warning("Anmeldung für %s fehlgeschlagen (%s)", name[:32], request.remote)
        return api.fail(str(err), 401)
    return with_session(request, web.json_response({"ok": True}), token)


@routes.post("/atrium/abmelden")
async def logout(request: web.Request) -> web.Response:
    accounts_of(request).logout(request.cookies.get(COOKIE, ""))
    response = web.json_response({"ok": True})
    response.del_cookie(COOKIE, path="/")
    return response


# -- for every account ----------------------------------------------------------------------------
@routes.get("/atrium/rooms")
async def tiles(request: web.Request) -> web.Response:
    core = request.app["core"]
    bus = core["bus"]
    return web.json_response([
        {"id": r.id, "name": r.manifest.name, "beschreibung": r.manifest.beschreibung, "symbol": r.manifest.symbol,
         "verbunden": bus.connected(r.id)}
        for r in core["registry"].rooms.values() if r.active and r.manifest.port and r.manifest.kachel])


@routes.post("/atrium/passwort")
async def password(request: web.Request) -> web.Response:
    data = await body(request)
    try:
        accounts_of(request).set_password(request["konto"]["name"], str(data.get("alt") or ""),
                                          str(data.get("neu") or ""))
    except AccountError as err:
        return api.fail(str(err))
    return web.json_response({"ok": True})


@routes.get("/atrium/zwei-faktor")
async def two_factor_state(request: web.Request) -> web.Response:
    account = accounts_of(request).accounts[request["konto"]["name"]]
    return web.json_response({"aktiv": bool(account.get("totp"))})


@routes.post("/atrium/zwei-faktor/start")
async def two_factor_start(request: web.Request) -> web.Response:
    return web.json_response(accounts_of(request).start_totp(request["konto"]["name"]))


@routes.post("/atrium/zwei-faktor/bestaetigen")
async def two_factor_confirm(request: web.Request) -> web.Response:
    data = await body(request)
    try:
        accounts_of(request).confirm_totp(request["konto"]["name"], str(data.get("code") or ""))
    except AccountError as err:
        return api.fail(str(err))
    return web.json_response({"aktiv": True})


@routes.post("/atrium/zwei-faktor/aus")
async def two_factor_off(request: web.Request) -> web.Response:
    data = await body(request)
    try:
        accounts_of(request).disable_totp(request["konto"]["name"], str(data.get("passwort") or ""))
    except AccountError as err:
        return api.fail(str(err))
    return web.json_response({"aktiv": False})


# -- passkeys -------------------------------------------------------------------------------------
def origin_of(request: web.Request) -> str:
    return f"{'https' if secure(request) else request.scheme}://{host_of(request)}"


def rp_id_of(request: web.Request) -> str:
    host = host_of(request).rsplit(":", 1)[0]
    if not secure(request) and host != "localhost":
        raise WebAuthnError("Passkeys gehen nur über HTTPS – unterwegs und zu Hause über die Tailscale-Adresse")
    return host


def state_of(request: web.Request) -> dict:
    return request.app["core"]["atrium_state"]


def new_challenge(request: web.Request, purpose: str, account: str | None = None) -> tuple[str, str]:
    pending = state_of(request).setdefault("passkeys", {})
    now = time.time()
    for key in [k for k, v in pending.items() if v["ablauf"] < now]:
        del pending[key]
    process, challenge = secrets.token_urlsafe(16), b64url(secrets.token_bytes(32))
    pending[process] = {"challenge": challenge, "zweck": purpose, "konto": account, "ablauf": now + CHALLENGE_SECONDS}
    return process, challenge


def take_challenge(request: web.Request, process: str, purpose: str) -> dict:
    entry = state_of(request).setdefault("passkeys", {}).pop(str(process or ""), None)
    if entry is None or entry["zweck"] != purpose or entry["ablauf"] < time.time():
        raise WebAuthnError("Anfrage abgelaufen – bitte neu versuchen")
    return entry


@routes.post("/atrium/passkey/anmelden/start")
async def passkey_login_start(request: web.Request) -> web.Response:
    try:
        rp_id = rp_id_of(request)
    except WebAuthnError as err:
        return api.fail(str(err))
    process, challenge = new_challenge(request, "anmelden")
    return web.json_response({"vorgang": process, "optionen": {
        "challenge": challenge, "rpId": rp_id, "timeout": 120000, "userVerification": "preferred",
        "allowCredentials": []}})


@routes.post("/atrium/passkey/anmelden")
async def passkey_login(request: web.Request) -> web.Response:
    if not attempts(request).ok("ip:" + (request.remote or "?")):
        return api.fail("Zu viele Versuche – bitte eine Minute warten", 429)
    data = await body(request)
    credential = data.get("credential") or {}
    accounts = accounts_of(request)
    try:
        entry = take_challenge(request, data.get("vorgang"), "anmelden")
        found = accounts.find_passkey(str(credential.get("id") or ""))
        if found is None:
            raise WebAuthnError("Diesen Passkey kennt Lares nicht (mehr)")
        name, key = found
        count = verify_assertion(credential, entry["challenge"], origin_of(request), rp_id_of(request),
                                 key["public_key"], int(key.get("sign_count", 0)))
    except WebAuthnError as err:
        log.warning("Passkey-Anmeldung fehlgeschlagen (%s): %s", request.remote, err)
        return api.fail(str(err), 401)
    accounts.used_passkey(key["id"], count)
    log.info("%s mit Passkey angemeldet", name)
    return with_session(request, web.json_response({"ok": True}), accounts.new_session(name))


@routes.post("/atrium/passkey/neu/start")
async def passkey_new_start(request: web.Request) -> web.Response:
    accounts, name = accounts_of(request), request["konto"]["name"]
    try:
        rp_id = rp_id_of(request)
    except WebAuthnError as err:
        return api.fail(str(err))
    process, challenge = new_challenge(request, "neu", name)
    return web.json_response({"vorgang": process, "optionen": {
        "challenge": challenge, "rp": {"name": "Lares", "id": rp_id},
        "user": {"id": accounts.webauthn_id(name), "name": name, "displayName": name},
        "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in (-7, -8, -257)],
        "timeout": 120000, "attestation": "none",
        "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True,
                                   "userVerification": "preferred"},
        "excludeCredentials": [{"type": "public-key", "id": p["id"]} for p in accounts.passkeys(name)]}})


@routes.post("/atrium/passkey/neu")
async def passkey_new(request: web.Request) -> web.Response:
    data = await body(request)
    accounts, name = accounts_of(request), request["konto"]["name"]
    try:
        entry = take_challenge(request, data.get("vorgang"), "neu")
        if entry["konto"] != name:
            raise WebAuthnError("Anfrage gehört zu einem anderen Konto")
        key = verify_registration(data.get("credential") or {}, entry["challenge"], origin_of(request),
                                  rp_id_of(request))
        accounts.add_passkey(name, key, str(data.get("name") or ""))
    except (WebAuthnError, AccountError) as err:
        return api.fail(str(err))
    log.info("Neuer Passkey für %s", name)
    return web.json_response(accounts.passkeys(name))


@routes.get("/atrium/passkeys")
async def passkey_list(request: web.Request) -> web.Response:
    return web.json_response(accounts_of(request).passkeys(request["konto"]["name"]))


@routes.delete("/atrium/passkeys/{id}")
async def passkey_delete(request: web.Request) -> web.Response:
    accounts, name = accounts_of(request), request["konto"]["name"]
    accounts.remove_passkey(name, request.match_info["id"])
    return web.json_response(accounts.passkeys(name))


# -- Apple and Google -----------------------------------------------------------------------------
def oidc_of(request: web.Request) -> OIDC:
    state = state_of(request)
    if "oidc" not in state:
        core = request.app["core"]
        state["oidc"] = OIDC(core["vault"], core["http"], core.get("oidc_providers"))
    return state["oidc"]


def back_with(message: str, page: str = "") -> web.HTTPSeeOther:
    return web.HTTPSeeOther("/?" + urlencode({"fehler": message}) + (f"#{page}" if page else ""))


@routes.get("/atrium/anmelden/{provider:google|apple}")
async def provider_start(request: web.Request) -> web.Response:
    provider = request.match_info["provider"]
    mode = "verbinden" if request.query.get("modus") == "verbinden" else "anmelden"
    if mode == "verbinden" and request["konto"] is None:
        raise back_with("Bitte zuerst anmelden")
    if not secure(request):
        raise back_with(f"Anmeldung mit {PROVIDERS[provider]['name']} geht nur über HTTPS (Tailscale-Adresse)",
                        "konto" if mode == "verbinden" else "")
    redirect = f"{origin_of(request)}/atrium/anmelden/{provider}/zurueck"
    try:
        url = oidc_of(request).start(provider, redirect, mode, (request["konto"] or {}).get("name"))
    except OIDCError as err:
        raise back_with(str(err), "konto" if mode == "verbinden" else "") from None
    raise web.HTTPFound(url)


@routes.route("*", "/atrium/anmelden/{provider:google|apple}/zurueck")
async def provider_back(request: web.Request) -> web.Response:
    provider = request.match_info["provider"]
    params = dict(request.query)
    if request.method == "POST":
        params.update({k: str(v) for k, v in (await request.post()).items()})
    if params.get("error"):
        raise back_with(f"{PROVIDERS[provider]['name']}: Anmeldung abgebrochen")
    try:
        result = await oidc_of(request).finish(provider, params.get("state", ""), params.get("code", ""))
    except OIDCError as err:
        log.warning("Anmeldung mit %s fehlgeschlagen: %s", provider, err)
        raise back_with(str(err)) from None
    accounts = accounts_of(request)
    if result["mode"] == "verbinden":
        try:
            accounts.link(result["account"], provider, result["sub"], result["email"])
        except AccountError as err:
            raise back_with(str(err), "konto") from None
        log.info("%s mit %s verbunden", result["account"], provider)
        raise web.HTTPSeeOther("/#konto")
    name = accounts.find_link(provider, result["sub"])
    if not name:
        raise back_with(f"Dieses {PROVIDERS[provider]['name']}-Konto ist mit keinem Lares-Konto verbunden – "
                        "erst mit Passwort anmelden und unter „Mein Konto“ verbinden")
    log.info("%s mit %s angemeldet", name, provider)
    response = web.HTTPSeeOther("/")
    with_session(request, response, accounts.new_session(name))
    raise response


@routes.get("/atrium/verknuepfungen")
async def provider_links(request: web.Request) -> web.Response:
    vault, links = request.app["core"]["vault"], accounts_of(request).links(request["konto"]["name"])
    return web.json_response({p: {"name": c["name"], "eingerichtet": configured(vault, p), "verbunden": links.get(p)}
                              for p, c in PROVIDERS.items()})


@routes.delete("/atrium/verknuepfungen/{provider:google|apple}")
async def provider_unlink(request: web.Request) -> web.Response:
    accounts_of(request).unlink(request["konto"]["name"], request.match_info["provider"])
    return web.json_response({"ok": True})


def install(app: web.Application, core: web.Application) -> None:
    """Adds the Atrium and the admin API to the home gateway (before its catch-all route)."""
    app["core"] = core
    app.middlewares.append(sessions)
    app.add_routes(routes)
    app.router.add_static("/atrium/static/", STATIC)
    for route in api.routes:
        if isinstance(route, web.RouteDef) and route.path.startswith("/api/v1/admin/"):
            app.router.add_route(route.method, route.path, route.handler)


def account_headers(account: dict | None) -> dict:
    """Who is using a room – the name percent-encoded (umlauts)."""
    return {"X-Lares-Benutzer": quote(account["name"]), "X-Lares-Rolle": account["rolle"]} if account else {}

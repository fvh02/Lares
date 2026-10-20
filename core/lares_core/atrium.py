"""The Atrium: login, start page with the tiles of the rooms, and the administration – served by the home gateway.

Everything behind the home gateway needs a session now: the Atrium itself, the rooms at /r/<room>/ and the admin
API (/api/v1/admin/…, for admins). Changing requests must come from the Atrium's own page (Origin check).
"""

import logging
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from . import api
from .accounts import AccountError, Accounts
from .notify import RateLimit

log = logging.getLogger("lares.atrium")
COOKIE = "lares_sitzung"
STATIC = Path(__file__).parent / "static"
SAFE = ("GET", "HEAD", "OPTIONS")
OPEN = {"/atrium/zustand", "/atrium/anmelden", "/atrium/einrichten"}

routes = web.RouteTableDef()
attempts = RateLimit(10)


def accounts_of(request: web.Request) -> Accounts:
    return request.app["core"]["accounts"]


def secure(request: web.Request) -> bool:
    return request.scheme == "https" or request.headers.get("X-Forwarded-Proto", "") == "https"


def same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if not origin:
        return False
    host = origin.split("://", 1)[-1].split("/", 1)[0]
    return host == request.host


@web.middleware
async def sessions(request: web.Request, handler):
    """Finds the account of the session cookie; checks the origin of changing requests."""
    request["konto"] = accounts_of(request).session(request.cookies.get(COOKIE, ""))
    own = request.path.startswith(("/atrium/", "/api/"))
    if own and request.method not in SAFE and not same_origin(request):
        return web.json_response({"fehler": "Anfrage nicht vom Atrium"}, status=403)
    if request.path.startswith("/atrium/") and request.path not in OPEN and request["konto"] is None:
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
        "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
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
    return web.json_response({"eingerichtet": not accounts.needs_setup, "konto": request["konto"]})


# -- login ----------------------------------------------------------------------------------------
@routes.post("/atrium/einrichten")
async def setup(request: web.Request) -> web.Response:
    """The first account (an admin), with the setup code from the core's log."""
    if not attempts.ok("einrichten:" + (request.remote or "?")):
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
    if not (attempts.ok("ip:" + (request.remote or "?")) and attempts.ok("name:" + name.lower())):
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

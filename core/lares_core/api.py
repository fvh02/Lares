"""The HTTP API of Lares Core.

/api/v1/…        for the rooms – each room shows its own token (Authorization: Bearer …); the core checks every
                 request against the rights in its room.yaml.
/api/v1/admin/…  for the Atrium and the lares command – with the admin token (until the Atrium has its login).
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from . import __version__, storage
from .bus import Bus, Busy, matches
from .manifest import EVENT, ManifestError, parse
from .notify import MAX_IMAGE, Notifier
from .registry import Registry, Room
from .remote import Remote
from .runtime import ContainerError, ContainerRuntime, NoRuntime, room_container, service_container
from .scheduler import Scheduler
from .system import System
from .vault import Vault

log = logging.getLogger("lares.api")
PING = 25
COMMAND_TIMEOUT = 120
AI_TIMEOUT = 900
NO_DEVICE_CORE = "Der Device Core ist noch nicht da (Etappe 3) – Geräte und Standort folgen"

routes = web.RouteTableDef()


# -- helpers --------------------------------------------------------------------------------------
def fail(message: str, status: int = 400) -> web.Response:
    return web.json_response({"fehler": message}, status=status)


def deny(message: str) -> web.HTTPForbidden:
    return web.HTTPForbidden(text=json.dumps({"fehler": message}, ensure_ascii=False), content_type="application/json")


async def body_of(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({"fehler": "JSON erwartet"}), content_type="application/json") from None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text=json.dumps({"fehler": "JSON-Objekt erwartet"}), content_type="application/json")
    return data


def bearer(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else ""


def room_of(request: web.Request) -> Room:
    room = request.app["registry"].room_for_token(bearer(request))
    if room is None:
        raise web.HTTPUnauthorized(text=json.dumps({"fehler": "Token unbekannt"}), content_type="application/json")
    if not room.active:
        raise deny(f"{room.manifest.name} ist ausgeschaltet")
    return room


def require_admin(request: web.Request) -> None:
    if not secrets.compare_digest(bearer(request).encode(), request.app["admin_token"].encode()):
        raise web.HTTPUnauthorized(text=json.dumps({"fehler": "Admin-Token nötig"}), content_type="application/json")


def allowed_patterns(requested: list[str], rights: list[str]) -> list[str]:
    """The subscriptions a room asked for, as far as its rights cover them."""
    return [p for p in requested if matches(p, rights)]


# -- for the rooms --------------------------------------------------------------------------------
@routes.get("/api/v1/ich")
async def me(request: web.Request) -> web.Response:
    room = room_of(request)
    return web.json_response({**room.info(), "kern": __version__})


@routes.get("/api/v1/einstellungen")
async def settings(request: web.Request) -> web.Response:
    return web.json_response(room_of(request).settings())


@routes.get("/api/v1/system")
async def system(request: web.Request) -> web.Response:
    room_of(request)
    return web.json_response(request.app["system"].public())


@routes.get("/api/v1/schluessel/{name}")
async def key(request: web.Request) -> web.Response:
    room, name = room_of(request), request.match_info["name"]
    if name not in room.manifest.rechte.schluessel:
        raise deny(f"{room.manifest.name} darf den Schlüssel {name} nicht lesen")
    value = request.app["vault"].get(name)
    if not value:
        return fail(f"Schlüssel {name} ist nicht eingetragen", 404)
    log.info("Schlüssel %s an %s ausgegeben", name, room.id)
    return web.json_response({"name": name, "wert": value})


@routes.post("/api/v1/melden")
async def notify(request: web.Request) -> web.Response:
    room = room_of(request)
    if not room.manifest.rechte.benachrichtigen:
        raise deny(f"{room.manifest.name} darf keine Nachrichten schicken")
    notifier: Notifier = request.app["notifier"]
    if not notifier.limit.ok(room.id):
        return fail("Zu viele Nachrichten – höchstens 20 pro Minute", 429)
    data = await body_of(request)
    text = str(data.get("text") or "").strip()
    image = None
    if data.get("bild_b64"):
        try:
            image = base64.b64decode(str(data["bild_b64"]), validate=True)
        except ValueError:
            return fail("Bild nicht lesbar")
        if len(image) > MAX_IMAGE:
            return fail("Bild zu groß")
    if not text and not image:
        return fail("Kein Text")
    try:
        result = await notifier.send(room.id, f"{room.manifest.name}: {text}" if text else room.manifest.name,
                                     to=data.get("an"), urgent=bool(data.get("dringend")), image=image,
                                     filename=os.path.basename(str(data.get("dateiname") or "bild.jpg")))
    except RuntimeError as err:
        return fail(str(err), 503)
    return web.json_response(result)


@routes.post("/api/v1/ki")
async def ai(request: web.Request) -> web.Response:
    room = room_of(request)
    if not room.manifest.rechte.ki:
        raise deny(f"{room.manifest.name} darf die KI nicht fragen")
    data = await body_of(request)
    question = str(data.get("frage") or "").strip()
    if not question:
        return fail("Keine Frage")
    registry: Registry = request.app["registry"]
    bus: Bus = request.app["bus"]
    provider = next((r for r in registry.rooms.values() if r.active and r.manifest.rechte.ki_anbieter
                     and r.id != room.id and bus.connected(r.id)), None)
    if provider is None:
        return fail("Kein KI-Room verbunden – die KI läuft über einen Room mit dem Recht ki_anbieter "
                    "(z. B. den Assistenten)", 503)
    usage = request.app["ai_usage"].setdefault(room.id, {"anfragen": 0, "zeichen": 0})
    usage["anfragen"] += 1
    usage["zeichen"] += len(question)
    storage.save(request.app["core_dir"] / "ki.json", request.app["ai_usage"])
    try:
        result = await bus.request(provider.id, {"typ": "ki", "von": room.id, "frage": question[:200_000],
                                                 "schema": data.get("schema")}, AI_TIMEOUT)
    except Busy as err:
        return fail(f"{provider.manifest.name}: {err}", 503)
    if result.get("fehler"):
        return fail(str(result["fehler"]), 502)
    return web.json_response({"antwort": result.get("ergebnis")})


@routes.get("/api/v1/standort")
async def location(request: web.Request) -> web.Response:
    room = room_of(request)
    if not room.manifest.rechte.standort:
        raise deny(f"{room.manifest.name} darf den Standort nicht sehen")
    return fail(NO_DEVICE_CORE, 503)


@routes.route("*", "/api/v1/geraete{rest:.*}")
async def devices(request: web.Request) -> web.Response:
    room = room_of(request)
    if not room.manifest.rechte.geraete:
        raise deny(f"{room.manifest.name} darf keine Geräte nutzen")
    return fail(NO_DEVICE_CORE, 503)


@routes.post("/api/v1/ereignisse")
async def send_event(request: web.Request) -> web.Response:
    room = room_of(request)
    data = await body_of(request)
    name = str(data.get("name") or "")
    if not EVENT.match(name) or "*" in name:
        return fail("Name wie serien.neue_folge")
    if not matches(name, room.manifest.rechte.ereignisse_senden):
        raise deny(f"{room.manifest.name} darf {name} nicht senden")
    payload = data.get("daten") or {}
    if not isinstance(payload, dict) or len(json.dumps(payload)) > 64_000:
        return fail("daten: ein JSON-Objekt bis 64 KB")
    event = request.app["bus"].publish(room.id, name, payload)
    return web.json_response({"nr": event["nr"]})


@routes.get("/api/v1/ereignisse")
async def recent_events(request: web.Request) -> web.Response:
    room = room_of(request)
    try:
        since = int(request.query.get("seit", "0"))
    except ValueError:
        return fail("seit: eine Nummer")
    rights = room.manifest.rechte.ereignisse_empfangen
    return web.json_response([e for e in request.app["bus"].recent if e["nr"] > since and matches(e["name"], rights)])


@routes.get("/api/v1/strom")
async def stream(request: web.Request) -> web.StreamResponse:
    """The room's stream (server-sent events): events it subscribed to, schedules, commands and AI questions."""
    room = room_of(request)
    requested = [p for p in request.query.get("abo", "").split(",") if p]
    patterns = allowed_patterns(requested, room.manifest.rechte.ereignisse_empfangen)
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
    await response.prepare(request)
    bus: Bus = request.app["bus"]
    conn = bus.connect(room.id, patterns)
    log.info("%s verbunden (Abos: %s)", room.id, ", ".join(patterns) or "keine")
    try:
        hello = {"typ": "hallo", "abos": patterns, "abgelehnt": [p for p in requested if p not in patterns]}
        await response.write(f"data: {json.dumps(hello)}\n\n".encode())
        while True:
            try:
                message = await asyncio.wait_for(conn.queue.get(), PING)
            except asyncio.TimeoutError:
                await response.write(b": ping\n\n")
                continue
            await response.write(f"data: {json.dumps(message, ensure_ascii=False)}\n\n".encode())
    except ConnectionResetError:
        pass
    finally:
        bus.disconnect(conn)
        log.info("%s getrennt", room.id)
    return response


@routes.post("/api/v1/strom/antwort")
async def stream_answer(request: web.Request) -> web.Response:
    room = room_of(request)
    data = await body_of(request)
    result = {"fehler": str(data["fehler"])} if data.get("fehler") else {"ergebnis": data.get("ergebnis")}
    if not request.app["bus"].answer(room.id, str(data.get("id") or ""), result):
        return fail("Keine offene Anfrage mit dieser id", 404)
    return web.json_response({"ok": True})


@routes.get("/api/v1/befehle")
async def commands(request: web.Request) -> web.Response:
    room = room_of(request)
    if not room.manifest.rechte.befehle:
        raise deny(f"{room.manifest.name} darf keine Befehle anderer Rooms nutzen")
    bus: Bus = request.app["bus"]
    return web.json_response([
        {"room": r.id, "name": r.manifest.name, "verbunden": bus.connected(r.id),
         "befehle": [vars(c) for c in r.manifest.befehle]}
        for r in request.app["registry"].rooms.values() if r.active and r.manifest.befehle and r.id != room.id])


@routes.post("/api/v1/befehle/{room}/{name}")
async def run_command(request: web.Request) -> web.Response:
    room = room_of(request)
    if not room.manifest.rechte.befehle:
        raise deny(f"{room.manifest.name} darf keine Befehle anderer Rooms nutzen")
    target = request.app["registry"].get(request.match_info["room"])
    name = request.match_info["name"]
    if target is None or not target.active or target.id == room.id:
        return fail("Room nicht installiert oder ausgeschaltet", 404)
    command = next((c for c in target.manifest.befehle if c.name == name), None)
    if command is None:
        return fail(f"{target.manifest.name} kennt den Befehl {name} nicht", 404)
    data = await body_of(request) if request.can_read_body else {}
    args = data.get("argumente") or {}
    if not isinstance(args, dict) or set(args) - set(command.argumente):
        return fail(f"argumente: nur {', '.join(command.argumente) or 'keine'}")
    try:
        result = await request.app["bus"].request(target.id, {"typ": "befehl", "von": room.id, "name": name,
                                                              "argumente": args}, COMMAND_TIMEOUT)
    except Busy as err:
        return fail(f"{target.manifest.name}: {err}", 503)
    if result.get("fehler"):
        return fail(str(result["fehler"]), 502)
    return web.json_response({"ergebnis": result.get("ergebnis")})


# -- for the Atrium and the lares command ---------------------------------------------------------
async def start_room(app: web.Application, room: Room) -> str:
    """Issues a fresh token and starts the container; returns the token (needed without a runtime)."""
    token = app["registry"].issue_token(room.id)
    await app["runtime"].start(room, token)
    return token


@routes.get("/api/v1/admin/rooms")
async def admin_rooms(request: web.Request) -> web.Response:
    require_admin(request)
    bus: Bus = request.app["bus"]
    out = []
    for room in request.app["registry"].rooms.values():
        out.append({**room.info(), "verbunden": bus.connected(room.id),
                    "container": await request.app["runtime"].status(room_container(room.id)),
                    "freigabe": request.app["remote"].public_release(room.id)})
    return web.json_response(out)


@routes.post("/api/v1/admin/rooms")
async def admin_install(request: web.Request) -> web.Response:
    """Installs or updates a room. The rights must be confirmed: without "rechte_bestaetigt" the answer lists them."""
    require_admin(request)
    data = await body_of(request)
    try:
        manifest = parse(str(data.get("room_yaml") or ""))
    except ManifestError as err:
        return fail(str(err))
    registry: Registry = request.app["registry"]
    old = registry.get(manifest.id)
    rights = manifest.rechte.summary()
    if not data.get("rechte_bestaetigt") and (old is None or old.manifest.rechte != manifest.rechte):
        return web.json_response({"bestaetigen": True, "room": manifest.id, "rechte": rights}, status=409)
    room = registry.install(manifest)
    out = {**room.info(), "neu": old is None}
    if room.active:
        try:
            token = await start_room(request.app, room)
        except ContainerError as err:
            return fail(f"Installiert, aber nicht gestartet: {err}", 502)
        if isinstance(request.app["runtime"], NoRuntime):
            out["token"] = token     # without a runtime the room is started by hand and needs its token
    log.info("%s %s %s", manifest.id, manifest.version, "installiert" if old is None else "aktualisiert")
    return web.json_response(out)


def admin_room(request: web.Request) -> Room:
    require_admin(request)
    room = request.app["registry"].get(request.match_info["room"])
    if room is None:
        raise web.HTTPNotFound(text=json.dumps({"fehler": "Room nicht installiert"}), content_type="application/json")
    return room


@routes.delete("/api/v1/admin/rooms/{room}")
async def admin_remove(request: web.Request) -> web.Response:
    room = admin_room(request)
    await request.app["runtime"].remove(room_container(room.id))
    request.app["registry"].remove(room.id, keep_data=request.query.get("daten") != "loeschen")
    request.app["remote"].release(room.id, None)
    return web.json_response({"entfernt": room.id})


@routes.post("/api/v1/admin/rooms/{room}/{action:start|stop}")
async def admin_start_stop(request: web.Request) -> web.Response:
    room = admin_room(request)
    registry: Registry = request.app["registry"]
    if request.match_info["action"] == "stop":
        registry.set_active(room.id, False)
        await request.app["runtime"].stop(room_container(room.id))
        return web.json_response({"aktiv": False})
    registry.set_active(room.id, True)
    try:
        token = await start_room(request.app, room)
    except ContainerError as err:
        return fail(str(err), 502)
    out = {"aktiv": True}
    if isinstance(request.app["runtime"], NoRuntime):
        out["token"] = token
    return web.json_response(out)


@routes.post("/api/v1/admin/rooms/{room}/token")
async def admin_token(request: web.Request) -> web.Response:
    """A new token for a room started by hand (development); the old one stops working."""
    room = admin_room(request)
    if not isinstance(request.app["runtime"], NoRuntime):
        return fail("Nur ohne Container-Laufzeit – sonst bekommt der Room sein Token beim Start")
    return web.json_response({"token": request.app["registry"].issue_token(room.id)})


@routes.get("/api/v1/admin/rooms/{room}/einstellungen")
async def admin_settings(request: web.Request) -> web.Response:
    room = admin_room(request)
    form = [{"name": s.name, "typ": s.typ, "titel": s.titel or s.name, "standard": s.standard, "min": s.min,
             "max": s.max, "werte": s.werte} for s in room.manifest.einstellungen.values()]
    return web.json_response({"formular": form, "werte": room.settings()})


@routes.put("/api/v1/admin/rooms/{room}/einstellungen")
async def admin_set_settings(request: web.Request) -> web.Response:
    room = admin_room(request)
    try:
        values = request.app["registry"].set_settings(room.id, await body_of(request))
    except ManifestError as err:
        return fail(str(err))
    request.app["bus"].tell(room.id, {"typ": "einstellungen", "werte": values})
    return web.json_response(values)


@routes.get("/api/v1/admin/rooms/{room}/logs")
async def admin_logs(request: web.Request) -> web.Response:
    room = admin_room(request)
    return web.Response(text=await request.app["runtime"].logs(room_container(room.id)))


@routes.get("/api/v1/admin/schluessel")
async def admin_keys(request: web.Request) -> web.Response:
    require_admin(request)
    registry: Registry = request.app["registry"]
    users: dict[str, list[str]] = {}
    for room in registry.rooms.values():
        for name in room.manifest.rechte.schluessel:
            users.setdefault(name, []).append(room.id)
    names = set(request.app["vault"].names()) | set(users)
    return web.json_response([{"name": n, "eingetragen": bool(request.app["vault"].get(n)),
                               "rooms": users.get(n, [])} for n in sorted(names)])


@routes.put("/api/v1/admin/schluessel/{name}")
async def admin_set_key(request: web.Request) -> web.Response:
    require_admin(request)
    data = await body_of(request)
    try:
        request.app["vault"].set(request.match_info["name"], str(data.get("wert") or ""))
    except ValueError as err:
        return fail(str(err))
    service = SERVICE_KEYS.get(request.match_info["name"])
    if service and request.app["remote"].data[service]["aktiv"]:
        try:
            await start_service(request.app, service)     # restart with the new key
        except (ContainerError, ValueError) as err:
            return fail(f"Gespeichert, aber {service} nicht neu gestartet: {err}", 502)
    return web.json_response({"gespeichert": True})


@routes.delete("/api/v1/admin/schluessel/{name}")
async def admin_delete_key(request: web.Request) -> web.Response:
    require_admin(request)
    request.app["vault"].delete(request.match_info["name"])
    return web.json_response({"geloescht": True})


@routes.get("/api/v1/admin/system")
async def admin_system(request: web.Request) -> web.Response:
    require_admin(request)
    return web.json_response(request.app["system"].data)


@routes.put("/api/v1/admin/system")
async def admin_set_system(request: web.Request) -> web.Response:
    require_admin(request)
    try:
        data = request.app["system"].update(await body_of(request))
    except ValueError as err:
        return fail(str(err))
    request.app["bus"].tell_all({"typ": "system", "werte": request.app["system"].public()})
    return web.json_response(data)


@routes.post("/api/v1/admin/test/melden")
async def admin_test_notify(request: web.Request) -> web.Response:
    require_admin(request)
    try:
        await request.app["notifier"].send("lares", "Lares: Testnachricht ✅", urgent=True)
    except RuntimeError as err:
        return fail(str(err), 503)
    return web.json_response({"gesendet": True})


@routes.get("/api/v1/admin/ki")
async def admin_ai_usage(request: web.Request) -> web.Response:
    require_admin(request)
    return web.json_response(request.app["ai_usage"])


# -- remote access (Tailscale, Cloudflare Tunnel) -------------------------------------------------
SERVICES = ("tailscale", "cloudflared")
SERVICE_KEYS = {"tailscale_authkey": "tailscale", "cloudflare_tunnel_token": "cloudflared"}
LOGIN_URL = re.compile(r"https://login\.tailscale\.com/a/[A-Za-z0-9]+")


async def start_service(app: web.Application, name: str) -> None:
    remote: Remote = app["remote"]
    service = remote.tailscale_service() if name == "tailscale" else remote.cloudflared_service()
    await app["runtime"].start_service(service)


async def service_state(app: web.Application, name: str) -> dict:
    runtime = app["runtime"]
    state = await runtime.status(service_container(name))
    out = {"container": state}
    if name == "tailscale" and state == "running":
        logs = await runtime.logs(service_container(name), 400)
        login = LOGIN_URL.findall(logs)
        if login and "Startup complete" not in logs.split(login[-1])[-1]:
            out["anmelden"] = login[-1]     # no key: log in once on this page (works from the phone)
    return out


@routes.get("/api/v1/admin/fernzugriff")
async def admin_remote(request: web.Request) -> web.Response:
    require_admin(request)
    remote: Remote = request.app["remote"]
    vault: Vault = request.app["vault"]
    return web.json_response({
        "tailscale": {**remote.data["tailscale"], "schluessel": bool(vault.get("tailscale_authkey")),
                      **await service_state(request.app, "tailscale")},
        "cloudflared": {**remote.data["cloudflared"], "schluessel": bool(vault.get("cloudflare_tunnel_token")),
                        "ziel": f"http://{remote.core_host}:{remote.public_port}",
                        **await service_state(request.app, "cloudflared")},
        "freigaben": {rid: remote.public_release(rid) for rid in remote.data["freigaben"]},
    })


@routes.put("/api/v1/admin/fernzugriff/{name:tailscale|cloudflared}")
async def admin_set_remote(request: web.Request) -> web.Response:
    """Saves the settings and starts or stops the service accordingly."""
    require_admin(request)
    name, remote = request.match_info["name"], request.app["remote"]
    data = await body_of(request)
    try:
        settings = remote.set_tailscale(data) if name == "tailscale" else remote.set_cloudflared(data)
        if settings["aktiv"]:
            await start_service(request.app, name)
        else:
            await request.app["runtime"].remove(service_container(name))
    except ValueError as err:
        return fail(str(err))
    except ContainerError as err:
        return fail(str(err), 502)
    return web.json_response({**settings, **await service_state(request.app, name)})


@routes.get("/api/v1/admin/fernzugriff/{name:tailscale|cloudflared}/logs")
async def admin_remote_logs(request: web.Request) -> web.Response:
    require_admin(request)
    return web.Response(text=await request.app["runtime"].logs(service_container(request.match_info["name"])))


@routes.put("/api/v1/admin/rooms/{room}/freigabe")
async def admin_release(request: web.Request) -> web.Response:
    """Releases a room for the internet: {host?, zugangscode?}. Only rooms with oberflaeche.oeffentlich."""
    room = admin_room(request)
    if not room.manifest.port or not room.manifest.oeffentlich:
        return fail(f"{room.manifest.name} ist laut room.yaml nicht für das Internet gedacht (oberflaeche.oeffentlich)")
    try:
        return web.json_response(request.app["remote"].release(room.id, await body_of(request)))
    except ValueError as err:
        return fail(str(err))


@routes.delete("/api/v1/admin/rooms/{room}/freigabe")
async def admin_withdraw(request: web.Request) -> web.Response:
    room = admin_room(request)
    request.app["remote"].release(room.id, None)
    return web.json_response({"freigabe": None})


@routes.get("/api/v1/status")
async def status(request: web.Request) -> web.Response:
    return web.json_response({"lares": __version__, "laufzeit": request.app["runtime"].name})


# -- the app --------------------------------------------------------------------------------------
@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def admin_token_of(core_dir: Path) -> str:
    path = core_dir / "admin.token"
    token = path.read_text().strip() if path.exists() else ""
    if not token:
        token = secrets.token_urlsafe(32)
        storage.write(path, token.encode())
        log.info("Admin-Token angelegt: %s", path)
    return token


def make_runtime(kind: str, core_url: str, host_data_dir: Path, local_data_dir: Path):
    if kind in ("docker", "podman"):
        return ContainerRuntime(kind, core_url, host_data_dir, local_data_dir)
    return NoRuntime()


def make_app(data_dir: Path, runtime=None, background: bool = True, core_host: str = "lares-core",
             home_port: int = 8000, public_port: int = 8001) -> web.Application:
    core_dir, rooms_dir = data_dir / "core", data_dir / "rooms"
    core_dir.mkdir(parents=True, exist_ok=True)
    app = web.Application(client_max_size=12 * 1024 * 1024, middlewares=[security_headers])
    app["core_dir"] = core_dir
    app["vault"] = Vault(core_dir)
    app["system"] = System(core_dir)
    app["registry"] = Registry(core_dir, rooms_dir)
    app["bus"] = Bus()
    app["admin_token"] = admin_token_of(core_dir)
    app["ai_usage"] = storage.load(core_dir / "ki.json", {})
    app["runtime"] = runtime or NoRuntime()
    app["remote"] = Remote(core_dir, app["vault"], core_host, home_port, public_port)
    app.add_routes(routes)

    async def on_startup(app: web.Application) -> None:
        app["http"] = aiohttp.ClientSession()
        app["notifier"] = Notifier(core_dir, app["vault"], app["system"], app["http"])
        app["scheduler"] = Scheduler(core_dir, app["registry"], app["system"], app["bus"], app["notifier"])
        if background:
            app["scheduler_task"] = asyncio.create_task(app["scheduler"].run())
            if not isinstance(app["runtime"], NoRuntime):
                for room in list(app["registry"].rooms.values()):
                    if room.active:
                        try:
                            await start_room(app, room)
                        except ContainerError as err:
                            log.warning("%s nicht gestartet: %s", room.id, err)
                for name in SERVICES:
                    if app["remote"].data[name]["aktiv"]:
                        try:
                            await start_service(app, name)
                        except (ContainerError, ValueError) as err:
                            log.warning("Dienst %s nicht gestartet: %s", name, err)

    async def on_cleanup(app: web.Application) -> None:
        task = app.get("scheduler_task")
        if task:
            task.cancel()
        await app["http"].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app["started"] = time.time()
    return app

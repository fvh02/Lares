"""The HTTP API of Lares Core.

/api/v1/…        for the rooms – each room shows its own token (Authorization: Bearer …); the core checks every
                 request against the rights in its room.yaml.
/api/v1/admin/…  for the Atrium and the lares command – with the admin token (until the Atrium has its login).
"""

import asyncio
import base64
import json
import logging
import ipaddress
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from . import __version__, laufwerke, storage
from .accounts import AccountError, Accounts
from .bus import Bus, Busy, matches
from .manifest import EVENT, ManifestError, parse
from .notify import MAX_IMAGE, Notifier
from .oidc import PROVIDERS, configured
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
    room = core_of(request)["registry"].room_for_token(bearer(request))
    if room is None:
        raise web.HTTPUnauthorized(text=json.dumps({"fehler": "Token unbekannt"}), content_type="application/json")
    if not room.active:
        raise deny(f"{room.manifest.name} ist ausgeschaltet")
    return room


def core_of(request: web.Request) -> web.Application:
    """The core app – also when an admin route is served by the home gateway for the Atrium."""
    return request.app.get("core", request.app)


def require_admin(request: web.Request) -> None:
    """The admin token (lares command) or an Atrium session of an admin."""
    account = request.get("konto")
    if account and account["rolle"] == "admin":
        return
    if account:
        raise deny("Nur für Admins")
    if not secrets.compare_digest(bearer(request).encode(), core_of(request)["admin_token"].encode()):
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
    return web.json_response(core_of(request)["system"].public())


@routes.get("/api/v1/schluessel/{name}")
async def key(request: web.Request) -> web.Response:
    room, name = room_of(request), request.match_info["name"]
    if name not in room.manifest.rechte.schluessel:
        raise deny(f"{room.manifest.name} darf den Schlüssel {name} nicht lesen")
    value = core_of(request)["vault"].get(name)
    if not value:
        return fail(f"Schlüssel {name} ist nicht eingetragen", 404)
    log.info("Schlüssel %s an %s ausgegeben", name, room.id)
    return web.json_response({"name": name, "wert": value})


@routes.post("/api/v1/melden")
async def notify(request: web.Request) -> web.Response:
    room = room_of(request)
    if not room.manifest.rechte.benachrichtigen:
        raise deny(f"{room.manifest.name} darf keine Nachrichten schicken")
    notifier: Notifier = core_of(request)["notifier"]
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
    registry: Registry = core_of(request)["registry"]
    bus: Bus = core_of(request)["bus"]
    provider = next((r for r in registry.rooms.values() if r.active and r.manifest.rechte.ki_anbieter
                     and r.id != room.id and bus.connected(r.id)), None)
    if provider is None:
        return fail("Kein KI-Room verbunden – die KI läuft über einen Room mit dem Recht ki_anbieter "
                    "(z. B. den Assistenten)", 503)
    usage = core_of(request)["ai_usage"].setdefault(room.id, {"anfragen": 0, "zeichen": 0})
    usage["anfragen"] += 1
    usage["zeichen"] += len(question)
    storage.save(core_of(request)["core_dir"] / "ki.json", core_of(request)["ai_usage"])
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
    event = core_of(request)["bus"].publish(room.id, name, payload)
    return web.json_response({"nr": event["nr"]})


@routes.get("/api/v1/ereignisse")
async def recent_events(request: web.Request) -> web.Response:
    room = room_of(request)
    try:
        since = int(request.query.get("seit", "0"))
    except ValueError:
        return fail("seit: eine Nummer")
    rights = room.manifest.rechte.ereignisse_empfangen
    return web.json_response([e for e in core_of(request)["bus"].recent if e["nr"] > since and matches(e["name"], rights)])


@routes.get("/api/v1/strom")
async def stream(request: web.Request) -> web.StreamResponse:
    """The room's stream (server-sent events): events it subscribed to, schedules, commands and AI questions."""
    room = room_of(request)
    requested = [p for p in request.query.get("abo", "").split(",") if p]
    patterns = allowed_patterns(requested, room.manifest.rechte.ereignisse_empfangen)
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
    await response.prepare(request)
    bus: Bus = core_of(request)["bus"]
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
    if not core_of(request)["bus"].answer(room.id, str(data.get("id") or ""), result):
        return fail("Keine offene Anfrage mit dieser id", 404)
    return web.json_response({"ok": True})


@routes.get("/api/v1/befehle")
async def commands(request: web.Request) -> web.Response:
    room = room_of(request)
    if not room.manifest.rechte.befehle:
        raise deny(f"{room.manifest.name} darf keine Befehle anderer Rooms nutzen")
    bus: Bus = core_of(request)["bus"]
    return web.json_response([
        {"room": r.id, "name": r.manifest.name, "verbunden": bus.connected(r.id),
         "befehle": [vars(c) for c in r.manifest.befehle]}
        for r in core_of(request)["registry"].rooms.values() if r.active and r.manifest.befehle and r.id != room.id])


@routes.post("/api/v1/befehle/{room}/{name}")
async def run_command(request: web.Request) -> web.Response:
    room = room_of(request)
    if not room.manifest.rechte.befehle:
        raise deny(f"{room.manifest.name} darf keine Befehle anderer Rooms nutzen")
    target = core_of(request)["registry"].get(request.match_info["room"])
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
        result = await core_of(request)["bus"].request(target.id, {"typ": "befehl", "von": room.id, "name": name,
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
    bus: Bus = core_of(request)["bus"]
    out = []
    for room in core_of(request)["registry"].rooms.values():
        out.append({**room.info(), "verbunden": bus.connected(room.id),
                    "container": await core_of(request)["runtime"].status(room_container(room.id)),
                    "freigabe": core_of(request)["remote"].public_release(room.id)})
    return web.json_response(out)


@routes.post("/api/v1/admin/rooms")
async def admin_install(request: web.Request) -> web.Response:
    """Installs or updates a room from {room_yaml} or {url} (also a GitHub link). The rights must be confirmed:
    without "rechte_bestaetigt" the answer lists them (and returns the room.yaml it read)."""
    require_admin(request)
    data = await body_of(request)
    text = str(data.get("room_yaml") or "")
    if not text and data.get("url"):
        try:
            text = await fetch_room_yaml(core_of(request)["http"], str(data["url"]))
        except ValueError as err:
            return fail(str(err))
    try:
        manifest = parse(text)
    except ManifestError as err:
        return fail(str(err))
    registry: Registry = core_of(request)["registry"]
    old = registry.get(manifest.id)
    rights = manifest.rechte.summary()
    if not data.get("rechte_bestaetigt") and (old is None or old.manifest.rechte != manifest.rechte):
        return web.json_response({"bestaetigen": True, "room": manifest.id, "name": manifest.name,
                                  "version": manifest.version, "rechte": rights, "room_yaml": text}, status=409)
    room = registry.install(manifest)
    out = {**room.info(), "neu": old is None}
    if room.active:
        try:
            token = await start_room(core_of(request), room)
        except ContainerError as err:
            return fail(f"Installiert, aber nicht gestartet: {err}", 502)
        if isinstance(core_of(request)["runtime"], NoRuntime):
            out["token"] = token     # without a runtime the room is started by hand and needs its token
    log.info("%s %s %s", manifest.id, manifest.version, "installiert" if old is None else "aktualisiert")
    return web.json_response(out)


MAX_ROOM_YAML = 256 * 1024
GITHUB = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/(blob|tree)/([^/]+)(/.*)?)?/?$")


def raw_url(url: str) -> str:
    """A GitHub link (repository, folder or file) becomes the address of the raw room.yaml."""
    match = GITHUB.match(url.strip())
    if not match:
        return url.strip()
    owner, repo, kind, ref, path = match.groups()
    path = (path or "").strip("/")
    if kind != "blob" and not path.endswith((".yaml", ".yml")):
        path = f"{path}/room.yaml".lstrip("/")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref or 'HEAD'}/{path}"


async def fetch_room_yaml(http: aiohttp.ClientSession, url: str) -> str:
    """Loads a room.yaml from the internet – only over HTTPS and never from addresses inside the home network."""
    url = raw_url(url)
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("Bitte einen https-Link")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or 443)
    except OSError:
        raise ValueError(f"{parts.hostname} nicht gefunden") from None
    if any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise ValueError("Links ins Heimnetz sind nicht erlaubt")
    try:
        async with http.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 404:
                raise ValueError("Dort liegt keine room.yaml (404)")
            if resp.status >= 300:
                raise ValueError(f"Abruf fehlgeschlagen ({resp.status})")
            raw = await resp.content.read(MAX_ROOM_YAML + 1)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise ValueError("Link nicht erreichbar") from None
    if len(raw) > MAX_ROOM_YAML:
        raise ValueError("Datei zu groß für eine room.yaml")
    return raw.decode("utf-8", errors="replace")


def admin_room(request: web.Request) -> Room:
    require_admin(request)
    room = core_of(request)["registry"].get(request.match_info["room"])
    if room is None:
        raise web.HTTPNotFound(text=json.dumps({"fehler": "Room nicht installiert"}), content_type="application/json")
    return room


@routes.delete("/api/v1/admin/rooms/{room}")
async def admin_remove(request: web.Request) -> web.Response:
    room = admin_room(request)
    await core_of(request)["runtime"].remove(room_container(room.id))
    core_of(request)["registry"].remove(room.id, keep_data=request.query.get("daten") != "loeschen")
    core_of(request)["remote"].release(room.id, None)
    return web.json_response({"entfernt": room.id})


@routes.post("/api/v1/admin/rooms/{room}/{action:start|stop}")
async def admin_start_stop(request: web.Request) -> web.Response:
    room = admin_room(request)
    registry: Registry = core_of(request)["registry"]
    if request.match_info["action"] == "stop":
        registry.set_active(room.id, False)
        await core_of(request)["runtime"].stop(room_container(room.id))
        return web.json_response({"aktiv": False})
    registry.set_active(room.id, True)
    try:
        token = await start_room(core_of(request), room)
    except ContainerError as err:
        return fail(str(err), 502)
    out = {"aktiv": True}
    if isinstance(core_of(request)["runtime"], NoRuntime):
        out["token"] = token
    return web.json_response(out)


@routes.post("/api/v1/admin/rooms/{room}/token")
async def admin_token(request: web.Request) -> web.Response:
    """A new token for a room started by hand (development); the old one stops working."""
    room = admin_room(request)
    if not isinstance(core_of(request)["runtime"], NoRuntime):
        return fail("Nur ohne Container-Laufzeit – sonst bekommt der Room sein Token beim Start")
    return web.json_response({"token": core_of(request)["registry"].issue_token(room.id)})


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
        values = core_of(request)["registry"].set_settings(room.id, await body_of(request))
    except ManifestError as err:
        return fail(str(err))
    core_of(request)["bus"].tell(room.id, {"typ": "einstellungen", "werte": values})
    return web.json_response(values)


@routes.get("/api/v1/admin/rooms/{room}/logs")
async def admin_logs(request: web.Request) -> web.Response:
    room = admin_room(request)
    return web.Response(text=await core_of(request)["runtime"].logs(room_container(room.id)))


@routes.get("/api/v1/admin/schluessel")
async def admin_keys(request: web.Request) -> web.Response:
    require_admin(request)
    registry: Registry = core_of(request)["registry"]
    users: dict[str, list[str]] = {}
    for room in registry.rooms.values():
        for name in room.manifest.rechte.schluessel:
            users.setdefault(name, []).append(room.id)
    names = set(core_of(request)["vault"].names()) | set(users)
    return web.json_response([{"name": n, "eingetragen": bool(core_of(request)["vault"].get(n)),
                               "rooms": users.get(n, [])} for n in sorted(names)])


@routes.put("/api/v1/admin/schluessel/{name}")
async def admin_set_key(request: web.Request) -> web.Response:
    require_admin(request)
    data = await body_of(request)
    try:
        core_of(request)["vault"].set(request.match_info["name"], str(data.get("wert") or ""))
    except ValueError as err:
        return fail(str(err))
    service = SERVICE_KEYS.get(request.match_info["name"])
    if service and core_of(request)["remote"].data[service]["aktiv"]:
        try:
            await start_service(core_of(request), service)     # restart with the new key
        except (ContainerError, ValueError) as err:
            return fail(f"Gespeichert, aber {service} nicht neu gestartet: {err}", 502)
    return web.json_response({"gespeichert": True})


@routes.delete("/api/v1/admin/schluessel/{name}")
async def admin_delete_key(request: web.Request) -> web.Response:
    require_admin(request)
    core_of(request)["vault"].delete(request.match_info["name"])
    return web.json_response({"geloescht": True})


@routes.get("/api/v1/admin/system")
async def admin_system(request: web.Request) -> web.Response:
    require_admin(request)
    return web.json_response(core_of(request)["system"].data)


@routes.put("/api/v1/admin/system")
async def admin_set_system(request: web.Request) -> web.Response:
    require_admin(request)
    try:
        data = core_of(request)["system"].update(await body_of(request))
    except ValueError as err:
        return fail(str(err))
    core_of(request)["bus"].tell_all({"typ": "system", "werte": core_of(request)["system"].public()})
    return web.json_response(data)


@routes.post("/api/v1/admin/test/melden")
async def admin_test_notify(request: web.Request) -> web.Response:
    require_admin(request)
    try:
        await core_of(request)["notifier"].send("lares", "Lares: Testnachricht ✅", urgent=True)
    except RuntimeError as err:
        return fail(str(err), 503)
    return web.json_response({"gesendet": True})


@routes.get("/api/v1/admin/ki")
async def admin_ai_usage(request: web.Request) -> web.Response:
    require_admin(request)
    return web.json_response(core_of(request)["ai_usage"])


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
    remote: Remote = core_of(request)["remote"]
    vault: Vault = core_of(request)["vault"]
    return web.json_response({
        "tailscale": {**remote.data["tailscale"], "schluessel": bool(vault.get("tailscale_authkey")),
                      **await service_state(core_of(request), "tailscale")},
        "cloudflared": {**remote.data["cloudflared"], "schluessel": bool(vault.get("cloudflare_tunnel_token")),
                        "ziel": f"http://{remote.core_host}:{remote.public_port}",
                        **await service_state(core_of(request), "cloudflared")},
        "freigaben": {rid: remote.public_release(rid) for rid in remote.data["freigaben"]},
    })


@routes.put("/api/v1/admin/fernzugriff/{name:tailscale|cloudflared}")
async def admin_set_remote(request: web.Request) -> web.Response:
    """Saves the settings and starts or stops the service accordingly."""
    require_admin(request)
    name, remote = request.match_info["name"], core_of(request)["remote"]
    data = await body_of(request)
    try:
        settings = remote.set_tailscale(data) if name == "tailscale" else remote.set_cloudflared(data)
        if settings["aktiv"]:
            await start_service(core_of(request), name)
        else:
            await core_of(request)["runtime"].remove(service_container(name))
    except ValueError as err:
        return fail(str(err))
    except ContainerError as err:
        return fail(str(err), 502)
    return web.json_response({**settings, **await service_state(core_of(request), name)})


@routes.get("/api/v1/admin/fernzugriff/{name:tailscale|cloudflared}/logs")
async def admin_remote_logs(request: web.Request) -> web.Response:
    require_admin(request)
    return web.Response(text=await core_of(request)["runtime"].logs(service_container(request.match_info["name"])))


@routes.put("/api/v1/admin/rooms/{room}/freigabe")
async def admin_release(request: web.Request) -> web.Response:
    """Releases a room for the internet: {host?, zugangscode?}. Only rooms with oberflaeche.oeffentlich."""
    room = admin_room(request)
    if not room.manifest.port or not room.manifest.oeffentlich:
        return fail(f"{room.manifest.name} ist laut room.yaml nicht für das Internet gedacht (oberflaeche.oeffentlich)")
    try:
        return web.json_response(core_of(request)["remote"].release(room.id, await body_of(request)))
    except ValueError as err:
        return fail(str(err))


@routes.delete("/api/v1/admin/rooms/{room}/freigabe")
async def admin_withdraw(request: web.Request) -> web.Response:
    room = admin_room(request)
    core_of(request)["remote"].release(room.id, None)
    return web.json_response({"freigabe": None})


# -- accounts of the Atrium -----------------------------------------------------------------------
@routes.get("/api/v1/admin/konten")
async def admin_accounts(request: web.Request) -> web.Response:
    require_admin(request)
    return web.json_response(core_of(request)["accounts"].public())


@routes.post("/api/v1/admin/konten")
async def admin_create_account(request: web.Request) -> web.Response:
    require_admin(request)
    data = await body_of(request)
    try:
        core_of(request)["accounts"].create(str(data.get("name") or ""), str(data.get("passwort") or ""),
                                            str(data.get("rolle") or "bewohner"))
    except AccountError as err:
        return fail(str(err))
    return web.json_response(core_of(request)["accounts"].public())


@routes.put("/api/v1/admin/konten/{name}")
async def admin_change_account(request: web.Request) -> web.Response:
    """{rolle?, passwort?} – a new password ends the sessions of that account."""
    require_admin(request)
    accounts: Accounts = core_of(request)["accounts"]
    name = accounts.find(request.match_info["name"])
    if not name:
        return fail("Konto gibt es nicht", 404)
    data = await body_of(request)
    try:
        if data.get("rolle"):
            accounts.set_role(name, str(data["rolle"]))
        if data.get("passwort"):
            accounts.reset_password(name, str(data["passwort"]))
    except AccountError as err:
        return fail(str(err))
    return web.json_response(accounts.public())


@routes.delete("/api/v1/admin/konten/{name}")
async def admin_delete_account(request: web.Request) -> web.Response:
    require_admin(request)
    try:
        core_of(request)["accounts"].delete(request.match_info["name"])
    except AccountError as err:
        return fail(str(err))
    return web.json_response(core_of(request)["accounts"].public())


@routes.get("/api/v1/admin/anmeldung")
async def admin_login_providers(request: web.Request) -> web.Response:
    """Which logins with Apple and Google are set up, and which keys are missing."""
    require_admin(request)
    vault: Vault = core_of(request)["vault"]
    return web.json_response({p: {"name": c["name"], "eingerichtet": configured(vault, p),
                                  "fehlt": [k for k in c["keys"] if not vault.get(k)]} for p, c in PROVIDERS.items()})


# -- where the rooms live (Lares OS) --------------------------------------------------------------
def storage_choice_missing(app: web.Application) -> bool:
    return bool(app["lares_os"]) and laufwerke.load_choice(app["data_dir"]) is None


@routes.get("/api/v1/admin/speicher")
async def admin_storage(request: web.Request) -> web.Response:
    require_admin(request)
    app = core_of(request)
    if not app["lares_os"]:
        return web.json_response({"lares_os": False})
    choice = laufwerke.load_choice(app["data_dir"])
    base = laufwerke.base_for(choice, app["data_dir"])
    try:
        disks = [d.public() for d in await laufwerke.list_disks()]
    except (laufwerke.LaufwerkError, OSError, ValueError) as err:
        disks, problem = [], str(err)
    else:
        problem = ""
    error_file = laufwerke.RUN / "speicher-fehler"
    usage = shutil.disk_usage(base) if base.exists() else None
    move = app["speicher"].get("vorgang")
    return web.json_response({
        "lares_os": True, "gewaehlt": choice is not None, "ort": choice or {"ort": "usb"},
        "frei": usage.free if usage else None, "gesamt": usage.total if usage else None,
        "usb_frei": shutil.disk_usage(app["data_dir"]).free if app["data_dir"].exists() else None,
        "fehler": (error_file.read_text() if error_file.exists() else "") or problem,
        "laufwerke": disks, "vorgang": move.public() if move else None})


@routes.post("/api/v1/admin/speicher")
async def admin_set_storage(request: web.Request) -> web.Response:
    """{ziel: "usb" or "/dev/…", loeschen?: bool, bestaetigt?: "/dev/…"} – moves rooms and container images."""
    require_admin(request)
    app = core_of(request)
    if not app["lares_os"]:
        return fail("Nur auf Lares OS")
    move = app["speicher"].get("vorgang")
    if move and not (move.done or move.error):
        return fail("Ein Umzug läuft schon", 409)
    data = await body_of(request)
    target, wipe = str(data.get("ziel") or ""), bool(data.get("loeschen"))
    if target != "usb":
        disks = {d.path: d for d in await laufwerke.list_disks()}
        disk = disks.get(target)
        if disk is None or not disk.public()["geeignet"]:
            return fail("Dieses Laufwerk geht nicht (nicht gefunden, der Lares-Stick selbst oder kleiner als 16 GB)")
        if (wipe or not disk.lares_partition) and data.get("bestaetigt") != target:
            return fail(f"Bitte bestätigen, dass alle Daten auf {disk.model} gelöscht werden", 409)
    elif laufwerke.load_choice(app["data_dir"]) is None:
        storage.save(laufwerke.config_file(app["data_dir"]), {"ort": "usb"})     # first choice: nothing to move
        return web.json_response({"ort": {"ort": "usb"}, "fertig": True})

    async def start_all(base: Path) -> None:
        await start_everything(app)      # the folders are bound to the same places as before: nothing to change

    move = laufwerke.Move(app["data_dir"], target, wipe, lambda: stop_everything(app), start_all)
    app["speicher"]["vorgang"] = move
    app["speicher"]["task"] = asyncio.create_task(move())
    return web.json_response(move.public(), status=202)


@routes.get("/api/v1/status")
async def status(request: web.Request) -> web.Response:
    return web.json_response({"lares": __version__, "laufzeit": core_of(request)["runtime"].name})


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


def make_runtime(kind: str, core_url: str, host_data_dir: Path, local_data_dir: Path,
                 host_rooms_dir: Path | None = None, core_on_host: bool = False):
    if kind in ("docker", "podman"):
        return ContainerRuntime(kind, core_url, host_data_dir, local_data_dir, host_rooms_dir=host_rooms_dir,
                                core_on_host=core_on_host)
    return NoRuntime()


CONSOLE = Path(os.environ.get("LARES_CONSOLE_ISSUE", "/run/issue.d/50-lares.issue"))


async def console_info(app: web.Application) -> None:
    """On Lares OS: the screen of the device shows where the Atrium is – and the setup code before the first login."""
    lines = ["", f"  Lares {__version__}", "",
             "  Atrium:  http://\\4   oder   http://lares.local", ""]
    if app["accounts"].needs_setup:
        lines += [f"  Einrichtungscode:  {app['accounts'].setup_code()}", ""]
    try:
        CONSOLE.parent.mkdir(parents=True, exist_ok=True)
        CONSOLE.write_text("\n".join(lines) + "\n")
        proc = await asyncio.create_subprocess_exec("agetty", "--reload", stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    except OSError:
        pass


async def start_everything(app: web.Application) -> None:
    """Starts the active rooms (each with a fresh token) and the switched-on services."""
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


async def stop_everything(app: web.Application) -> None:
    for room in list(app["registry"].rooms.values()):
        await app["runtime"].remove(room_container(room.id))
    for name in SERVICES:
        await app["runtime"].remove(service_container(name))


def make_app(data_dir: Path, runtime=None, background: bool = True, core_host: str = "lares-core",
             home_port: int = 8000, public_port: int = 8001, rooms_dir: Path | None = None,
             lares_os: bool = False) -> web.Application:
    core_dir, rooms_dir = data_dir / "core", rooms_dir or data_dir / "rooms"
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
    app["accounts"] = Accounts(core_dir)
    app["atrium_state"] = {}        # passkey challenges and Apple/Google logins in progress
    app["data_dir"], app["lares_os"] = data_dir, lares_os
    app["speicher"] = {}            # a move of the rooms in progress
    app.add_routes(routes)

    async def on_startup(app: web.Application) -> None:
        app["http"] = aiohttp.ClientSession()
        app["notifier"] = Notifier(core_dir, app["vault"], app["system"], app["http"])
        app["scheduler"] = Scheduler(core_dir, app["registry"], app["system"], app["bus"], app["notifier"])
        if app["accounts"].needs_setup:
            log.warning("Lares ist noch nicht eingerichtet – Einrichtungscode für das Atrium: %s",
                        app["accounts"].setup_code())
        if lares_os:
            await console_info(app)
        if background:
            app["scheduler_task"] = asyncio.create_task(app["scheduler"].run())
            if not isinstance(app["runtime"], NoRuntime):
                await start_everything(app)

    async def on_cleanup(app: web.Application) -> None:
        task = app.get("scheduler_task")
        if task:
            task.cancel()
        await app["http"].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app["started"] = time.time()
    return app

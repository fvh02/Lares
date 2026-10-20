"""The gateways to the surfaces of the rooms.

home    (port 8000) – home network and Tailscale: the Atrium, and every active room with a surface at /r/<room>/,
        both only after the login
public  (port 8001) – only for cloudflared, never published on the host: only rooms released for the internet,
        by their own address (spiele.example.de) or at /r/<room>/, optionally behind an access code

Rooms see X-Lares-Prefix (and X-Ingress-Path, like Home Assistant ingress), X-Lares-Zugang (heim or extern) and,
at home, X-Lares-Benutzer and X-Lares-Rolle.
"""

import asyncio
import hmac
import html
import logging
from urllib.parse import quote

import aiohttp
from aiohttp import WSMsgType, web

from . import atrium
from .notify import RateLimit
from .remote import check_code

log = logging.getLogger("lares.gateway")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length"}
OWN = ("x-lares-", "x-ingress-path", "x-forwarded-", "x-real-ip")
CODE_PATH = "/_lares/zugang"
COOKIE = "lares_zugang_"


def page(title: str, body: str, status: int = 200) -> web.Response:
    return web.Response(status=status, content_type="text/html", text=f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{color-scheme:light dark;--bg:#f6f4ef;--fg:#1d1b18;--card:#fff;--line:#ddd6c8;--accent:#8a5a2b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16140f;--fg:#ece6da;--card:#221f19;--line:#3a352b;--accent:#d9a066}}}}
body{{margin:0;font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:640px;margin:0 auto;padding:32px 16px}}h1{{font-weight:600;margin:0 0 24px}}
a.room{{display:block;padding:16px;margin:0 0 12px;background:var(--card);border:1px solid var(--line);
border-radius:12px;color:inherit;text-decoration:none}}a.room small{{display:block;opacity:.7}}
input,button{{font:inherit;padding:10px 12px;border-radius:8px;border:1px solid var(--line);width:100%;
box-sizing:border-box;margin:0 0 12px;background:var(--card);color:var(--fg)}}
button{{background:var(--accent);color:#fff;border:0;cursor:pointer}}.fehler{{color:#c0392b}}
</style></head><body><main>{body}</main></body></html>""")


class Gateway:
    def __init__(self, core_app: web.Application, public: bool):
        self.core, self.public = core_app, public
        self.attempts = RateLimit(10)

    def app(self) -> web.Application:
        app = web.Application(client_max_size=1024 ** 3)
        if not self.public:
            atrium.install(app, self.core)
        app.router.add_route("*", "/{path:.*}", self.handle)
        return app

    # -- which room ---------------------------------------------------------------------------------
    def _route(self, request: web.Request):
        """Returns (room, prefix, rest) or None."""
        registry, remote = self.core["registry"], self.core["remote"]
        path = request.path
        if self.public:
            room_id = remote.room_for_host(request.host or "")
            if room_id:
                return registry.get(room_id), "", path
        if path.startswith("/r/"):
            room_id, _, rest = path[3:].partition("/")
            return registry.get(room_id), f"/r/{room_id}", "/" + rest
        return None

    def _allowed(self, room) -> bool:
        if room is None or not room.active or not room.manifest.port:
            return False
        if self.public:
            return room.manifest.oeffentlich and self.core["remote"].released(room.id) is not None
        return True

    # -- requests -----------------------------------------------------------------------------------
    async def handle(self, request: web.Request) -> web.StreamResponse:
        route = self._route(request)
        if route is None:
            return page("Nicht gefunden", "<h1>Nicht gefunden</h1>", 404)
        room, prefix, rest = route
        if not self._allowed(room):
            return page("Nicht gefunden", "<h1>Nicht gefunden</h1>", 404)
        if not self.public and request.get("konto") is None:
            if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
                raise web.HTTPFound("/?weiter=" + quote(request.path_qs, safe=""))
            return web.json_response({"fehler": "Bitte im Atrium anmelden"}, status=401)
        if prefix and request.path == prefix:
            raise web.HTTPFound(prefix + "/" + (f"?{request.query_string}" if request.query_string else ""))
        if self.public:
            gate = await self.gate(request, room, prefix, rest)
            if gate is not None:
                return gate
        return await self.proxy(request, room, prefix, rest)

    async def gate(self, request: web.Request, room, prefix: str, rest: str) -> web.StreamResponse | None:
        """The access code of a released room; None when the request may pass."""
        remote = self.core["remote"]
        if not (remote.released(room.id) or {}).get("code"):
            return None
        expected = remote.cookie_for(room.id)
        if hmac.compare_digest(request.cookies.get(COOKIE + room.id, ""), expected):
            return None
        action = f"{prefix}{CODE_PATH}"
        target = request.query.get("weiter", "") if rest == CODE_PATH else request.path_qs
        error = ""
        if rest == CODE_PATH and request.method == "POST":
            who = request.headers.get("CF-Connecting-IP") or request.remote or "?"
            form = await request.post()
            if not self.attempts.ok(who):
                error = "Zu viele Versuche – bitte eine Minute warten."
            elif check_code(str(form.get("code", "")), remote.released(room.id)["code"]):
                weiter = str(form.get("weiter") or "")
                if not weiter.startswith("/") or weiter.startswith("//"):
                    weiter = prefix + "/"
                response = web.HTTPSeeOther(weiter)
                response.set_cookie(COOKIE + room.id, expected, max_age=90 * 86400, httponly=True, secure=True,
                                    samesite="Lax", path=prefix or "/")
                log.info("Zugang zu %s freigeschaltet", room.id)
                raise response
            else:
                log.warning("Falscher Zugangscode für %s", room.id)
                error = "Der Code stimmt nicht."
            target = str(form.get("weiter") or "")
        if request.method not in ("GET", "HEAD", "POST") or request.headers.get("Upgrade"):
            return web.json_response({"fehler": "Zugangscode nötig"}, status=401)
        name = html.escape(room.manifest.name)
        return page(name, f"""<h1>{name}</h1><form method="post" action="{html.escape(action)}">
<input type="hidden" name="weiter" value="{html.escape(target)}">
<input name="code" type="password" placeholder="Zugangscode" autofocus autocomplete="current-password">
{f'<p class="fehler">{html.escape(error)}</p>' if error else ''}<button>Weiter</button></form>""", 401)

    # -- proxy --------------------------------------------------------------------------------------
    def _headers(self, request: web.Request, prefix: str) -> dict:
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP and not k.lower().startswith(OWN)}
        client = request.headers.get("CF-Connecting-IP") if self.public else None
        headers.update({
            "X-Forwarded-For": client or request.remote or "",
            "X-Forwarded-Proto": request.headers.get("X-Forwarded-Proto", request.scheme) if self.public
            else request.scheme,
            "X-Forwarded-Host": request.host or "",
            "X-Lares-Prefix": prefix, "X-Ingress-Path": prefix,
            "X-Lares-Zugang": "extern" if self.public else "heim",
            **atrium.account_headers(request.get("konto")),
        })
        return headers

    async def proxy(self, request: web.Request, room, prefix: str, rest: str) -> web.StreamResponse:
        address = self.core["runtime"].address(room.id, room.manifest.port)
        query = f"?{request.query_string}" if request.query_string else ""
        headers = self._headers(request, prefix)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.websocket(request, f"ws://{address}{quote(rest)}{query}", headers)
        session: aiohttp.ClientSession = self.core["http"]
        try:
            upstream = await session.request(
                request.method, f"http://{address}{quote(rest)}{query}", headers=headers,
                data=request.content if request.body_exists else None, allow_redirects=False,
                auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return page("Nicht erreichbar", f"<h1>{html.escape(room.manifest.name)} ist gerade nicht erreichbar</h1>",
                        502)
        async with upstream:
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for key, value in upstream.headers.items():
                if key.lower() in HOP and key.lower() != "content-length":
                    continue
                if key.lower() == "location" and prefix and value.startswith("/") and not value.startswith("//"):
                    value = prefix + value
                response.headers.add(key, value)
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
            await response.write_eof()
            return response

    async def websocket(self, request: web.Request, url: str, headers: dict) -> web.StreamResponse:
        protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        headers = {k: v for k, v in headers.items() if not k.lower().startswith("sec-websocket")}
        session: aiohttp.ClientSession = self.core["http"]
        try:
            upstream = await session.ws_connect(url, headers=headers, protocols=protocols, autoping=False,
                                                max_msg_size=0, timeout=aiohttp.ClientWSTimeout(ws_close=10))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return web.Response(status=502, text="Room nicht erreichbar")
        client = web.WebSocketResponse(protocols=[upstream.protocol] if upstream.protocol else (), autoping=False,
                                       max_msg_size=0)
        await client.prepare(request)

        async def pump(source, target):
            async for msg in source:
                if msg.type == WSMsgType.TEXT:
                    await target.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await target.send_bytes(msg.data)
                elif msg.type == WSMsgType.PING:
                    await target.ping(msg.data)
                elif msg.type == WSMsgType.PONG:
                    await target.pong(msg.data)
                else:
                    break

        tasks = [asyncio.create_task(pump(client, upstream)), asyncio.create_task(pump(upstream, client))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await upstream.close()
            await client.close()
        return client

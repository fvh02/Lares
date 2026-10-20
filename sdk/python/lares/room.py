"""The SDK for rooms: everything a room needs from Lares Core, without its own server.

    from lares import room

    key = room.schluessel("tvmaze_key")
    room.melden("📺 Neu: Severance – S02E05")

    @room.zeitplan("pruefen")            # room.yaml: zeitplaene: [{name: pruefen, alle: 30m}]
    def pruefen(): ...

    @room.befehl("demnaechst")           # room.yaml: befehle: [{name: demnaechst, …}]
    def demnaechst(): return "…"

    room.laufen()                        # keeps the stream to the core open; blocks

The core sets LARES_URL and LARES_TOKEN when it starts the room; for development (`lares room dev`) they come from
the environment as well. Only the standard library is used.
"""

import base64
import fnmatch
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("lares")

daten = Path(os.environ.get("LARES_DATEN", "/data"))


class LaresFehler(RuntimeError):
    """The core refused or could not do it; `status` is the HTTP status (0: core not reachable)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _url() -> str:
    return os.environ.get("LARES_URL", "http://lares-core:8080/api/v1").rstrip("/")


def _token() -> str:
    token = os.environ.get("LARES_TOKEN", "")
    if not token:
        raise LaresFehler("LARES_TOKEN fehlt – der Room läuft nicht unter Lares")
    return token


def _call(method: str, path: str, body: dict | None = None, timeout: float = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{_url()}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as err:
        try:
            message = json.loads(err.read()).get("fehler") or str(err)
        except ValueError:
            message = str(err)
        raise LaresFehler(message, err.code) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
        raise LaresFehler(f"Lares Core nicht erreichbar: {err}") from None


# -- reading --------------------------------------------------------------------------------------
def ich() -> dict:
    """This room as the core sees it: name, version, rights."""
    return _call("GET", "/ich")


def einstellungen() -> dict:
    return _call("GET", "/einstellungen")


def system() -> dict:
    """Home, time zone, language and residents."""
    return _call("GET", "/system")


def schluessel(name: str) -> str | None:
    """A key from the vault (only those named in room.yaml); None if it is not entered yet."""
    try:
        return _call("GET", f"/schluessel/{urllib.parse.quote(name)}")["wert"]
    except LaresFehler as err:
        if err.status == 404:
            return None
        raise


# -- doing ----------------------------------------------------------------------------------------
def melden(text: str, bild: bytes | str | Path | None = None, an: str | None = None, dringend: bool = False) -> dict:
    """A message to the first resident (or the named one). Outside urgent cases it waits for the end of the quiet
    hours; the answer is {"gesendet": True} or {"wartet": True}."""
    body: dict = {"text": text, "dringend": dringend}
    if an:
        body["an"] = an
    if bild is not None:
        if isinstance(bild, (str, Path)):
            body["dateiname"] = Path(bild).name
            bild = Path(bild).read_bytes()
        body["bild_b64"] = base64.b64encode(bild).decode()
    return _call("POST", "/melden", body)


def ki(frage: str, schema: dict | None = None, timeout: float = 900):
    """Asks the AI (through the room that offers it). With a JSON schema the answer is an object."""
    return _call("POST", "/ki", {"frage": frage, "schema": schema}, timeout=timeout)["antwort"]


def standort(person: str | None = None) -> dict:
    """Position of a resident: lat, lon, genauigkeit, quelle (needs the Device Core)."""
    return _call("GET", "/standort" + (f"?person={urllib.parse.quote(person)}" if person else ""))


class Geraet:
    def __init__(self, entity_id: str):
        self.id = entity_id

    def zustand(self) -> dict:
        return _call("GET", f"/geraete/{self.id}")

    def dienst(self, name: str, **daten) -> dict:
        return _call("POST", f"/geraete/{self.id}/{name}", daten)

    def an(self, **daten) -> dict:
        return self.dienst("turn_on", **daten)

    def aus(self) -> dict:
        return self.dienst("turn_off")


def geraet(entity_id: str) -> Geraet:
    return Geraet(entity_id)


def ereignis(name: str, inhalt: dict | None = None) -> int:
    """Sends an event (only names allowed in room.yaml); returns its number."""
    return _call("POST", "/ereignisse", {"name": name, "daten": inhalt or {}})["nr"]


def befehle() -> list[dict]:
    """The commands of the other rooms (needs the right befehle)."""
    return _call("GET", "/befehle")


def ausfuehren(room_id: str, befehl_name: str, /, **argumente):
    """Runs a command of another room and returns its result."""
    return _call("POST", f"/befehle/{room_id}/{befehl_name}", {"argumente": argumente}, timeout=130)["ergebnis"]


# -- the stream -----------------------------------------------------------------------------------
_handlers: dict[str, dict] = {"ereignis": {}, "zeitplan": {}, "befehl": {}}
_special: dict[str, object] = {}
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="lares")
_stop = threading.Event()


def _register(kind: str, name: str, funktion):
    if funktion is not None:
        _handlers[kind][name] = funktion
        return funktion

    def decorator(f):
        _handlers[kind][name] = f
        return f
    return decorator


def abonnieren(muster: str, funktion=None):
    """Calls funktion(name, daten, von) for every event matching muster (e.g. "person.*")."""
    return _register("ereignis", muster, funktion)


def zeitplan(name: str, funktion=None):
    """Calls funktion() when the schedule of that name in room.yaml is due."""
    return _register("zeitplan", name, funktion)


def befehl(name: str, funktion=None):
    """Answers the command of that name in room.yaml: funktion(**argumente) returns the result (JSON)."""
    return _register("befehl", name, funktion)


def ki_anbieten(funktion):
    """For the room with the right ki_anbieter: funktion(frage, schema, von) returns the answer."""
    _special["ki"] = funktion
    return funktion


def bei_einstellungen(funktion):
    """Calls funktion(werte) when the settings were changed in the Atrium."""
    _special["einstellungen"] = funktion
    return funktion


def _answer(request_id: str, work) -> None:
    try:
        body = {"id": request_id, "ergebnis": work()}
    except Exception as err:  # noqa: BLE001 – the error goes back to the asking room
        log.exception("Anfrage fehlgeschlagen")
        body = {"id": request_id, "fehler": str(err) or type(err).__name__}
    try:
        _call("POST", "/strom/antwort", body)
    except LaresFehler as err:
        log.warning("Antwort nicht zugestellt: %s", err)


def _safe(work) -> None:
    try:
        work()
    except Exception:  # noqa: BLE001 – one bad handler must not end the stream
        log.exception("Fehler im Room")


def _fails(message: str):
    def work():
        raise LaresFehler(message)
    return work


def _dispatch(msg: dict) -> None:
    kind = msg.get("typ")
    if kind == "ereignis":
        for pattern, f in list(_handlers["ereignis"].items()):
            if fnmatch.fnmatchcase(msg["name"], pattern):
                _pool.submit(_safe, lambda f=f: f(msg["name"], msg.get("daten") or {}, msg.get("von")))
    elif kind == "zeitplan":
        f = _handlers["zeitplan"].get(msg["name"])
        if f:
            _pool.submit(_safe, f)
        else:
            log.warning("Kein Handler für Zeitplan %s", msg["name"])
    elif kind == "befehl":
        f = _handlers["befehl"].get(msg["name"])
        work = (lambda: f(**(msg.get("argumente") or {}))) if f else \
            _fails(f"Befehl {msg['name']} ist nicht umgesetzt")
        _pool.submit(_answer, msg["id"], work)
    elif kind == "ki":
        f = _special.get("ki")
        work = (lambda: f(msg["frage"], msg.get("schema"), msg.get("von"))) if f else \
            _fails("Dieser Room beantwortet keine KI-Fragen")
        _pool.submit(_answer, msg["id"], work)
    elif kind == "einstellungen" and _special.get("einstellungen"):
        _pool.submit(_safe, lambda: _special["einstellungen"](msg.get("werte") or {}))


def _stream_once(on_hello=None) -> None:
    query = urllib.parse.urlencode({"abo": ",".join(_handlers["ereignis"])})
    req = urllib.request.Request(f"{_url()}/strom?{query}", headers={"Authorization": f"Bearer {_token()}",
                                                                      "Accept": "text/event-stream"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        for raw in resp:
            if _stop.is_set():
                return
            line = raw.decode().rstrip("\n")
            if not line.startswith("data: "):
                continue
            msg = json.loads(line[6:])
            if msg.get("typ") == "hallo":
                if msg.get("abgelehnt"):
                    log.warning("Abos ohne Recht (room.yaml → rechte.ereignisse.empfangen): %s",
                                ", ".join(msg["abgelehnt"]))
                if on_hello:
                    on_hello(msg)
                continue
            _dispatch(msg)


def laufen(on_hello=None) -> None:
    """Keeps the stream to the core open and calls the handlers; reconnects by itself. Blocks until stoppen()."""
    _stop.clear()
    wait = 1
    while not _stop.is_set():
        try:
            _stream_once(on_hello)
            wait = 1
        except urllib.error.HTTPError as err:
            if err.code == 401:
                log.error("Lares Core kennt das Token nicht mehr – Neustart nötig")
            else:
                log.warning("Strom: %s", err)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as err:
            log.warning("Strom unterbrochen: %s", err)
        if not _stop.is_set():
            _stop.wait(wait)
            wait = min(wait * 2, 30)


def stoppen() -> None:
    _stop.set()

"""The stream between the core and the rooms: events, schedules, commands and AI questions.

A room keeps one connection open (GET /api/v1/strom, server-sent events). Everything the core has for the room
arrives there, so a room needs no server of its own; answers to commands go back with POST /api/v1/strom/antwort.
"""

import asyncio
import fnmatch
import itertools
import secrets
import time
from collections import deque

RECENT = 200


def matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


class Busy(RuntimeError):
    """The room is not connected or did not answer in time."""


class Connection:
    def __init__(self, room_id: str, patterns: list[str]):
        self.room_id, self.patterns = room_id, patterns
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    def put(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            pass    # the room does not read its stream; it loses messages rather than the core memory


class Bus:
    def __init__(self):
        self.connections: dict[str, list[Connection]] = {}
        self.pending: dict[str, tuple[str, asyncio.Future]] = {}
        self.recent: deque = deque(maxlen=RECENT)
        self.seq = itertools.count(1)

    # -- connections --------------------------------------------------------------------------------
    def connect(self, room_id: str, patterns: list[str]) -> Connection:
        conn = Connection(room_id, patterns)
        self.connections.setdefault(room_id, []).append(conn)
        return conn

    def disconnect(self, conn: Connection) -> None:
        conns = self.connections.get(conn.room_id, [])
        if conn in conns:
            conns.remove(conn)
        if not conns:
            self.connections.pop(conn.room_id, None)

    def connected(self, room_id: str) -> bool:
        return bool(self.connections.get(room_id))

    def _to_room(self, room_id: str, message: dict) -> None:
        conns = self.connections.get(room_id)
        if not conns:
            raise Busy("Room ist nicht verbunden")
        conns[-1].put(message)     # the newest connection of the room; older ones are about to close

    def tell(self, room_id: str, message: dict) -> None:
        """Best effort: a note to one room (e.g. its settings changed)."""
        for conn in self.connections.get(room_id, [])[-1:]:
            conn.put(message)

    def tell_all(self, message: dict) -> None:
        for room_id in list(self.connections):
            self.tell(room_id, message)

    # -- events -------------------------------------------------------------------------------------
    def publish(self, sender: str, name: str, data: dict) -> dict:
        """Delivers to every connection that subscribed to the event (rights were checked on connect)."""
        event = {"typ": "ereignis", "nr": next(self.seq), "name": name, "von": sender, "daten": data,
                 "zeit": time.time()}
        self.recent.append(event)
        for conns in self.connections.values():
            for conn in conns:
                if matches(name, conn.patterns):
                    conn.put(event)
        return event

    # -- schedules ----------------------------------------------------------------------------------
    def wake(self, room_id: str, schedule: str) -> bool:
        try:
            self._to_room(room_id, {"typ": "zeitplan", "name": schedule, "zeit": time.time()})
            return True
        except Busy:
            return False

    # -- requests with an answer (commands, AI) -----------------------------------------------------
    async def request(self, room_id: str, message: dict, timeout: float) -> dict:
        """Sends a request into the room's stream and waits for its answer; only that room may answer."""
        request_id = secrets.token_urlsafe(12)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = (room_id, future)
        try:
            self._to_room(room_id, {**message, "id": request_id})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise Busy("Room hat nicht rechtzeitig geantwortet") from None
        finally:
            self.pending.pop(request_id, None)

    def answer(self, room_id: str, request_id: str, result: dict) -> bool:
        owner, future = self.pending.get(request_id, (None, None))
        if future is None or future.done() or owner != room_id:
            return False
        future.set_result(result)
        return True

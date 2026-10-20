"""Schedules from room.yaml: the core wakes the room through its stream ("every 30 minutes", "at 17:00")."""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from . import storage
from .manifest import Schedule

log = logging.getLogger("lares.scheduler")
TICK = 15


def due(schedule: Schedule, last: float, now: datetime) -> bool:
    if schedule.alle is not None:
        return now.timestamp() - last >= schedule.alle
    hour, minute = map(int, schedule.um.split(":"))
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= today and last < today.timestamp()


class Scheduler:
    def __init__(self, core_dir: Path, registry, system, bus, notifier):
        self.registry, self.system, self.bus, self.notifier = registry, system, bus, notifier
        self.file = core_dir / "schedules.json"
        self.last: dict[str, float] = storage.load(self.file, {})

    def tick(self) -> list[str]:
        """Wakes every connected room whose schedule is due; returns what was started."""
        now, started = self.system.now(), []
        for room in list(self.registry.rooms.values()):
            if not room.active or not self.bus.connected(room.id):
                continue
            for schedule in room.manifest.zeitplaene:
                key = f"{room.id}/{schedule.name}"
                if due(schedule, self.last.get(key, 0), now) and self.bus.wake(room.id, schedule.name):
                    self.last[key] = time.time()
                    started.append(key)
        if started:
            storage.save(self.file, self.last)
        return started

    async def run(self) -> None:
        while True:
            try:
                self.tick()
                await self.notifier.flush()
            except Exception:   # noqa: BLE001 – the loop must survive a bad room or a broken notifier
                log.exception("Zeitplan-Durchlauf fehlgeschlagen")
            await asyncio.sleep(TICK)

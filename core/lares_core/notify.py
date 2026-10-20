"""Notifications to the residents – today iMessage through Inkbox; held back during quiet hours unless urgent."""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import aiohttp

from . import storage

log = logging.getLogger("lares.notify")
INKBOX = os.environ.get("INKBOX_IMESSAGE_URL", "https://inkbox.ai/api/v1/imessage")
MAX_IMAGE = 8 * 1024 * 1024


def inkbox_error(status: int, body: str) -> str:
    """A message the person can act on, for an error answer from Inkbox."""
    try:
        detail = json.loads(body)
        detail = detail.get("detail") or detail.get("error") or detail.get("message") or ""
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)
    except (ValueError, AttributeError):
        detail = ""
    hint = {
        401: "Inkbox lehnt den API-Key ab – er ist falsch, abgelaufen oder gelöscht. Auf inkbox.ai → Einstellungen → "
             "API-Keys einen Key kopieren und im Atrium unter Schlüssel als inkbox_api_key eintragen",
        403: "Inkbox erlaubt das Senden an diese Nummer nicht (Kontaktregel oder Key ohne Rechte für die Identität)",
        404: "Für diese Nummer gibt es bei Inkbox noch keine iMessage-Verbindung – schreib der Inkbox-Identität "
             "einmal von deinem Handy aus",
        409: "Die iMessage-Verbindung ist noch nicht bereit – schreib der Inkbox-Identität einmal von deinem Handy aus",
        429: "Inkbox: Tageslimit für Nachrichten erreicht",
    }.get(status, f"Inkbox meldet Fehler {status}")
    return f"{hint} (Fehler {status}{': ' + str(detail)[:160] if detail else ''})"


class RateLimit:
    def __init__(self, per_minute: int):
        self.per_minute, self.hits = per_minute, {}

    def ok(self, who: str) -> bool:
        now = time.time()
        hits = [t for t in self.hits.get(who, []) if now - t < 60]
        if len(hits) >= self.per_minute:
            self.hits[who] = hits
            return False
        self.hits[who] = hits + [now]
        return True


class Notifier:
    def __init__(self, core_dir: Path, vault, system, http_session):
        self.vault, self.system, self.http = vault, system, http_session
        self.outbox_file = core_dir / "outbox.json"
        self.outbox: list[dict] = storage.load(self.outbox_file, [])
        self.limit = RateLimit(20)

    async def send(self, room_id: str, text: str, to: str | None = None, urgent: bool = False,
                   image: bytes | None = None, filename: str = "bild.jpg") -> dict:
        """Returns {"gesendet": True} or {"wartet": True} (quiet hours); raises RuntimeError with a message."""
        phone = self.system.phone_of(to)
        if not phone:
            raise RuntimeError(f"Für {to or 'den ersten Bewohner'} ist keine Handynummer eingetragen")
        text = text[:4000]
        if not urgent and self.system.quiet():
            if image:
                text += "\n(Bild in der Ruhezeit nicht gesendet)"
            self.outbox.append({"room": room_id, "text": text, "an": to, "zeit": int(time.time())})
            self.outbox = self.outbox[-200:]
            storage.save(self.outbox_file, self.outbox)
            return {"wartet": True}
        await self._imessage(phone, text, image, filename)
        return {"gesendet": True}

    async def flush(self) -> None:
        """Sends what waited for the end of the quiet hours."""
        if not self.outbox or self.system.quiet():
            return
        waiting, self.outbox = self.outbox, []
        storage.save(self.outbox_file, self.outbox)
        for msg in waiting:
            try:
                await self._imessage(self.system.phone_of(msg.get("an")), msg["text"])
            except RuntimeError as err:
                log.warning("Wartende Nachricht von %s nicht gesendet: %s", msg["room"], err)

    async def _imessage(self, phone: str, text: str, image: bytes | None = None, filename: str = "bild.jpg") -> None:
        key = self.vault.get("inkbox_api_key")
        if not key or not phone:
            raise RuntimeError("iMessage ist nicht eingerichtet (Schlüssel inkbox_api_key und Handynummer)")
        headers = {"X-API-Key": key}
        payload: dict = {"to": phone, "text": text}
        try:
            if image:
                form = aiohttp.FormData()
                ctype = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
                form.add_field("file", image, filename=filename, content_type=ctype)
                async with self.http.post(f"{INKBOX}/media", data=form, headers=headers,
                                          timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status < 300:
                        payload["media_urls"] = [(await resp.json(content_type=None))["media_url"]]
                    else:
                        log.warning("Bild-Upload fehlgeschlagen: %s", resp.status)
            async with self.http.post(f"{INKBOX}/messages", json=payload, headers=headers,
                                      timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status >= 300:
                    body = (await resp.text())[:300]
                    log.warning("iMessage fehlgeschlagen: %s", resp.status)
                    raise RuntimeError(inkbox_error(resp.status, body))
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as err:
            log.warning("iMessage fehlgeschlagen: %s", err)
            raise RuntimeError("Inkbox nicht erreichbar") from None

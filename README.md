# Lares

Ein eigenes, schlankes Betriebssystem für den Heimserver. Ein Kern verwaltet Schlüssel, Benachrichtigungen,
KI, Standort und Geräte – darauf laufen **Rooms**, kleine Apps, die man im **Atrium** öffnet und aus dem
**Room Store** installiert. Geräte funktionieren mit den Integrationen von Home Assistant über den
**Device Core**, ohne dass Home Assistant sichtbar ist.

| Begriff | Bedeutung |
|---|---|
| **Lares** | das Betriebssystem |
| **Lares Core** | der zentrale Dienst (Rooms, Schlüssel, Rechte, Systemdienste) |
| **Atrium** | das Home-Menü mit Anmeldung und den Kacheln aller Rooms |
| **Rooms** | die Apps |
| **Room Store** | der Katalog für Rooms |
| **Device Core** | Geräte-Motor mit den Home-Assistant-Integrationen, unsichtbar |

Konzept: [docs/KONZEPT.md](docs/KONZEPT.md)

## Inhalt

| Ordner | Was |
|---|---|
| [`core/`](core/README.md) | **Lares Core** – der zentrale Dienst (Version 0.5) |
| [`sdk/python/`](sdk/python/README.md) | das SDK für Rooms: `from lares import room` |
| [`examples/hallo/`](examples/hallo) | ein Beispiel-Room |

Status: Etappe 1 begonnen – Room-Format, Tokens und Rechte, Tresor, Benachrichtigungen, Ereignisse, Zeitpläne,
Befehle, KI-Weiterleitung, SDK, Fernzugriff mit Tailscale und Cloudflare Tunnel sowie das Atrium mit Anmeldung
(Passwort, Passkeys, Apple, Google) laufen. Room Store, Device Core und Lares-Image folgen.

# Lares

Ein eigenes, schlankes Betriebssystem für den Heimserver. Ein Kern verwaltet Schlüssel, Benachrichtigungen,
KI, Standort und Geräte – darauf laufen **Rooms**, kleine Apps, die man im **Atrium** öffnet und aus dem
**Room Store** installiert. Geräte funktionieren mit den Integrationen von Home Assistant über den
**Device Core**, ohne dass Home Assistant sichtbar ist.

| Begriff | Bedeutung |
|---|---|
| **Lares** | das Betriebssystem |
| **Lares-Kern** | der zentrale Dienst (Rooms, Schlüssel, Rechte, Systemdienste) |
| **Atrium** | das Home-Menü mit Anmeldung und den Kacheln aller Rooms |
| **Rooms** | die Apps |
| **Room Store** | der Katalog für Rooms |
| **Device Core** | Geräte-Motor mit den Home-Assistant-Integrationen, unsichtbar |

Status: **Konzept** – siehe [docs/KONZEPT.md](docs/KONZEPT.md).

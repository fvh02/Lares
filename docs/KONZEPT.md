# Lares – Konzept

> Stand: Oktober 2026 · Status: Etappe 1 begonnen – Lares Core 0.1 (siehe [core/README.md](../core/README.md))

Lares ist ein eigenes, schlankes Betriebssystem für den Heimserver. Ein Kern verwaltet alles – Schlüssel,
Benachrichtigungen, KI, Standort, Geräte –, und darauf laufen **Rooms**: kleine Apps wie ein Spieleabend, eine
Restaurant-Karte oder ein Serien-Kalender. Geräte funktionieren mit den Integrationen von Home Assistant, ohne
dass Home Assistant selbst sichtbar ist. Neue Rooms lassen sich mit einem SDK einfach entwickeln.

Der Name: Die *Lares* waren im römischen Haus die Schutzgeister, die über Haus und Bewohner wachten.
Das *Atrium* war der zentrale Raum, von dem alle anderen Räume abgingen.

---

## 1. Begriffe

| Begriff | Bedeutung |
|---|---|
| **Lares** | das Betriebssystem als Ganzes |
| **Lares Core** | der zentrale Dienst (heute: Fabians-HA-Master) – verwaltet Rooms, Schlüssel, Rechte und die Systemdienste |
| **Atrium** | das Home-Menü: Anmeldung, Startseite mit den Kacheln aller Rooms, Einstellungen |
| **Room** / **Rooms** | eine App auf Lares (heute: ein Add-on), läuft als Container |
| **Room Store** | der Katalog, aus dem Rooms installiert und aktualisiert werden |
| **Device Core** | der versteckte Geräte-Motor: Home Assistant Core mit seinen Integrationen, ohne Oberfläche |
| **SDK** | Bibliothek und Schnittstelle, mit der Rooms den Kern nutzen |
| `lares` | Kommandozeilen-Werkzeug zum Entwickeln und Verwalten |

---

## 2. Aufbau

```
┌───────────────────────────────────────────────────────────────────┐
│ Atrium  (Anmeldung · Startseite mit Room-Kacheln · Einstellungen) │
├───────────────────────────────────────────────────────────────────┤
│ Rooms:  Assistent · Spieleabend · Karte · Serien · Himmel ·       │
│         Urlaub · Downloader · Gedächtnis · eigene Rooms …         │
├───────────────────────────────────────────────────────────────────┤
│ Lares Core                                                        │
│  Room-Verwaltung & Room Store · Rechte & Tokens · Schlüssel ·     │
│  Benachrichtigung · KI (Claude) · Standort · Geräte · Ereignisse ·│
│  Zeitpläne · Speicher · Backups · Updates · Fernzugriff · Logs    │
├──────────────────────────────┬────────────────────────────────────┤
│ Device Core (HA Core, intern)│ Container-Laufzeit (Podman/Docker) │
├──────────────────────────────┴────────────────────────────────────┤
│ Basis-OS: minimales Linux, schreibgeschützt, A/B-Updates          │
└───────────────────────────────────────────────────────────────────┘
```

Grundsätze:

- **Alles läuft über den Kern.** Rooms reden nie direkt miteinander, mit dem Device Core oder dem Internet-Zugang
  des Systems, sondern über die Schnittstellen des Kerns – mit den Rechten, die sie haben.
- **Rooms sind austauschbar.** Jeder Room ist ein Container mit einer Beschreibung (`room.yaml`). Fällt einer aus,
  läuft der Rest weiter.
- **Keine Schlüssel in Rooms oder Repositories.** Schlüssel liegen ausschließlich im Kern und gehen nur an die Rooms,
  die sie laut Rechten bekommen.
- **Home Assistant ist nicht sichtbar.** Es gibt keine HA-Oberfläche, keinen Supervisor, kein HA OS – nur den
  Device Core als Baustein für Geräte.

---

## 3. Basis-OS

| Thema | Plan |
|---|---|
| Grundlage | minimales Debian (stabil), gebaut mit `mkosi` – kein eigener Paketbau nötig |
| Zielhardware | zuerst x86-64 (Lenovo M600 Tiny, 4 GB RAM), später ARM (Raspberry Pi) |
| Dateisystem | System schreibgeschützt; alle Daten auf einer eigenen Datenpartition (`/data`) |
| Updates | **A/B-Updates mit RAUC**: neues System in die zweite Partition, Neustart, bei Fehler automatisch zurück |
| Container | Podman (ohne Root-Dienst) oder Docker; Images aus der GitHub Container Registry |
| Installation | Image für USB-Stick: booten, Zielplatte wählen, fertig; danach Einrichtung im Atrium (Browser) |
| Wiederherstellung | Wiederherstellungsmodus im Bootmenü; Backup-Datei zurückspielen |
| Netz | feste Adresse oder DHCP, mDNS (`lares.local`), optional Fernzugriff über einen Tunnel-Room |

Ziel: Das Basis-OS ist langweilig und stabil. Neues passiert in Kern und Rooms, nicht im OS.

---

## 4. Lares Core

Der Kern ist ein Dienst (heute der Master, weiterentwickelt). Er bietet:

| Dienst | Aufgabe |
|---|---|
| **Room-Verwaltung** | installieren, starten, stoppen, aktualisieren, Logs, Ressourcen-Grenzen, Neustart bei Absturz |
| **Room Store** | Kataloge lesen, Versionen anzeigen, Updates melden |
| **Atrium** | Anmeldung (Passkey oder Passwort + 2FA), Startseite, Einstellungen, Reverse-Proxy zu den Rooms |
| **Rechte & Tokens** | jeder Room bekommt beim Start ein eigenes Token; der Kern prüft jede Anfrage gegen die Rechte |
| **Schlüssel** | Tresor für API-Keys, Passwörter, Tokens (verschlüsselt auf der Datenpartition) |
| **Benachrichtigung** | Nachrichten an Bewohner (iMessage über Inkbox, später weitere Wege), mit Ruhezeiten |
| **KI** | KI-Anfragen der Rooms; beantwortet von dem Room mit dem Recht `ki_anbieter` (heute der Assistent mit seiner claude.ai-Anmeldung, ohne API-Key), mit Übersicht pro Room |
| **Standort** | Standort der Bewohner – aus „Wo ist?“ (über den Device Core) oder OwnTracks |
| **Geräte** | einheitliche Sicht auf alle Geräte aus dem Device Core; schalten, lesen, abonnieren |
| **Ereignisse** | Nachrichten zwischen Rooms („paket.zugestellt“, „person.angekommen“) |
| **Zeitpläne** | „jeden Tag um 17 Uhr“, „alle 30 Minuten“ – der Kern weckt den Room |
| **Speicher** | Datenordner pro Room, Teil der Backups |
| **Backups** | komplette oder einzelne Rooms, zeitgesteuert, optional extern |
| **Updates** | Basis-OS, Kern, Device Core und Rooms; mit Rückfall bei Problemen |
| **Fernzugriff** | Freigabe einzelner Rooms nach außen (z. B. Spieleabend für Freunde), mit Zugangscodes |
| **Systemeinstellungen** | Wohnort, Zeitzone, Sprache, Bewohner |

---

## 5. Device Core

Der Device Core ist **Home Assistant Core** als interner Baustein:

- läuft als versteckter Container, nur für den Kern erreichbar;
- **keine** HA-Oberfläche, **kein** Supervisor, **keine** HA-Automationen, **keine** HA-Handy-App;
- liefert die über 2000 Integrationen (Zigbee/ZHA, Matter, Shelly, Hue, Sonos, iCloud/„Wo ist?“ …).

So arbeitet der Kern mit ihm:

| Aufgabe | Weg |
|---|---|
| Integration hinzufügen | Der Kern nutzt die Einrichtungs-Assistenten (Config Flows) des Device Core und zeigt sie im Atrium an – inklusive Zwei-Faktor-Codes und Hersteller-Anmeldungen |
| Geräte und Zustände | Der Kern hört über die WebSocket-Schnittstelle auf alle Änderungen und führt eine eigene Geräteliste |
| Schalten | Der Kern ruft die Dienste des Device Core auf |
| Rechte | Rooms sehen nur die Geräte, die ihnen freigegeben sind |
| Updates | Der Kern wählt die Version, aktualisiert, prüft danach die Geräte und geht bei Problemen zurück |

Ressourcen: etwa 300–500 MB RAM.

Lizenz: Home Assistant Core steht unter Apache 2.0 und darf so genutzt werden.

---

## 6. Rooms

### 6.1 `room.yaml`

```yaml
id: serien
name: Serien
version: 1.0.0
beschreibung: Neue Folgen und Staffeln deiner Serien
symbol: mdi:television-classic
image: ghcr.io/fvh02/{arch}-lares-serien
arch: [amd64, aarch64]

oberflaeche:            # erscheint als Kachel im Atrium
  port: 8080
  kachel: true
  oeffentlich: false    # true: über den Fernzugriff erreichbar (mit Zugangscode)

rechte:
  - benachrichtigen
  - ki
  - standort
  - geraete: [sensor.*, light.wohnzimmer]
  - ereignisse: { senden: [serien.*], empfangen: [person.angekommen] }
  - schluessel: [tvmaze_key]

einstellungen:          # vom Atrium als Formular angezeigt
  meldungen: { typ: bool, standard: true, name: Meldungen per Nachricht }
  ruhe_ab:   { typ: int, standard: 22, min: 0, max: 23, name: Nachtruhe ab }

zeitplaene:
  - { name: pruefen, alle: 30m }

befehle:                # was der Assistent mit diesem Room tun darf
  - { name: demnaechst, beschreibung: "Folgen der nächsten Tage" }
  - { name: folgen, beschreibung: "Serie hinzufügen", argumente: [name] }

ressourcen: { ram: 128M }
```

### 6.2 Lebenszyklus

1. Installation aus dem Room Store: Rechte werden angezeigt und bestätigt.
2. Der Kern legt Datenordner und Token an und startet den Container.
3. Der Room holt Einstellungen und Schlüssel über das SDK.
4. Updates: neue Version starten, bei Fehler zurück auf die alte.
5. Entfernen: Daten bleiben auf Wunsch im Backup.

### 6.3 Rooms und der Assistent

Rooms können **Befehle** anbieten (siehe `befehle`). Der Assistent-Room sieht alle Befehle der installierten
Rooms und kann sie nutzen – so wie heute `karte`, `serien` oder `himmel`, nur ohne eigene Skripte im Assistenten.

---

## 7. SDK

Zuerst für Python, später JavaScript. Darunter liegt eine einfache HTTP-Schnittstelle, sodass Rooms in jeder
Sprache geschrieben werden können.

```python
from lares import room

einstellungen = room.einstellungen()
key = room.schluessel("tvmaze_key")

room.melden("📺 Neu: Severance – S02E05")                  # Benachrichtigung
antwort = room.ki("Fasse zusammen: …", schema={...})       # Claude
ort = room.standort()                                      # lat, lon, Genauigkeit, Quelle
lampe = room.geraet("light.wohnzimmer"); lampe.an(helligkeit=60)
room.ereignis("serien.neue_folge", {"serie": "Severance"})
room.abonnieren("person.angekommen", bei_ankunft)
room.zeitplan("pruefen", pruefen)                          # Takt steht in room.yaml
room.befehl("demnaechst", demnaechst)                      # Befehl für den Assistenten
room.daten  # Pfad zum eigenen Datenordner
room.laufen()                                              # hält den Strom zum Kern offen
```

| Schnittstelle | Beispiel |
|---|---|
| Authentifizierung | `Authorization: Bearer <Room-Token>` (vom Kern beim Start gesetzt) |
| Basis-URL | `http://lares-core:8080/api/v1` (setzt der Kern als `LARES_URL`) |
| Endpunkte | `/schluessel`, `/melden`, `/ki`, `/standort`, `/geraete`, `/ereignisse`, `/einstellungen`, `/befehle`, `/strom` |
| Strom | Ein Room hält `GET /strom` offen (Server-Sent Events): Ereignisse, Zeitpläne, Befehle und KI-Fragen kommen dort an – ein Room braucht dafür keinen eigenen Server |

---

## 8. Rechte und Sicherheit

- **Token statt IP-Adresse:** Jeder Room weist sich mit seinem Token aus; die Rechte stehen in `room.yaml` und
  werden bei der Installation bestätigt.
- **Kleinste Rechte:** Ein Room bekommt nur die Schlüssel, Geräte und Ereignisse, die er angibt.
- **Netz:** Rooms laufen in einem eigenen Netz; nach außen nur über den Fernzugriff des Kerns, mit Zugangscode.
- **Anmeldung:** Das Atrium braucht Anmeldung; einzelne Rooms können für Gäste ohne Konto freigegeben werden.
- **Schlüssel:** verschlüsselt gespeichert, nie im Atrium wieder angezeigt, nie in Logs.
- **KI-Kosten:** pro Room sichtbar, mit Grenzen.
- **Updates:** signierte Images, Rückfall bei Fehlern.

---

## 9. Entwickeln

```
lares room neu wetter           # Vorlage mit room.yaml, SDK, Dockerfile, Tests
lares room dev                  # lokal starten, mit simuliertem Kern
lares room bauen                # Images für alle Architekturen
lares room veröffentlichen      # in den eigenen Room Store
```

- Der **simulierte Kern** ersetzt Benachrichtigungen, KI, Standort und Geräte durch Attrappen. So lassen sich Rooms
  ohne echtes System testen, wie heute die Add-ons.
- **GitHub Actions** bauen die Images (wie heute) und aktualisieren den Store-Katalog.

---

## 10. Room Store

- Ein Store ist ein Git-Repository mit einem Katalog (`store.yaml`): Rooms, Versionen, Images, Rechte, Bilder.
- Im Atrium können mehrere Stores eingetragen werden: der eigene und später fremde.
- Anzeige: Beschreibung, Bilder, Rechte, Größe, Änderungen; ein Klick zum Installieren oder Aktualisieren.

---

## 11. Umzug der heutigen Add-ons

| Heute (Fabians-HA-Addons) | In Lares | Änderungen |
|---|---|---|
| Master | **Lares Core** | Room-Verwaltung, Atrium, Tokens und Device-Core-Anbindung kommen dazu |
| Assistent | Room | Befehle der anderen Rooms statt eigener Skripte; Standort und Geräte über den Kern |
| Spieleabend, Bestenliste | Rooms | Zugangscode und E-Mail über das SDK; Fernzugriff über den Kern |
| Karte | Room | Standort über den Kern statt direkt von Home Assistant |
| Serien | Room | kaum Änderungen |
| Himmel | Room | Wohnort aus den Systemeinstellungen statt aus Home Assistant |
| Urlaub, Gedächtnis | Rooms | KI über den Kern |
| Downloader, Video-Downloader, Browser | Rooms | Speicherorte als Room-Daten bzw. freigegebene Ordner |
| Krypto-Trader, 3D-Druck | Rooms | Schlüssel über das SDK; Drucker über den Device Core (Bambu-Lab-Integration) |
| Home Assistant (Geräte) | **Device Core** | Integrationen werden übernommen, die Oberfläche fällt weg |

Die Rooms sind heute schon Container und sprechen schon über den Master – der Umzug betrifft vor allem die
Stellen, die heute Home Assistant direkt fragen, und das Format der Beschreibung.

---

## 12. Etappen

| Phase | Inhalt | Ergebnis |
|---|---|---|
| **1 · SDK und Room-Format** | `room.yaml`, SDK (Python), Tokens und Rechte; läuft noch auf dem heutigen System | Rooms sind unabhängig von Home Assistant |
| **2 · Kern eigenständig** | Room-Verwaltung, Atrium, Systemeinstellungen; auf normalem Debian mit Podman/Docker | System ohne HA OS |
| **3 · Device Core** | HA Core als interner Baustein, Einrichtungs-Assistenten im Atrium, Geräte im SDK | Geräte und „Wo ist?“ wie heute |
| **4 · Lares-Image** | Basis-OS mit `mkosi`, RAUC, Installations-Stick, Wiederherstellung | Lares als eigenes OS |
| **5 · Room Store und Werkzeuge** | Store im Atrium, `lares`-CLI, Vorlagen, Doku | Rooms entwickeln und teilen |

Phasen 1–3 bringen den größten Nutzen; Phase 4 lohnt sich, wenn alles stabil läuft.

---

## 13. Grenzen und Risiken

- **Wartung:** Ein eigenes OS braucht Sicherheitsupdates und einen zuverlässigen Update-Weg – deshalb ein bewährtes
  Fundament (Debian, RAUC) und ein schreibgeschütztes System.
- **Device Core:** Er hängt an den Versionen von Home Assistant Core; der Kern muss Updates prüfen und zurückrollen
  können. Integrationen, die die HA-Oberfläche voraussetzen, brauchen ggf. eigene Lösungen.
- **Arbeitsspeicher:** 4 GB reichen für Kern, Device Core und eine Handvoll Rooms; Rooms sollten schlank sein und
  ihre Grenzen angeben.
- **Fernzugriff und Anmeldung** müssen sicher sein, bevor Rooms nach außen freigegeben werden.

---

## 14. Offene Fragen

- Standort: nur „Wo ist?“ über den Device Core, oder zusätzlich OwnTracks für Bewohner ohne iPhone?
- Mehrere Bewohner mit eigenen Konten und Rechten – von Anfang an oder später?
- Welche Benachrichtigungswege außer iMessage (Push über das Atrium als Web-App, E-Mail)?

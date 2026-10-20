# Lares Core

Der zentrale Dienst von Lares. Er verwaltet die Rooms mit ihrer `room.yaml`, gibt jedem Room ein eigenes Token und
prüft jede Anfrage gegen die Rechte des Rooms. Daneben hält er den Schlüsseltresor, die Systemeinstellungen, die
Benachrichtigungen, Ereignisse, Zeitpläne und Befehle bereit und leitet KI-Anfragen weiter.

Version 0.1 ist der Anfang von Etappe 1 aus dem [Konzept](../docs/KONZEPT.md).

## Was schon geht

| Bereich | Stand |
|---|---|
| `room.yaml` | wird vollständig geprüft: id, Version, Oberfläche, Rechte, Einstellungen (bool, int, float, text, auswahl), Zeitpläne, Befehle, RAM |
| Rooms | installieren, aktualisieren, entfernen, ein- und ausschalten; die Rechte müssen bei der Installation bestätigt werden |
| Tokens | jeder Start bekommt ein neues Token; gespeichert wird nur der Hash |
| Schlüssel | Tresor, verschlüsselt mit Fernet (`/data/core/vault.*`); ein Room bekommt nur die Schlüssel aus seinen Rechten |
| Einstellungen | pro Room, geprüft gegen die `room.yaml`; Änderungen gehen sofort an den Room |
| System | Wohnort, Zeitzone, Sprache, Bewohner mit Handynummer, Ruhezeit |
| Benachrichtigung | iMessage über Inkbox (Schlüssel `inkbox_api_key`); in der Ruhezeit wartet sie, außer wenn sie dringend ist; höchstens 20 pro Minute und Room |
| Ereignisse | senden und empfangen nach Mustern (`serien.*`), nur im Rahmen der Rechte |
| Zeitpläne | `alle: 30m` oder `um: "07:30"`; der Kern weckt den Room über den Strom |
| Befehle | ein Room mit dem Recht `befehle` (z. B. der Assistent) ruft die Befehle der anderen Rooms auf |
| KI | geht an den Room mit dem Recht `ki_anbieter` (der Assistent mit seiner claude.ai-Anmeldung), ohne API-Key; Anfragen werden pro Room gezählt |
| Container | Docker oder Podman: Netz `lares`, Datenordner, RAM-Grenze, Neustart bei Absturz; das Token geht über stdin hinein, nicht über die Befehlszeile |
| Standort, Geräte | Schnittstelle und Rechte gibt es schon, die Antwort ist 503, bis der Device Core da ist (Etappe 3) |

Noch nicht da sind das Atrium mit Anmeldung, der Proxy zu den Room-Oberflächen, Backups, Updates, der Fernzugriff,
der Device Core und das Kommandozeilen-Werkzeug `lares`.

## Starten

```sh
pip install -r core/requirements.txt
cd core && LARES_DATA=/tmp/lares LARES_PORT=8080 python -m lares_core
```

| Variable | Bedeutung | Standard |
|---|---|---|
| `LARES_DATA` | Datenpartition | `/data` |
| `LARES_PORT` | Port der API | `8080` |
| `LARES_RUNTIME` | `docker`, `podman` oder `keine` (Rooms werden dann von Hand gestartet, zum Entwickeln) | `keine` |
| `LARES_CORE_URL` | Adresse, unter der die Rooms den Kern erreichen | `http://lares-core:<port>/api/v1` |
| `LARES_HOST_DATA` | Pfad der Datenpartition auf dem Host, wenn der Kern selbst im Container läuft | = `LARES_DATA` |

Als Container:

```sh
docker build -f core/Dockerfile -t lares-core .
docker network create lares
docker run -d --name lares-core --network lares -p 8080:8080 \
  -v /srv/lares:/data -v /var/run/docker.sock:/var/run/docker.sock \
  -e LARES_RUNTIME=docker -e LARES_HOST_DATA=/srv/lares lares-core
```

Beim ersten Start legt der Kern `/data/core/admin.token` an. Damit arbeiten das Atrium und `lares`, bis es eine
Anmeldung gibt. Diese Datei gehört niemandem sonst.

## Schnittstelle

Rooms schicken `Authorization: Bearer <Room-Token>`. Fehler kommen immer als `{"fehler": "…"}`.

| Methode und Pfad | Recht | Was |
|---|---|---|
| `GET /api/v1/ich` | – | eigener Room, Rechte in Worten |
| `GET /api/v1/einstellungen` | – | eigene Einstellungen |
| `GET /api/v1/system` | – | Wohnort, Zeitzone, Sprache, Namen der Bewohner |
| `GET /api/v1/schluessel/{name}` | `schluessel: [name]` | `{"wert": …}`, 404 wenn nicht eingetragen |
| `POST /api/v1/melden` | `benachrichtigen` | `{text, bild_b64?, dateiname?, an?, dringend?}` |
| `POST /api/v1/ki` | `ki` | `{frage, schema?}` → `{antwort}` |
| `GET /api/v1/standort` | `standort` | ab Etappe 3 |
| `* /api/v1/geraete/…` | `geraete: [muster]` | ab Etappe 3 |
| `POST /api/v1/ereignisse` | `ereignisse.senden` | `{name, daten}` |
| `GET /api/v1/ereignisse?seit=nr` | `ereignisse.empfangen` | die letzten Ereignisse |
| `GET /api/v1/strom?abo=muster,…` | – | Server-Sent Events: `hallo`, `ereignis`, `zeitplan`, `befehl`, `ki`, `einstellungen`, `system` |
| `POST /api/v1/strom/antwort` | – | `{id, ergebnis}` oder `{id, fehler}` auf `befehl` und `ki` |
| `GET /api/v1/befehle` | `befehle` | Befehle der anderen Rooms |
| `POST /api/v1/befehle/{room}/{name}` | `befehle` | `{argumente}` → `{ergebnis}` |

Für die Verwaltung (Admin-Token):

| Methode und Pfad | Was |
|---|---|
| `GET/POST /api/v1/admin/rooms` | Liste; installieren/aktualisieren mit `{room_yaml, rechte_bestaetigt}` (ohne Bestätigung: 409 mit den Rechten) |
| `DELETE /api/v1/admin/rooms/{id}?daten=loeschen` | entfernen (Daten bleiben sonst) |
| `POST /api/v1/admin/rooms/{id}/start` · `/stop` · `/token` | starten, stoppen, neues Token (nur ohne Laufzeit) |
| `GET/PUT /api/v1/admin/rooms/{id}/einstellungen` | Formular und Werte |
| `GET /api/v1/admin/rooms/{id}/logs` | Container-Log |
| `GET /api/v1/admin/schluessel` · `PUT/DELETE …/{name}` | Namen und welche Rooms sie nutzen; Werte werden nie gezeigt |
| `GET/PUT /api/v1/admin/system` | Systemeinstellungen |
| `POST /api/v1/admin/test/melden` | Testnachricht |
| `GET /api/v1/admin/ki` | KI-Anfragen pro Room |

## Tests

```sh
cd core && PYTHONPATH=. python -m unittest discover -s tests -v
```

Die Tests starten den Kern, den Beispiel-Room `hallo` als eigenen Prozess und einen KI-Room und prüfen dann Befehle,
Einstellungen, Ereignisse zwischen Rooms, Schlüssel und Rechte, die KI-Weiterleitung, Benachrichtigungen mit
Ruhezeit und Zeitpläne.

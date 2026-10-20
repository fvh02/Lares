# Lares Core

Der zentrale Dienst von Lares. Er verwaltet die Rooms mit ihrer `room.yaml`, gibt jedem Room ein eigenes Token und
prüft jede Anfrage gegen die Rechte des Rooms. Daneben hält er den Schlüsseltresor, die Systemeinstellungen, die
Benachrichtigungen, Ereignisse, Zeitpläne und Befehle bereit und leitet KI-Anfragen weiter.

Version 0.5 gehört zu Etappe 1 und 2 aus dem [Konzept](../docs/KONZEPT.md): Fernzugriff mit Tailscale und Cloudflare
Tunnel und das Atrium mit Anmeldung per Passwort, Passkey, Apple oder Google.

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
| Atrium | Anmeldung mit Passwort (optional Zwei-Faktor), Passkey, Apple oder Google; Startseite mit Room-Kacheln; Rooms als Kacheln mit ⋯-Menü, hinzufügen per „+“ (Datei, Text, Link, GitHub); Schlüssel, System, Fernzugriff, Konten; Avatar-Menü mit „Mein Konto“ und „Abmelden“; Designs „Klassisch“ und „Himmel“, jeweils hell, dunkel oder automatisch – im Browser auf Port 8000, auch auf dem Handy |
| Fernzugriff | Tailscale und Cloudflare Tunnel sind als Systemdienste eingebaut, siehe unten |
| Gateways | Heim-Gateway (Port 8000) für alle Rooms mit Oberfläche unter `/r/<room>/`; Internet-Gateway (Port 8001, nur für cloudflared) nur für freigegebene Rooms, optional mit Zugangscode; HTTP, Uploads, Weiterleitungen und WebSockets |
| Standort, Geräte | Schnittstelle und Rechte gibt es schon, die Antwort ist 503, bis der Device Core da ist (Etappe 3) |

Noch nicht da sind Backups, Updates, der Room Store, der Device Core und das Kommandozeilen-Werkzeug `lares`.

## Starten

```sh
pip install -r core/requirements.txt
cd core && LARES_DATA=/tmp/lares LARES_PORT=8080 python -m lares_core
```

| Variable | Bedeutung | Standard |
|---|---|---|
| `LARES_DATA` | Datenpartition | `/data` |
| `LARES_PORT` | Port der API | `8080` |
| `LARES_HEIM_PORT` | Heim-Gateway (Heimnetz, Tailscale) | `8000` |
| `LARES_EXTERN_PORT` | Internet-Gateway, nur im Netz `lares` für cloudflared – nicht auf dem Host freigeben | `8001` |
| `LARES_CORE_HOST` | Name des Kerns im Netz `lares` | `lares-core` |
| `LARES_RUNTIME` | `docker`, `podman` oder `keine` (Rooms werden dann von Hand gestartet, zum Entwickeln) | `keine` |
| `LARES_CORE_URL` | Adresse, unter der die Rooms den Kern erreichen | `http://<host>:<port>/api/v1` |
| `LARES_TAILSCALE_IMAGE` · `LARES_CLOUDFLARED_IMAGE` | Images der Dienste | `tailscale/tailscale:v1.86.2` · `cloudflare/cloudflared:2025.8.0` |
| `LARES_HOST_DATA` | Pfad der Datenpartition auf dem Host, wenn der Kern selbst im Container läuft | = `LARES_DATA` |

Als Container:

```sh
docker build -f core/Dockerfile -t lares-core .
docker network create lares
docker run -d --name lares-core --network lares -p 8080:8080 -p 8000:8000 \
  -v /srv/lares:/data -v /var/run/docker.sock:/var/run/docker.sock \
  -e LARES_RUNTIME=docker -e LARES_HOST_DATA=/srv/lares lares-core
```

Beim ersten Start legt der Kern `/data/core/admin.token` an. Damit arbeiten das Atrium und `lares`, bis es eine
Anmeldung gibt. Diese Datei gehört niemandem sonst.

## Atrium

`http://<server>:8000` (zu Hause) oder `https://lares.<tailnet>.ts.net` (unterwegs).

- **Ersteinrichtung:** Beim ersten Start schreibt der Kern einen Einrichtungscode ins Log und nach
  `/data/core/setup.code`. Mit diesem Code legst du das erste Konto an, ein Admin-Konto. So kann niemand sonst im
  Heimnetz ein frisches Lares übernehmen.
- **Konten:** Admins verwalten das System, Bewohner öffnen nur die Rooms. Passwörter werden mit scrypt gespeichert
  (mindestens 10 Zeichen). Zwei-Faktor geht mit jeder Authenticator-App; auf dem iPhone öffnet der Link direkt die
  Passwörter-App.
- **Sitzungen:** Ein Cookie (HttpOnly, SameSite=Lax, über HTTPS Secure) gilt 30 Tage und verlängert sich bei Nutzung.
  Gespeichert wird nur sein Hash. Ändernde Anfragen müssen von der Atrium-Seite selbst kommen (Origin-Prüfung), und
  nach 10 Fehlversuchen pro Minute ist Pause.
- **Passkeys:** Unter „Mein Konto“ anlegen (Face ID, Touch ID, Geräte-PIN), danach „Mit Passkey anmelden“ ohne Namen
  und Passwort. Passkeys gehen nur über HTTPS, also über die Tailscale-Adresse, und gehören zu genau dieser Adresse.
  Geprüft wird alles im Kern (Challenge, Ursprung, Adresse, Signatur, Zähler). Gespeichert wird nur der öffentliche
  Schlüssel.
- **Apple und Google:** Anmelden können sich nur Konten, die vorher unter „Mein Konto“ verbunden wurden. Ein Apple- oder
  Google-Konto allein legt nie ein Lares-Konto an. Es gibt keine Anmeldung ohne HTTPS; die Rücksprung-Adresse ist
  `https://<tailscale-adresse>/atrium/anmelden/google/zurueck` bzw. `…/apple/zurueck` (steht auch unter „Konten“).
  - Google: in der Google Cloud Console einen OAuth-Client (Webanwendung) anlegen; Schlüssel `google_client_id` und
    `google_client_secret`.
  - Apple: braucht ein Apple-Developer-Konto. Eine Services ID mit „Sign in with Apple“ und einen Schlüssel (.p8)
    anlegen; Schlüssel `apple_client_id` (Services ID), `apple_team_id`, `apple_key_id` und `apple_private_key`
    (Inhalt der .p8-Datei).
  - Lares prüft das Token des Anbieters selbst (Signatur, Aussteller, App, Ablauf, Nonce; bei Google zusätzlich PKCE).
- **Darstellung:** unter „Mein Konto“, gespeichert im Konto (und auf dem Gerät, damit schon die Anmeldeseite passt).
  - *Klassisch* – das ruhige, warme Design.
  - *Himmel* – im Hintergrund das Wetter am Ort des Geräts (sonst am Wohnort aus „System“), davor Milchglas für
    Kopfzeile, Container, Menüs und Dialoge. Die Farben folgen dem echten Sonnenstand, den Lares aus Ort und Uhrzeit
    berechnet: Dämmerung, Sonnenaufgang und -untergang laufen genau dann, wenn sie draußen passieren. Wolken nach
    Bedeckung, Wind bewegt sie und neigt den Regen; dazu Niesel, Regen, Schnee, Nebel, Gewitter mit Blitzen, nachts
    Sterne und Mond mit Phase. Im dunklen Modus wird der Himmel dunkel getönt; im hellen Modus wird die Schrift
    direkt auf dem Himmel hell, sobald er dunkel ist. Bei „Bewegung reduzieren“ steht das Bild still.
  - Wetter von Open-Meteo (ohne Schlüssel) über `GET /atrium/wetter`: Lares rundet den Ort auf etwa einen Kilometer
    und fragt höchstens alle zehn Minuten, der genaue Standort verlässt Lares nicht.
  - Zum Ausprobieren in der Browser-Konsole: `LaresSky.set({zeit: "2026-10-20T07:45:00+02:00", code: 63, wolken: 90,
    niederschlag: 2})`, zurück mit `LaresSky.set(null)`.
- **Rooms:** Ohne Anmeldung kommt niemand mehr an `/r/<room>/` – der Browser landet auf der Anmeldung und danach
  wieder im Room. Rooms bekommen `X-Lares-Benutzer` (URL-kodiert) und `X-Lares-Rolle`.
- **Verwaltung:** Die Seiten nutzen dieselbe Admin-API wie `lares`; das Heim-Gateway reicht sie nur für Admins durch.

## Fernzugriff

Beides läuft als Container neben dem Kern (`lares-tailscale`, `lares-cloudflared`). Die Schlüssel liegen im Tresor
und gehen über stdin in den Container, also nie über die Befehlszeile.

**Tailscale** – deine Geräte, privat:

1. Auf login.tailscale.com → Settings → Keys einen Auth-Key anlegen und als Schlüssel `tailscale_authkey`
   eintragen. Ohne Key zeigt `GET /api/v1/admin/fernzugriff` unter `anmelden` einen Link zum einmaligen Anmelden,
   das geht auch vom Handy.
2. `PUT /api/v1/admin/fernzugriff/tailscale` mit `{"aktiv": true, "hostname": "lares"}`, optional
   `"routen": ["192.168.178.0/24"]` (Heimnetz von unterwegs) und `"exit_node": true`.
3. Lares ist dann unter `https://lares.<tailnet>.ts.net` erreichbar (im Tailnet HTTPS einschalten). Das ist das
   Heim-Gateway mit allen Rooms.

**Cloudflare Tunnel** – das Internet, nur für freigegebene Rooms:

1. Im Cloudflare-Dashboard unter Zero Trust → Networks → Tunnels einen Tunnel anlegen. Das Token wird als
   Schlüssel `cloudflare_tunnel_token` eingetragen.
2. Im Tunnel für jede öffentliche Adresse (z. B. `spiele.example.de`) als Dienst `http://lares-core:8001` eintragen.
3. `PUT /api/v1/admin/fernzugriff/cloudflared` mit `{"aktiv": true}`.
4. Einen Room freigeben: `PUT /api/v1/admin/rooms/<id>/freigabe` mit `{"host": "spiele.example.de",
   "zugangscode": "…"}`. Das geht nur bei Rooms mit `oberflaeche.oeffentlich: true` in der `room.yaml`. Der
   Zugangscode ist optional (mindestens 8 Zeichen) und wird nur als Hash gespeichert. Nach der Eingabe gilt ein
   Cookie 90 Tage, bei 10 Fehlversuchen pro Minute ist Pause.

Alles andere bleibt aus dem Internet unerreichbar: das Internet-Gateway kennt nur freigegebene Rooms, die API und das
Heim-Gateway hängen nicht am Tunnel. Rooms erkennen am Kopf `X-Lares-Zugang` (`heim` oder `extern`), woher eine
Anfrage kommt, und bekommen `X-Lares-Prefix` bzw. `X-Ingress-Path` wie bei Home Assistant Ingress.

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
| `GET/POST /api/v1/admin/rooms` | Liste; installieren/aktualisieren mit `{room_yaml}` oder `{url}` (auch GitHub-Links auf Repository, Ordner oder Datei; nur HTTPS, nie ins Heimnetz) und `rechte_bestaetigt` (ohne Bestätigung: 409 mit den Rechten) |
| `GET /api/v1/admin/anmeldung` | ob Apple und Google eingerichtet sind und welche Schlüssel fehlen |
| `DELETE /api/v1/admin/rooms/{id}?daten=loeschen` | entfernen (Daten bleiben sonst) |
| `POST /api/v1/admin/rooms/{id}/start` · `/stop` · `/token` | starten, stoppen, neues Token (nur ohne Laufzeit) |
| `GET/PUT /api/v1/admin/rooms/{id}/einstellungen` | Formular und Werte |
| `GET /api/v1/admin/rooms/{id}/logs` | Container-Log |
| `GET /api/v1/admin/schluessel` · `PUT/DELETE …/{name}` | Namen und welche Rooms sie nutzen; Werte werden nie gezeigt |
| `GET/PUT /api/v1/admin/system` | Systemeinstellungen |
| `POST /api/v1/admin/test/melden` | Testnachricht |
| `GET /api/v1/admin/ki` | KI-Anfragen pro Room |
| `GET /api/v1/admin/fernzugriff` | Tailscale, cloudflared (Zustand, Anmelde-Link) und Freigaben |
| `PUT /api/v1/admin/fernzugriff/tailscale` · `/cloudflared` | einstellen und ein- oder ausschalten; `…/logs` für das Log |
| `GET/POST /api/v1/admin/konten` · `PUT/DELETE …/{name}` | Konten anlegen, Rolle oder Passwort ändern, löschen (das letzte Admin-Konto bleibt) |
| `PUT/DELETE /api/v1/admin/rooms/{id}/freigabe` | Room fürs Internet freigeben (`{host?, zugangscode?}`) oder zurückziehen |

## Tests

```sh
cd core && PYTHONPATH=. python -m unittest discover -s tests -v
```

Die Tests starten den Kern, den Beispiel-Room `hallo` als eigenen Prozess und einen KI-Room und prüfen dann Befehle,
Einstellungen, Ereignisse zwischen Rooms, Schlüssel und Rechte, die KI-Weiterleitung, Benachrichtigungen mit
Ruhezeit und Zeitpläne. Dazu kommen die Gateways mit einem Test-Room (Präfix, Weiterleitungen, Uploads, WebSockets,
Freigaben, Zugangscode), die Container-Befehle für Tailscale und cloudflared sowie das Atrium (Einrichtung,
Anmeldung mit Zwei-Faktor, Rollen, Origin-Prüfung, Sperre nach Fehlversuchen). Passkeys werden mit einem
Software-Authenticator getestet (auch Wiederholung, fremde Adresse, fremder Schlüssel, Zähler), Apple/Google mit einem
nachgebauten Anbieter (Signatur, Nonce, PKCE, nur verbundene Konten).

# Lares SDK für Python

```python
from lares import room

key = room.schluessel("tvmaze_key")          # nur Schlüssel aus den Rechten; None, wenn nicht eingetragen
room.melden("📺 Neu: Severance – S02E05")    # wartet in der Ruhezeit, außer dringend=True
antwort = room.ki("Fasse zusammen: …")
room.ereignis("serien.neue_folge", {"serie": "Severance"})

@room.zeitplan("pruefen")                    # Takt in room.yaml: zeitplaene: [{name: pruefen, alle: 30m}]
def pruefen(): ...

@room.befehl("demnaechst")                   # room.yaml: befehle: [{name: demnaechst, …}]
def demnaechst():
    return ["Severance S02E05 – Freitag"]

@room.abonnieren("person.*")                 # room.yaml: rechte: [{ereignisse: {empfangen: [person.*]}}]
def person(name, daten, von): ...

@room.bei_einstellungen
def neu(werte): ...

room.laufen()                                # hält den Strom zum Kern offen und ruft die Funktionen auf
```

Außerdem gibt es `room.ich()`, `room.einstellungen()`, `room.system()`, `room.standort()`,
`room.geraet("light.wohnzimmer").an(helligkeit=60)`, `room.befehle()`, `room.ausfuehren("serien", "demnaechst")`,
`room.ki_anbieten(funktion)` (für den KI-Room) und `room.daten` (der eigene Datenordner).

Fehler kommen als `LaresFehler` mit `status`: 403 heißt, das Recht fehlt in der `room.yaml`; 503 heißt, der Dienst
ist gerade nicht da.

Der Kern setzt `LARES_URL` und `LARES_TOKEN`. Das SDK braucht nur die Standardbibliothek.

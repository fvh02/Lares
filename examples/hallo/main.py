"""Beispiel-Room „Hallo“ – zeigt Befehle, Zeitpläne, Ereignisse und Einstellungen mit dem SDK."""

import logging

from lares import room

logging.basicConfig(level=logging.INFO)


@room.befehl("gruessen")
def gruessen(name: str = "Welt") -> str:
    return f"{room.einstellungen()['gruss']}, {name}!"


@room.zeitplan("morgens")
def morgens() -> None:
    if room.einstellungen()["morgens_melden"]:
        room.melden(f"{room.einstellungen()['gruss']} – guten Morgen!")


@room.abonnieren("person.*")
def person(name: str, daten: dict, von: str) -> None:
    if name == "person.angekommen":
        room.ereignis("hallo.begruesst", {"person": daten.get("name", "?")})


if __name__ == "__main__":
    room.laufen()

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lares_core.bus import matches
from lares_core.manifest import ManifestError, parse
from lares_core.registry import Registry
from lares_core.scheduler import due
from lares_core.system import System
from lares_core.vault import Vault, VaultError

SERIEN = """
id: serien
name: Serien
version: 1.0.0
image: ghcr.io/fvh02/{arch}-lares-serien
arch: [amd64, aarch64]
oberflaeche: {port: 8080}
rechte:
  - benachrichtigen
  - ki
  - geraete: [sensor.*, light.wohnzimmer]
  - ereignisse: {senden: [serien.*], empfangen: [person.angekommen]}
  - schluessel: [tvmaze_key]
einstellungen:
  meldungen: {typ: bool, standard: true, name: Meldungen per Nachricht}
  ruhe_ab: {typ: int, standard: 22, min: 0, max: 23}
  modus: {typ: auswahl, werte: [kurz, lang], standard: kurz}
zeitplaene:
  - {name: pruefen, alle: 30m}
  - {name: morgens, um: "07:30"}
befehle:
  - {name: folgen, beschreibung: Serie hinzufügen, argumente: [name]}
ressourcen: {ram: 128M}
"""


class ManifestTest(unittest.TestCase):
    def test_full_example(self):
        m = parse(SERIEN)
        self.assertEqual(m.id, "serien")
        self.assertTrue(m.kachel)
        self.assertTrue(m.rechte.benachrichtigen and m.rechte.ki and not m.rechte.standort)
        self.assertEqual(m.rechte.schluessel, ["tvmaze_key"])
        self.assertEqual(m.rechte.ereignisse_empfangen, ["person.angekommen"])
        self.assertEqual(m.defaults(), {"meldungen": True, "ruhe_ab": 22, "modus": "kurz"})
        self.assertEqual(m.zeitplaene[0].alle, 1800)
        self.assertEqual(m.zeitplaene[1].um, "07:30")
        self.assertIn("Schlüssel: tvmaze_key", m.rechte.summary())

    def test_invalid(self):
        cases = {
            "id: X\nname: a\nversion: 1.0.0": "id",
            "id: abc\nversion: 1.0.0": "name",
            "id: abc\nname: a\nversion: eins": "version",
            "id: abc\nname: a\nversion: 1.0.0\nrechte: [alles]": "alles",
            "id: abc\nname: a\nversion: 1.0.0\nzeitplaene: [{name: x, alle: 10s}]": "Minute",
            "id: abc\nname: a\nversion: 1.0.0\nzeitplaene: [{name: x, alle: 1h, um: '07:00'}]": "entweder",
            "id: abc\nname: a\nversion: 1.0.0\neinstellungen: {x: {typ: int, standard: 5, max: 3}}": "zwischen",
            "id: abc\nname: a\nversion: 1.0.0\nrechte: [{geraete: [wohnzimmer]}]": "ungültig",
            "id: abc\nname: a\nversion: 1.0.0\narch: [sparc]": "arch",
            "- a\n- b": "oben",
        }
        for text, word in cases.items():
            with self.subTest(text=text), self.assertRaises(ManifestError) as ctx:
                parse(text)
            self.assertIn(word, str(ctx.exception))

    def test_setting_types(self):
        s = parse(SERIEN).einstellungen
        self.assertIs(s["meldungen"].check("nein"), False)
        self.assertEqual(s["ruhe_ab"].check("7"), 7)
        with self.assertRaises(ManifestError):
            s["ruhe_ab"].check(True)
        with self.assertRaises(ManifestError):
            s["modus"].check("mittel")


class VaultTest(unittest.TestCase):
    def test_roundtrip_and_cleaning(self):
        with tempfile.TemporaryDirectory() as d:
            v = Vault(Path(d))
            v.set("tvmaze_key", ' "abc\n def" ')
            v.set("passwort", " geheim mit leerzeichen ")
            self.assertEqual(Vault(Path(d)).get("tvmaze_key"), "abcdef")
            self.assertEqual(Vault(Path(d)).get("passwort"), "geheim mit leerzeichen")
            self.assertNotIn(b"abcdef", (Path(d) / "vault.bin").read_bytes())
            self.assertEqual((Path(d) / "vault.key").stat().st_mode & 0o777, 0o600)
            v.set("passwort", "")
            self.assertEqual(Vault(Path(d)).names(), ["tvmaze_key"])

    def test_missing_key_file(self):
        with tempfile.TemporaryDirectory() as d:
            Vault(Path(d)).set("a_key", "x")
            (Path(d) / "vault.key").unlink()
            with self.assertRaises(VaultError):
                Vault(Path(d))


class SystemTest(unittest.TestCase):
    def test_update_and_quiet_hours(self):
        with tempfile.TemporaryDirectory() as d:
            s = System(Path(d))
            s.update({"bewohner": [{"name": "Fabian", "telefon": "0049 170 1234567"}, {"name": "Gast"}],
                      "wohnort": {"lat": 52.5, "lon": 13.4}, "ruhezeit": {"von": 22, "bis": 7}})
            self.assertEqual(s.phone_of(), "+491701234567")
            self.assertEqual(s.phone_of("gast"), "")
            self.assertEqual(System(Path(d)).public()["bewohner"], ["Fabian", "Gast"])
            zone = ZoneInfo("Europe/Berlin")
            self.assertTrue(s.quiet(datetime(2026, 1, 1, 23, tzinfo=zone)))
            self.assertTrue(s.quiet(datetime(2026, 1, 1, 6, tzinfo=zone)))
            self.assertFalse(s.quiet(datetime(2026, 1, 1, 12, tzinfo=zone)))
            for bad in ({"zeitzone": "Mond/Krater"}, {"bewohner": [{"name": "A", "telefon": "123"}]},
                        {"wohnort": {"lat": 100, "lon": 0}}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    s.update(bad)


class RegistryTest(unittest.TestCase):
    def test_tokens_settings_persist(self):
        with tempfile.TemporaryDirectory() as d:
            r = Registry(Path(d) / "core", Path(d) / "rooms")
            room = r.install(parse(SERIEN))
            self.assertTrue((Path(d) / "rooms" / "serien").is_dir())
            first = r.issue_token("serien")
            second = r.issue_token("serien")
            self.assertIsNone(r.room_for_token(first))
            self.assertIs(r.room_for_token(second), room)
            r.set_settings("serien", {"ruhe_ab": "21"})
            with self.assertRaises(ManifestError):
                r.set_settings("serien", {"gibtsnicht": 1})
            again = Registry(Path(d) / "core", Path(d) / "rooms")
            self.assertEqual(again.get("serien").settings()["ruhe_ab"], 21)
            self.assertEqual(again.room_for_token(second).id, "serien")
            self.assertNotIn(second, (Path(d) / "core" / "rooms.json").read_text())


class ScheduleAndPatternTest(unittest.TestCase):
    def test_due(self):
        m = parse(SERIEN)
        every, daily = m.zeitplaene
        zone = ZoneInfo("Europe/Berlin")
        now = datetime(2026, 10, 20, 8, 0, tzinfo=zone)
        self.assertTrue(due(every, 0, now))
        self.assertFalse(due(every, now.timestamp() - 60, now))
        self.assertTrue(due(daily, 0, now))
        self.assertFalse(due(daily, now.timestamp() - 60, now))
        self.assertFalse(due(daily, 0, now.replace(hour=7, minute=0)))

    def test_matches(self):
        self.assertTrue(matches("serien.neue_folge", ["serien.*"]))
        self.assertFalse(matches("karte.neu", ["serien.*"]))
        self.assertTrue(matches("person.*", ["person.*"]))
        self.assertFalse(matches("person.*", ["person.angekommen"]))


if __name__ == "__main__":
    unittest.main()

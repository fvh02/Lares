# Lares OS

Lares als eigenes System auf einem USB-Stick: einstecken, vom Stick starten, im Browser einrichten.

- **Grundlage:** Debian 13 (trixie), x86-64 mit UEFI. Gebaut mit [mkosi](https://github.com/systemd/mkosi).
- **Auf dem Stick:** Lares selbst, also das System, Lares Core, Schlüssel, Konten und Einstellungen.
- **Rooms:** Ihre Daten und Container-Images liegen **wahlweise auf dem Stick oder auf der HDD/SSD des Geräts**.
  Gefragt wird bei der Einrichtung im Atrium, ändern lässt es sich später unter „System → Speicher für Rooms“.

## Aufbau des Sticks

| Partition | Inhalt |
|---|---|
| ESP (512 MB, FAT) | systemd-boot und der Lares-Kernel (UKI) |
| `lares-system` (ext4) | Debian, Docker, Lares Core |
| `lares-data` (ext4) | Wird beim ersten Start angelegt und füllt den Rest des Sticks: Schlüssel, Konten, Einstellungen, auf Wunsch die Rooms |

Wählst du eine Festplatte, richtet Lares sie als `lares-rooms` (ext4) ein. Bei jedem Start bindet Lares sie unter
`/var/lib/docker` und `/var/lib/lares/rooms` ein. Fehlt die Platte einmal, startet Lares trotzdem vom Stick und
zeigt im Atrium, was los ist. Eine Platte mit vorhandenem `lares-rooms` wird erkannt und übernommen, ohne sie zu löschen.

## Auf einen USB-Stick schreiben

Das Image kommt aus GitHub Actions („Lares OS“ → Artifacts) als `lares-os-<version>.raw.zst`. Ein Stick ab 16 GB
reicht, schneller ist ein USB-3-Stick oder eine kleine USB-SSD.

- **balenaEtcher** oder **Raspberry Pi Imager** („Eigenes Image“): die `.raw.zst` direkt auswählen.
- **macOS/Linux:** `zstd -dc lares-os-<version>.raw.zst | sudo dd of=/dev/<stick> bs=4M status=progress conv=fsync`

## Erster Start

1. Stick einstecken und im Bootmenü des Geräts den Stick wählen. Beim Lenovo M600 Tiny ist das **F12**.
   Secure Boot muss aus sein, das Image ist noch nicht signiert.
2. Auf dem Bildschirm erscheinen die Adresse des Atriums und der **Einrichtungscode**.
3. Im Browser `http://lares.local` öffnen (oder die angezeigte IP-Adresse), mit dem Code das erste Konto anlegen.
4. Das Atrium fragt **„Wo sollen die Rooms laufen?“**: USB-Stick oder eine der erkannten Festplatten/SSDs.
   - Eine Platte mit Daten wird nur nach ausdrücklicher Bestätigung gelöscht.
   - Der Lares-Stick selbst und Platten unter 16 GB werden nicht angeboten.

Wechselst du später den Ort, kopiert Lares die Rooms und Container-Images um und startet sie neu. Die alten Room-Daten
bleiben unter `alt/<datum>` liegen, bis du sie entfernst.

## Netz und Ports

| Port | Was | Erreichbar |
|---|---|---|
| 80 | Atrium und Rooms (Heim-Gateway) | Heimnetz, Tailscale |
| 8080 | API für Rooms und Verwaltung | nur das Gerät und seine Container |
| 8001 | Internet-Gateway für cloudflared | nur die Container |

Kein SSH, kein Root-Passwort: verwaltet wird Lares über das Atrium.

## Selbst bauen

```sh
# mkosi ≥ 25 (z. B. git clone --branch v25.3 https://github.com/systemd/mkosi), dazu systemd-ukify,
# systemd-boot, dosfstools, mtools, e2fsprogs, kmod, cpio, zstd
cd os
sudo mkosi -f build                      # → lares-os.raw
sudo ./test-vm.py lares-os.raw           # Boot-Test in QEMU: Stick + leere NVMe-SSD, Rooms umziehen, Neustart
```

Wo Debians Paketquellen nicht erreichbar sind, lässt sich derselbe Aufbau zum Testen mit
`mkosi --distribution=ubuntu -f build` auf Ubuntu 24.04 bauen.

## Noch offen

- A/B-Updates (RAUC) und ein schreibgeschütztes System
- Secure Boot (signierte UKI)
- ARM (Raspberry Pi)

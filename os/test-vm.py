#!/usr/bin/env python3
"""Boot test for Lares OS in a virtual machine – as at home: the image as a USB stick, an empty NVMe SSD inside.

1. start from the stick (UEFI), wait for the Atrium
2. read the setup code from the screen (serial console) and create the first account
3. choose the SSD for the rooms, follow the move
4. restart the machine: the SSD must be mounted again, the choice kept

    sudo ./test-vm.py lares-os_0.6.0.raw          (QEMU and OVMF needed; uses KVM when there is /dev/kvm)
"""

import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 18080
URL = f"http://127.0.0.1:{PORT}"
KVM = os.path.exists("/dev/kvm")
WAIT = 600 if KVM else 1800
OVMF = next((p for p in ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_CODE.fd",
                         "/usr/share/ovmf/OVMF.fd") if os.path.exists(p)), None)
OVMF_VARS = next((p for p in ("/usr/share/OVMF/OVMF_VARS_4M.fd", "/usr/share/OVMF/OVMF_VARS.fd")
                  if os.path.exists(p)), None)


def step(text):
    print(f"\n== {text}", flush=True)


class VM:
    def __init__(self, work: Path, image: Path):
        self.work, self.proc = work, None
        self.stick, self.ssd = work / "stick.qcow2", work / "ssd.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw", "-b", str(image.resolve()),
                        str(self.stick), "16G"], check=True)
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(self.ssd), "20G"], check=True)
        self.vars = work / "vars.fd"
        shutil.copy(OVMF_VARS, self.vars)
        self.boots = 0

    def start(self):
        self.boots += 1
        self.serial = self.work / f"serial-{self.boots}.log"
        cmd = ["qemu-system-x86_64", "-machine", "q35", "-m", "3072", "-smp", "2",
               "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF}",
               "-drive", f"if=pflash,format=raw,file={self.vars}",
               "-device", "qemu-xhci",
               "-drive", f"if=none,id=stick,format=qcow2,file={self.stick}",
               "-device", "usb-storage,drive=stick,removable=on,bootindex=0",
               "-drive", f"if=none,id=ssd,format=qcow2,file={self.ssd}",
               "-device", "nvme,serial=LARES-TEST-SSD,drive=ssd",
               "-netdev", f"user,id=net,hostfwd=tcp:127.0.0.1:{PORT}-:80",
               "-device", "virtio-net-pci,netdev=net",
               "-serial", f"file:{self.serial}", "-display", "none",
               "-monitor", f"unix:{self.work / 'monitor.sock'},server,nowait"]
        cmd += ["-enable-kvm", "-cpu", "host"] if KVM else ["-cpu", "max"]
        self.proc = subprocess.Popen(cmd)

    def screen(self) -> str:
        return self.serial.read_text(errors="replace") if self.serial.exists() else ""

    def power_off(self):
        subprocess.run(["sh", "-c", f"echo system_powerdown | socat - UNIX-CONNECT:{self.work / 'monitor.sock'}"],
                       capture_output=True)
        try:
            self.proc.wait(180)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


class Atrium:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                  urllib.request.ProxyHandler({}))

    def call(self, method, path, body=None):
        req = urllib.request.Request(URL + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "Origin": URL})
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"null")

    def wait(self, vm: VM, timeout=WAIT):
        end = time.time() + timeout
        while time.time() < end:
            if vm.proc.poll() is not None:
                raise SystemExit(f"QEMU beendet sich\n{vm.screen()[-3000:]}")
            try:
                status, data = self.call("GET", "/atrium/zustand")
                if status == 200:
                    return data
            except (OSError, ValueError):
                pass
            time.sleep(5)
        raise SystemExit(f"Atrium nicht erreichbar nach {timeout} s\n{vm.screen()[-4000:]}")


def check(cond, text):
    print(("  ok   " if cond else "  FEHLER ") + text, flush=True)
    if not cond:
        raise SystemExit(1)


def main():
    if len(sys.argv) != 2 or not OVMF:
        raise SystemExit("Aufruf: test-vm.py <lares-os.raw>   (QEMU und OVMF nötig)")
    work = Path(tempfile.mkdtemp(prefix="lares-vm-"))
    vm = VM(work, Path(sys.argv[1]))
    atrium = Atrium()
    try:
        step(f"Start vom USB-Stick ({'KVM' if KVM else 'ohne KVM, langsam'})")
        started = time.time()
        vm.start()
        state = atrium.wait(vm)
        print(f"  Atrium nach {time.time() - started:.0f} s erreichbar")
        check(state["eingerichtet"] is False, "frisches System wartet auf die Einrichtung")

        step("Einrichtungscode vom Bildschirm")
        end, code = time.time() + 120, None
        while time.time() < end and not code:
            match = re.findall(r"Einrichtungscode:\s+([0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4})", vm.screen())
            code = match[-1] if match else None
            time.sleep(2)
        check(code is not None, f"Code auf dem Bildschirm: {code}")
        status, _ = atrium.call("POST", "/atrium/einrichten", {"code": code, "name": "Test",
                                                               "passwort": "langes-passwort"})
        check(status == 200, "erstes Konto angelegt")
        state = atrium.call("GET", "/atrium/zustand")[1]
        check(state["speicher_waehlen"] is True, "Atrium fragt, wo die Rooms laufen sollen")

        step("Laufwerke")
        status, storage = atrium.call("GET", "/api/v1/admin/speicher")
        for disk in storage["laufwerke"]:
            print(f"  {disk['geraet']}: {disk['modell']} {disk['groesse'] // 1024 ** 3} GB {disk['typ']}"
                  f"{' (Lares-Stick)' if disk['system'] else ''}{' geeignet' if disk['geeignet'] else ''}")
        stick = [d for d in storage["laufwerke"] if d["system"]]
        ssd = [d for d in storage["laufwerke"] if d["geeignet"]]
        check(len(stick) == 1, "Lares-Stick erkannt und gesperrt")
        check(len(ssd) == 1 and ssd[0]["typ"] == "nvme", "NVMe-SSD als Ziel angeboten")
        check(storage["usb_frei"] > 8 * 1024 ** 3, f"Datenpartition auf dem Stick gewachsen "
                                                    f"({storage['usb_frei'] // 1024 ** 3} GB frei)")

        step("Rooms auf die SSD")
        target = ssd[0]["geraet"]
        status, _ = atrium.call("POST", "/api/v1/admin/speicher", {"ziel": target, "loeschen": True})
        check(status == 409, "ohne Bestätigung wird nichts gelöscht")
        status, _ = atrium.call("POST", "/api/v1/admin/speicher", {"ziel": target, "loeschen": True,
                                                                   "bestaetigt": target})
        check(status == 202, "Umzug gestartet")
        end = time.time() + 600
        while time.time() < end:
            move = atrium.call("GET", "/api/v1/admin/speicher")[1]["vorgang"]
            if move["fertig"] or move["fehler"]:
                break
            time.sleep(3)
        for line in move["schritte"]:
            print("   ·", line)
        check(move["fertig"] and not move["fehler"], f"Umzug fertig {move['fehler']}")
        storage = atrium.call("GET", "/api/v1/admin/speicher")[1]
        check(storage["ort"]["ort"] == "laufwerk" and storage["gesamt"] > 15 * 1024 ** 3,
              f"Rooms liegen auf der SSD ({storage['gesamt'] // 1024 ** 3} GB)")
        check(atrium.call("GET", "/atrium/zustand")[1]["speicher_waehlen"] is False, "Auswahl erledigt")

        step("Neustart")
        vm.power_off()
        vm.start()
        atrium = Atrium()
        state = atrium.wait(vm)
        check(state["eingerichtet"] is True, "Konto ist noch da")
        check("Einrichtungscode" not in vm.screen(), "kein Einrichtungscode mehr auf dem Bildschirm")
        status, _ = atrium.call("POST", "/atrium/anmelden", {"name": "Test", "passwort": "langes-passwort"})
        check(status == 200, "Anmeldung")
        storage = atrium.call("GET", "/api/v1/admin/speicher")[1]
        check(storage["ort"]["ort"] == "laufwerk" and not storage["fehler"], "SSD wieder eingehängt")
        check(storage["gesamt"] > 15 * 1024 ** 3, "Docker und Rooms liegen auf der SSD")
        print("\nAlles in Ordnung.")
    finally:
        vm.kill()
        if os.environ.get("LARES_VM_KEEP") != "1":
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"Arbeitsordner: {work}")


if __name__ == "__main__":
    main()

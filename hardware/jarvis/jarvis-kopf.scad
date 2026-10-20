// Jarvis-Kopf – Gehäuse für Beamer, 45°-Spiegel, IR-Kamera, Raspberry Pi 5 und Mikrofon.
//
// Der Kopf hängt über dem Schreibtisch (Schwenkarm oder Decke). Der Beamer liegt waagerecht im Gehäuse und
// projiziert über einen Oberflächenspiegel senkrecht nach unten auf den Tisch. Neben dem Projektionsfenster sitzen
// die IR-Kamera und zwei IR-Strahler; oben Mikrofon-Array und LED-Ring; hinten der Pi 5 mit KI-Beschleuniger.
//
// Maße in mm. Zuerst die Werte deines Beamers unten eintragen, dann:
//   openscad -o grundplatte.stl -D 'teil="grundplatte"' jarvis-kopf.scad     (siehe README: alle Teile)
//
// Druck: PETG (Wärme vom Beamer!), 0,2 mm, 4 Wände, 25 % Gyroid; Gewindeeinsätze M3.

/* [Teil] */
teil = "zusammen";   // [zusammen, explosion, unten, grundplatte, spiegelbox, rueckteil, deckel_vorne, deckel_mitte, deckel_hinten, kamerahalter, schieber, laserhalter]

/* [Beamer – bitte messen] */
beamer_l = 200;        // Länge (vorne Objektiv bis hinten)
beamer_b = 150;        // Breite
beamer_h = 75;         // Höhe
objektiv_z = 38;       // Mitte Objektiv über Unterkante
objektiv_x = 0;        // Versatz der Objektivmitte zur Mitte (rechts +)
objektiv_d = 34;       // Durchmesser Objektiv

/* [Spiegel] */
spiegel = 100;         // Oberflächenspiegel (quadratisch), Kantenlänge
spiegel_dicke = 3;
spiegel_abstand = 60;  // Objektiv bis Spiegelmitte

/* [Allgemein] */
wand = 3;
luft = 2;              // Spiel um den Beamer
$fn = 64;

innen_b = beamer_b + 2 * luft;
innen_h = beamer_h + 2 * luft + 6;     // + Auflage-Rippen
mitte_l = beamer_l + 2 * luft;
box_l = spiegel_abstand + spiegel * 0.75 + 10;   // Spiegelbox vor dem Objektiv
pi_l = 74;                                       // Rückteil für Pi 5 + KI-HAT
aussen_b = innen_b + 2 * wand;
// Kamera und IR-Strahler vor dem Projektionsfenster, mittig über der Strahlachse
cam_x = (-box_l + wand + (-spiegel_abstand - spiegel * 0.45)) / 2;
cam_y = wand + (beamer_b + 2 * luft) / 2 + objektiv_x;
aussen_h = innen_h + 2 * wand;
gesamt_l = box_l + mitte_l + pi_l + 2 * wand;

// ---------------------------------------------------------------------------------------------
module abgerundet(l, b, h, r = 6) {
    hull() for (x = [r, l - r], y = [r, b - r]) translate([x, y, 0]) cylinder(r = r, h = h);
}

module einsatz_loch() { cylinder(d = 4.2, h = 6); }          // M3-Gewindeeinsatz
module schraub_loch() { cylinder(d = 3.4, h = 40, center = true); }

// Lüftungsschlitze
module schlitze(n, l, b = 3, abstand = 7) {
    for (i = [0 : n - 1]) translate([i * abstand, 0, 0]) cube([b, l, 20]);
}

// ---------------------------------------------------------------------------------------------
// Mittelteil: Wanne für den Beamer (unten offen nur für Luft), Flansche vorne und hinten
module grundplatte() {
    difference() {
        abgerundet(mitte_l + 2 * wand, aussen_b, aussen_h - wand);
        translate([wand, wand, wand]) cube([mitte_l, innen_b, innen_h + 1]);
        // vorne Durchbruch fürs Objektiv
        translate([-1, wand + innen_b / 2 + objektiv_x, wand + luft + 6 + objektiv_z])
            rotate([0, 90, 0]) cylinder(d = objektiv_d + 12, h = wand + 2);
        // hinten Kabel und Luft zum Pi
        translate([mitte_l + wand - 1, wand + 20, wand + 10]) cube([wand + 2, innen_b - 40, innen_h - 20]);
        // Lüftung Boden und Seiten (Beamer hat meist seitliche Lüfter)
        translate([30, wand + 25, -1]) schlitze(floor((mitte_l - 60) / 7), innen_b - 50);
        for (y = [-1, aussen_b - wand - 1])
            translate([25, y, wand + 15]) rotate([0, 0, 0]) cube([mitte_l - 50, wand + 2, innen_h - 30]);
        // Schraublöcher Deckel
        for (x = [12, mitte_l / 2, mitte_l - 8], y = [wand / 2 + 1.5, aussen_b - wand / 2 - 1.5])
            translate([x, y, aussen_h - wand - 6]) einsatz_loch();
    }
    // Auflage-Rippen mit Gummifüßen für den Beamer
    for (x = [wand + 20, wand + mitte_l - 40], y = [wand + 15, wand + innen_b - 35])
        translate([x, y, wand]) difference() {
            cube([20, 20, 6]);
            translate([10, 10, 3]) cylinder(d = 12, h = 4);           // 12-mm-Gummifuß einkleben
        }
    // seitliche Gitter statt offener Fenster
    for (y = [0, aussen_b - wand]) for (x = [25 : 10 : mitte_l - 30])
        translate([x, y, wand + 15]) cube([2, wand, innen_h - 30]);
}

// Spiegelbox: 45°-Spiegel vor dem Objektiv, unten das Projektionsfenster, daneben Kamera und IR-Strahler
module spiegelbox() {
    sp_x = spiegel_abstand;     // Spiegelmitte ab Gehäusevorderkante der Grundplatte (Objektiv ~ dort)
    difference() {
        translate([-box_l, 0, 0]) abgerundet(box_l, aussen_b, aussen_h - wand);
        // Innenraum
        translate([-box_l + wand, wand, wand]) cube([box_l, innen_b, innen_h + 1]);
        // Projektionsfenster unten
        translate([-sp_x - spiegel * 0.45, wand + innen_b / 2 + objektiv_x - spiegel * 0.45, -1])
            cube([spiegel * 0.9, spiegel * 0.9, wand + 2]);
        // Kamerafenster und IR-Strahler (vor dem Projektionsfenster)
        translate([cam_x, cam_y, -1]) cylinder(d = 14, h = wand + 2);
        for (dy = [-24, 24]) translate([cam_x, cam_y + dy, -1]) cylinder(d = 11, h = wand + 2);
        // Schlitz für den Privatsphäre-Schieber (quer, Griff an der Seite)
        translate([cam_x - 11, -1, wand - 1.6]) cube([22, cam_y + 34, 1.8]);
        // Schraublöcher zur Grundplatte
        for (y = [12, aussen_b - 12], z = [12, aussen_h - 20]) translate([-2, y, z]) rotate([0, 90, 0]) schraub_loch();
        // Gewindeeinsätze für den vorderen Deckel
        for (x = [-box_l + 10, -box_l / 2], y = [wand / 2 + 1.5, aussen_b - wand / 2 - 1.5])
            translate([x, y, aussen_h - wand - 6]) einsatz_loch();
    }
    // 45°-Spiegelhalter: zwei Wangen mit Nut, Spiegel wird eingeschoben und mit Silikonpunkten fixiert
    // (auf den Innenraum beschnitten, damit nichts in den Deckel ragt)
    intersection() {
    translate([-box_l + wand, 0, wand]) cube([box_l - wand, aussen_b, innen_h]);
    for (y = [wand, wand + innen_b - 8])
        translate([-sp_x, y, wand + luft + 6 + objektiv_z]) rotate([0, 0, 0])
            difference() {
                rotate([0, 45, 0]) translate([-spiegel / 2 - 4, 0, -6]) cube([spiegel + 8, 8, 12]);
                rotate([0, 45, 0]) translate([-spiegel / 2 - 1, -1, -spiegel_dicke / 2]) cube([spiegel + 2, 10, spiegel_dicke + 0.4]);
            }
    }
}

// Rückteil: Raspberry Pi 5 mit AI-HAT+, Lüfter, Anschlüsse nach hinten
module rueckteil() {
    translate([mitte_l + 2 * wand, 0, 0]) difference() {
        abgerundet(pi_l, aussen_b, aussen_h - wand);
        translate([-1, wand, wand]) cube([pi_l - wand + 1, innen_b, innen_h + 1]);
        // Rückwand: USB/LAN (Pi steht hochkant an der Rückwand), Strom, Beamer-HDMI
        translate([pi_l - wand - 1, wand + 12, wand + 12]) cube([wand + 2, 60, 26]);
        translate([pi_l - wand - 1, wand + 85, wand + 12]) cube([wand + 2, 40, 18]);
        // Lüfter 40 mm im Boden, Abluft oben
        translate([pi_l / 2, aussen_b / 2, -1]) cylinder(d = 38, h = wand + 2);
        for (x = [pi_l / 2 - 16, pi_l / 2 + 16], y = [aussen_b / 2 - 16, aussen_b / 2 + 16])
            translate([x, y, -1]) cylinder(d = 3.4, h = wand + 2);
        for (y = [12, aussen_b - 12], z = [12, aussen_h - 20]) translate([2, y, z]) rotate([0, 90, 0]) schraub_loch();
        // Gewindeeinsätze für den hinteren Deckel
        for (x = [pi_l / 2 - 15, pi_l - 12], y = [wand / 2 + 1.5, aussen_b - wand / 2 - 1.5])
            translate([x, y, aussen_h - wand - 6]) einsatz_loch();
    }
    // Montageplatte für den Pi (Lochabstand 58 × 49)
    translate([mitte_l + 2 * wand + 10, wand + 20, wand]) for (x = [0, 49], y = [0, 58])
        translate([x, y, 0]) difference() { cylinder(d = 7, h = 6); cylinder(d = 2.6, h = 7); }
}

// Deckel: Mikrofon-Array mittig, LED-Ring darum (Diffusor-Nut), Halterung für Arm/Decke
module deckel() {
    ring_d = 68;        // WS2812-Ring 24 LEDs, außen ~66 mm
    mic_d = 70;         // ReSpeaker-Platine
    l = box_l + mitte_l + pi_l + 2 * wand;
    translate([-box_l, 0, aussen_h - wand]) difference() {
        abgerundet(l, aussen_b, wand);
        // (Öffnungen) Mikrofon
        translate([box_l + mitte_l / 2, aussen_b / 2, -1]) for (a = [0 : 45 : 359])
            rotate([0, 0, a]) translate([mic_d / 2 - 8, 0, 0]) cylinder(d = 4, h = wand + 2);
        translate([box_l + mitte_l / 2, aussen_b / 2, -1]) cylinder(d = 10, h = wand + 2);
        // Lichtschlitz für den LED-Ring (mit transluzentem PETG bedrucken oder Diffusorband einkleben)
        translate([box_l + mitte_l / 2, aussen_b / 2, -1]) difference() {
            cylinder(d = ring_d + 6, h = wand + 2);
            cylinder(d = ring_d - 6, h = wand + 2);
        }
        // Abluft über dem Pi und dem Beamer
        translate([box_l + mitte_l + 2 * wand + 10, 20, -1]) schlitze(floor((pi_l - 20) / 7), aussen_b - 40);
        // Befestigung: VESA 75 + mittig 1/4"-Einschlagmutter (Kameragewinde)
        for (dx = [-37.5, 37.5], dy = [-37.5, 37.5])
            translate([box_l + mitte_l / 2 + 70 + dx, aussen_b / 2 + dy, -1]) cylinder(d = 4.5, h = wand + 2);
        translate([box_l + mitte_l / 2 + 70, aussen_b / 2, -1]) cylinder(d = 8, h = wand + 2);
        // Deckelschrauben
        for (x = [10, box_l / 2, box_l + 12, box_l + mitte_l / 2, box_l + mitte_l - 8,
                  box_l + mitte_l + 2 * wand + pi_l / 2 - 15, box_l + mitte_l + 2 * wand + pi_l - 12],
             y = [wand / 2 + 1.5, aussen_b - wand / 2 - 1.5])
            translate([x, y, -1]) cylinder(d = 3.4, h = wand + 2);
    }
    // Ring-Steg für die LED-Platine
    translate([mitte_l / 2, aussen_b / 2, aussen_h - wand - 5]) difference() {
        cylinder(d = ring_d + 10, h = 5);
        cylinder(d = ring_d + 1, h = 6);
    }
}

// Der Deckel in drei druckbaren Stücken (je über Spiegelbox, Beamer, Pi) – Stoß über den Gehäusefugen
module deckel_stueck(von, bis) {
    intersection() {
        deckel();
        translate([von, -1, 0]) cube([bis - von, aussen_b + 2, aussen_h + 10]);
    }
}
module deckel_vorne()  { deckel_stueck(-box_l - 1, 0); }
module deckel_mitte()  { deckel_stueck(0, mitte_l + 2 * wand); }
module deckel_hinten() { deckel_stueck(mitte_l + 2 * wand, mitte_l + 2 * wand + pi_l + 1); }

// Kamerahalter innen über dem Kamerafenster: Pi Camera Module 3 NoIR (25 × 24 mm) + 2 IR-Strahler (Ø 10)
module kamerahalter() {
    translate([cam_x - 15, cam_y - 32, wand]) difference() {
        cube([30, 64, 4]);
        translate([15, 32, -1]) cylinder(d = 14, h = 6);
        for (dy = [-24, 24]) translate([15, 32 + dy, -1]) cylinder(d = 10.4, h = 6);
        // Kameraplatine: Lochbild 21 × 12,5
        for (dx = [-6.25, 6.25], dy = [-10.5, 10.5]) translate([15 + dx, 32 + dy, -1]) cylinder(d = 2.2, h = 6);
    }
}

// Privatsphäre-Schieber: deckt Kamera und IR ab und drückt dabei einen Mikroschalter (Mikro aus)
// Geschlossen deckt er Kamera und beide IR-Strahler ab und drückt dabei einen Mikroschalter (Mikrofon aus);
// offen steht er seitlich heraus (Griff außen).
module schieber(offen = true) {
    translate([cam_x - 10.5, offen ? -cam_y - 5 : -6, wand - 1.5]) union() {
        cube([21, cam_y + 36, 1.4]);
        translate([3, 0, -6]) cube([15, 6, 7.4]);       // Griff
    }
}

// Halter für IR-Linienlaser an der hinteren Tischkante (zweimal drucken): Laser Ø 12 mm, 6 mm über der Platte
module laserhalter() {
    difference() {
        union() {
            cube([40, 30, 4]);                            // Klebe-/Schraubfuß
            translate([20, 15, 0]) cylinder(d = 18, h = 12);
        }
        translate([20, 15, 6]) rotate([0, 90, 0]) translate([0, 0, -25]) cylinder(d = 12.2, h = 50);
        translate([20, 15, 6]) rotate([0, 90, 0]) translate([-1, -12, -25]) cube([20, 24, 50]);  // oben offen
        for (x = [6, 34]) translate([x, 15, -1]) cylinder(d = 3.4, h = 6);
    }
}

// Platzhalter für die Darstellung
module beamer_attrappe() {
    color("dimgray") translate([wand + luft, wand + luft, wand + 6 + luft]) cube([beamer_l, beamer_b, beamer_h]);
    color("black") translate([wand + luft, wand + luft + beamer_b / 2 + objektiv_x, wand + 6 + luft + objektiv_z])
        rotate([0, -90, 0]) cylinder(d = objektiv_d, h = 12);
}
module spiegel_attrappe() {
    color("lightcyan", 0.8) translate([-spiegel_abstand, wand + innen_b / 2 + objektiv_x, wand + luft + 6 + objektiv_z])
        rotate([0, 45, 0]) cube([spiegel, spiegel, spiegel_dicke], center = true);
}
module strahl() {
    color("gold", 0.18) translate([-spiegel_abstand, wand + innen_b / 2 + objektiv_x, wand + luft + 6 + objektiv_z])
        mirror([0, 0, 1]) linear_extrude(height = 260, scale = [3.2, 2.0]) square([spiegel * 0.6, spiegel * 0.6], center = true);
}

module zusammen(e = 0) {
    color("whitesmoke") grundplatte();
    color("whitesmoke") translate([-e, 0, 0]) spiegelbox();
    color("whitesmoke") translate([e, 0, 0]) rueckteil();
    color("gainsboro", 0.9) translate([0, 0, 2 * e]) deckel();
    color("orange") translate([-e, 0, -e / 2]) kamerahalter();
    color("tomato") translate([-e, 0, -e]) schieber();
    beamer_attrappe();
    translate([-e, 0, 0]) spiegel_attrappe();
}

if (teil == "zusammen") { zusammen(); strahl(); }
else if (teil == "explosion") zusammen(60);
else if (teil == "grundplatte") grundplatte();
else if (teil == "spiegelbox") spiegelbox();
else if (teil == "rueckteil") rueckteil();
else if (teil == "deckel_vorne") deckel_vorne();
else if (teil == "deckel_mitte") deckel_mitte();
else if (teil == "deckel_hinten") deckel_hinten();
else if (teil == "unten") { zusammen(); }
else if (teil == "kamerahalter") kamerahalter();
else if (teil == "schieber") schieber();
else if (teil == "laserhalter") laserhalter();

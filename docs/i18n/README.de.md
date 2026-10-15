# Licht.Haus

[English](../../README.md) · **Deutsch**

Ledvance-Volkslicht-Lampen (Bluetooth SIG Mesh) unter Linux steuern und der
Bildschirmhelligkeit folgen lassen, mit einem Schieberegler im
Helligkeits-Applet von KDE Plasma. Ohne Ledvance-App, Konto oder Cloud.

- **Offenlegung KI-Einsatz**
  - Agent: Claude Code mit Claude Opus 5.5 (Anthropic) hat Code und
    Dokumentation geschrieben und untersucht, wie die offizielle App die
    Helligkeit setzt; rund 50 Mio. verarbeitete Tokens (überwiegend
    zwischengespeicherter Kontext), 0,13 Mio. erzeugt.
  - Autor: an den echten Lampen getestet, Funktionen und Regler-Modi festgelegt.

[Lizenz: AGPL-3.0](../../LICENSE) · [Notizen und Erkenntnisse (englisch)](../../dev/takeaways.md) ·
[Fehlermeldungen](https://github.com/Gelaende51/Licht.Haus/issues) ·
[Spenden (CO₂-Ausgleich für die Tokens)](https://ko-fi.com/vault51)

## Funktionen

- **Bildschirm-Kopplung:** Die Lampen folgen der Hintergrundbeleuchtung des
  Laptops (`lichthaus sync`, ein systemd-Benutzerdienst).
- **Regler im Helligkeits-Applet von Plasma:** erscheint als
  *Tastaturhelligkeit* (bei Laptops ohne Tastaturbeleuchtung ist die Zeile
  frei). Vier Modi, gewählt mit `lichthaus mode <Modus>`:

  | Modus | Der Regler bestimmt |
  |---|---|
  | `maximum` (Standard) | die Obergrenze: die Lampen folgen dem Bildschirm zwischen 0 und dem Reglerwert |
  | `minimum` | die Untergrenze: die Lampen folgen dem Bildschirm zwischen Reglerwert und 100 % |
  | `independent` | die Lampen direkt, ohne Bildschirm |
  | `coupling` | die Kurve: oberhalb der Mitte reagieren die Lampen stark bei mittlerer Bildschirmhelligkeit und kaum an den Enden; unterhalb umgekehrt |

  `lichthaus curve <Modus>` zeigt die Tabelle dazu.
- **Tray-Menü** (`lichthaus-tray`): dem Bildschirm *immer*, *nur bei aktivem
  Nachtlicht* oder *nie* folgen (dann steuert allein der Regler die Lampen);
  Regler-Modus; Lampen ein/aus; nach einem manuellen Befehl wieder folgen.
  Auch per `lichthaus track always|night|never`.
- **Bildschirmzustand** (standardmäßig an; Häkchen im Tray,
  `lichthaus screen-state on|off`): solange der Bildschirm an ist, gehen die
  Lampen nie unter 0,5 %, auch bei Bildschirmhelligkeit 0; geht er aus
  (abgedunkelt, Deckel zu, Ruhezustand), gehen sie aus und kommen mit ihm
  zurück. Eine logind-Verzögerungssperre schaltet sie vor dem Ruhezustand ab.
- **Feines Dimmen:** unter 50 % nehmen die Lampen 0,5-%-Schritte an;
  `dim 0.5` liegt zwischen aus und 1 % (feiner nimmt die Firmware nichts an).
- **Befehle:** `lichthaus on | off | dim 40 | status [Lampe]`, `slider 60`,
  `pause | resume`, `list`. Ein manuelles `on`/`off`/`dim` hält die Kopplung
  an, bis `resume` oder der Regler bewegt wird.
- **Lampen übernehmen:** `lichthaus adopt` nimmt eine auf Werkseinstellung
  zurückgesetzte Lampe ins eigene Mesh-Netz des Laptops auf.

## Installation

Benötigt Linux mit BlueZ, Python 3.12+ und für den Regler KDE Plasma 6 mit
UPower.

```sh
git clone https://github.com/Gelaende51/Licht.Haus && cd Licht.Haus
sudo install/setup-root.sh   # einmalig: uleds-Treiber + Zugriff für den Regler
install/install.sh           # venv, Befehle `lichthaus` + `lichthaus-tray`, Dienst, Autostart
```

Das Tray braucht PyQt6 aus der Distribution (`python-pyqt6` unter Arch).

**Lampe übernehmen:** auf Werkseinstellung zurücksetzen (fünfmal je 5 s ein-
und ausschalten, dann eingeschaltet lassen; sie blinkt zur Bestätigung).
Innerhalb von 8 Minuten `lichthaus adopt` ausführen, danach
`systemctl --user enable --now lichthaus`. Eine hier übernommene Lampe ist
nicht mehr in der Ledvance-App. `lichthaus.json` enthält die Mesh-Schlüssel:
sichern, ohne sie lassen sich die Lampen nur noch zurücksetzen.

## Funktioniert mit

- KDE Plasma 6 (Helligkeits-Applet, PowerDevil); ohne Plasma funktioniert
  alles außer dem Regler.
- BlueZ muss Low Energy scannen: `ControllerMode = dual` (oder `le`) in
  `/etc/bluetooth/main.conf`.
- Verträgt sich nicht mit anderer Software, die eine echte oder nachgebildete
  Tastaturbeleuchtung über UPower steuert.

## Ähnliche Projekte

- [ha-bluetooth-mesh](https://github.com/dasimon135/ha-bluetooth-mesh):
  Home-Assistant-Integration auf demselben Mesh-Stack (btmesh, MIT; dieses
  Projekt nutzt dessen Provisionierungs-Skript, siehe
  [LICENSE-btmesh](../../LICENSE-btmesh)). Kennt die 0–100-Helligkeitsskala
  des Volkslichts noch nicht und hat keine Desktop-Anbindung.
- [LEDVANCE_BLE_MESH](https://github.com/Ultraworg/LEDVANCE_BLE_MESH):
  ESP32-Gateway für Ledvance-Mesh-Lampen, gesteuert aus Home Assistant;
  braucht zusätzliche Hardware.
- BlueZ `bluetooth-meshd` / `mesh-cfgclient`: der offizielle Linux-Mesh-Stack;
  belegt den Bluetooth-Adapter exklusiv.

# Haus.Licht

[English](../../README.md) · **Deutsch**

Ledvance-Volkslicht-Lampen (Bluetooth SIG Mesh) unter Linux steuern und der
Bildschirmhelligkeit folgen lassen, mit einem Schieberegler im
Helligkeits-Applet von KDE Plasma. Ohne Ledvance-App, Konto oder Cloud.

Abgespalten von [Licht.Haus](https://github.com/Gelaende51/Licht.Haus).

- **Offenlegung KI-Einsatz**
  - Agent: Claude Code mit Claude Opus 5.5 (Anthropic) hat Code und
    Dokumentation geschrieben und untersucht, wie die offizielle App die
    Helligkeit setzt; rund 50 Mio. verarbeitete Tokens (überwiegend
    zwischengespeicherter Kontext), 0,13 Mio. erzeugt.
  - Autor: an den echten Lampen getestet, Funktionen und Regler-Modi festgelegt.

[Lizenz: AGPL-3.0](../../LICENSE) · [Notizen und Erkenntnisse (englisch)](../../dev/takeaways.md) ·
[Fehlermeldungen](https://github.com/Gelaende51/Haus.Licht/issues) ·
[Spenden (CO₂-Ausgleich für die Tokens)](https://ko-fi.com/vault51)

## Funktionen

- **Bildschirm-Kopplung:** Die Lampen folgen der Hintergrundbeleuchtung des
  Laptops (`hauslicht sync`, ein systemd-Benutzerdienst).
- **Regler im Helligkeits-Applet von Plasma:** erscheint als
  *Tastaturhelligkeit* (bei Laptops ohne Tastaturbeleuchtung ist die Zeile
  frei). Vier Modi, gewählt mit `hauslicht mode <Modus>`:

  | Modus | Der Regler bestimmt |
  |---|---|
  | `maximum` (Standard) | die Obergrenze: die Lampen folgen dem Bildschirm zwischen 0 und dem Reglerwert |
  | `minimum` | die Untergrenze: die Lampen folgen dem Bildschirm zwischen Reglerwert und 100 % |
  | `independent` | die Lampen direkt, ohne Bildschirm |
  | `coupling` | die Kurve: oberhalb der Mitte reagieren die Lampen stark bei mittlerer Bildschirmhelligkeit und kaum an den Enden; unterhalb umgekehrt |

- **Tray-Widget** (Plasma): Linksklick öffnet es, Mittelklick schaltet die
  Lampen ein/aus. Ein **Bildschirmfenster** mit zwei Griffen auf einer Skala
  von −100…200 % (0…100 ist der Bildschirm): Bildschirmhelligkeit am linken
  Griff heißt aus, am rechten voll; −100…200 drückt die Lampen auf 33…67 %,
  50…100 lässt sie aus, bis der Bildschirm 50 % überschreitet. Ein
  **Kurven**-Regler (0 linear, positiv steigt langsam für feine Stufen im
  Dunklen). Untermenüs für den Tastaturregler (auch *aus*), das Folgen
  (*immer*, *nur bei aktivem Nachtlicht*, *nie*) und **Voreinstellungen**
  (speichern, anwenden, löschen). `hauslicht-ctl get | set key=value |
  preset save|apply|delete NAME` erledigt dasselbe aus Skripten.
- **Tastaturregler als Fernbedienung:** der Modus legt fest, welches
  Widget-Element der Regler *Tastaturhelligkeit* bewegt: rechten
  Fenstergriff (`maximum`, Standard), linken Griff (`minimum`), Kurve
  (`coupling`), Helligkeit (`independent`, Bildschirm egal) oder nichts
  (`off`). Regler hoch heißt immer hellere Lampen, Griffe und Kurve wandern
  dabei nach links.
- **Helligkeitsregler** im Widget zeigt, was die Lampen zeigen; er setzt den
  Wert, wenn der Bildschirm keine Rolle spielt, sonst gilt er als manuelle
  Einstellung bis *Wieder folgen*. Die Kopfzeile zeigt die Verbindung zur Lampe.
- **Wandschalter:** Licht kurz aus- und wieder einschalten schaltet die
  Lampen um; nach längerem Aus (über ~20 s) gehen sie an. Der Schalter muss
  so lange aus sein, dass die Lampe neu startet (ihr Netzteil überbrückt
  kurze Lücken); der Dienst erkennt ihre erste Meldung außerhalb des
  10-s-Takts und verbindet sofort neu. Außerdem hält der Dienst das
  Einschaltverhalten der Lampe (Generic OnPowerUp) auf dem umgeschalteten
  Zustand (dunkel, solange sie leuchtet, an, solange sie dunkel ist), sodass
  sie nach dem Schalten gleich so startet; ist der Dienst beendet oder der
  Laptop im Ruhezustand, geht sie mit dem Wandschalter wie gewohnt an.
  `install/setup-root.sh` richtet ein kleines Root-Hilfsprogramm ein, das die BLE-Verbindungs-Zeitüberschreitung
  auf 1 s verkürzt; das Umschalten folgt etwa 1,5-5 s nach dem Schalten.
- **Bei 0 % ausschalten** (standardmäßig aus; `hauslicht off-at-zero on|off`):
  eingeschaltet gehen die Lampen in jedem Modus nie unter 0,5 %, auch bei
  Bildschirmhelligkeit 0 oder Stufe 0; ist es an, bleiben sie bei berechneten
  0 % dunkel. Ein abgedunkelter oder ausgeschalteter Bildschirm ändert nichts
  an den Lampen: sie behalten die zuletzt bei eingeschaltetem Bildschirm
  gelesene Helligkeit.
- **Deckel** (`hauslicht lid close|open|both|never`, Standard `both`):
  Zuklappen schaltet die Lampen aus, Aufklappen ein, wie der Mittelklick;
  dazwischen gelten Ein/Aus wie gewohnt. Bei `close` oder `both` schaltet eine
  logind-Verzögerungssperre sie außerdem vor dem Ruhezustand ab.
- **Feines Dimmen:** unter 50 % nehmen die Lampen 0,5-%-Schritte an;
  `dim 0.5` liegt zwischen aus und 1 % (feiner nimmt die Firmware nichts an).
- **Befehle:** `hauslicht on | off | dim 40 | status [Lampe]`, `slider 60`,
  `nudge 5 | nudge -5` (Scrollen auf dem Leistensymbol: verschiebt den Regler,
  im Modus `off` die Helligkeit), `pause | resume`, `list`. Ein manuelles `on`/`off`/`dim` hält die Kopplung
  an, bis `resume` oder der Regler bewegt wird.
- **Lampen übernehmen:** `hauslicht adopt` nimmt eine auf Werkseinstellung
  zurückgesetzte Lampe ins eigene Mesh-Netz des Laptops auf.

## Installation

Benötigt Linux mit BlueZ, Python 3.12+ und für den Regler KDE Plasma 6 mit
UPower.

```sh
git clone https://github.com/Gelaende51/Haus.Licht && cd Haus.Licht
sudo install/setup-root.sh   # einmalig: uleds-Treiber + Zugriff für den Regler
install/install.sh           # venv, Befehl `hauslicht`, Dienst, Tray-Widget
```

**Lampe übernehmen:** auf Werkseinstellung zurücksetzen (fünfmal je 5 s ein-
und ausschalten, dann eingeschaltet lassen; sie blinkt zur Bestätigung).
Innerhalb von 8 Minuten `hauslicht adopt` ausführen, danach
`systemctl --user enable --now hauslicht`. Eine hier übernommene Lampe ist
nicht mehr in der Ledvance-App. `hauslicht.json` enthält die Mesh-Schlüssel:
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

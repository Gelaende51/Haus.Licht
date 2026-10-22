# Haus.Licht

[English](../../README.md) · **Deutsch**

Smarte Lampen unter Linux der Bildschirmhelligkeit des Laptops folgen lassen,
mit einem Tray-Widget und einem Regler im Helligkeits-Applet von KDE Plasma.
Gemacht für Ledvance-Volkslicht-Lampen (Bluetooth SIG Mesh), mit
experimentellen Backends für viele weitere Arten. Nur lokal: ohne
Hersteller-App, Konto oder Cloud.

Abgespalten von [Licht.Haus](https://github.com/Gelaende51/Licht.Haus).

- **Offenlegung KI-Einsatz**
  - Agent: Claude Code mit Claude Opus 5.5 (Anthropic) hat Code und
    Dokumentation geschrieben und untersucht, wie die offizielle App die
    Helligkeit setzt; rund 52,5 Mio. verarbeitete Tokens (überwiegend
    zwischengespeicherter Kontext; 2,5 Mio. davon für die Runde mit mehreren
    Lampen und ihren Backends), 0,13 Mio. erzeugt vor dieser Runde.
  - Autor: an den echten Lampen getestet, Funktionen und Regler-Modi festgelegt.

[Lizenz: AGPL-3.0](../../LICENSE) · [Notizen und Erkenntnisse (englisch)](../../dev/takeaways.md) ·
[Fehlermeldungen](https://github.com/Gelaende51/Haus.Licht/issues) ·
[Spenden (CO₂-Ausgleich für die Tokens)](https://ko-fi.com/vault51)

## Funktionen

- **Mehrere Lampen, mehrere Arten:** jede Lampe hat ihren eigenen
  Ein/Aus-Zustand (bleibt über Neustarts erhalten) und ihr eigenes **Profil**
  (Bildschirmfenster, Kurve, Helligkeit, Regel fürs Folgen). Lampen und Profile
  werden im **Einrichten**-Dialog des Widgets verwaltet (Seiten *Lampen*,
  *Profile*); die Kopfzeile des Popups wählt, auf welche Lampe (oder *Alle
  Lampen*) die Regler wirken.
- **Folgt dem Bildschirm:** die Lampen folgen der Hintergrundbeleuchtung
  (`hauslicht sync`, ein systemd-Benutzerdienst): *immer*, nur mit Nachtlicht,
  nur bei Tageslicht, nur im dunklen Farbschema, nur im hellen Farbschema oder
  *nie*.
- **Tray-Widget** (Plasma 6): ein **Bildschirmfenster** mit zwei Griffen
  (Bildschirmhelligkeit am linken Griff heißt aus, am rechten voll), eine
  **Kurve**, ein Helligkeitsregler (manuelle Einstellung, bis das
  Monitorsymbol daneben angeklickt wird), Schalter **Klatschen**, **Flick**
  und **An/Aus**; die Kopfzeile der Systemleiste zeigt den Verbindungsstatus
  und **Manuell** / **Automatisch** (aktuelle Helligkeit halten / dem
  Bildschirm folgen; auf der Arbeitsfläche stehen sie in der Kopfzeile des
  Popups). Mittelklick auf das
  Symbol schaltet ein/aus, das Mausrad macht heller/dunkler.
- **Tastaturregler als Fernbedienung:** der Regler *Tastaturhelligkeit* von
  Plasma bewegt ein Widget-Element nach Wahl (rechter oder linker Griff, Kurve,
  Helligkeit oder nichts); braucht einmalig `sudo install/setup-root.sh`.
- **Bei 0 % ausschalten** (wählbar): sonst bleibt bei 0 % ein Glimmen von 0,5 %.
- **Deckel:** Zuklappen schaltet die Lampen aus, Aufklappen ein (oder nur eins
  davon, oder nie); aus vor dem Ruhezustand.
- **Flick** (Mesh-Lampen; Schalter *Flick*): Wandschalter kurz aus und wieder
  an schaltet das Licht sofort um, weil der Dienst das Einschaltverhalten der
  Lampe auf dem umgeschalteten Zustand hält.
  - Vorteil: der Wandschalter schaltet um, auch während der Laptop die Lampe
    steuert; die Lampe startet gleich im neuen Zustand.
  - Nachteil: kürzere Unterbrechungen als etwa 1–5 s überbrückt ihr Netzteil,
    sie bleiben unbemerkt; nach einem langen Stromausfall bei eingeschaltetem
    Licht bleibt es dunkel, bis der Laptop es einschaltet; pro Umschalten ein
    kleiner Schreibvorgang in die Einstellungen der Lampe. Aus: der
    Wandschalter wirkt wie ein normaler Schalter.
- **Klatschen schaltet** (wählbar, anfangs aus; Schalter *Klatschen*):
  doppeltes Klatschen oder Fingerschnippen schaltet das Licht (die Auswahl
  jedes Widgets, dessen Schalter an ist und dessen Modus passt, wie das
  Tastenkürzel). Modus (Klatschen, Schnippen oder beides) je Widget, Mikrofon,
  Empfindlichkeit und eine Testanzeige: *Einrichten* → *Ton*. Ein sparsamer Pegelfilter hört zu; nur ein scharfes
  Geräusch prüft ein kleiner Klangklassifikator (YAMNet) auf diesem Computer.
  Nichts wird gespeichert oder verschickt; Geräusche aus den Lautsprechern
  zählen nicht. Braucht `requirements-clap.txt`.
- **Skripte:** `hauslicht on | off | dim 40 | status | slider 60 | nudge 5 |
  select ID | discover | add | remove` und `hauslicht-ctl` mit JSON
  ([Protokoll](../ctl-protocol.md)).

## Unterstützte Lampen

| Art | Bibliothek | Status | Was man zum Hinzufügen eingibt |
|---|---|---|---|
| Ledvance Volkslicht, SIG-Mesh-Lampen (`mesh`) | btmesh, bleak | **an echter Hardware getestet** | nichts: `hauslicht adopt` |
| Philips Hue Bridge (`hue`) | aiohttp | experimentell (gegen Nachbildungen getestet) | Bridge-Adresse (vorher Link-Taste drücken), Lampe |
| Philips Hue Bluetooth (`huebt`) | HueBLE | experimentell (gegen Nachbildungen getestet) | Bluetooth-Adresse (Suche) |
| Home Assistant (`homeassistant`) | aiohttp | experimentell (gegen Nachbildungen getestet) | URL, Zugriffstoken, Lampen-Entität |
| MQTT / zigbee2mqtt (`mqtt`) | aiomqtt | experimentell (gegen Nachbildungen getestet) | Broker-Adresse, Gerät oder Befehls-Topic |
| Matter-Server (`matter`) | aiohttp | experimentell (gegen Nachbildungen getestet) | Server-URL, Knoten |
| WiZ (`wiz`) | pywizlight | experimentell (gegen Nachbildungen getestet) | Adresse (Suche) |
| LIFX (`lifx`) | – (eigener LAN-Code) | experimentell (gegen Nachbildungen getestet) | Adresse (Suche) |
| Yeelight (`yeelight`) | – (eigener LAN-Code) | experimentell (gegen Nachbildungen getestet) | Adresse (Suche; LAN-Steuerung einschalten) |
| Govee LAN-API (`govee`) | – (eigener LAN-Code) | experimentell (gegen Nachbildungen getestet) | Adresse (Suche; LAN-Steuerung einschalten) |
| TP-Link Kasa / Tapo (`kasa`) | python-kasa | experimentell (gegen Nachbildungen getestet) | Adresse (Suche); Tapo: E-Mail und Passwort des TP-Link-Kontos |
| Tuya / Smart Life (`tuya`) | tinytuya | experimentell (gegen Nachbildungen getestet) | Adresse, Geräte-ID, Local Key |
| Shelly (`shelly`) | aiohttp | experimentell (gegen Nachbildungen getestet) | Adresse (Suche), Passwort falls gesetzt |
| Tasmota (`tasmota`) | aiohttp | experimentell (gegen Nachbildungen getestet) | Adresse (Suche), Benutzer/Passwort falls gesetzt |
| ESPHome (`esphome`) | aioesphomeapi | experimentell (gegen Nachbildungen getestet) | Adresse, Verschlüsselungsschlüssel |
| Triones / LEDBlue BLE (`ledble`) | led-ble | experimentell (gegen Nachbildungen getestet) | Bluetooth-Adresse (Suche) |
| Linux-LED-Klasse, `/sys/class/leds` (`sysled`) | – (sysfs, logind) | experimentell (gegen Nachbildungen getestet) | LED-Name (Suche; zeigt ihren Trigger) |
| USB-Statuslichter: BlinkStick, Luxafor, blink(1), Kuando, Embrava, MuteMe … (`usblight`) | busylight-core | experimentell (gegen Nachbildungen getestet) | Gerät (Suche; Zugriff: Systemeinrichtung) |
| Serielle LED-Controller: Adalight-Sketches, WLED über USB (`serial`) | pyserial | experimentell (gegen Nachbildungen getestet) | Schnittstelle (die Suche listet, öffnet nie), Protokoll, Baudrate, Anzahl LEDs |
| Generisches Licht: Befehl ohne Shell, HTTP, MQTT, seriell oder UDP per Vorlage (`generic`) | – (aiohttp, aiomqtt oder pyserial je nach Übertragung) | experimentell (gegen Nachbildungen getestet) | Übertragung, ihr Ziel, Vorlagen für Ein, Aus, Helligkeit, Farbe, Weiß (Platzhalter wie `{percent}`, `{level}`, `{r}`) |
| WLED im Netzwerk (`wled`) | aiohttp, zeroconf | experimentell (gegen Nachbildungen getestet) | Adresse (Suche) |
| DMX-Scheinwerfer: Art-Net, sACN, OLA, Enttec DMX USB Pro / Open DMX (`dmx`) | sacn, pyserial, aiohttp | experimentell (gegen Nachbildungen getestet) | Ausgang, Host oder Schnittstelle (Suche: Art-Net-Knoten, OLA, Enttec-Schnittstellen), Universum, Startadresse, Kanalbelegung (z. B. `dimmer,r,g,b,w`) |

Wie jede Art funktioniert und was sie kann: [Backend-API](../backend-api.md),
[Recherche-Notizen](../backends-research.md) (englisch).

## Installation

Benötigt Linux mit BlueZ, Python 3.12+ und für Widget und Regler KDE Plasma 6
mit UPower.

```sh
git clone https://github.com/Gelaende51/Haus.Licht && cd Haus.Licht
sudo install/setup-root.sh             # einmalig: Tastaturregler, 1 s Bluetooth-Zeitüberschreitung
install/install.sh                     # venv, Befehle, Dienst, Tray-Widget (nur Mesh)
install/install.sh --backend wiz,hue   # dazu die Bibliotheken dieser Arten
install/install.sh --all-backends      # dazu die Bibliotheken aller Arten
systemctl --user enable --now hauslicht
```

`install/install.sh --dry-run …` zeigt, was installiert würde;
`hauslicht backends --requires ART` listet die pip-Pakete einer Art.
Tests: `pip install -r requirements-dev.txt` (siehe dort den Hinweis zu amqtt).
Klatschen schaltet: `.venv/bin/pip install -r requirements-clap.txt` (numpy,
ai-edge-litert; das 4-MB-Klangmodell wird beim ersten Einschalten geladen).

## Lampen hinzufügen

- **Die meisten Arten:** Widget → *Einrichten* → *Lampen* → *Lampe
  hinzufügen*: Art wählen, *Netzwerk durchsuchen* oder die Felder aus der
  Tabelle ausfüllen, *Hinzufügen*. Im Terminal: `hauslicht discover [ART]`,
  `hauslicht add ART schlüssel=wert…`.
- **Ledvance Volkslicht (Mesh):** Lampe auf Werkseinstellung zurücksetzen
  (fünfmal je 5 s ein- und ausschalten, dann eingeschaltet lassen; sie
  blinkt). Innerhalb von 8 Minuten `hauslicht adopt` ausführen. Die Lampe ist
  danach nicht mehr in der Ledvance-App. `hauslicht.json` enthält die
  Mesh-Schlüssel: sichern, ohne sie lassen sich die Lampen nur noch
  zurücksetzen.

## Funktioniert mit

- KDE Plasma 6 (Helligkeits-Applet, PowerDevil, Nachtlicht, Farbschema); ohne
  Plasma funktioniert alles außer Widget und Regler.
- Home Assistant, zigbee2mqtt, Matter-Server und Hue Bridges als Lampenquellen.
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

## Drittanbieter

- **YAMNet**-Klangklassifikator (`yamnet.tflite` aus MediaPipes Audio
  Classifier, aus tensorflow/models): Copyright Google LLC, Apache License
  2.0 (https://www.apache.org/licenses/LICENSE-2.0). Nicht hier enthalten:
  beim ersten Einschalten von *Klatschen schaltet* von einer festen Adresse
  geladen und gegen seine sha256 geprüft (`haus/clap.py`).
- Ausgeführt mit **LiteRT** (`ai-edge-litert`, Apache-2.0) und **NumPy** (BSD-3-Clause).

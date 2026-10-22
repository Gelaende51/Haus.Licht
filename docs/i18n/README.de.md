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
    Lampen und ihren Backends), 0,13 Mio. erzeugt vor dieser Runde
    (Stand 16.10.2026).
  - Autor: an den echten Lampen getestet, Funktionen und Regler-Modi festgelegt.

[Lizenz: AGPL-3.0](../../LICENSE) · [Notizen und Erkenntnisse (englisch)](../../dev/takeaways.md) ·
[Fehlermeldungen](https://github.com/Gelaende51/Haus.Licht/issues) ·
[Spenden (CO₂-Ausgleich für die Tokens)](https://ko-fi.com/vault51)

## Funktionen

**Datenschutz:** Mikrofon, Bildschirm und Benachrichtigungen werden nur auf
diesem Computer gelesen; nichts wird gespeichert oder verschickt, und
Benachrichtigungstexte werden nie protokolliert.

### Dem Bildschirm folgen

- Die Lampen folgen der Bildschirmhelligkeit (`hauslicht sync`, ein
  systemd-Benutzerdienst): *immer*, nur mit Nachtlicht, bei Tageslicht, im
  dunklen Farbschema, im hellen Farbschema oder *nie*.
- **Bildschirmfenster** (zwei Griffe: Bildschirmhelligkeit am linken heißt
  aus, am rechten voll) und **Kurve** bestimmen, wie die Lampen folgen;
  Farblampen können der Rotverschiebung des Nachtlichts folgen.
- **Mehrere Lampen, mehrere Arten:** jede Lampe hat ihren eigenen
  Ein/Aus-Zustand und ihr eigenes **Profil** (Fenster, Kurve, Helligkeit,
  Farbe, Regel fürs Folgen). Einrichten → *Lampen* / *Profile*; die Kopfzeile
  des Popups wählt die Lampe (oder *Alle Lampen*).
- **Manuell / Automatisch** in der Kopfzeile der Systemleiste: die aktuelle
  Helligkeit halten oder dem Bildschirm folgen. Wer den Helligkeitsregler
  bewegt, hält eine manuelle Helligkeit, bis das Monitorsymbol daneben
  angeklickt wird.
- **Tastaturregler als Fernbedienung:** der Regler *Tastaturhelligkeit* von
  Plasma bewegt ein Widget-Element nach Wahl (einen Fenstergriff, die Kurve,
  die Helligkeit); braucht die Systemeinrichtung.
- **Bei 0 % ausschalten** (wählbar): sonst bleibt ein Glimmen von 0,5 %.

### Wandschalter, Deckel und Strom

- **Umschalten per Wandschalter** (Mesh-Lampen; Schalter *Schalter*, Modus in
  Einrichten → *Lampen*): Wandschalter kurz aus und wieder an schaltet das
  Licht um, weil der Dienst das Einschaltverhalten der Lampe auf dem
  umgeschalteten Zustand hält.
  - *Aus*: ein normaler Schalter. *An*: Umschalten per Wandschalter.
    *Schnell*: wie An, mit Bluetooth auf schnelles Wiederverbinden
    eingestellt. *Exklusiv*: wie Schnell, ohne Bluetooth-Verbindung;
    Haus.Licht nimmt einen Bluetooth-Adapter für sich (andere
    Bluetooth-Geräte daran funktionieren dann nicht) und macht die Lampe
    schneller wieder scharf.
  - Schnell und Exklusiv brauchen die **Systemeinrichtung**
    (`sudo install/setup-root.sh`, oder das Widget fragt nach dem
    Administratorpasswort): sie lädt den Treiber für den Tastaturregler,
    erlaubt den Zugriff auf USB- und serielle Lichter, stellt BlueZ auf
    schnelles Wiederverbinden ein und installiert die Helfer für die kurze
    Verbindungs-Zeitüberschreitung und den exklusiven Adapter.
  - Den Schalter etwa 1,5 s oder länger aus lassen: eine kürzere
    Unterbrechung kann das Netzteil der Lampe überbrücken. Bis etwa 10 s aus
    gilt als Umschalten; nach einem längeren Stromausfall kommt das Licht
    eingeschaltet zurück.
- **Ruhezustand und Abstürze sind sicher:** vor dem Ruhezustand und beim
  Beenden wird das Einschaltverhalten der Lampen wieder auf „an“ gesetzt, der
  Wandschalter wirkt dann wie ein normaler Schalter, solange der Laptop weg
  ist. Nach einem Absturz oder Abbruch setzt die Dienst-Unit die Lampen
  zurück, die noch scharf sein könnten (`hauslicht disarm --if-armed`).
- **Laptop-Deckel:** Zuklappen schaltet die Lampen aus, Aufklappen ein (oder
  nur eins davon, oder nie); aus vor dem Ruhezustand.
- **Monitorschalter folgen** (nur Desktop-PCs, wo es erkennbar ist): das Licht
  geht aus, wenn jeder Monitor ausgeschaltet wird (DisplayPort getrennt oder
  per DDC/CI ausgeschaltet), und wieder an, wenn er zurückkommt. Standby und
  Bildschirmabschaltung machen den Raum nie dunkel.

### Ton, Video und Benachrichtigungen

- **Klatschen schaltet** (anfangs aus; Schalter *Klatschen*): doppeltes
  Klatschen oder Fingerschnippen schaltet die Auswahl des Widgets um. Modus
  (Klatschen, Schnippen, beides) je Widget; Mikrofon, Empfindlichkeit,
  Testanzeige und letzte Geräusche in Einrichten → *Ton*. Ein Pegelfilter hört
  zu; nur ein scharfes Geräusch geht an einen kleinen Klangklassifikator
  (YAMNet) auf diesem Computer; Geräusche aus den Lautsprechern zählen nicht.
- **Audio folgen** (je Lampe, Einrichten → *Ton*): Lautstärke wird
  Helligkeit im Bereich des Profils, Tonhöhe wird Farbe bei Farblampen. Hört
  auf das, was der Computer abspielt (oder eine beliebige Quelle).
- **Video folgen** (je Lampe, Einrichten → *Video*): jede Lampe folgt einem
  **Bildschirmbereich** (ganz, eine Hälfte oder ein Viertel) bis zur
  **maximalen Helligkeit**, bei Farblampen mit Farbe, mit einstellbarer
  **zeitlicher Mittelung**. Der Bildschirm wird über die
  Bildschirmfreigabe des Desktops gelesen, einmal erfragt.
- Beide höchstens 1–3 Aktualisierungen pro Sekunde und Lampe (Standard 2),
  nie schneller: schnelles Blinken ist ein Risiko bei Lichtempfindlichkeit.
- **Bei Benachrichtigungen blinken** (Einrichten → *Benachrichtigungen*):
  Regeln je Profil prüfen App, Titel oder Text (Regex) und lassen blinken,
  pulsieren oder doppelt blinken, mit Anzahl, Stärke und Farbe; nur
  eingeschaltete Lampen, wenn nicht anders erlaubt. „Nicht stören“ wird
  beachtet.

### Widget und Werkzeuge

- **Tray-Widget** (Plasma 6) mit den Schaltern **Klatschen**, **Schalter** und
  **An/Aus**; Mittelklick schaltet um, das Mausrad dimmt (Scrollrichtung
  umkehrbar). Weitere Widgets für die Kontrollleiste, jedes mit eigenen
  Lampen.
- **Symbolsätze:** sechs Sätze für Tray- und Lampensymbole, mit Vorschau in
  einer Auswahlliste unter Einrichten → *Lampen*, passend zum
  Plasma-Farbschema; Quellen und Lizenzen in
  [plasmoid/haus.licht.tray/LICENSES.md](../../plasmoid/haus.licht.tray/LICENSES.md).
- **Identifizieren und Testen** (Einrichten → *Lampen*, oder
  `hauslicht identify` / `test`): Identifizieren lässt eine Lampe langsam
  blinken, um sie zu finden (aus nach 2 Minuten). Testen zeigt am hellsten,
  ein Abblenden, Farben und An/Aus von schnell nach langsam.
  **Warnung:** Testen blitzt schnell; nicht für Menschen mit fotosensibler
  Epilepsie.
- **Kommandozeile:** alles, was das Widget kann, ist auch ein
  `hauslicht`-Befehl (`hauslicht --help` gruppiert sie), z. B.
  `hauslicht on | off | dim 40 | status | flick exclusive | video on`;
  `hauslicht-ctl` spricht JSON ([Protokoll](../ctl-protocol.md)).
  `man hauslicht` und `man hauslicht-ctl` (auch deutsch) nach
  `install/install.sh`.
- **Bei jedem Push getestet** (Python 3.12–3.14, Handbuchseiten, QML-Lint),
  wöchentlich in fünf Distributionen und mit dem Widget in Plasma 6:
  [docs/ci.md](../ci.md) (englisch).

## Unterstützte Lampen

Status: die Art `mesh` ist **an echter Hardware getestet**; alle anderen Arten
sind experimentell (gegen Nachbildungen getestet). Farbe: *hs* =
Farbton/Sättigung, *Weiß* = Farbtemperatur, jeweils soweit das Gerät sie
meldet.

**Bluetooth**

| Art | Bibliothek | Farbe | Was man zum Hinzufügen eingibt |
|---|---|---|---|
| Ledvance Volkslicht, SIG-Mesh-Lampen (`mesh`) | btmesh, bleak | hs, Weiß, wo die Lampe sie hat | nichts: `hauslicht adopt` |
| Philips Hue Bluetooth (`huebt`) | HueBLE | hs, Weiß | Bluetooth-Adresse (Suche) |
| Triones / LEDBlue BLE (`ledble`) | led-ble | hs | Bluetooth-Adresse (Suche) |

**Netzwerk**

| Art | Bibliothek | Farbe | Was man zum Hinzufügen eingibt |
|---|---|---|---|
| Philips Hue Bridge (`hue`) | aiohttp | hs, Weiß | Bridge-Adresse (vorher Link-Taste drücken), Lampe |
| Home Assistant (`homeassistant`) | aiohttp | hs, Weiß | URL, Zugriffstoken, Lampen-Entität |
| MQTT / zigbee2mqtt (`mqtt`) | aiomqtt | hs, Weiß (nur zigbee2mqtt) | Broker-Adresse, Gerät oder Befehls-Topic |
| Matter-Server (`matter`) | aiohttp | hs, Weiß | Server-URL, Knoten |
| WiZ (`wiz`) | pywizlight | hs, Weiß | Adresse (Suche) |
| LIFX (`lifx`) | – (eigener LAN-Code) | hs, Weiß | Adresse (Suche) |
| Yeelight (`yeelight`) | – (eigener LAN-Code) | hs, Weiß | Adresse (Suche; LAN-Steuerung einschalten) |
| Govee LAN-API (`govee`) | – (eigener LAN-Code) | hs, Weiß | Adresse (Suche; LAN-Steuerung einschalten) |
| TP-Link Kasa / Tapo (`kasa`) | python-kasa | hs, Weiß | Adresse (Suche); Tapo: E-Mail und Passwort des TP-Link-Kontos |
| Tuya / Smart Life (`tuya`) | tinytuya | hs, Weiß | Adresse, Geräte-ID, Local Key |
| Shelly (`shelly`) | aiohttp | hs, Weiß (Gen1-Farblampen), Weiß (Duo); Gen2+ nur Helligkeit | Adresse (Suche), Passwort falls gesetzt |
| Tasmota (`tasmota`) | aiohttp | hs, Weiß | Adresse (Suche), Benutzer/Passwort falls gesetzt |
| ESPHome (`esphome`) | aioesphomeapi | hs, Weiß | Adresse, Verschlüsselungsschlüssel |
| WLED (`wled`) | aiohttp, zeroconf | hs; Weiß bei CCT-/RGBW-Streifen | Adresse (Suche) |
| DMX-Scheinwerfer: Art-Net, sACN, OLA, Enttec DMX USB Pro / Open DMX (`dmx`) | sacn, pyserial, aiohttp | nach Kanalbelegung | Ausgang, Host oder Schnittstelle (Suche: Art-Net-Knoten, OLA, Enttec-Schnittstellen), Universum, Startadresse, Kanalbelegung (z. B. `dimmer,r,g,b,w`) |

**Lokal**

| Art | Bibliothek | Farbe | Was man zum Hinzufügen eingibt |
|---|---|---|---|
| Linux-LED-Klasse, `/sys/class/leds` (`sysled`) | – (sysfs, logind) | hs bei mehrfarbigen LEDs | LED-Name (Suche; zeigt ihren Trigger) |
| USB-Statuslichter: BlinkStick, Luxafor, blink(1), Kuando, Embrava, MuteMe … (`usblight`) | busylight-core | hs | Gerät (Suche; Zugriff: Systemeinrichtung) |
| Serielle LED-Controller: Adalight-Sketches, WLED über USB (`serial`) | pyserial | hs | Schnittstelle (die Suche listet, öffnet nie), Protokoll, Baudrate, Anzahl LEDs |

**Generisch**

| Art | Bibliothek | Farbe | Was man zum Hinzufügen eingibt |
|---|---|---|---|
| Generisches Licht: Befehl ohne Shell, HTTP, MQTT, seriell oder UDP per Vorlage (`generic`) | – (aiohttp, aiomqtt oder pyserial je nach Übertragung) | wie die Vorlagen es festlegen | Übertragung, ihr Ziel, Vorlagen für Ein, Aus, Helligkeit, Farbe, Weiß (Platzhalter wie `{percent}`, `{level}`, `{r}`) |

Rein weiße Lampen bleiben bei der Helligkeit. Wie jede Art funktioniert und
was sie kann: [Backend-API](../backend-api.md),
[Recherche-Notizen](../backends-research.md) (englisch).

## Installation

Benötigt Linux mit BlueZ, Python 3.12+ und für Widget und Regler KDE Plasma 6
mit UPower.

```sh
git clone https://github.com/Gelaende51/Haus.Licht && cd Haus.Licht
sudo install/setup-root.sh             # einmalig: Tastaturregler, Gerätezugriff, Bluetooth-Einstellungen
install/install.sh                     # venv, Befehle, Handbuchseiten, Dienst, Tray-Widget (nur Mesh)
install/install.sh --backend wiz,hue   # dazu die Bibliotheken dieser Arten
install/install.sh --all-backends      # dazu die Bibliotheken aller Arten
systemctl --user enable --now hauslicht
```

- `install/install.sh --dry-run …` zeigt, was installiert würde;
  `hauslicht backends --requires ART` listet die pip-Pakete einer Art.
- Klatschen schaltet und Audio folgen: `.venv/bin/pip install -r
  requirements-clap.txt` (numpy, ai-edge-litert; das 4-MB-Klangmodell wird
  beim ersten Klatschen geladen). Beide nehmen mit `pw-record` oder `parec`
  auf.
- Video folgen: GStreamer mit seinem PipeWire-Plugin (`gst-launch-1.0`,
  `pipewiresrc`) und xdg-desktop-portal.
- Tests: `pip install -r requirements-dev.txt` (siehe dort den Hinweis zu
  amqtt); wie die CI sie ausführt, auch lokal: [docs/ci.md](../ci.md)
  (englisch).

## Lampen hinzufügen

- **Die meisten Arten:** Widget → *Einrichten* → *Lampen* → *Lampe
  hinzufügen*: Art wählen, *Netzwerk durchsuchen* (oder *Bluetooth
  durchsuchen*, *Alles durchsuchen*) oder die Felder aus der Tabelle
  ausfüllen, *Hinzufügen*. Im Terminal: `hauslicht discover [ART]`,
  `hauslicht add ART schlüssel=wert…`.
- **Ledvance Volkslicht (Mesh):** Lampe auf Werkseinstellung zurücksetzen
  (fünfmal je 5 s ein- und ausschalten, dann eingeschaltet lassen; sie
  blinkt). Innerhalb von 8 Minuten `hauslicht adopt` ausführen. Die Lampe ist
  danach nicht mehr in der Ledvance-App. `hauslicht.json` enthält die
  Mesh-Schlüssel: sichern, ohne sie lassen sich die Lampen nur noch
  zurücksetzen.

## Funktioniert mit

- KDE Plasma 6 (Helligkeits-Applet, PowerDevil, Nachtlicht, Farbschema,
  globale Tastenkürzel, Benachrichtigungen); ohne Plasma funktioniert alles
  außer Widget und Regler.
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

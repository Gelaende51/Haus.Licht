/*
 * Shared by the Configure pages: talks to the daemon through hauslicht-ctl
 * (the executable data engine, as main.qml does), keeps the last `get`
 * reply and translates the pages' strings (English + German table).
 *
 * run(args, done): done(reply) gets the parsed JSON line, {"error": …} on a
 * refusal or when no daemon answers. A reply that carries `bulbs_list` (most
 * do: the get object) becomes the new state, so an action refreshes the page.
 *
 * widget: which of the daemon's widgets this instance is (plasmoid config
 * key widgetIndex); every command goes out as `@N CMD …` when N is not 1.
 */
import QtQuick
import org.kde.plasma.plasma5support as P5Support

QtObject {
    id: ctl

    property int widget: 1
    onWidgetChanged: refresh()

    property var st: ({})
    property bool reachable: true
    property bool loaded: false
    property int pending: 0
    property var callbacks: ({})

    // ------------------------------------------------------------- i18n
    readonly property bool german: Qt.locale().name.startsWith("de")
    readonly property var de: ({
        // shared
        "Default": "Standard",
        "Cancel": "Abbrechen",
        "Rename": "Umbenennen",
        "Delete": "Löschen",
        "Remove": "Entfernen",
        "Save": "Speichern",
        "Add": "Hinzufügen",
        "all bulbs": "alle Lampen",
        "The Haus.Licht service does not answer. Start it with: systemctl --user start hauslicht":
            "Der Haus.Licht-Dienst antwortet nicht. Starten mit: systemctl --user start hauslicht",
        // bulbs page
        "No bulbs yet. Add one below.": "Noch keine Lampen. Unten eine hinzufügen.",
        "Name": "Name",
        "experimental": "experimentell",
        "Connected": "Verbunden",
        "Disconnected": "Getrennt",
        "Unreachable": "Nicht erreichbar",
        "Profile:": "Profil:",
        "Remove %1? Haus.Licht stops controlling it.": "%1 entfernen? Haus.Licht steuert sie dann nicht mehr.",
        "Identify": "Identifizieren",
        "Blink slowly to find this bulb": "Langsam blinken, um diese Lampe zu finden",
        "Test": "Testen",
        "Stop": "Stopp",
        "Brightest, a fade, colours, then on/off from fast to slow (about 25 s)":
            "Am hellsten, Abblenden, Farben, dann an/aus von schnell nach langsam (etwa 25 s)",
        "Flashes quickly: not for people sensitive to flashing light (photosensitive epilepsy)":
            "Blitzt schnell: nicht für Menschen, die empfindlich auf Lichtblitze reagieren (fotosensible Epilepsie)",
        "Test: %1": "Test: %1",
        "Brightest": "Am hellsten",
        "Fade": "Abblenden",
        "Colours": "Farben",
        "On/off, fast to slow": "An/aus, von schnell nach langsam",
        "Fastest on/off: %1 per second": "Schnellstes An/Aus: %1 pro Sekunde",
        "Test stopped": "Test abgebrochen",
        "Add a bulb": "Lampe hinzufügen",
        "Connect new devices automatically": "Neue Geräte automatisch verbinden",
        "Warning: this may take over lights that belong to someone else nearby (neighbours' bulbs, shared spaces). Bluetooth mesh bulbs are only added by hand.":
            "Achtung: Dabei können Lampen in der Nähe übernommen werden, die jemand anderem gehören (Lampen der Nachbarn, gemeinsam genutzte Räume). Bluetooth-Mesh-Lampen werden nur von Hand hinzugefügt.",
        "Removed, not connected automatically again:": "Entfernt, nicht wieder automatisch verbunden:",
        "Allow again": "Wieder zulassen",
        "Kind:": "Art:",
        "needs: pip install %1": "braucht: pip install %1",
        "Name:": "Name:",
        "required": "Pflichtfeld",
        "Search network": "Netzwerk durchsuchen",
        "Search Bluetooth": "Per Bluetooth suchen",
        "Scan Bluetooth": "Bluetooth durchsuchen",
        "Auto scan": "Automatisch suchen",
        "(added)": "(hinzugefügt)",
        "No Bluetooth adapter is free to scan: flick mode Exclusive holds it. Set the flick mode to Fast to search.":
            "Kein Bluetooth-Adapter frei zum Suchen: der Umschaltmodus Exklusiv belegt ihn. Zum Suchen den Modus auf Schnell stellen.",
        "Searching…": "Suche läuft…",
        "Found:": "Gefunden:",
        "Nothing found.": "Nichts gefunden.",
        "Bluetooth-mesh bulbs are adopted in a terminal:": "Bluetooth-Mesh-Lampen werden im Terminal übernommen:",
        "Added %1.": "%1 hinzugefügt.",
        "Widgets:": "Widgets:",
        "Wall-switch flick:": "Umschalten per Wandschalter:",
        "Off": "Aus",
        "On": "An",
        "Fast (needs system setup)": "Schnell (braucht Systemeinrichtung)",
        "Exclusive (needs system setup)": "Exklusiv (braucht Systemeinrichtung)",
        "The wall switch works like a normal switch: power on means light on.":
            "Der Wandschalter wirkt wie ein normaler Schalter: Strom an heißt Licht an.",
        "Flicking the wall switch off and on toggles the light; the bulb boots into the toggled state.":
            "Wandschalter kurz aus und wieder an schaltet das Licht um; die Lampe startet im umgeschalteten Zustand.",
        "Like On, with Bluetooth tuned to reconnect faster after a flick.":
            "Wie An, dazu ist Bluetooth auf schnelleres Wiederverbinden nach kurzem Aus- und Einschalten eingestellt.",
        "Like Fast, without a Bluetooth connection: Haus.Licht takes one Bluetooth adapter for itself and re-arms the bulb sooner after a flick.":
            "Wie Schnell, aber ohne Bluetooth-Verbindung: Haus.Licht nimmt einen Bluetooth-Adapter ganz für sich und stellt die Lampe nach kurzem Aus- und Einschalten schneller wieder ein.",
        "Adapter:": "Adapter:",
        "built-in": "eingebaut",
        "USB": "USB",
        "Other Bluetooth devices (headphones, mice) stop working while Exclusive mode runs.":
            "Andere Bluetooth-Geräte (Kopfhörer, Mäuse) funktionieren nicht, solange der Exklusiv-Modus läuft.",
        "System setup: running (enter the administrator password; Bluetooth restarts)":
            "Systemeinrichtung läuft (Administratorpasswort eingeben; Bluetooth startet neu)",
        "System setup: installed v%1": "Systemeinrichtung: installiert v%1",
        "System setup: outdated": "Systemeinrichtung: veraltet",
        "System setup: missing": "Systemeinrichtung: fehlt",
        "System setup cancelled: wall-switch flick stays %1.": "Systemeinrichtung abgebrochen: Umschalten per Wandschalter bleibt %1.",
        "System setup not allowed, or no password prompt available: wall-switch flick stays %1.":
            "Systemeinrichtung nicht erlaubt, oder keine Passwortabfrage verfügbar: Umschalten per Wandschalter bleibt %1.",
        "System setup failed (exit %2): wall-switch flick stays %1.":
            "Systemeinrichtung fehlgeschlagen (Exit %2): Umschalten per Wandschalter bleibt %1.",
        "(Fast and Exclusive install it: administrator password, Bluetooth restarts)":
            "(Schnell und Exklusiv richten sie ein: Administratorpasswort, Bluetooth startet neu)",
        "Widget 1 sits in the system tray, the others in the panel next to it.":
            "Widget 1 sitzt im Systemabschnitt, die anderen daneben in der Leiste.",
        "Use": "Benutzen",
        "Off: Haus.Licht leaves this bulb alone. It comes on after a power cut like an ordinary bulb.":
            "Aus: Haus.Licht lässt diese Lampe in Ruhe. Nach einem Stromausfall geht sie an wie eine gewöhnliche Lampe.",
        "Show in this widget": "In diesem Widget zeigen",
        "Icon when on": "Symbol, wenn an",
        "Icon when off (optional)": "Symbol, wenn aus (optional)",
        "Icon when disconnected (optional)": "Symbol, wenn getrennt (optional)",
        "Firmware %1": "Firmware %1",
        "Invert scroll direction": "Bildlaufrichtung umkehren",
        "The mouse wheel on the tray icon and on the popup's sliders.":
            "Das Mausrad auf dem Symbol in der Systemleiste und auf den Reglern im Popup.",
        "View logs": "Protokoll anzeigen",
        "Shows the service's log in a terminal (journalctl).":
            "Zeigt das Protokoll des Dienstes in einem Terminal (journalctl).",
        "Choose…": "Auswählen…",
        "Clear": "Leeren",
        "Drag to reorder": "Ziehen zum Umsortieren",
        "Add bulb…": "Lampe hinzufügen…",
        // profiles page
        "used by %1 bulbs": "von %1 Lampen benutzt",
        "used by 1 bulb": "von 1 Lampe benutzt",
        "not used": "nicht benutzt",
        "Use for the selection": "Für die Auswahl verwenden",
        "The bulb selected in the popup uses this profile. With all bulbs selected, its values become the default for every bulb.":
            "Die im Popup ausgewählte Lampe nutzt dieses Profil. Sind alle Lampen ausgewählt, werden seine Werte der Standard für jede Lampe.",
        "Delete profile %1? Bulbs using it go back to Default.": "Profil %1 löschen? Lampen, die es nutzen, gehen zurück auf Standard.",
        "Save the current settings as a profile": "Aktuelle Einstellungen als Profil speichern",
        "Copies the settings of the bulb selected in the popup (now: %1) into a new profile, or overwrites the profile of that name.":
            "Kopiert die Einstellungen der im Popup ausgewählten Lampe (jetzt: %1) in ein neues Profil oder überschreibt das Profil mit diesem Namen.",
        "Profile name": "Profilname",
        "Saved as %1.": "Als %1 gespeichert.",
        // sound page
        "Microphone:": "Mikrofon:",
        "The microphone is muted: claps are not heard.": "Das Mikrofon ist stummgeschaltet: Klatschen wird nicht gehört.",
        "Default microphone": "Standardmikrofon",
        "Sensitivity:": "Empfindlichkeit:",
        "Low": "Niedrig",
        "High": "Hoch",
        "Test:": "Test:",
        "Clap": "Klatschen",
        "Snap": "Schnippen",
        "Clap+Snap": "Klatschen+Schnippen",
        "Speech": "Sprache",
        "Music": "Musik",
        "Knock": "Klopfen",
        "Tap": "Tippen",
        "Typing": "Tastaturanschlag",
        "Computer keyboard": "Computertastatur",
        "(ignored)": "(ignoriert)",
        "(from the speakers)": "(aus den Lautsprechern)",
        "Last: –": "Zuletzt: –",
        "Recent sounds": "Letzte Geräusche",
        "Clap detection is off": "Klatscherkennung ist aus",
        "No sounds heard yet.": "Noch keine Geräusche gehört.",
        "Heard %1 · accepted %2 · doubles %3": "Gehört %1 · angenommen %2 · Doppelte %3",
        "Bars: %1": "Schwellen: %1",
        "first": "erster",
        "partner": "Partner",
        "veto": "Veto",
        "family %1": "Familie %1",
        "accepted": "angenommen",
        "part of a double": "Teil eines Doppelten",
        "speakers": "Lautsprecher",
        "Last: %1 · %2 s ago": "Zuletzt: %1 · vor %2 s",
        "Switch Clap on in a widget's popup to test.": "Zum Testen im Popup eines Widgets Klatschen einschalten.",
        "This widget:": "Dieses Widget:",
        "Both": "Beides",
        "Downloading the sound model…": "Das Klangmodell wird heruntergeladen…",
        "Starting…": "Startet…",
        "Error: %1": "Fehler: %1",
        "Clap detection needs extra packages (%1). Install them with:":
            "Die Klatscherkennung braucht zusätzliche Pakete (%1). Installieren mit:",
        "Sound is analysed on this computer only and never stored. Plasma shows the microphone in use.":
            "Der Ton wird nur auf diesem Computer ausgewertet und nie gespeichert. Plasma zeigt das Mikrofon als benutzt an.",
        "Follow audio": "Audio folgen",
        "Follow audio needs extra packages (%1). Install them with:":
            "„Audio folgen“ braucht zusätzliche Pakete (%1). Installieren mit:",
        "Source:": "Quelle:",
        "Default output (what you hear)": "Standardausgabe (was Sie hören)",
        "Colour from pitch": "Farbe aus der Tonhöhe",
        "Updates per second:": "Aktualisierungen pro Sekunde:",
        "Bluetooth mesh: at most 2 recommended; every update uses one of the bulb's message numbers":
            "Bluetooth-Mesh: höchstens 2 empfohlen; jede Aktualisierung verbraucht eine der Nachrichtennummern der Lampe",
        "Mesh messages this session:": "Mesh-Nachrichten in dieser Sitzung:",
        "Brightness changes at most 3 times per second.": "Die Helligkeit ändert sich höchstens 3-mal pro Sekunde.",
        // video page
        "Follow video": "Video folgen",
        "Follow video needs extra packages (%1). Install them with:":
            "„Video folgen“ braucht zusätzliche Pakete (%1). Installieren mit:",
        "Max brightness:": "Maximale Helligkeit:",
        "Time averaging:": "Zeitliche Mittelung:",
        "%1 s": "%1 s",
        "Shared with Follow audio.": "Gilt auch für „Audio folgen“.",
        "Screen region per bulb": "Bildschirmbereich je Lampe",
        "Screen region": "Bildschirmbereich",
        "Whole screen": "Ganzer Bildschirm",
        "Left": "Links",
        "Right": "Rechts",
        "Top": "Oben",
        "Bottom": "Unten",
        "Top left": "Oben links",
        "Top right": "Oben rechts",
        "Bottom left": "Unten links",
        "Bottom right": "Unten rechts",
        "No bulbs yet.": "Noch keine Lampen.",
        "Screen permission": "Bildschirmfreigabe",
        "Screen permission:": "Bildschirmfreigabe:",
        "Granted: no dialog on the next start": "Erteilt: beim nächsten Start kein Dialog",
        "Not granted: the first start asks": "Nicht erteilt: der erste Start fragt nach",
        "Follow video needs permission again": "„Video folgen“ braucht erneut die Freigabe",
        "Forget screen permission": "Bildschirmfreigabe vergessen",
        // notifications page
        "Blink (on/off)": "Blinken (an/aus)",
        "Pulse (dim and back)": "Pulsieren (dunkler und zurück)",
        "Double (two quick blinks)": "Doppelt (zweimal kurz blinken)",
        "All bulbs in use": "Alle benutzten Lampen",
        "The widget's selection": "Die Auswahl des Widgets",
        "App: %1": "App: %1",
        "Title: %1": "Titel: %1",
        "Text: %1": "Text: %1",
        "Every notification": "Jede Benachrichtigung",
        "also when the light is off": "auch wenn das Licht aus ist",
        "Notifications cannot be watched: %1": "Benachrichtigungen können nicht beobachtet werden: %1",
        "Blink on notifications:": "Bei Benachrichtigungen blinken:",
        "Do Not Disturb is on: nothing blinks.": "„Nicht stören“ ist an: nichts blinkt.",
        "Last match: %1 (%2) at %3": "Letzter Treffer: %1 (%2) um %3",
        "Rules are saved in the current profile (%1).": "Regeln werden im aktuellen Profil (%1) gespeichert.",
        "The first rule that matches wins. Patterns are regular expressions, case does not matter; an empty one matches anything.":
            "Die erste passende Regel gilt. Muster sind reguläre Ausdrücke, Groß- und Kleinschreibung egal; ein leeres passt auf alles.",
        "Run this rule's pattern now": "Das Muster dieser Regel jetzt abspielen",
        "Edit": "Bearbeiten",
        "Move up": "Nach oben",
        "Move down": "Nach unten",
        "No rules yet.": "Noch keine Regeln.",
        "Add rule…": "Regel hinzufügen…",
        "New rule": "Neue Regel",
        "Edit rule": "Regel bearbeiten",
        "App (regex):": "App (Regex):",
        "Title (regex):": "Titel (Regex):",
        "Text (regex):": "Text (Regex):",
        "any": "beliebig",
        "Pattern:": "Muster:",
        "Times:": "Anzahl:",
        "Brightness:": "Helligkeit:",
        "Colour:": "Farbe:",
        "Colour bulbs only": "Nur Farblampen",
        "Bulbs:": "Lampen:",
        "Also when the light is off": "Auch wenn das Licht aus ist",
        "Bulbs that are off light up for the blink, also at night.":
            "Ausgeschaltete Lampen leuchten zum Blinken auf, auch nachts.",
        "Only bulbs that are lit blink: they dim and come back, a dark room stays dark.":
            "Nur leuchtende Lampen blinken: sie werden dunkler und kommen zurück, ein dunkler Raum bleibt dunkel."
    })
    function tr(text, ...args) {
        let out = (german && de[text]) ? de[text] : text
        args.forEach((a, i) => { out = out.replace("%" + (i + 1), a) })
        return out
    }

    // ------------------------------------------------------- daemon IO
    property var exec: P5Support.DataSource {
        engine: "executable"
        connectedSources: []
        onNewData: (source, data) => {
            disconnectSource(source)
            const done = ctl.callbacks[source]
            delete ctl.callbacks[source]
            ctl.pending = Math.max(0, ctl.pending - 1)
            const out = (data["stdout"] || "").trim()
            let reply
            try {
                reply = JSON.parse(out)
            } catch (e) {
                reply = {"error": out || (data["stderr"] || "").trim() || "no reply"}
            }
            if (reply && reply.bulbs_list) {
                ctl.st = reply
                ctl.reachable = true
                ctl.loaded = true
            }
            if (done) done(reply || {})
        }
    }

    // Any other command (the root setup's pkexec, which must run in the login
    // session): done(exitCode, stdout, stderr).
    property var shellCallbacks: ({})
    property var shellExec: P5Support.DataSource {
        engine: "executable"
        connectedSources: []
        onNewData: (source, data) => {
            disconnectSource(source)
            const done = ctl.shellCallbacks[source]
            delete ctl.shellCallbacks[source]
            if (done) done(data["exit code"], (data["stdout"] || "").trim(), (data["stderr"] || "").trim())
        }
    }
    function shell(args, done) {
        const source = args.map(quote).join(" ") + " # " + Date.now() + Math.random()
        const cbs = shellCallbacks
        cbs[source] = done || null
        shellCallbacks = cbs
        shellExec.connectSource(source)
    }

    function quote(word) { return "'" + String(word).replace(/'/g, "'\\''") + "'" }
    function run(args, done) {
        if (widget !== 1) args = ["@" + widget].concat(args)
        const source = '"$HOME/.local/bin/hauslicht-ctl" ' + args.map(quote).join(" ")
                       + " # " + Date.now() + Math.random()  // unique: the engine runs each source once
        const cbs = callbacks
        cbs[source] = done || null
        callbacks = cbs
        pending += 1
        exec.connectSource(source)
    }
    function refresh() {
        run(["get"], reply => { if (reply.error) ctl.reachable = false })
    }

    // Statuses change on their own: refresh now and then, never faster than 2 s.
    property Timer poll: Timer {
        interval: 10000
        running: true
        repeat: true
        onTriggered: if (ctl.pending === 0) ctl.refresh()
    }
    Component.onCompleted: refresh()

    // ------------------------------------------------------ derived data
    readonly property var bulbs: st.bulbs_list || []
    readonly property var profiles: st.profiles || ["default"]
    readonly property var backends: st.backends || []
    function profileLabel(name) { return name === "default" ? tr("Default") : name }
    function bulbName(id) {
        if (id === "all" || !id) return tr("all bulbs")
        const b = bulbs.find(x => x.id === id)
        return b ? b.name : id
    }
    function backend(kind) { return backends.find(b => b.kind === kind) || null }
    function kindTitle(kind) {
        const b = backend(kind)
        return b ? b.title.replace(/\s*\(.*\)\s*$/, "") : kind
    }
}

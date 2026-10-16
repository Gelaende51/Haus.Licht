/*
 * Shared by the Configure pages: talks to the daemon through hauslicht-ctl
 * (the executable data engine, as main.qml does), keeps the last `get`
 * reply and translates the pages' strings (English + German table).
 *
 * run(args, done): done(reply) gets the parsed JSON line, {"error": …} on a
 * refusal or when no daemon answers. A reply that carries `bulbs_list` (most
 * do: the get object) becomes the new state, so an action refreshes the page.
 */
import QtQuick
import org.kde.plasma.plasma5support as P5Support

QtObject {
    id: ctl

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
        "Add a bulb": "Lampe hinzufügen",
        "Kind:": "Art:",
        "needs: pip install %1": "braucht: pip install %1",
        "Name:": "Name:",
        "required": "Pflichtfeld",
        "Search network": "Netzwerk durchsuchen",
        "Searching…": "Suche läuft…",
        "Found:": "Gefunden:",
        "Nothing found.": "Nichts gefunden.",
        "Bluetooth-mesh bulbs are adopted in a terminal:": "Bluetooth-Mesh-Lampen werden im Terminal übernommen:",
        "Added %1.": "%1 hinzugefügt.",
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
        "Saved as %1.": "Als %1 gespeichert."
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

    function quote(word) { return "'" + String(word).replace(/'/g, "'\\''") + "'" }
    function run(args, done) {
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

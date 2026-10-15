/*
 * Haus.Licht tray widget: settings for the hauslicht sync daemon.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Talks to the daemon only through `hauslicht-ctl` (socket client), run by
 * the executable data engine. Left click opens the popup, middle click
 * switches the bulbs on/off.
 */
import QtQml
import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PC3
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    property var st: ({})
    property bool reachable: false
    readonly property bool powered: reachable && st.power !== false && st.bulbs !== 0

    // ------------------------------------------------------------- i18n
    readonly property bool german: Qt.locale().name.startsWith("de")
    readonly property var de: ({
        "Bulbs": "Lampen",
        "Service not running": "Dienst läuft nicht",
        "Screen %1 % → bulbs %2 %": "Bildschirm %1 % → Lampen %2 %",
        "following the screen": "folgt dem Bildschirm",
        "not following the screen": "folgt nicht dem Bildschirm",
        "manual setting": "manuelle Einstellung",
        "Night Light on": "Nachtlicht an",
        "screen off": "Bildschirm aus",
        "lid closed": "Deckel zu",
        "Screen window: %1 … %2 %": "Bildschirmfenster: %1 … %2 %",
        "Screen brightness at the left handle means off, at the right one full.":
            "Bildschirmhelligkeit am linken Griff heißt aus, am rechten voll.",
        "Curve: %1": "Kurve: %1",
        "rises fast": "steigt schnell",
        "linear": "linear",
        "rises slowly (fine low end)": "steigt langsam (fein im Dunklen)",
        "Off with the screen, never fully dark while it is on":
            "Aus mit dem Bildschirm, nie ganz dunkel solange er an ist",
        "Off while the lid is closed": "Aus bei geschlossenem Deckel",
        "Keyboard slider": "Tastaturregler",
        "Maximum: bulbs up to the slider": "Maximum: Lampen bis zum Regler",
        "Minimum: bulbs from the slider up": "Minimum: Lampen ab dem Regler",
        "Independent: the slider alone": "Unabhängig: nur der Regler",
        "Coupling: how strongly bulbs react": "Kopplung: wie stark die Lampen reagieren",
        "Off: ignore the keyboard slider": "Aus: Tastaturregler ignorieren",
        "Follow screen": "Bildschirm folgen",
        "Always": "Immer",
        "Only while Night Light is on": "Nur bei aktivem Nachtlicht",
        "Never": "Nie",
        "Presets": "Voreinstellungen",
        "Save current as…": "Aktuelle speichern als…",
        "Delete": "Löschen",
        "No presets yet": "Noch keine Voreinstellungen",
        "Preset name": "Name der Voreinstellung",
        "Save": "Speichern",
        "Resume following": "Wieder folgen",
        "Middle click: bulbs on/off": "Mittelklick: Lampen ein/aus",
        "Connected to the bulb": "Mit der Lampe verbunden",
        "Bulb off at the wall switch": "Lampe am Wandschalter aus",
        "Connecting…": "Verbinde…",
        "Brightness: %1 %": "Helligkeit: %1 %",
        "(keyboard slider)": "(Tastaturregler)",
        "Keyboard slider moves": "Tastaturregler bewegt",
        "Right window handle": "Rechten Fenstergriff",
        "Left window handle": "Linken Fenstergriff",
        "Curve": "Kurve",
        "Brightness (screen ignored)": "Helligkeit (Bildschirm egal)",
        "Nothing": "Nichts",
    })
    function tr(text) { return (german && de[text]) ? de[text] : text }

    // ---------------------------------------------------------- daemon IO
    P5Support.DataSource {
        id: exec
        engine: "executable"
        connectedSources: []
        onNewData: (source, data) => {
            const out = (data["stdout"] || "").trim()
            disconnectSource(source)
            if (!out) return
            try {
                const reply = JSON.parse(out)
                if (reply.error) { root.reachable = false; return }
                root.st = reply
                root.reachable = true
            } catch (e) {
                root.reachable = false
            }
        }
    }

    function quote(word) { return "'" + String(word).replace(/'/g, "'\\''") + "'" }
    function ctl(...args) {
        exec.connectSource('"$HOME/.local/bin/hauslicht-ctl" ' + args.map(quote).join(" ")
                           + " # " + Date.now())  // unique: the engine runs each source once
    }
    function set(key, value) { ctl("set", key + "=" + value) }

    Timer {
        interval: root.expanded ? 2000 : 15000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.ctl("get")
    }

    // ------------------------------------------------------- tray / tooltip
    Plasmoid.icon: Qt.resolvedUrl(root.powered ? "../icons/bulb-on.svg" : "../icons/bulb-off.svg")
    Plasmoid.status: PlasmaCore.Types.ActiveStatus
    toolTipMainText: "Haus.Licht"
    toolTipSubText: connectionText() + "\n" + statusText() + "\n" + tr("Middle click: bulbs on/off")

    function connectionText() {
        if (!reachable) return tr("Service not running")
        if (st.online === false) return tr("Bulb off at the wall switch")
        return st.connected ? tr("Connected to the bulb") : tr("Connecting…")
    }
    function kbdMark(key) { return st.kbd_binds === key ? " ⌨ " + tr("(keyboard slider)") : "" }

    function statusText() {
        if (!reachable) return tr("Service not running")
        const parts = [tr("Screen %1 % → bulbs %2 %").arg(st.screen).arg(st.bulbs === null ? "–" : st.bulbs)]
        if (!st.following) parts.push(tr("manual setting"))
        else parts.push(st.tracking ? tr("following the screen") : tr("not following the screen"))
        if (st.night) parts.push(tr("Night Light on"))
        if (st.lid_closed) parts.push(tr("lid closed"))
        else if (!st.screen_on) parts.push(tr("screen off"))
        return parts.join(" · ")
    }

    compactRepresentation: MouseArea {
        acceptedButtons: Qt.LeftButton | Qt.MiddleButton
        onClicked: mouse => {
            if (mouse.button === Qt.MiddleButton) root.ctl("toggle")
            else root.expanded = !root.expanded
        }
        Kirigami.Icon {
            anchors.fill: parent
            source: Plasmoid.icon
            isMask: true
            active: parent.containsMouse
        }
    }

    // ---------------------------------------------------------------- popup
    fullRepresentation: PlasmaExtras.Representation {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 22
        Layout.minimumHeight: content.implicitHeight + Kirigami.Units.largeSpacing * 2
        collapseMarginsHint: true

        header: PlasmaExtras.PlasmoidHeading {
            RowLayout {
                anchors.fill: parent
                PlasmaExtras.Heading { level: 1; text: "Haus.Licht" }
                Kirigami.Icon {
                    implicitWidth: Kirigami.Units.iconSizes.small
                    implicitHeight: implicitWidth
                    source: !root.reachable ? "dialog-error"
                          : root.st.online === false ? "network-disconnect"
                          : root.st.connected ? "network-connect" : "view-refresh"
                }
                PC3.Label {
                    text: root.connectionText()
                    opacity: 0.8
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                PC3.Label { text: root.tr("Bulbs") }
                PC3.Switch {
                    checked: root.reachable && root.st.power !== false
                    enabled: root.reachable
                    onToggled: root.ctl(checked ? "on" : "off")
                }
            }
        }

        ColumnLayout {
            id: content
            anchors.fill: parent
            anchors.margins: Kirigami.Units.largeSpacing
            spacing: Kirigami.Units.smallSpacing
            enabled: root.reachable

            PC3.Label {
                text: root.statusText()
                wrapMode: Text.Wrap
                Layout.fillWidth: true
                opacity: 0.8
            }
            PC3.Button {
                visible: root.reachable && !root.st.following
                text: root.tr("Resume following")
                icon.name: "media-playback-start"
                onClicked: root.ctl("resume")
            }

            // Brightness: what the bulbs show; sets the level where it rules.
            PC3.Label {
                Layout.topMargin: Kirigami.Units.largeSpacing
                text: root.tr("Brightness: %1 %").arg(level.pressed || levelSend.running
                      ? level.value : (root.st.bulbs === null || root.st.bulbs === undefined ? "–" : root.st.bulbs))
                      + root.kbdMark("level")
            }
            PC3.Slider {
                id: level
                Layout.fillWidth: true
                from: 0; to: 100; stepSize: 0.5
                onMoved: levelSend.restart()
                Timer { id: levelSend; interval: 250; onTriggered: root.ctl("bulb", level.value) }
            }

            // Screen window: two handles on -100 … 200, 0 … 100 is the screen.
            PC3.Label {
                text: root.tr("Screen window: %1 … %2 %").arg(Math.round(range.first.value))
                                                        .arg(Math.round(range.second.value))
                      + root.kbdMark("range_lo") + root.kbdMark("range_hi")
                Layout.topMargin: Kirigami.Units.largeSpacing
            }
            Item {
                Layout.fillWidth: true
                implicitHeight: range.implicitHeight
                // shade the part of the scale that is real screen brightness
                Rectangle {
                    x: range.leftPadding + (range.availableWidth) * (100 / 300)
                    width: range.availableWidth * (100 / 300)
                    anchors.verticalCenter: parent.verticalCenter
                    height: Kirigami.Units.smallSpacing * 3
                    radius: height / 2
                    color: Kirigami.Theme.highlightColor
                    opacity: 0.25
                }
                PC3.RangeSlider {
                    id: range
                    anchors.fill: parent
                    from: -100; to: 200; stepSize: 1
                    first.onMoved: rangeSend.restart()
                    second.onMoved: rangeSend.restart()
                }
                Timer {
                    id: rangeSend
                    interval: 250
                    onTriggered: {
                        root.set("range_lo", Math.round(range.first.value))
                        root.set("range_hi", Math.round(range.second.value))
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                PC3.Label { text: "−100 %"; font: Kirigami.Theme.smallFont; opacity: 0.6 }
                Item { Layout.fillWidth: true }
                PC3.Label { text: "0 … 100 %"; font: Kirigami.Theme.smallFont; opacity: 0.6 }
                Item { Layout.fillWidth: true }
                PC3.Label { text: "200 %"; font: Kirigami.Theme.smallFont; opacity: 0.6 }
            }
            PC3.Label {
                text: root.tr("Screen brightness at the left handle means off, at the right one full.")
                wrapMode: Text.Wrap; Layout.fillWidth: true
                font: Kirigami.Theme.smallFont; opacity: 0.6
            }

            // Curve: -100 … 100, 0 linear.
            PC3.Label {
                Layout.topMargin: Kirigami.Units.largeSpacing
                text: root.tr("Curve: %1").arg(curve.value === 0 ? root.tr("linear")
                      : curve.value > 0 ? root.tr("rises slowly (fine low end)") : root.tr("rises fast"))
                      + root.kbdMark("curve")
            }
            PC3.Slider {
                id: curve
                Layout.fillWidth: true
                from: -100; to: 100; stepSize: 1
                onMoved: curveSend.restart()
                Timer { id: curveSend; interval: 250; onTriggered: root.set("curve", curve.value) }
            }

            PC3.CheckBox {
                Layout.topMargin: Kirigami.Units.largeSpacing
                text: root.tr("Off with the screen, never fully dark while it is on")
                checked: !!root.st.screen_state
                onToggled: root.set("screen_state", checked ? "on" : "off")
            }
            PC3.CheckBox {
                text: root.tr("Off while the lid is closed")
                checked: !!root.st.lid_state
                onToggled: root.set("lid_state", checked ? "on" : "off")
            }

            // Sub menus: keyboard slider, follow screen, presets.
            RowLayout {
                Layout.topMargin: Kirigami.Units.largeSpacing
                Layout.fillWidth: true
                PC3.ToolButton {
                    text: root.tr("Keyboard slider moves")
                    icon.name: "input-keyboard-brightness"
                    onClicked: modeMenu.popup(this, 0, height)
                }
                PC3.ToolButton {
                    text: root.tr("Follow screen")
                    icon.name: "video-display-brightness"
                    onClicked: trackMenu.popup(this, 0, height)
                }
                PC3.ToolButton {
                    text: root.tr("Presets")
                    icon.name: "bookmarks"
                    onClicked: presetMenu.popup(this, 0, height)
                }
            }
            RowLayout {
                visible: saveRow.saving
                id: saveRow
                property bool saving: false
                Layout.fillWidth: true
                PC3.TextField {
                    id: presetName
                    Layout.fillWidth: true
                    placeholderText: root.tr("Preset name")
                    onAccepted: saveButton.clicked()
                }
                PC3.Button {
                    id: saveButton
                    text: root.tr("Save")
                    icon.name: "document-save"
                    enabled: presetName.text.trim().length > 0
                    onClicked: {
                        root.ctl("preset", "save", presetName.text.trim())
                        presetName.text = ""
                        saveRow.saving = false
                    }
                }
            }

            PC3.Menu {
                id: modeMenu
                Instantiator {
                    onObjectAdded: (index, object) => modeMenu.insertItem(index, object)
                    onObjectRemoved: (index, object) => modeMenu.removeItem(object)
                    model: [["maximum", "Right window handle"],
                            ["minimum", "Left window handle"],
                            ["coupling", "Curve"],
                            ["independent", "Brightness (screen ignored)"],
                            ["off", "Nothing"]]
                    PC3.MenuItem {
                        required property var modelData
                        text: root.tr(modelData[1])
                        checkable: true
                        checked: root.st.mode === modelData[0]
                        onTriggered: root.set("mode", modelData[0])
                    }
                }
            }
            PC3.Menu {
                id: trackMenu
                Instantiator {
                    onObjectAdded: (index, object) => trackMenu.insertItem(index, object)
                    onObjectRemoved: (index, object) => trackMenu.removeItem(object)
                    model: [["always", "Always"], ["night", "Only while Night Light is on"], ["never", "Never"]]
                    PC3.MenuItem {
                        required property var modelData
                        text: root.tr(modelData[1])
                        checkable: true
                        checked: root.st.track === modelData[0]
                        onTriggered: root.set("track", modelData[0])
                    }
                }
            }
            PC3.Menu {
                id: presetMenu
                PC3.MenuItem {
                    text: root.tr("No presets yet")
                    enabled: false
                    visible: !(root.st.presets && root.st.presets.length)
                    height: visible ? implicitHeight : 0
                }
                Instantiator {
                    onObjectAdded: (index, object) => presetMenu.insertItem(index + 1, object)
                    onObjectRemoved: (index, object) => presetMenu.removeItem(object)
                    model: root.st.presets || []
                    PC3.MenuItem {
                        required property string modelData
                        text: modelData
                        icon.name: "bookmarks"
                        onTriggered: root.ctl("preset", "apply", modelData)
                    }
                }
                PC3.MenuSeparator {}
                PC3.MenuItem {
                    text: root.tr("Save current as…")
                    icon.name: "document-save-as"
                    onTriggered: { saveRow.saving = true; presetName.forceActiveFocus() }
                }
                PC3.Menu {
                    id: deleteMenu
                    title: root.tr("Delete")
                    enabled: !!(root.st.presets && root.st.presets.length)
                    Instantiator {
                        onObjectAdded: (index, object) => deleteMenu.insertItem(index, object)
                        onObjectRemoved: (index, object) => deleteMenu.removeItem(object)
                        model: root.st.presets || []
                        PC3.MenuItem {
                            required property string modelData
                            text: modelData
                            icon.name: "edit-delete"
                            onTriggered: root.ctl("preset", "delete", modelData)
                        }
                    }
                }
            }
        }

        // Pull the daemon's values into the sliders, except while dragging.
        Connections {
            target: root
            function onStChanged() {
                if (!root.reachable) return
                if (!range.first.pressed && !range.second.pressed && !rangeSend.running)
                    range.setValues(root.st.range_lo, root.st.range_hi)
                if (!curve.pressed && !curveSend.running)
                    curve.value = root.st.curve
                if (!level.pressed && !levelSend.running && root.st.bulbs !== null && root.st.bulbs !== undefined)
                    level.value = root.st.bulbs
            }
        }
    }
}

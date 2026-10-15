/*
 * Haus.Licht tray widget: settings for the hauslicht sync daemon.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Talks to the daemon only through `hauslicht-ctl` (socket client), run by
 * the executable data engine. Left click opens the popup, middle click
 * switches the bulbs on/off, the wheel nudges the brightness.
 *
 * The popup's rows follow Plasma's Brightness and Color applet
 * (powerdevil applets/brightness/BrightnessItem.qml): icon left, title and
 * value on one line, the control below them.
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
    readonly property bool lit: reachable && st.power !== false && st.bulbs > 0
    readonly property bool disconnected: !reachable || st.connected === false || st.online === false

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
        "Brightness": "Helligkeit",
        "Screen window": "Bildschirmfenster",
        "Screen brightness at the left handle means off, at the right one full.":
            "Bildschirmhelligkeit am linken Griff heißt aus, am rechten voll.",
        "Curve": "Kurve",
        "rises fast": "steigt schnell",
        "linear": "linear",
        "rises slowly": "steigt langsam",
        "Turn off at 0 %": "Bei 0 % ausschalten",
        "Otherwise a 0.5 % glow stays at 0 %.": "Sonst bleibt bei 0 % ein Glimmen von 0,5 %.",
        "Keyboard slider": "Tastaturregler",
        "Right window handle": "Rechter Fenstergriff",
        "Left window handle": "Linker Fenstergriff",
        "Brightness (screen ignored)": "Helligkeit (Bildschirm egal)",
        "Nothing": "Nichts",
        "Follow screen": "Bildschirm folgen",
        "Always": "Immer",
        "Only with Night Light": "Nur mit Nachtlicht",
        "Never": "Nie",
        "Lid": "Deckel",
        "On close": "Beim Schließen",
        "On open": "Beim Öffnen",
        "On both": "Bei beidem",
        "On close switches the bulbs off when the lid closes, on open switches them on when it opens.":
            "Beim Schließen schaltet die Lampen aus, wenn der Deckel zugeht, beim Öffnen schaltet sie ein, wenn er aufgeht.",
        "Presets": "Voreinstellungen",
        "none": "keine",
        "Save current as…": "Aktuelle speichern als…",
        "Delete": "Löschen",
        "No presets yet": "Noch keine Voreinstellungen",
        "Preset name": "Name der Voreinstellung",
        "Save": "Speichern",
        "Resume following": "Wieder folgen",
        "Middle click: bulbs on/off": "Mittelklick: Lampen ein/aus",
        "Wheel: brighter/darker": "Mausrad: heller/dunkler",
        "Connected to the bulb": "Mit der Lampe verbunden",
        "Bulb off at the wall switch": "Lampe am Wandschalter aus",
        "Connecting…": "Verbinde…",
    })
    function tr(text) { return (german && de[text]) ? de[text] : text }

    // Menu entries: daemon value, label.
    readonly property var modeChoices: [["maximum", "Right window handle"],
                                        ["minimum", "Left window handle"],
                                        ["coupling", "Curve"],
                                        ["independent", "Brightness (screen ignored)"],
                                        ["off", "Nothing"]]
    readonly property var trackChoices: [["always", "Always"], ["night", "Only with Night Light"], ["never", "Never"]]
    readonly property var lidChoices: [["close", "On close"], ["open", "On open"], ["both", "On both"], ["never", "Never"]]
    function choiceLabel(choices, value) {
        for (const c of choices) if (c[0] === value) return tr(c[1])
        return "–"
    }

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
    Plasmoid.icon: Qt.resolvedUrl(root.disconnected ? "../icons/lighthouse-disconnected.svg"
                                  : root.lit ? "../icons/lighthouse-on.svg" : "../icons/lighthouse-off.svg")
    Plasmoid.status: PlasmaCore.Types.ActiveStatus
    toolTipMainText: "Haus.Licht"
    toolTipSubText: connectionText() + "\n" + statusText() + "\n" + tr("Middle click: bulbs on/off")
                    + "\n" + tr("Wheel: brighter/darker")

    function connectionText() {
        if (!reachable) return tr("Service not running")
        if (st.online === false) return tr("Bulb off at the wall switch")
        return st.connected ? tr("Connected to the bulb") : tr("Connecting…")
    }
    // The keyboard slider's binding, shown in the value of the row it moves.
    function kbdMark(key) { return st.kbd_binds === key ? " ⌨" : "" }

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
        property int wheelDelta: 0
        acceptedButtons: Qt.LeftButton | Qt.MiddleButton
        hoverEnabled: true
        onClicked: mouse => {
            if (mouse.button === Qt.MiddleButton) root.ctl("toggle")
            else root.expanded = !root.expanded
        }
        // 120 per notch; touchpads send smaller steps that add up.
        onWheel: wheel => {
            wheelDelta += wheel.angleDelta.y
            const notches = (wheelDelta > 0 ? Math.floor : Math.ceil)(wheelDelta / 120)
            if (notches === 0) return
            wheelDelta -= notches * 120
            for (let i = 0; i < Math.abs(notches); i++) root.ctl("nudge", notches > 0 ? 5 : -5)
            wheelRefresh.restart()
        }
        Timer { id: wheelRefresh; interval: 300; onTriggered: root.ctl("get") }
        Kirigami.Icon {
            anchors.fill: parent
            source: Plasmoid.icon
            isMask: true
            active: parent.containsMouse
        }
    }

    // One popup row, laid out like BrightnessItem: icon | title … value / control.
    // `dropDown` rows are clickable as a whole and show an arrow at the right.
    component ControlRow: PC3.ItemDelegate {
        id: row
        property string iconName
        property string title
        property string valueText
        property string tip
        property bool dropDown: false
        default property alias body: column.data

        Layout.fillWidth: true
        text: title
        hoverEnabled: true
        background.visible: dropDown || highlighted
        Accessible.name: title
        Accessible.description: valueText

        contentItem: RowLayout {
            spacing: Kirigami.Units.gridUnit
            Kirigami.Icon {
                Layout.alignment: Qt.AlignVCenter
                Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                source: row.iconName
            }
            ColumnLayout {
                id: column
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
                spacing: 0
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing
                    PC3.Label {
                        Layout.fillWidth: true
                        text: row.title
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                    }
                    PC3.Label {
                        Layout.alignment: Qt.AlignRight
                        text: row.valueText
                        textFormat: Text.PlainText
                        font.features: { "tnum": 1 }
                        opacity: row.dropDown ? 0.75 : 1
                    }
                    Kirigami.Icon {
                        visible: row.dropDown
                        Layout.preferredWidth: Kirigami.Units.iconSizes.small
                        Layout.preferredHeight: Kirigami.Units.iconSizes.small
                        source: "arrow-down"
                    }
                }
            }
        }
        PC3.ToolTip {
            text: row.tip
            visible: row.hovered && row.tip !== ""
            delay: Kirigami.Units.toolTipDelay
        }
    }

    // ---------------------------------------------------------------- popup
    fullRepresentation: PlasmaExtras.Representation {
        id: popup
        Layout.minimumWidth: Kirigami.Units.gridUnit * 16
        Layout.preferredWidth: Kirigami.Units.gridUnit * 18
        // Follows the content, so the popup grows when the save row appears.
        Layout.minimumHeight: content.implicitHeight + Kirigami.Units.largeSpacing * 2
                              + (header ? header.implicitHeight : 0)
        Layout.preferredHeight: Layout.minimumHeight
        collapseMarginsHint: true

        header: PlasmaExtras.PlasmoidHeading {
            RowLayout {
                anchors.fill: parent
                spacing: Kirigami.Units.smallSpacing
                PlasmaExtras.Heading { level: 1; text: "Haus.Licht" }
                Kirigami.Icon {
                    Layout.leftMargin: Kirigami.Units.smallSpacing
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
                    Layout.minimumWidth: 0
                    Layout.preferredWidth: 0
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
            spacing: Kirigami.Units.smallSpacing * 2
            enabled: root.reachable

            PC3.Label {
                text: root.statusText()
                wrapMode: Text.Wrap
                Layout.fillWidth: true
                Layout.preferredWidth: 0  // wrap at the popup width, never widen it
                opacity: 0.8
            }
            PC3.Button {
                visible: root.reachable && !root.st.following
                text: root.tr("Resume following")
                icon.name: "media-playback-start"
                onClicked: root.ctl("resume")
            }

            // Brightness: what the bulbs show; sets the level where it rules.
            ControlRow {
                iconName: "brightness-high"
                title: root.tr("Brightness")
                valueText: (level.pressed || levelSend.running ? level.value
                            : (root.st.bulbs === null || root.st.bulbs === undefined ? "–" : root.st.bulbs))
                           + " %" + root.kbdMark("level")
                PC3.Slider {
                    id: level
                    Layout.fillWidth: true
                    from: 0; to: 100; stepSize: 0.5
                    onMoved: levelSend.restart()
                    Timer { id: levelSend; interval: 250; onTriggered: root.ctl("bulb", level.value) }
                }
            }

            // Screen window: two handles on -100 … 200, 0 … 100 is the screen.
            ControlRow {
                iconName: "video-display-brightness"
                title: root.tr("Screen window")
                valueText: Math.round(range.first.value) + " … " + Math.round(range.second.value) + " %"
                           + root.kbdMark("range_lo") + root.kbdMark("range_hi")
                tip: root.tr("Screen brightness at the left handle means off, at the right one full.")
                Item {
                    Layout.fillWidth: true
                    implicitHeight: range.implicitHeight
                    // shade the part of the scale that is real screen brightness
                    Rectangle {
                        x: range.leftPadding + range.availableWidth * (100 / 300)
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
            }

            // Curve: -100 … 100, 0 linear.
            ControlRow {
                iconName: "draw-bezier-curves"
                title: root.tr("Curve")
                valueText: (curve.value === 0 ? root.tr("linear")
                            : curve.value > 0 ? root.tr("rises slowly") : root.tr("rises fast"))
                           + root.kbdMark("curve")
                PC3.Slider {
                    id: curve
                    Layout.fillWidth: true
                    from: -100; to: 100; stepSize: 1
                    onMoved: curveSend.restart()
                    Timer { id: curveSend; interval: 250; onTriggered: root.set("curve", curve.value) }
                }
            }

            // Drop-down rows: keyboard slider, follow screen, lid, presets.
            ControlRow {
                id: modeRow
                dropDown: true
                iconName: "input-keyboard-brightness"
                title: root.tr("Keyboard slider")
                valueText: root.choiceLabel(root.modeChoices, root.st.mode)
                onClicked: modeMenu.popup(modeRow, 0, modeRow.height)
            }
            ControlRow {
                id: trackRow
                dropDown: true
                iconName: "monitor-symbolic"
                title: root.tr("Follow screen")
                valueText: root.choiceLabel(root.trackChoices, root.st.track)
                onClicked: trackMenu.popup(trackRow, 0, trackRow.height)
            }
            ControlRow {
                id: lidRow
                dropDown: true
                iconName: "computer-laptop-symbolic"
                title: root.tr("Lid")
                valueText: root.choiceLabel(root.lidChoices, root.st.lid || "close")
                tip: root.tr("On close switches the bulbs off when the lid closes, on open switches them on when it opens.")
                onClicked: lidMenu.popup(lidRow, 0, lidRow.height)
            }
            ControlRow {
                id: presetRow
                dropDown: true
                iconName: "bookmarks"
                title: root.tr("Presets")
                valueText: root.st.presets && root.st.presets.length ? String(root.st.presets.length) : root.tr("none")
                onClicked: presetMenu.popup(presetRow, 0, presetRow.height)
            }

            RowLayout {
                id: saveRow
                property bool saving: false
                function cancel() { presetName.text = ""; saving = false }
                visible: saving
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing
                PC3.TextField {
                    id: presetName
                    Layout.fillWidth: true
                    placeholderText: root.tr("Preset name")
                    onAccepted: if (saveButton.enabled) saveButton.clicked()
                    Keys.onEscapePressed: event => { saveRow.cancel(); event.accepted = true }
                }
                PC3.Button {
                    id: saveButton
                    text: root.tr("Save")
                    icon.name: "document-save"
                    enabled: presetName.text.trim().length > 0
                    onClicked: {
                        root.ctl("preset", "save", presetName.text.trim())
                        saveRow.cancel()
                    }
                }
                // Focus after the menu has closed and handed focus back.
                Timer { id: focusName; interval: 50; onTriggered: presetName.forceActiveFocus() }
            }

            PC3.CheckBox {
                text: root.tr("Turn off at 0 %")
                checked: !!root.st.off_at_zero
                onToggled: root.set("off_at_zero", checked ? "on" : "off")
                PC3.ToolTip {
                    text: root.tr("Otherwise a 0.5 % glow stays at 0 %.")
                    visible: parent.hovered
                    delay: Kirigami.Units.toolTipDelay
                }
            }

            PC3.Menu {
                id: modeMenu
                Instantiator {
                    onObjectAdded: (index, object) => modeMenu.insertItem(index, object)
                    onObjectRemoved: (index, object) => modeMenu.removeItem(object)
                    model: root.modeChoices
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
                    model: root.trackChoices
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
                id: lidMenu
                Instantiator {
                    onObjectAdded: (index, object) => lidMenu.insertItem(index, object)
                    onObjectRemoved: (index, object) => lidMenu.removeItem(object)
                    model: root.lidChoices
                    PC3.MenuItem {
                        required property var modelData
                        text: root.tr(modelData[1])
                        checkable: true
                        checked: (root.st.lid || "close") === modelData[0]
                        onTriggered: root.set("lid", modelData[0])
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
                    onTriggered: { saveRow.saving = true; focusName.restart() }
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

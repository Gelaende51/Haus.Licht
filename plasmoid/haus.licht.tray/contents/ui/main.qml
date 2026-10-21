/*
 * Haus.Licht tray widget: settings for the hauslicht sync daemon.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Talks to the daemon only through `hauslicht-ctl` (socket client), run by
 * the executable data engine. Left click opens the popup, middle click
 * switches the selection on/off, the wheel nudges its brightness. Everything
 * acts on the selection (one bulb or all), which the header's box picks.
 *
 * Each instance is one of the daemon's widgets (config key widgetIndex):
 * every command goes out as `@N …` through HausCtl, so the box lists this
 * widget's bulbs and "All bulbs" means them.
 *
 * The popup's rows follow Plasma's Brightness and Color applet
 * (powerdevil applets/brightness/BrightnessItem.qml): icon left, title and
 * value on one line, the control below them.
 *
 * In the system tray the popup has the tray's own size (popupWidth /
 * popupHeight in its config, resizable by the user); the applet's
 * Layout.preferredHeight only counts on the desktop or in plasmawindowed.
 * So the rows sit in a ScrollView.
 */
pragma ComponentBehavior: Bound
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

    // This instance's widget: its bulb list and selection (`@N`).
    readonly property int widgetIndex: Plasmoid.configuration.widgetIndex || 1
    HausCtl {
        id: hc
        widget: root.widgetIndex
        poll.running: false  // root's timer polls, faster while the popup is open
    }
    readonly property var st: hc.st
    readonly property bool reachable: hc.loaded && hc.reachable
    readonly property bool powered: reachable && st.power !== false && st.bulbs !== 0
    readonly property bool lit: reachable && st.power !== false && st.bulbs > 0
    readonly property bool disconnected: !reachable || st.connected === false || st.online === false

    // ------------------------------------------------------------- i18n
    readonly property bool german: Qt.locale().name.startsWith("de")
    readonly property string flickTip: "On: flicking the wall switch off and on toggles the light at once; the bulb itself boots into the toggled state. "
        + "Flicks shorter than about 1–5 s are bridged by the bulb's power supply and go unnoticed. "
        + "After a long power cut while the light was on, it comes back dark until the laptop switches it on. "
        + "One small settings write to the bulb per toggle. Only bulbs that support a power-on setting.\n"
        + "Off: the wall switch works like a normal switch (power on = light on), also without the laptop. "
        + "No toggling by flicking."
    readonly property string followTip: "Shift the bulb's white the way Night Light shifts the screen's colours. "
        + "While on, the second slider sets how strongly: 100 % the same as the screen, 0 % not at all, 200 % twice as much."
    readonly property var de: ({
        "All bulbs": "Alle Lampen",
        "Kind: %1": "Art: %1",
        "Profile: %1": "Profil: %1",
        "experimental": "experimentell",
        "Connected": "Verbunden",
        "Disconnected": "Getrennt",
        "Service not running": "Dienst läuft nicht",
        "Flick": "Flick",
        [flickTip]: "An: Wandschalter kurz aus und wieder an schaltet das Licht sofort um; die Lampe startet selbst im umgeschalteten Zustand. "
            + "Kürzere Unterbrechungen als etwa 1–5 s überbrückt ihr Netzteil, sie bleiben unbemerkt. "
            + "Nach einem langen Stromausfall bei eingeschaltetem Licht bleibt es dunkel, bis der Laptop es einschaltet. "
            + "Pro Umschalten ein kleiner Schreibvorgang in die Einstellungen der Lampe. Nur Lampen mit einstellbarem Einschaltverhalten.\n"
            + "Aus: Der Wandschalter wirkt wie ein normaler Schalter (Strom an = Licht an), auch ohne den Laptop. "
            + "Kein Umschalten durch kurzes Aus- und Einschalten.",
        "On/Off": "An/Aus",
        "Switches the selected bulb, or all bulbs. Middle click on the tray icon does the same.":
            "Schaltet die gewählte Lampe oder alle Lampen. Mittelklick auf das Symbol in der Systemleiste tut dasselbe.",
        "Brightness": "Helligkeit",
        "Screen window": "Bildschirmfenster",
        "Screen brightness at the left handle means off, at the right one full.":
            "Bildschirmhelligkeit am linken Griff heißt aus, am rechten voll.",
        "Curve": "Kurve",
        "concave·%1": "konkav·%1",
        "linear": "linear",
        "convex·%1": "konvex·%1",
        "Follows the screen brightness": "Folgt der Bildschirmhelligkeit",
        "Manual level — click to follow the screen again": "Manuelle Helligkeit – klicken, um wieder dem Bildschirm zu folgen",
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
        "Only in daylight": "Nur bei Tageslicht",
        "Only in dark mode": "Nur im dunklen Farbschema",
        "Only in light mode": "Nur im hellen Farbschema",
        "Never": "Nie",
        "Lid": "Deckel",
        "On close": "Beim Schließen",
        "On open": "Beim Öffnen",
        "On both": "Bei beidem",
        "On close switches the bulbs off when the lid closes, on open switches them on when it opens.":
            "Beim Schließen schaltet die Lampen aus, wenn der Deckel zugeht, beim Öffnen schaltet sie ein, wenn er aufgeht.",
        "Middle click: on/off · Wheel: brighter/darker": "Mittelklick: ein/aus · Mausrad: heller/dunkler",
        "Colour": "Farbe",
        "Hue": "Farbton",
        "Saturation": "Sättigung",
        "Shift": "Verschiebung",
        "Screen colour": "Bildschirmfarbe",
        "Follow": "Folgen",
        [followTip]: "Verschiebt das Weiß der Lampe so, wie das Nachtlicht die Farben des Bildschirms verschiebt. "
            + "Solange an, legt der zweite Regler fest, wie stark: 100 % genau wie der Bildschirm, 0 % gar nicht, 200 % doppelt so stark.",
        "Screen %1 K": "Bildschirm %1 K",
        "Debug window": "Debug-Fenster",
        "Clap": "Klatschen",
        "A double clap or double finger snap switches this widget's lights on or off. Clap, snap or both, microphone and sensitivity: Configure → Sound.":
            "Doppeltes Klatschen oder Fingerschnippen schaltet die Lampen dieses Widgets ein oder aus. Klatschen, Schnippen oder beides, Mikrofon und Empfindlichkeit: Einrichten → Ton.",
        "Needs extra packages: Configure → Sound.": "Braucht zusätzliche Pakete: Einrichten → Ton.",
        "The microphone is muted: claps are not heard.": "Das Mikrofon ist stummgeschaltet: Klatschen wird nicht gehört.",
        "Manual: hold the current brightness": "Manuell: die aktuelle Helligkeit halten",
        "Automatic: follow the screen brightness": "Automatisch: der Bildschirmhelligkeit folgen",
    })
    function tr(text) { return (german && de[text]) ? de[text] : text }

    // Menu entries: daemon value, label.
    readonly property var modeChoices: [["maximum", "Right window handle"],
                                        ["minimum", "Left window handle"],
                                        ["coupling", "Curve"],
                                        ["independent", "Brightness (screen ignored)"],
                                        ["off", "Nothing"]]
    readonly property var trackChoices: [["always", "Always"], ["night", "Only with Night Light"],
                                         ["day", "Only in daylight"], ["dark", "Only in dark mode"],
                                         ["light", "Only in light mode"], ["never", "Never"]]
    readonly property var lidChoices: [["close", "On close"], ["open", "On open"], ["both", "On both"], ["never", "Never"]]
    function choiceLabel(choices, value) {
        for (const c of choices) if (c[0] === value) return tr(c[1])
        return "–"
    }

    // The header's bulb box: "all" and this widget's bulbs. Replaced only
    // when ids or names change, so the open box is not reset by every poll;
    // the delegates look up power and icons themselves.
    property var bulbChoices: [{ "id": "all", "name": tr("All bulbs") }]
    property string bulbChoicesKey: ""
    function updateBulbChoices() {
        const list = widgetBulbs()
        const key = JSON.stringify(list.map(b => [b.id, b.name]))
        if (key === bulbChoicesKey) return
        bulbChoicesKey = key
        bulbChoices = [{ "id": "all", "name": tr("All bulbs") }]
                      .concat(list.map(b => ({ "id": b.id, "name": b.name || b.id })))
    }
    onStChanged: { updateBulbChoices(); syncClapLast() }

    // This widget's used bulbs (`in_widget`), in list order.
    function widgetBulbs() {
        const ids = st.in_widget || []
        return (st.bulbs_list || []).filter(b => ids.includes(b.id))
    }
    function bulbById(id) {
        for (const b of st.bulbs_list || []) if (b.id === id) return b
        return null
    }
    // The bulbs the controls act on: the selected one, or the widget's.
    function selectionBulbs() {
        const b = selectedBulb()
        return b ? [b] : widgetBulbs()
    }
    function selectionHas(cap) { return selectionBulbs().some(b => (b.caps || []).includes(cap)) }
    readonly property bool selHs: reachable && selectionHas("hs")
    readonly property bool selCt: reachable && selectionHas("ct")
    readonly property bool followCt: reachable && !!st.follow_ct

    // Mouse wheel on the tray icon and the sliders (Configure: invertScroll).
    readonly property bool invertScroll: !!Plasmoid.configuration.invertScroll
    // The brightness slider moves in the selection's native step (`step`,
    // percent); "all": the finest of the widget's bulbs.
    readonly property real levelStep: {
        const steps = selectionBulbs().map(b => Number(b.step)).filter(x => x > 0)
        return steps.length ? Math.min(...steps) : 1
    }
    function levelText(value) {
        return Number(value).toFixed(levelStep >= 1 ? 0 : 1)
    }
    // The selection follows the screen, or a manual level overrides it; while
    // the screen is ignored anyway (mode independent, track not holding) and
    // nothing is manual, there is nothing to show.
    readonly property bool manualLevel: reachable && st.following === false
    readonly property bool screenRules: reachable && st.mode !== "independent" && st.tracking !== false

    // Manual / automatic: manual holds the selection at its current level,
    // `resume` follows the screen again. Lit bulbs all at one level: `dim`
    // that level (exact, so nothing changes, and `get` keeps reporting it);
    // otherwise `pause`, which sends nothing (but `get` reports no output
    // until the next level). A click on the mode already set changes
    // nothing; modeTick re-evaluates the checked bindings the click toggled.
    property int modeTick: 0
    function setManual(manual) {
        modeTick++
        if (!reachable || manual === manualLevel) return
        if (!manual) {
            ctl("resume")
            return
        }
        const levels = selectionBulbs().filter(b => b.power !== false && b.online !== false).map(b => b.output)
        const one = levels.length > 0
                    && levels.every(x => typeof x === "number" && x > 0 && Math.abs(x - levels[0]) < 0.05)
        if (one) hc.run(["dim", levels[0]], reply => { if (reply.error) hc.reachable = false; else ctl("get") })
        else ctl("pause")
    }
    readonly property string manualIcon: "transform-browse-symbolic"
    readonly property string autoIcon: String(Qt.resolvedUrl("../icons/auto-mode.svg"))

    // Clap: on/off in the header; "on" is this widget's last mode but off,
    // kept in its configuration (Configure → Sound chooses it).
    readonly property bool clapAvailable: !st.clap_status || st.clap_status.available !== false
    readonly property bool clapOn: reachable && !!st.clap && st.clap !== "off"
    readonly property bool clapMuted: !!st.clap_status && st.clap_status.muted === true
    readonly property string clapLast: Plasmoid.configuration.clapLast || "both"
    // From st itself: clapOn may not be re-evaluated yet when stChanged runs.
    function syncClapLast() {
        const mode = st.clap
        if (["clap", "snap", "both"].includes(mode) && mode !== Plasmoid.configuration.clapLast)
            Plasmoid.configuration.clapLast = mode
    }
    // In the system tray the tray's heading shows the status and mode
    // actions; elsewhere (desktop, plasmawindowed) our own heading does.
    readonly property bool trayHeading: (Plasmoid.containmentDisplayHints & PlasmaCore.Types.ContainmentDrawsPlasmoidHeading) !== 0

    // ----------------------------------------------------------- icons
    // A bulb's own icon (name or absolute path) where it has one, else the
    // lighthouse. {source, mask, dim, badge}: theme masks only for the
    // lighthouse; a lit-only icon stands for "off" at half opacity, and for
    // "disconnected" at 40 % with a network-disconnect badge.
    function lighthouse(name) { return String(Qt.resolvedUrl("../icons/lighthouse-" + name + ".svg")) }
    function iconUrl(icon) { return icon.startsWith("/") ? "file://" + icon : icon }
    function bulbLit(b) { return b.power !== false && b.output > 0 }
    function bulbDisconnected(b) { return b.connected === false || b.online === false }
    function iconFor(bulb, lit, disconnected) {
        if (disconnected) {
            if (bulb && bulb.icon_disconnected)
                return { "source": iconUrl(bulb.icon_disconnected), "mask": false, "dim": false, "badge": false }
            if (bulb && bulb.icon_on)
                return { "source": iconUrl(bulb.icon_on), "mask": false, "dim": 0.4, "badge": true }
            return { "source": lighthouse("disconnected"), "mask": true, "dim": false, "badge": false }
        }
        if (bulb && bulb.icon_on) {
            if (lit) return { "source": iconUrl(bulb.icon_on), "mask": false, "dim": false, "badge": false }
            if (bulb.icon_off) return { "source": iconUrl(bulb.icon_off), "mask": false, "dim": false, "badge": false }
            return { "source": iconUrl(bulb.icon_on), "mask": false, "dim": true, "badge": false }
        }
        return { "source": lighthouse(lit ? "on" : "off"), "mask": true, "dim": false, "badge": false }
    }
    // dim: false, true (half) or an opacity
    function iconOpacity(icon) {
        return icon.dim === true ? 0.5 : (typeof icon.dim === "number" ? icon.dim : 1)
    }
    // One entry of the header's box ("all" or a bulb id).
    function choiceIcon(id) {
        if (!reachable) return iconFor(null, false, true)
        if (id === "all") {
            const list = widgetBulbs()
            return iconFor(null, list.some(bulbLit), list.some(bulbDisconnected))
        }
        const b = bulbById(id)
        return b ? iconFor(b, bulbLit(b), bulbDisconnected(b)) : iconFor(null, false, false)
    }
    readonly property var trayIcon: iconFor(selectedBulb(), lit, disconnected)

    // ---------------------------------------------------------- daemon IO
    // HausCtl sends `@N CMD …` and keeps the last get object (st); a refusal
    // or a missing daemon counts as unreachable until the next get answers.
    function ctl(...args) {
        hc.run(args, reply => { if (reply.error) hc.reachable = false })
    }
    function set(key, value) { ctl("set", key + "=" + value) }

    // The debug window runs on its own, detached from the widget.
    P5Support.DataSource {
        id: launcher
        engine: "executable"
        connectedSources: []
        onNewData: source => disconnectSource(source)
    }
    function openDebugWindow() {
        launcher.connectSource('h="$HOME/.local/bin/hauslicht"; [ -x "$h" ] || h=hauslicht; '
                               + 'setsid -f "$h" debug >/dev/null 2>&1 # ' + Date.now())
    }
    // HighPriority actions: the system tray shows them as icon buttons in
    // its heading (plasma-workspace applets/systemtray/qml/
    // ExpandedRepresentation.qml: icon from icon.name, tooltip = text).
    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: root.connectionText()
            icon.name: root.connectionIcon
            priority: PlasmaCore.Action.HighPriority
            onTriggered: root.ctl("get")
        },
        PlasmaCore.Action {
            text: root.tr("Manual: hold the current brightness")
            icon.name: root.manualIcon
            priority: PlasmaCore.Action.HighPriority
            checkable: true
            checked: root.modeTick >= 0 && root.manualLevel
            enabled: root.reachable
            onTriggered: root.setManual(true)
        },
        PlasmaCore.Action {
            text: root.tr("Automatic: follow the screen brightness")
            icon.name: root.autoIcon
            priority: PlasmaCore.Action.HighPriority
            checkable: true
            checked: root.modeTick >= 0 && root.reachable && !root.manualLevel
            enabled: root.reachable
            onTriggered: root.setManual(false)
        },
        PlasmaCore.Action {
            text: root.tr("Debug window")
            icon.name: "debug-run"
            onTriggered: root.openDebugWindow()
        }
    ]

    Timer {
        interval: root.expanded ? 2000 : 15000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.ctl("get")
    }

    // ------------------------------------------------------- tray / tooltip
    // The top-level `get` fields describe the selection, so icon, middle
    // click (toggle) and wheel (nudge) follow the header's choice.
    Plasmoid.icon: root.trayIcon.source
    Plasmoid.status: PlasmaCore.Types.ActiveStatus
    toolTipMainText: "Haus.Licht"
    toolTipSubText: selectionName() + ": " + connectionText() + "\n"
                    + tr("Middle click: on/off · Wheel: brighter/darker")

    readonly property string connectionIcon: !reachable ? "dialog-error"
        : st.online === false ? "network-disconnect"
        : st.connected ? "network-connect" : "view-refresh"
    function connectionText() {
        if (!reachable) return tr("Service not running")
        return st.connected === false || st.online === false ? tr("Disconnected") : tr("Connected")
    }
    function selectedBulb() {
        for (const b of st.bulbs_list || []) if (b.id === st.selected) return b
        return null
    }
    function selectionName() {
        const b = selectedBulb()
        return b ? (b.name || b.id) : tr("All bulbs")
    }
    function kindTitle(kind) {
        for (const be of st.backends || []) if (be.kind === kind) return be.title || kind
        return kind
    }
    function selectionTip() {
        if (!reachable) return ""
        const b = selectedBulb()
        if (!b) return tr("Profile: %1").arg(st.profile || "default")
        const parts = [tr("Kind: %1").arg(kindTitle(b.kind)), tr("Profile: %1").arg(b.profile)]
        if (b.experimental) parts.push(tr("experimental"))
        return parts.join("\n")
    }
    // The keyboard slider's binding, shown in the value of the row it moves.
    function kbdMark(key) { return st.kbd_binds === key ? " ⌨" : "" }

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
            const up = (notches > 0) !== root.invertScroll
            for (let i = 0; i < Math.abs(notches); i++) root.ctl("nudge", up ? 5 : -5)
            wheelRefresh.restart()
        }
        Timer { id: wheelRefresh; interval: 300; onTriggered: root.ctl("get") }
        Kirigami.Icon {
            anchors.fill: parent
            source: root.trayIcon.source
            isMask: root.trayIcon.mask
            opacity: root.iconOpacity(root.trayIcon)
            active: parent.containsMouse
        }
        DisconnectBadge { visible: !!root.trayIcon.badge }
    }

    // The small network-disconnect mark on a bulb's own icon (bottom right).
    component DisconnectBadge: Kirigami.Icon {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        width: Math.max(Kirigami.Units.iconSizes.small / 2, parent.width / 2)
        height: width
        source: "network-disconnect"
    }

    // On a slider: the wheel with the direction reversed (Configure: Invert
    // scroll direction). Off, Plasma's own wheel handling of the slider acts.
    component InvertedWheel: MouseArea {
        id: iw
        property int wheelDelta: 0
        anchors.fill: parent
        enabled: root.invertScroll
        acceptedButtons: Qt.NoButton
        onWheel: wheel => {
            const slider = iw.parent
            const lastValue = slider.value
            // as PC3.Slider does, negated
            wheelDelta -= (wheel.angleDelta.y || -wheel.angleDelta.x) * (wheel.inverted ? -1 : 1)
            while (wheelDelta >= 120) { wheelDelta -= 120; slider.increase() }
            while (wheelDelta <= -120) { wheelDelta += 120; slider.decrease() }
            if (lastValue !== slider.value) slider.moved()
        }
    }

    // A label and a switch in the header; the label toggles too, both show the tip.
    // switchEnabled: false greys out the switch but keeps the tip; dim fades the label.
    component HeaderSwitch: RowLayout {
        id: hs
        property string label
        property string tip
        property bool checkedState: false
        property bool switchEnabled: true
        property bool dim: false
        signal switched(bool checked)
        spacing: Kirigami.Units.smallSpacing
        PC3.Label {
            text: hs.label
            textFormat: Text.PlainText
            opacity: hs.dim || !hs.switchEnabled ? 0.6 : 1
            TapHandler { onTapped: if (sw.enabled) { sw.toggle(); hs.switched(sw.checked) } }
        }
        PC3.Switch {
            id: sw
            enabled: hs.switchEnabled
            checked: hs.checkedState
            Accessible.name: hs.label
            Accessible.description: hs.tip
            onToggled: hs.switched(checked)
        }
        HoverHandler { id: hover }
        PC3.ToolTip {
            text: hs.tip
            visible: hover.hovered && hs.tip !== ""
            delay: Kirigami.Units.toolTipDelay
        }
    }

    // Manual / automatic outside the tray: icon-only, checkable, one checked.
    component ModeButton: PC3.ToolButton {
        id: mb
        property bool manual
        property string iconSource
        visible: !root.trayHeading
        enabled: root.reachable
        display: PC3.AbstractButton.IconOnly
        icon.source: mb.iconSource
        checkable: true
        checked: root.modeTick >= 0 && root.reachable && root.manualLevel === mb.manual
        onClicked: root.setManual(mb.manual)
        Accessible.name: mb.text
        PC3.ToolTip {
            text: mb.text
            visible: mb.hovered
            delay: Kirigami.Units.toolTipDelay
        }
    }

    // A labelled slider inside a row (the colour row holds two side by side):
    // name … value above, the slider below (added by the user of the cell).
    component SliderCell: ColumnLayout {
        id: cell
        property string label
        property string valueText
        property string tip
        Layout.fillWidth: true
        Layout.preferredWidth: 1  // cells share the row equally
        spacing: 0
        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            PC3.Label {
                Layout.fillWidth: true
                text: cell.label
                textFormat: Text.PlainText
                elide: Text.ElideRight
                font: Kirigami.Theme.smallFont
                opacity: cell.enabled ? 1 : 0.6
            }
            PC3.Label {
                text: cell.valueText
                textFormat: Text.PlainText
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                font.features: { "tnum": 1 }
                opacity: cell.enabled ? 1 : 0.6
            }
        }
        HoverHandler { id: cellHover }
        PC3.ToolTip {
            text: cell.tip
            visible: cellHover.hovered && cell.tip !== ""
            delay: Kirigami.Units.toolTipDelay
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
        // optional small icon left of the title (the Brightness row's monitor)
        property string leadIcon: ""
        property real leadOpacity: 1
        property string leadTip: ""
        property bool leadClickable: false
        signal leadClicked()
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
                    Kirigami.Icon {
                        visible: row.leadIcon !== ""
                        Layout.preferredWidth: Kirigami.Units.iconSizes.small
                        Layout.preferredHeight: Kirigami.Units.iconSizes.small
                        source: row.leadIcon
                        opacity: row.leadOpacity
                        active: row.leadClickable && leadHover.hovered
                        Accessible.role: row.leadClickable ? Accessible.Button : Accessible.Graphic
                        Accessible.name: row.leadTip
                        HoverHandler {
                            id: leadHover
                            cursorShape: row.leadClickable ? Qt.PointingHandCursor : Qt.ArrowCursor
                        }
                        TapHandler { enabled: row.leadClickable; onTapped: row.leadClicked() }
                        PC3.ToolTip {
                            text: row.leadTip
                            visible: leadHover.hovered && row.leadTip !== ""
                            delay: Kirigami.Units.toolTipDelay
                        }
                    }
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
        readonly property int margin: Kirigami.Units.largeSpacing
        Layout.minimumWidth: Kirigami.Units.gridUnit * 16
        // Wide enough for the header's switches (and, outside the tray, its
        // status and mode buttons) in any language; the tray has its own size.
        Layout.preferredWidth: Math.max(Kirigami.Units.gridUnit * 18,
                                        headRow.implicitWidth + heading.leftPadding + heading.rightPadding)
        // Desktop / plasmawindowed: as tall as the rows. The tray ignores it
        // and the ScrollView takes over.
        Layout.minimumHeight: Kirigami.Units.gridUnit * 10
        Layout.preferredHeight: content.implicitHeight + margin * 2 + (header ? header.implicitHeight : 0)
        collapseMarginsHint: true

        header: PlasmaExtras.PlasmoidHeading {
            id: heading
            RowLayout {
                id: headRow
                anchors.fill: parent
                spacing: Kirigami.Units.smallSpacing

                PC3.ComboBox {
                    id: bulbBox
                    // Takes the room the switches leave, never less than this.
                    Layout.fillWidth: true
                    Layout.minimumWidth: Kirigami.Units.gridUnit * 6
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 6
                    enabled: root.reachable
                    model: root.bulbChoices
                    textRole: "name"
                    valueRole: "id"
                    Accessible.description: root.selectionTip()
                    onModelChanged: currentIndex = Math.max(0, indexOfValue(root.st.selected || "all"))
                    onActivated: index => root.ctl("select", valueAt(index))
                    delegate: PC3.ItemDelegate {
                        id: choice
                        required property var modelData
                        required property int index
                        readonly property var bulbIcon: root.choiceIcon(modelData.id)
                        width: ListView.view ? ListView.view.width : implicitWidth
                        text: modelData.name
                        highlighted: bulbBox.highlightedIndex === index
                        hoverEnabled: bulbBox.hoverEnabled
                        contentItem: RowLayout {
                            spacing: Kirigami.Units.smallSpacing
                            Item {
                                Layout.preferredWidth: Kirigami.Units.iconSizes.small
                                Layout.preferredHeight: Kirigami.Units.iconSizes.small
                                Kirigami.Icon {
                                    anchors.fill: parent
                                    source: choice.bulbIcon.source
                                    isMask: choice.bulbIcon.mask
                                    opacity: root.iconOpacity(choice.bulbIcon)
                                }
                                DisconnectBadge { visible: !!choice.bulbIcon.badge }
                            }
                            PC3.Label {
                                Layout.fillWidth: true
                                text: choice.text
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                            }
                        }
                    }
                    PC3.ToolTip {
                        text: root.selectionTip()
                        visible: bulbBox.hovered && !bulbBox.popup.visible && text !== ""
                        delay: Kirigami.Units.toolTipDelay
                    }
                }

                // Clap: off <-> this widget's last mode (Configure → Sound).
                HeaderSwitch {
                    Layout.leftMargin: Kirigami.Units.largeSpacing
                    label: root.tr("Clap")
                    tip: !root.clapAvailable ? root.tr("Needs extra packages: Configure → Sound.")
                        : root.tr("A double clap or double finger snap switches this widget's lights on or off. Clap, snap or both, microphone and sensitivity: Configure → Sound.")
                          + (root.clapMuted ? "\n" + root.tr("The microphone is muted: claps are not heard.") : "")
                    checkedState: root.clapOn
                    enabled: root.reachable
                    switchEnabled: root.clapAvailable
                    dim: root.clapMuted
                    onSwitched: checked => root.set("clap", checked ? root.clapLast : "off")
                }
                HeaderSwitch {
                    Layout.leftMargin: Kirigami.Units.largeSpacing
                    label: root.tr("Flick")
                    tip: root.tr(root.flickTip)
                    checkedState: root.reachable && !!root.st.flick && root.st.flick !== "off"  // a mode since schema 4
                    enabled: root.reachable
                    // off <-> the last mode but off (fast/exclusive survive toggling); one
                    // that needs a setup no longer current comes back as plain on
                    onSwitched: checked => root.set("flick", !checked ? "off"
                        : (root.st.flick_last && (root.st.flick_last === "on"
                           || (root.st.setup && root.st.setup.status === "current")) ? root.st.flick_last : "on"))
                }
                HeaderSwitch {
                    Layout.leftMargin: Kirigami.Units.largeSpacing
                    label: root.tr("On/Off")
                    tip: root.tr("Switches the selected bulb, or all bulbs. Middle click on the tray icon does the same.")
                    checkedState: root.reachable && root.st.power !== false
                    enabled: root.reachable
                    onSwitched: checked => root.ctl(checked ? "on" : "off")
                }

                // Not in the system tray: no tray heading shows the status and
                // mode actions, so they sit here.
                Kirigami.Icon {
                    visible: !root.trayHeading
                    Layout.leftMargin: Kirigami.Units.largeSpacing
                    implicitWidth: Kirigami.Units.iconSizes.small
                    implicitHeight: implicitWidth
                    source: root.connectionIcon
                    Accessible.role: Accessible.Indicator
                    Accessible.name: root.connectionText()
                    PC3.ToolTip {
                        text: root.connectionText()
                        visible: iconHover.hovered
                        delay: Kirigami.Units.toolTipDelay
                    }
                    HoverHandler { id: iconHover }
                }
                ModeButton {
                    manual: true
                    iconSource: root.manualIcon
                    text: root.tr("Manual: hold the current brightness")
                }
                ModeButton {
                    manual: false
                    iconSource: root.autoIcon
                    text: root.tr("Automatic: follow the screen brightness")
                }
            }
        }

        PC3.ScrollView {
            id: scroll
            anchors.fill: parent
            contentWidth: availableWidth
            contentHeight: content.implicitHeight + popup.margin * 2
            PC3.ScrollBar.horizontal.policy: PC3.ScrollBar.AlwaysOff

            ColumnLayout {
                id: content
                x: popup.margin
                y: popup.margin
                width: scroll.availableWidth - popup.margin * 2
                spacing: Kirigami.Units.smallSpacing * 2
                enabled: root.reachable

                // Brightness: what the bulbs show; sets the level where it rules.
                ControlRow {
                    iconName: "brightness-high"
                    title: root.tr("Brightness")
                    valueText: (level.pressed || levelSend.running ? root.levelText(level.value)
                                : (root.st.bulbs === null || root.st.bulbs === undefined ? "–" : root.levelText(root.st.bulbs)))
                               + " %" + root.kbdMark("level")
                    leadIcon: root.manualLevel || root.screenRules ? "video-display" : ""
                    leadOpacity: root.manualLevel ? 0.4 : 1
                    leadClickable: root.manualLevel
                    leadTip: root.manualLevel ? root.tr("Manual level — click to follow the screen again")
                                              : root.tr("Follows the screen brightness")
                    onLeadClicked: root.ctl("resume")
                    PC3.Slider {
                        id: level
                        Layout.fillWidth: true
                        from: 0; to: 100; stepSize: root.levelStep
                        onMoved: levelSend.restart()
                        Timer { id: levelSend; interval: 250; onTriggered: root.ctl("bulb", level.value) }
                        InvertedWheel {}
                    }
                }

                // Colour, for bulbs with `hs` (hue, saturation) or `ct`: while
                // following Night Light the hue rests and the second slider
                // says how strongly the white shifts. ct-only bulbs show only
                // that, and only while following.
                ControlRow {
                    visible: root.selHs || (root.selCt && root.followCt)
                    iconName: "color-management"
                    title: root.tr("Colour")
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.largeSpacing

                        SliderCell {
                            visible: root.selHs
                            enabled: !root.followCt
                            label: root.tr("Hue")
                            valueText: Math.round(hue.value) + "°"
                            PC3.Slider {
                                id: hue
                                Layout.fillWidth: true
                                from: 0; to: 360; stepSize: 1
                                onMoved: hueSend.restart()
                                Timer { id: hueSend; interval: 250; onTriggered: root.set("hue", Math.round(hue.value)) }
                                InvertedWheel {}
                                background: Rectangle {
                                    x: hue.leftPadding
                                    y: hue.topPadding + (hue.availableHeight - height) / 2
                                    width: hue.availableWidth
                                    height: Kirigami.Units.smallSpacing * 1.5
                                    radius: height / 2
                                    opacity: hue.enabled ? 1 : 0.35
                                    gradient: Gradient {
                                        orientation: Gradient.Horizontal
                                        GradientStop { position: 0 / 6; color: "#ff0000" }
                                        GradientStop { position: 1 / 6; color: "#ffff00" }
                                        GradientStop { position: 2 / 6; color: "#00ff00" }
                                        GradientStop { position: 3 / 6; color: "#00ffff" }
                                        GradientStop { position: 4 / 6; color: "#0000ff" }
                                        GradientStop { position: 5 / 6; color: "#ff00ff" }
                                        GradientStop { position: 6 / 6; color: "#ff0000" }
                                    }
                                }
                            }
                        }
                        SliderCell {
                            visible: root.selHs && !root.followCt
                            label: root.tr("Saturation")
                            valueText: Math.round(sat.value) + " %"
                            PC3.Slider {
                                id: sat
                                Layout.fillWidth: true
                                from: 0; to: 100; stepSize: 1
                                onMoved: satSend.restart()
                                Timer { id: satSend; interval: 250; onTriggered: root.set("sat", Math.round(sat.value)) }
                                InvertedWheel {}
                                background: Rectangle {
                                    x: sat.leftPadding
                                    y: sat.topPadding + (sat.availableHeight - height) / 2
                                    width: sat.availableWidth
                                    height: Kirigami.Units.smallSpacing * 1.5
                                    radius: height / 2
                                    gradient: Gradient {
                                        orientation: Gradient.Horizontal
                                        GradientStop { position: 0; color: "#ffffff" }
                                        GradientStop { position: 1; color: Qt.hsva(hue.value / 360, 1, 1, 1) }
                                    }
                                }
                            }
                        }
                        SliderCell {
                            visible: root.followCt
                            label: root.tr("Shift")
                            valueText: Math.round(shift.value) + " %"
                            tip: root.tr(root.followTip)
                            Item {
                                Layout.fillWidth: true
                                implicitHeight: shift.implicitHeight
                                // the mark at 100 %: the same shift as the screen
                                Rectangle {
                                    x: shift.leftPadding + shift.availableWidth / 2 - width / 2
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: Kirigami.Units.smallSpacing / 2
                                    height: Kirigami.Units.smallSpacing * 3
                                    color: Kirigami.Theme.textColor
                                    opacity: 0.5
                                }
                                PC3.Slider {
                                    id: shift
                                    anchors.fill: parent
                                    from: 0; to: 200; stepSize: 1
                                    onMoved: shiftSend.restart()
                                    Timer { id: shiftSend; interval: 250; onTriggered: root.set("shift", Math.round(shift.value)) }
                                    InvertedWheel {}
                                }
                            }
                        }
                    }
                }

                // Screen colour: follow Night Light's white.
                ControlRow {
                    visible: root.selHs || root.selCt
                    iconName: "redshift-status-on"
                    title: root.tr("Screen colour")
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing
                        PC3.Label {
                            visible: root.followCt && typeof root.st.screen_kelvin === "number"
                            text: root.tr("Screen %1 K").arg(Math.round(root.st.screen_kelvin))
                            textFormat: Text.PlainText
                            font: Kirigami.Theme.smallFont
                            opacity: 0.7
                        }
                        Item { Layout.fillWidth: true }
                        PC3.Switch {
                            id: followSwitch
                            text: root.tr("Follow")
                            Accessible.description: root.tr(root.followTip)
                            onToggled: root.set("follow_ct", checked ? "on" : "off")
                            PC3.ToolTip {
                                text: root.tr(root.followTip)
                                visible: followSwitch.hovered
                                delay: Kirigami.Units.toolTipDelay
                            }
                        }
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
                    // > 0 rises slowly first (convex), < 0 fast (concave); N its strength
                    valueText: (Math.round(curve.value) === 0 ? root.tr("linear")
                                : root.tr(curve.value > 0 ? "convex·%1" : "concave·%1").arg(Math.round(Math.abs(curve.value))))
                               + root.kbdMark("curve")
                    PC3.Slider {
                        id: curve
                        Layout.fillWidth: true
                        from: -100; to: 100; stepSize: 1
                        onMoved: curveSend.restart()
                        Timer { id: curveSend; interval: 250; onTriggered: root.set("curve", curve.value) }
                        InvertedWheel {}
                    }
                }

                // Drop-down rows: keyboard slider, follow screen, lid.
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

                // Turn off at 0 %: a switch row like Screen colour's, the switch at the right.
                ControlRow {
                    iconName: "system-shutdown"
                    title: root.tr("Turn off at 0 %")
                    tip: root.tr("Otherwise a 0.5 % glow stays at 0 %.")
                    PC3.Switch {
                        id: offAtZero
                        Layout.alignment: Qt.AlignRight
                        checked: !!root.st.off_at_zero
                        Accessible.name: root.tr("Turn off at 0 %")
                        Accessible.description: root.tr("Otherwise a 0.5 % glow stays at 0 %.")
                        onToggled: root.set("off_at_zero", checked ? "on" : "off")
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
            }
        }

        // Pull the daemon's values into the controls, except while in use.
        Connections {
            target: root
            function onStChanged() {
                if (!root.reachable) return
                if (!bulbBox.popup.visible)
                    bulbBox.currentIndex = Math.max(0, bulbBox.indexOfValue(root.st.selected || "all"))
                if (!range.first.pressed && !range.second.pressed && !rangeSend.running)
                    range.setValues(root.st.range_lo, root.st.range_hi)
                if (!curve.pressed && !curveSend.running)
                    curve.value = root.st.curve
                if (!level.pressed && !levelSend.running && root.st.bulbs !== null && root.st.bulbs !== undefined)
                    level.value = root.st.bulbs
                if (!hue.pressed && !hueSend.running && typeof root.st.hue === "number")
                    hue.value = root.st.hue
                if (!sat.pressed && !satSend.running && typeof root.st.sat === "number")
                    sat.value = root.st.sat
                if (!shift.pressed && !shiftSend.running && typeof root.st.shift === "number")
                    shift.value = root.st.shift
                followSwitch.checked = !!root.st.follow_ct
                offAtZero.checked = !!root.st.off_at_zero
            }
        }
    }
}

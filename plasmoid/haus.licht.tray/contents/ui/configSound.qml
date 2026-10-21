/*
 * Configure → Sound: the microphone and sensitivity of clap detection
 * (global; haus/clap.py) and this widget's mode (clap, snap or both: what
 * its header's Clap switch turns on, config key clapLast). The daemon listens
 * while any widget's switch is on. Every daemon setting changes at once; the
 * mode is saved with the dialog (Apply / OK) and, while this widget's clap is
 * on, sent at once too. The test meter polls `clap-meter` only while this
 * page is shown, `clap-hits` (Recent sounds) every 2 s.
 */
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM
import org.kde.plasma.plasmoid

KCM.SimpleKCM {
    id: page

    property int cfg_widgetIndex: 1
    property int cfg_widgetIndexDefault: 1
    property string cfg_clapLast: "both"
    property string cfg_clapLastDefault: "both"

    property HausCtl ctl: HausCtl { widget: 1 }  // clap settings are global
    function tr(text, ...args) { return ctl.tr(text, ...args) }
    // This widget (`@N`): its clap mode is per widget.
    property HausCtl wctl: HausCtl { widget: page.cfg_widgetIndex; poll.running: false }
    readonly property bool widgetClapOn: !!wctl.st.clap && wctl.st.clap !== "off"
    readonly property var modeChoices: [{ "text": tr("Clap"), "value": "clap" },
                                        { "text": tr("Snap"), "value": "snap" },
                                        { "text": tr("Both"), "value": "both" }]

    readonly property var st: ctl.st
    readonly property var status: st.clap_status || ({})
    readonly property bool available: ctl.loaded && status.available !== false
    // Some widget has a mode: the daemon listens (or is getting there).
    readonly property bool listening: !!status.state && status.state !== "off"
    // The chosen source's mute switch: the daemon's while it listens, else
    // what clap-sources said.
    readonly property bool muted: status.muted === true
        || (status.muted !== false && sources.some(s => s.value === (st.clap_source || "") && s.muted))

    property string message: ""
    function act(args) {
        ctl.run(args, reply => {
            page.message = reply.error || ""
            if (!reply.error && !reply.bulbs_list) ctl.refresh()
        })
    }

    // Microphones: "Default microphone" first, then what pactl lists.
    property var sources: [{ "text": tr("Default microphone"), "value": "", "muted": false }]
    function loadSources() {
        ctl.run(["clap-sources"], reply => {
            if (reply.error || !reply.sources) return
            const def = reply.sources.find(s => s.default)
            page.sources = [{ "text": tr("Default microphone"), "value": "", "muted": !!(def && def.muted) }].concat(
                reply.sources.map(s => ({ "text": s.description || s.name, "value": s.name, "muted": !!s.muted })))
        })
    }

    // The test meter: polled only while the page is visible and detection runs.
    property var meter: ({})
    Timer {
        interval: 250
        repeat: true
        triggeredOnStart: true
        running: page.visible && page.available && page.listening
        onTriggered: if (!busy) {
            busy = true
            page.ctl.run(["clap-meter"], reply => { busy = false; if (!reply.error) page.meter = reply })
        }
        property bool busy: false
    }
    function lastText() {
        const last = meter.last
        if (!last) return tr("Last: –")
        let what = tr(last.label) + " " + Number(last.score).toFixed(2)
        if (last.mirrored) what += " " + tr("(from the speakers)")
        else if (!last.accepted) what += " " + tr("(ignored)")
        return tr("Last: %1 · %2 s ago", what, Math.round(last.age))
    }

    // Recent sounds: the detector's last hits (`clap-hits`), newest first.
    property var hits: ({})
    readonly property var hitList: (hits.hits || []).slice().reverse()
    Timer {
        interval: 2000
        repeat: true
        triggeredOnStart: true
        running: page.visible && page.available && page.listening
        onTriggered: if (!busy) {
            busy = true
            page.ctl.run(["clap-hits"], reply => { busy = false; if (!reply.error) page.hits = reply })
        }
        property bool busy: false
    }
    function hitsSummary() {
        const list = hits.hits || []
        const paired = list.filter(h => h.paired).length
        return tr("Heard %1 · accepted %2 · doubles %3", list.length,
                  list.filter(h => h.accepted).length, Math.ceil(paired / 2))
    }
    function barsText() {
        const bars = hits.bars || {}
        const keys = Object.keys(bars)
        if (!keys.length) return ""
        return tr("Bars: %1", keys.map(k => tr(k) + " " + Number(bars[k]).toFixed(2)).join(" · "))
    }
    function hitLabel(h) {
        if (h.kind) return tr(h.kind)
        return h.top && h.top.length ? String(h.top[0][0]) : "–"
    }
    // (no flatMap in QML's JavaScript)
    readonly property var hitCells: [].concat(...hitList.map(h => [
        { "text": String(h.time || "").slice(0, 8), "dim": true },
        { "text": hitLabel(h), "tip": (h.top || []).map(t => t[0] + " " + Number(t[1]).toFixed(2)).join("\n") },
        { "text": tr("family %1", Number(h.family || 0).toFixed(2)) },
        { "text": hitVerdict(h), "fill": true, "dim": !h.accepted },
        { "text": h.paired ? "×2" : "", "tip": h.paired ? tr("part of a double") : "" }
    ]))
    function hitVerdict(h) {
        if (h.accepted) return "✓"
        return h.why === "speakers" ? tr("speakers") : (h.why || "")
    }

    Component.onCompleted: loadSources()
    onVisibleChanged: if (visible) { ctl.refresh(); wctl.refresh(); loadSources() }

    ColumnLayout {
        spacing: Kirigami.Units.largeSpacing

        Kirigami.InlineMessage {
            Layout.fillWidth: true
            type: Kirigami.MessageType.Warning
            visible: !page.ctl.reachable
            text: page.tr("The Haus.Licht service does not answer. Start it with: systemctl --user start hauslicht")
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            type: Kirigami.MessageType.Error
            visible: page.message !== ""
            text: page.message
        }

        // The extra is missing: the install command instead of the controls.
        ColumnLayout {
            visible: page.ctl.loaded && !page.available
            Layout.fillWidth: true
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: page.tr("Clap detection needs extra packages (%1). Install them with:", (page.status.missing || []).join(", "))
            }
            QQC2.TextField {
                Layout.fillWidth: true
                readOnly: true
                selectByMouse: true
                text: page.status.install || ""
                font.family: "monospace"
            }
        }

        Kirigami.FormLayout {
            visible: page.available
            Layout.fillWidth: true

            QQC2.ComboBox {
                id: modeBox
                Kirigami.FormData.label: page.tr("This widget:")
                textRole: "text"
                valueRole: "value"
                model: page.modeChoices
                // count: re-evaluated once the model's entries exist
                currentIndex: count > 0 ? Math.max(0, indexOfValue(page.cfg_clapLast || "both")) : -1
                onActivated: {
                    page.cfg_clapLast = currentValue
                    if (page.widgetClapOn)
                        page.wctl.run(["set", "clap=" + currentValue], reply => {
                            page.message = reply.error || ""
                            if (!reply.error && !reply.bulbs_list) page.wctl.refresh()
                        })
                }
            }
            QQC2.ComboBox {
                id: sourceBox
                Kirigami.FormData.label: page.tr("Microphone:")
                Layout.fillWidth: true
                textRole: "text"
                valueRole: "value"
                model: page.sources
                currentIndex: Math.max(0, indexOfValue(page.st.clap_source || ""))
                onActivated: page.act(["set", "clap_source=" + (currentValue || "default")])
            }
            QQC2.Label {
                Layout.fillWidth: true
                visible: page.muted
                wrapMode: Text.Wrap
                text: page.tr("The microphone is muted: claps are not heard.")
            }
            RowLayout {
                Kirigami.FormData.label: page.tr("Sensitivity:")
                Layout.fillWidth: true
                QQC2.Label { text: page.tr("Low") }
                QQC2.Slider {
                    id: sens
                    Layout.fillWidth: true
                    from: 0
                    to: 100
                    stepSize: 5
                    value: typeof page.st.clap_sensitivity === "number" ? page.st.clap_sensitivity : 50
                    onPressedChanged: if (!pressed) page.act(["set", "clap_sensitivity=" + Math.round(value)])
                    onMoved: if (!pressed) page.act(["set", "clap_sensitivity=" + Math.round(value)])  // keys, wheel
                    // The wheel reversed while Invert scroll direction is on (as in
                    // main.qml); off, the style's own wheel handling acts.
                    MouseArea {
                        property int wheelDelta: 0
                        anchors.fill: parent
                        enabled: !!Plasmoid.configuration.invertScroll
                        acceptedButtons: Qt.NoButton
                        onWheel: wheel => {
                            const lastValue = sens.value
                            wheelDelta -= (wheel.angleDelta.y || -wheel.angleDelta.x) * (wheel.inverted ? -1 : 1)
                            while (wheelDelta >= 120) { wheelDelta -= 120; sens.increase() }
                            while (wheelDelta <= -120) { wheelDelta += 120; sens.decrease() }
                            if (lastValue !== sens.value) sens.moved()
                        }
                    }
                }
                QQC2.Label { text: page.tr("High") }
            }

            Item { Kirigami.FormData.isSection: true }

            QQC2.ProgressBar {
                Kirigami.FormData.label: page.tr("Test:")
                Layout.fillWidth: true
                from: 0
                to: 1
                value: page.listening && typeof page.meter.level === "number" ? page.meter.level : 0
                enabled: page.listening
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: !page.listening ? page.tr("Switch Clap on in a widget's popup to test.")
                      : page.meter.state && page.meter.state !== "listening"
                        ? page.tr(page.meter.state === "downloading" ? "Downloading the sound model…"
                                  : page.meter.state === "starting" ? "Starting…" : "Error: %1", page.meter.error || "")
                        : page.lastText()
            }
        }

        // ------------------------------------------------- recent sounds
        ColumnLayout {
            visible: page.available
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            Kirigami.Heading {
                level: 4
                text: page.tr("Recent sounds")
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                font: Kirigami.Theme.smallFont
                text: !page.listening ? page.tr("Clap detection is off")
                      : page.hitsSummary() + (page.barsText() ? "\n" + page.barsText() : "")
            }
            QQC2.Label {
                visible: page.listening && page.hitList.length === 0
                font: Kirigami.Theme.smallFont
                opacity: 0.7
                text: page.tr("No sounds heard yet.")
            }
            // time | kind or best class | family score | ✓ or why not | ×2 (part of a double)
            GridLayout {
                visible: page.listening && page.hitList.length > 0
                Layout.fillWidth: true
                columns: 5
                columnSpacing: Kirigami.Units.largeSpacing
                rowSpacing: 0
                Repeater {
                    // five cells per hit, row by row
                    model: page.listening ? page.hitCells : []
                    delegate: QQC2.Label {
                        id: cell
                        required property var modelData
                        Layout.fillWidth: !!modelData.fill
                        Layout.maximumWidth: modelData.fill ? Number.POSITIVE_INFINITY : Kirigami.Units.gridUnit * 8
                        text: modelData.text
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                        font.features: { "tnum": 1 }
                        opacity: modelData.dim ? 0.7 : 1
                        HoverHandler { id: cellHover }
                        QQC2.ToolTip.text: modelData.tip || cell.text
                        QQC2.ToolTip.visible: cellHover.hovered && (cell.truncated || !!modelData.tip)
                        QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                    }
                }
            }
        }

        QQC2.Label {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            opacity: 0.7
            text: page.tr("Sound is analysed on this computer only and never stored. Plasma shows the microphone in use.")
        }
    }
}

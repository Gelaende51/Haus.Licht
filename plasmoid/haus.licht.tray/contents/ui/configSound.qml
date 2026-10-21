/*
 * Configure → Sound: the microphone and sensitivity of clap detection
 * (global; haus/clap.py) and this widget's mode (clap, snap or both: what
 * its header's Clap switch turns on, config key clapLast). The daemon listens
 * while any widget's switch is on. Every daemon setting changes at once; the
 * mode is saved with the dialog (Apply / OK) and, while this widget's clap is
 * on, sent at once too. The test meter polls `clap-meter` only while this
 * page is shown.
 */
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM

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

        QQC2.Label {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            opacity: 0.7
            text: page.tr("Sound is analysed on this computer only and never stored. Plasma shows the microphone in use.")
        }
    }
}

/*
 * Configure → Sound: a double clap or snap toggles the lights (the daemon's
 * settings.clap, haus/clap.py). Every change goes to the daemon at once; the
 * test meter polls `clap-meter` only while this page is shown.
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

    property HausCtl ctl: HausCtl { widget: 1 }  // clap settings are global
    function tr(text, ...args) { return ctl.tr(text, ...args) }

    readonly property var st: ctl.st
    readonly property var status: st.clap_status || ({})
    readonly property bool available: ctl.loaded && status.available !== false
    readonly property string mode: st.clap || "off"

    property string message: ""
    function act(args) {
        ctl.run(args, reply => {
            page.message = reply.error || ""
            if (!reply.error && !reply.bulbs_list) ctl.refresh()
        })
    }

    readonly property var modeChoices: [["off", "Off"], ["clap", "Double clap"],
                                        ["snap", "Double snap"], ["both", "Both"]]

    // Microphones: "Default microphone" first, then what pactl lists.
    property var sources: [{ "text": tr("Default microphone"), "value": "" }]
    function loadSources() {
        ctl.run(["clap-sources"], reply => {
            if (reply.error || !reply.sources) return
            page.sources = [{ "text": tr("Default microphone"), "value": "" }].concat(
                reply.sources.map(s => ({ "text": s.description || s.name, "value": s.name })))
        })
    }

    // The test meter: polled only while the page is visible and detection runs.
    property var meter: ({})
    Timer {
        interval: 250
        repeat: true
        triggeredOnStart: true
        running: page.visible && page.available && page.mode !== "off"
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
    onVisibleChanged: if (visible) { ctl.refresh(); loadSources() }

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
                Kirigami.FormData.label: page.tr("Mode:")
                implicitContentWidthPolicy: QQC2.ComboBox.WidestText
                model: page.modeChoices.map(c => page.tr(c[1]))
                currentIndex: Math.max(0, page.modeChoices.findIndex(c => c[0] === page.mode))
                onActivated: index => page.act(["clap", page.modeChoices[index][0]])
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
                value: page.mode !== "off" && typeof page.meter.level === "number" ? page.meter.level : 0
                enabled: page.mode !== "off"
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: page.mode === "off" ? page.tr("Switch a mode on to test.")
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

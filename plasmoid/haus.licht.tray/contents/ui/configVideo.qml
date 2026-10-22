pragma ComponentBehavior: Bound
/*
 * Configure → Video: follow video (haus/videofollow.py). The global settings
 * (max brightness, time averaging, updates per second, shared with follow
 * audio), each bulb's region of the screen, and the screen permission the
 * portal granted (the restore token; forgetting it makes the next start ask
 * again). Every change goes to the daemon at once.
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

    property HausCtl ctl: HausCtl { widget: 1 }  // video settings are global, regions per bulb
    // "Connect new bulb…" while this page shows: switch to Bulbs.
    property AddBulbRedirect addBulb: AddBulbRedirect { configPage: page }
    function tr(text, ...args) { return ctl.tr(text, ...args) }

    readonly property var st: ctl.st
    readonly property var status: st.video_status || ({})

    property string message: ""
    function act(args) {
        ctl.run(args, reply => {
            page.message = reply.error || ""
            if (!reply.error && !reply.bulbs_list) ctl.refresh()
        })
    }

    function seconds(v) { return tr("%1 s", String(v).replace(".", Qt.locale().decimalPoint)) }
    readonly property var smoothChoices: [{ "text": tr("Off"), "value": 0 }].concat(
        [0.2, 0.5, 1, 2, 5].map(v => ({ "text": seconds(v), "value": v })))
    readonly property var regionChoices: [
        { "text": tr("Whole screen"), "value": "whole" },
        { "text": tr("Left"), "value": "left" },
        { "text": tr("Right"), "value": "right" },
        { "text": tr("Top"), "value": "top" },
        { "text": tr("Bottom"), "value": "bottom" },
        { "text": tr("Top left"), "value": "top-left" },
        { "text": tr("Top right"), "value": "top-right" },
        { "text": tr("Bottom left"), "value": "bottom-left" },
        { "text": tr("Bottom right"), "value": "bottom-right" }
    ]
    readonly property var usedBulbs: ctl.bulbs.filter(b => b.use !== false)

    onVisibleChanged: if (visible) ctl.refresh()

    ColumnLayout {
        spacing: Kirigami.Units.largeSpacing

        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: !page.ctl.reachable
            type: Kirigami.MessageType.Warning
            text: page.tr("The Haus.Licht service does not answer. Start it with: systemctl --user start hauslicht")
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: page.message !== ""
            type: Kirigami.MessageType.Error
            text: page.message
        }

        Kirigami.FormLayout {
            Layout.fillWidth: true

            Item {
                Kirigami.FormData.isSection: true
                Kirigami.FormData.label: page.tr("Follow video")
            }
            QQC2.Label {
                Layout.fillWidth: true
                visible: page.ctl.loaded && page.status.available === false
                wrapMode: Text.Wrap
                text: page.tr("Follow video needs extra packages (%1). Install them with:", (page.status.missing || []).join(", "))
                      + "\n" + (page.status.install || "")
            }
            RowLayout {
                Kirigami.FormData.label: page.tr("Max brightness:")
                Layout.fillWidth: true
                QQC2.Slider {
                    id: maxSlider
                    Layout.fillWidth: true
                    from: 0
                    to: 100
                    stepSize: 1
                    value: typeof page.st.video_max === "number" ? page.st.video_max : 100
                    onMoved: maxSend.restart()
                    Timer { id: maxSend; interval: 250; onTriggered: page.act(["set", "video_max=" + Math.round(maxSlider.value)]) }
                }
                QQC2.Label {
                    text: Math.round(maxSlider.value) + " %"
                    font.features: { "tnum": 1 }
                }
            }
            QQC2.ComboBox {
                Kirigami.FormData.label: page.tr("Time averaging:")
                textRole: "text"
                valueRole: "value"
                model: page.smoothChoices
                currentIndex: Math.max(0, indexOfValue(typeof page.st.video_smooth === "number" ? page.st.video_smooth : 0.5))
                onActivated: page.act(["set", "video_smooth=" + currentValue])
            }
            RowLayout {
                Kirigami.FormData.label: page.tr("Updates per second:")
                Layout.fillWidth: true
                QQC2.SpinBox {
                    from: 1
                    to: 3
                    value: typeof page.st.follow_rate === "number" ? page.st.follow_rate : 2
                    onValueModified: page.act(["set", "follow_rate=" + value])
                }
                QQC2.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    opacity: 0.7
                    text: page.tr("Bluetooth mesh: at most 2 recommended; every update uses one of the bulb's message numbers")
                          + " " + page.tr("Shared with Follow audio.")
                }
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: page.tr("Brightness changes at most 3 times per second.")
            }

            Item {
                Kirigami.FormData.isSection: true
                Kirigami.FormData.label: page.tr("Screen region per bulb")
            }
        }

        // One row per used bulb: its name and its region.
        GridLayout {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            columns: 2
            columnSpacing: Kirigami.Units.largeSpacing
            visible: page.usedBulbs.length > 0

            Repeater {
                model: page.usedBulbs
                delegate: QQC2.Label {
                    required property var modelData
                    required property int index
                    Layout.row: index
                    Layout.column: 0
                    Layout.fillWidth: true
                    text: modelData.name || modelData.id
                    elide: Text.ElideRight
                }
            }
            Repeater {
                model: page.usedBulbs
                delegate: QQC2.ComboBox {
                    required property var modelData
                    required property int index
                    Layout.row: index
                    Layout.column: 1
                    textRole: "text"
                    valueRole: "value"
                    model: page.regionChoices
                    currentIndex: Math.max(0, indexOfValue(modelData.video_region || "whole"))
                    onActivated: page.act(["video-region", modelData.id, currentValue])
                    Accessible.name: page.tr("Screen region") + " " + (modelData.name || modelData.id)
                }
            }
        }
        QQC2.Label {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            visible: page.ctl.loaded && page.usedBulbs.length === 0
            text: page.tr("No bulbs yet.")
        }

        Kirigami.FormLayout {
            Layout.fillWidth: true

            Item {
                Kirigami.FormData.isSection: true
                Kirigami.FormData.label: page.tr("Screen permission")
            }
            QQC2.Label {
                Kirigami.FormData.label: page.tr("Screen permission:")
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: page.status.permission ? page.tr("Granted: no dialog on the next start")
                                             : page.tr("Not granted: the first start asks")
            }
            QQC2.Label {
                Layout.fillWidth: true
                visible: !!page.status.hint
                wrapMode: Text.Wrap
                text: page.tr(page.status.hint || "")
            }
            QQC2.Label {
                Layout.fillWidth: true
                visible: page.status.state === "error" && !!page.status.error
                wrapMode: Text.Wrap
                text: page.tr("Error: %1", page.status.error || "")
            }
            QQC2.Button {
                text: page.tr("Forget screen permission")
                icon.name: "edit-clear-history"
                enabled: page.status.permission === true
                onClicked: page.act(["video-forget"])
            }
        }
    }
}

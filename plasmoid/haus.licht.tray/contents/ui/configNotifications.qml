pragma ComponentBehavior: Bound
/*
 * Configure → Notifications: desktop notifications blink the bulbs by rules
 * (haus/notifywatch.py, profiles.notify_*). The global switch, and the rules
 * of the profile of this widget's selection: an ordered list (the first match
 * wins), each edited in a small dialog, moved, removed or tested. Every change
 * sends the whole list (`notify-rules-set`); the daemon validates it and a
 * refusal is shown as it is.
 */
import QtQuick
import QtQuick.Layouts
import QtQuick.Dialogs as QD
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM

KCM.SimpleKCM {
    id: page

    property int cfg_widgetIndex: 1
    property int cfg_widgetIndexDefault: 1

    property HausCtl ctl: HausCtl { widget: page.cfg_widgetIndex }
    function tr(text, ...args) { return ctl.tr(text, ...args) }

    readonly property var st: ctl.st
    readonly property var rules: st.notify_rules || []
    readonly property var status: st.notify_status || ({})

    property string message: ""
    function act(args, done) {
        ctl.run(args, reply => {
            page.message = reply.error || ""
            if (!reply.error && !reply.bulbs_list) ctl.refresh()
            if (done) done(reply)
        })
    }
    function sendRules(list, done) { act(["notify-rules-set", JSON.stringify(list)], done) }
    function copyRules() { return rules.map(r => Object.assign({}, r)) }
    function move(index, by) {
        const list = copyRules()
        const to = index + by
        if (to < 0 || to >= list.length) return
        const r = list.splice(index, 1)[0]
        list.splice(to, 0, r)
        sendRules(list)
    }
    function remove(index) {
        const list = copyRules()
        list.splice(index, 1)
        sendRules(list)
    }

    readonly property var patternChoices: [
        { "text": tr("Blink (on/off)"), "value": "blink" },
        { "text": tr("Pulse (dim and back)"), "value": "pulse" },
        { "text": tr("Double (two quick blinks)"), "value": "double" }
    ]
    readonly property var bulbChoices: [
        { "text": tr("All bulbs in use"), "value": "all" },
        { "text": tr("The widget's selection"), "value": "selection" }
    ]
    function patternLabel(value) {
        const c = patternChoices.find(x => x.value === value)
        return c ? c.text : value
    }
    function summary(r) {
        const parts = []
        if (r.app) parts.push(tr("App: %1", r.app))
        if (r.title) parts.push(tr("Title: %1", r.title))
        if (r.body) parts.push(tr("Text: %1", r.body))
        if (!parts.length) parts.push(tr("Every notification"))
        parts.push(patternLabel(r.pattern) + " × " + r.count + ", " + Math.round(r.intensity) + " %")
        if (r.bulbs === "selection") parts.push(tr("The widget's selection"))
        if (r.dark) parts.push(tr("also when the light is off"))
        return parts.join(" · ")
    }

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
            visible: page.message !== "" && !editor.visible
            type: Kirigami.MessageType.Error
            text: page.message
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: page.st.notify === true && page.status.state === "unavailable"
            type: Kirigami.MessageType.Warning
            text: page.tr("Notifications cannot be watched: %1", page.status.error || "")
        }

        Kirigami.FormLayout {
            Layout.fillWidth: true

            QQC2.Switch {
                Kirigami.FormData.label: page.tr("Blink on notifications:")
                checked: page.st.notify === true
                onToggled: page.act(["notify", checked ? "on" : "off"])
            }
            QQC2.Label {
                Layout.fillWidth: true
                visible: page.status.inhibited === true
                wrapMode: Text.Wrap
                text: page.tr("Do Not Disturb is on: nothing blinks.")
            }
            QQC2.Label {
                Layout.fillWidth: true
                visible: !!page.status.last
                wrapMode: Text.Wrap
                text: page.status.last
                      ? page.tr("Last match: %1 (%2) at %3", page.status.last.rule, page.status.last.app,
                                new Date(page.status.last.time * 1000).toLocaleTimeString(Qt.locale(), Locale.ShortFormat))
                      : ""
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: page.tr("Rules are saved in the current profile (%1).", page.ctl.profileLabel(page.st.profile || "default"))
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.7
                text: page.tr("The first rule that matches wins. Patterns are regular expressions, case does not matter; an empty one matches anything.")
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: page.tr("Brightness changes at most 3 times per second.")
            }
        }

        // The rules, in order.
        ColumnLayout {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            spacing: Kirigami.Units.smallSpacing

            Repeater {
                model: page.rules
                delegate: RowLayout {
                    id: row
                    required property var modelData
                    required property int index
                    Layout.fillWidth: true

                    Rectangle {
                        Layout.preferredWidth: Kirigami.Units.iconSizes.small
                        Layout.preferredHeight: Kirigami.Units.iconSizes.small
                        radius: width / 2
                        visible: !!row.modelData.color
                        color: row.modelData.color || "transparent"
                        border.color: Kirigami.Theme.textColor
                        border.width: 1
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0
                        QQC2.Label {
                            Layout.fillWidth: true
                            text: row.modelData.name
                            font.bold: true
                            elide: Text.ElideRight
                        }
                        QQC2.Label {
                            Layout.fillWidth: true
                            text: page.summary(row.modelData)
                            opacity: 0.7
                            elide: Text.ElideRight
                        }
                    }
                    QQC2.Button {
                        text: page.tr("Test")
                        icon.name: "media-playback-start"
                        onClicked: page.act(["notify-test", row.modelData.name])
                        QQC2.ToolTip.text: page.tr("Run this rule's pattern now")
                        QQC2.ToolTip.visible: hovered
                    }
                    QQC2.ToolButton {
                        icon.name: "document-edit"
                        display: QQC2.AbstractButton.IconOnly
                        text: page.tr("Edit")
                        onClicked: editor.edit(row.index)
                        QQC2.ToolTip.text: text
                        QQC2.ToolTip.visible: hovered
                    }
                    QQC2.ToolButton {
                        icon.name: "go-up"
                        display: QQC2.AbstractButton.IconOnly
                        text: page.tr("Move up")
                        enabled: row.index > 0
                        onClicked: page.move(row.index, -1)
                        QQC2.ToolTip.text: text
                        QQC2.ToolTip.visible: hovered
                    }
                    QQC2.ToolButton {
                        icon.name: "go-down"
                        display: QQC2.AbstractButton.IconOnly
                        text: page.tr("Move down")
                        enabled: row.index < page.rules.length - 1
                        onClicked: page.move(row.index, 1)
                        QQC2.ToolTip.text: text
                        QQC2.ToolTip.visible: hovered
                    }
                    QQC2.ToolButton {
                        icon.name: "edit-delete"
                        display: QQC2.AbstractButton.IconOnly
                        text: page.tr("Remove")
                        onClicked: page.remove(row.index)
                        QQC2.ToolTip.text: text
                        QQC2.ToolTip.visible: hovered
                    }
                }
            }
            QQC2.Label {
                Layout.fillWidth: true
                visible: page.ctl.loaded && page.rules.length === 0
                wrapMode: Text.Wrap
                text: page.tr("No rules yet.")
            }
            QQC2.Button {
                text: page.tr("Add rule…")
                icon.name: "list-add"
                onClicked: editor.edit(-1)
            }
        }
    }

    // ------------------------------------------------------------ the rule dialog
    QQC2.Dialog {
        id: editor
        property int index: -1  // -1: a new rule
        property string error: ""
        property string color: ""
        title: index < 0 ? page.tr("New rule") : page.tr("Edit rule")
        modal: true
        anchors.centerIn: parent
        width: Math.min(page.width, Kirigami.Units.gridUnit * 28)
        standardButtons: QQC2.Dialog.Save | QQC2.Dialog.Cancel

        function edit(i) {
            index = i
            error = ""
            const r = i >= 0 ? page.rules[i] : { "name": "", "app": "", "title": "", "body": "", "pattern": "blink",
                                                 "count": 2, "intensity": 100, "color": "", "bulbs": "all", "dark": false }
            nameField.text = r.name
            appField.text = r.app
            titleField.text = r.title
            bodyField.text = r.body
            patternBox.currentIndex = Math.max(0, patternBox.indexOfValue(r.pattern))
            countBox.value = r.count
            intensityBox.value = Math.round(r.intensity)
            color = r.color || ""
            colorCheck.checked = color !== ""
            bulbsBox.currentIndex = Math.max(0, bulbsBox.indexOfValue(r.bulbs))
            darkCheck.checked = r.dark === true
            open()
        }
        onAccepted: {
            const rule = {
                "name": nameField.text.trim(), "app": appField.text, "title": titleField.text, "body": bodyField.text,
                "pattern": patternBox.currentValue, "count": countBox.value, "intensity": intensityBox.value,
                "color": colorCheck.checked ? color : "", "bulbs": bulbsBox.currentValue, "dark": darkCheck.checked
            }
            const list = page.copyRules()
            if (index < 0) list.push(rule)
            else list[index] = rule
            page.sendRules(list, reply => {
                if (reply.error) {  // refused (an invalid regex, a name twice): back to the dialog
                    editor.error = reply.error
                    editor.open()
                }
            })
        }

        ColumnLayout {
            anchors.fill: parent
            Kirigami.InlineMessage {
                Layout.fillWidth: true
                visible: editor.error !== ""
                type: Kirigami.MessageType.Error
                text: editor.error
            }
            Kirigami.FormLayout {
                Layout.fillWidth: true
                QQC2.TextField {
                    id: nameField
                    Kirigami.FormData.label: page.tr("Name:")
                    placeholderText: page.tr("required")
                }
                QQC2.TextField {
                    id: appField
                    Kirigami.FormData.label: page.tr("App (regex):")
                    placeholderText: page.tr("any")
                }
                QQC2.TextField {
                    id: titleField
                    Kirigami.FormData.label: page.tr("Title (regex):")
                    placeholderText: page.tr("any")
                }
                QQC2.TextField {
                    id: bodyField
                    Kirigami.FormData.label: page.tr("Text (regex):")
                    placeholderText: page.tr("any")
                }
                QQC2.ComboBox {
                    id: patternBox
                    Kirigami.FormData.label: page.tr("Pattern:")
                    textRole: "text"
                    valueRole: "value"
                    model: page.patternChoices
                }
                QQC2.SpinBox {
                    id: countBox
                    Kirigami.FormData.label: page.tr("Times:")
                    from: 1
                    to: 10
                }
                QQC2.SpinBox {
                    id: intensityBox
                    Kirigami.FormData.label: page.tr("Brightness:")
                    from: 1
                    to: 100
                    textFromValue: (value, locale) => value + " %"
                    valueFromText: (text, locale) => parseInt(text)
                }
                RowLayout {
                    Kirigami.FormData.label: page.tr("Colour:")
                    QQC2.CheckBox {
                        id: colorCheck
                        text: page.tr("Colour bulbs only")
                        onToggled: if (checked && editor.color === "") colorDialog.open()
                    }
                    QQC2.Button {
                        enabled: colorCheck.checked
                        text: page.tr("Choose…")
                        icon.name: "color-picker"
                        onClicked: colorDialog.open()
                    }
                    Rectangle {
                        visible: colorCheck.checked && editor.color !== ""
                        Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                        Layout.preferredHeight: Kirigami.Units.iconSizes.small
                        color: editor.color || "transparent"
                        border.color: Kirigami.Theme.textColor
                        border.width: 1
                    }
                }
                QQC2.ComboBox {
                    id: bulbsBox
                    Kirigami.FormData.label: page.tr("Bulbs:")
                    textRole: "text"
                    valueRole: "value"
                    model: page.bulbChoices
                }
                QQC2.CheckBox {
                    id: darkCheck
                    text: page.tr("Also when the light is off")
                }
                QQC2.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    opacity: 0.7
                    text: darkCheck.checked
                          ? page.tr("Bulbs that are off light up for the blink, also at night.")
                          : page.tr("Only bulbs that are lit blink: they dim and come back, a dark room stays dark.")
                }
            }
        }
    }

    QD.ColorDialog {
        id: colorDialog
        selectedColor: editor.color || "#ffffff"
        onAccepted: {
            const c = selectedColor
            const hex = v => ("0" + Math.round(v * 255).toString(16)).slice(-2)
            editor.color = "#" + hex(c.r) + hex(c.g) + hex(c.b)
        }
        onRejected: if (editor.color === "") colorCheck.checked = false
    }
}

/*
 * Configure → Profiles: rename, delete and use profiles; save the selected
 * bulb's settings as one. Every change goes to the daemon at once.
 */
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM

KCM.SimpleKCM {
    id: page

    // Filled by the Configure dialog from main.xml: which widget this is
    // (the profile actions act on that widget's selection).
    property int cfg_widgetIndex: 1
    property int cfg_widgetIndexDefault: 1

    property HausCtl ctl: HausCtl { widget: page.cfg_widgetIndex }
    // "Connect new bulb…" while this page shows: switch to Bulbs.
    property AddBulbRedirect addBulb: AddBulbRedirect { configPage: page }
    function tr(text, ...args) { return ctl.tr(text, ...args) }

    property string message: ""
    property bool messageOk: false
    function act(args, okText, done) {
        ctl.run(args, reply => {
            if (reply.error) {
                page.message = reply.error
                page.messageOk = false
            } else {
                page.message = okText || ""
                page.messageOk = true
                if (!reply.bulbs_list) ctl.refresh()
            }
            if (done) done(reply)
        })
    }

    // Rebuilt only when the list of profiles changes (an open rename stays).
    property var names: []
    Connections {
        target: page.ctl
        function onStChanged() {
            const list = ["default"].concat(page.ctl.profiles.filter(p => p !== "default"))
            if (list.join("\n") !== page.names.join("\n")) page.names = list
        }
    }
    function usedBy(name) { return ctl.bulbs.filter(b => (b.profile || "default") === name).length }
    function usage(n) {
        return n === 0 ? tr("not used") : n === 1 ? tr("used by 1 bulb") : tr("used by %1 bulbs", n)
    }

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
            type: page.messageOk ? Kirigami.MessageType.Positive : Kirigami.MessageType.Error
            visible: page.message !== ""
            text: page.message
            showCloseButton: true
            onVisibleChanged: if (!visible) page.message = ""
        }

        // ------------------------------------------------- one row per profile
        Repeater {
            model: page.names
            delegate: Kirigami.AbstractCard {
                id: row
                required property string modelData
                readonly property bool isDefault: modelData === "default"
                property string mode: ""   // "", "rename" or "delete"
                Layout.fillWidth: true

                contentItem: ColumnLayout {
                    spacing: Kirigami.Units.smallSpacing

                    RowLayout {
                        visible: row.mode !== "rename"
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0
                            Kirigami.Heading {
                                Layout.fillWidth: true
                                level: 4
                                text: page.ctl.profileLabel(row.modelData)
                                elide: Text.ElideRight
                            }
                            QQC2.Label {
                                Layout.fillWidth: true
                                text: page.usage(page.usedBy(row.modelData))
                                opacity: 0.7
                                elide: Text.ElideRight
                            }
                        }
                        QQC2.Button {
                            text: page.tr("Use for the selection")
                            icon.name: "dialog-ok-apply"
                            QQC2.ToolTip.visible: hovered
                            QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                            QQC2.ToolTip.text: page.tr("The bulb selected in the popup uses this profile. With all bulbs selected, its values become the default for every bulb.")
                            onClicked: page.act(["profile", "apply", row.modelData])
                        }
                        QQC2.Button {
                            visible: !row.isDefault
                            text: page.tr("Rename")
                            icon.name: "edit-rename"
                            onClicked: {
                                renameField.text = row.modelData
                                row.mode = "rename"
                                renameField.forceActiveFocus()
                                renameField.selectAll()
                            }
                        }
                        QQC2.Button {
                            visible: !row.isDefault
                            text: page.tr("Delete")
                            icon.name: "edit-delete"
                            enabled: row.mode === ""
                            onClicked: row.mode = "delete"
                        }
                    }
                    RowLayout {
                        visible: row.mode === "rename"
                        QQC2.TextField {
                            id: renameField
                            Layout.fillWidth: true
                            onAccepted: renameButton.clicked()
                            Keys.onEscapePressed: row.mode = ""
                        }
                        QQC2.Button {
                            id: renameButton
                            text: page.tr("Rename")
                            icon.name: "dialog-ok"
                            enabled: renameField.text.trim() !== ""
                            onClicked: {
                                const name = renameField.text.trim()
                                if (name === row.modelData) { row.mode = ""; return }
                                page.act(["profile", "rename", row.modelData, name], "",
                                         reply => { if (!reply.error) row.mode = "" })
                            }
                        }
                        QQC2.Button {
                            text: page.tr("Cancel")
                            icon.name: "dialog-cancel"
                            onClicked: row.mode = ""
                        }
                    }
                    RowLayout {
                        visible: row.mode === "delete"
                        QQC2.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            text: page.tr("Delete profile %1? Bulbs using it go back to Default.", row.modelData)
                        }
                        QQC2.Button {
                            text: page.tr("Delete")
                            icon.name: "edit-delete"
                            onClicked: page.act(["profile", "delete", row.modelData])
                        }
                        QQC2.Button {
                            text: page.tr("Cancel")
                            icon.name: "dialog-cancel"
                            onClicked: row.mode = ""
                        }
                    }
                }
            }
        }

        // ------------------------------------------- save the current settings
        Kirigami.Heading {
            Layout.fillWidth: true
            Layout.topMargin: Kirigami.Units.largeSpacing
            level: 3
            wrapMode: Text.Wrap
            text: page.tr("Save the current settings as a profile")
        }
        QQC2.Label {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            opacity: 0.7
            text: page.tr("Copies the settings of the bulb selected in the popup (now: %1) into a new profile, or overwrites the profile of that name.",
                          page.ctl.bulbName(page.ctl.st.selected))
        }
        RowLayout {
            Layout.fillWidth: true
            QQC2.TextField {
                id: saveField
                Layout.fillWidth: true
                Layout.minimumWidth: Kirigami.Units.gridUnit * 10
                placeholderText: page.tr("Profile name")
                onAccepted: if (saveButton.enabled) saveButton.clicked()
            }
            QQC2.Button {
                id: saveButton
                text: page.tr("Save")
                icon.name: "document-save"
                enabled: saveField.text.trim() !== ""
                onClicked: {
                    const name = saveField.text.trim()
                    page.act(["profile", "save", name], page.tr("Saved as %1.", name),
                             reply => { if (!reply.error) saveField.text = "" })
                }
            }
        }
    }
}

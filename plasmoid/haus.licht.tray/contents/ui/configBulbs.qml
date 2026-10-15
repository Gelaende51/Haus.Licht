/*
 * Configure → Bulbs: rename, profile and remove each bulb; add a new one.
 * Every change goes to the daemon at once (hauslicht-ctl), nothing to apply.
 */
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM

KCM.SimpleKCM {
    id: page

    property HausCtl ctl: HausCtl {}
    function tr(text, ...args) { return ctl.tr(text, ...args) }

    // Feedback of the last action (daemon errors are worded for the user).
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

    // Delegates are keyed by id and only rebuilt when the set of bulbs
    // changes, so a refresh never throws away a name being typed.
    property var bulbIds: []
    // The kind list only changes when the backends do: the user's choice stays.
    property var kinds: []
    Connections {
        target: page.ctl
        function onStChanged() {
            const ids = page.ctl.bulbs.map(b => b.id)
            if (ids.join("\n") !== page.bulbIds.join("\n")) page.bulbIds = ids
            if (JSON.stringify(page.ctl.backends) !== JSON.stringify(page.kinds)) {
                const old = page.addKind
                page.kinds = page.ctl.backends
                const keep = page.kinds.findIndex(k => k.kind === old && k.available)
                kindBox.currentIndex = keep >= 0 ? keep : Math.max(0, page.kinds.findIndex(k => k.available))
                page.resetForm()
            }
        }
    }
    readonly property var profileNames: ["default"].concat(ctl.profiles.filter(p => p !== "default"))

    // ------------------------------------------------------- add form
    readonly property var addBackend: kinds[kindBox.currentIndex] || null
    readonly property string addKind: addBackend ? addBackend.kind : ""
    readonly property var addFields: addBackend ? addBackend.fields : []
    property string formName: ""
    property var formValues: ({})
    property var found: []
    property bool searching: false
    property bool searched: false

    function fieldLabel(f) {
        const label = (ctl.german && f.label_de) ? f.label_de : (f.label || f.key)
        return label + (f.required ? " *" : "")
    }
    function setValue(key, value) {
        const values = Object.assign({}, formValues)
        values[key] = value
        formValues = values
    }
    function defaults() {
        const values = {}
        addFields.forEach(f => { if (f.default !== undefined) values[f.key] = f.default })
        return values
    }
    function resetForm() {
        formName = ""
        formValues = defaults()
        found = []
        searched = false
    }
    function given(value) { return value !== undefined && value !== null && String(value) !== "" }
    readonly property bool formComplete: addBackend !== null && addBackend.available
        && addFields.every(f => !f.required || given(formValues[f.key]))
    function addBulb() {
        const args = ["add", addKind]
        if (formName.trim()) args.push("name=" + formName.trim())
        addFields.forEach(f => {
            const v = formValues[f.key]
            if (!given(v)) return
            args.push(f.key + "=" + (f.type === "bool" ? (isTrue(v) ? "on" : "off") : String(v)))
        })
        act(args, "", reply => {
            if (reply.error) return
            page.message = tr("Added %1.", ctl.bulbName(reply.added))
            resetForm()
        })
    }
    function isTrue(v) { return v === true || ["on", "true", "1", "yes"].includes(String(v)) }
    function search() {
        searching = true
        found = []
        ctl.run(["discover", addKind], reply => {
            page.searching = false
            page.searched = true
            if (reply.error) {
                page.message = reply.error
                page.messageOk = false
            } else {
                page.message = ""
                page.found = reply.discovered || []
            }
        })
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

        QQC2.Label {
            visible: page.ctl.loaded && page.bulbIds.length === 0
            text: page.tr("No bulbs yet. Add one below.")
            opacity: 0.7
        }

        // ------------------------------------------------- one card per bulb
        Repeater {
            model: page.bulbIds
            delegate: Kirigami.AbstractCard {
                id: card
                required property string modelData
                readonly property var bulb: page.ctl.bulbs.find(b => b.id === modelData) || ({})
                property bool confirming: false
                Layout.fillWidth: true
                onBulbChanged: if (!nameField.activeFocus) nameField.text = bulb.name || ""

                contentItem: ColumnLayout {
                    spacing: Kirigami.Units.smallSpacing

                    RowLayout {
                        QQC2.TextField {
                            id: nameField
                            Layout.fillWidth: true
                            placeholderText: page.tr("Name")
                            Component.onCompleted: text = card.bulb.name || ""
                            onEditingFinished: {
                                const name = text.trim()
                                if (name && name !== card.bulb.name)
                                    page.act(["bulb-set", card.modelData, "name=" + name])
                                else
                                    text = card.bulb.name || ""
                            }
                        }
                        QQC2.Label {
                            visible: card.bulb.experimental === true
                            text: page.tr("experimental")
                            font: Kirigami.Theme.smallFont
                            leftPadding: Kirigami.Units.smallSpacing
                            rightPadding: Kirigami.Units.smallSpacing
                            color: Kirigami.Theme.neutralTextColor
                            background: Rectangle {
                                radius: Kirigami.Units.cornerRadius
                                color: Kirigami.Theme.neutralBackgroundColor
                            }
                        }
                        QQC2.Label {
                            readonly property string state: card.bulb.online === false ? "Unreachable"
                                : card.bulb.connected === false ? "Disconnected" : "Connected"
                            text: page.tr(state)
                            color: state === "Connected" ? Kirigami.Theme.positiveTextColor
                                 : state === "Disconnected" ? Kirigami.Theme.neutralTextColor
                                 : Kirigami.Theme.negativeTextColor
                        }
                    }
                    QQC2.Label {
                        Layout.fillWidth: true
                        text: page.ctl.kindTitle(card.bulb.kind) + " · " + String(card.modelData).replace(/^[^:]*:/, "")
                        opacity: 0.7
                        elide: Text.ElideRight
                    }
                    RowLayout {
                        visible: !card.confirming
                        QQC2.Label { text: page.tr("Profile:") }
                        QQC2.ComboBox {
                            model: page.profileNames.map(p => page.ctl.profileLabel(p))
                            currentIndex: Math.max(0, page.profileNames.indexOf(card.bulb.profile || "default"))
                            onActivated: index => page.act(["bulb-set", card.modelData,
                                                            "profile=" + page.profileNames[index]])
                        }
                        Item { Layout.fillWidth: true }
                        QQC2.Button {
                            text: page.tr("Remove")
                            icon.name: "edit-delete-remove"
                            onClicked: card.confirming = true
                        }
                    }
                    RowLayout {
                        visible: card.confirming
                        QQC2.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            text: page.tr("Remove %1? Haus.Licht stops controlling it.", card.bulb.name || card.modelData)
                        }
                        QQC2.Button {
                            text: page.tr("Remove")
                            icon.name: "edit-delete-remove"
                            onClicked: page.act(["remove", card.modelData])
                        }
                        QQC2.Button {
                            text: page.tr("Cancel")
                            icon.name: "dialog-cancel"
                            onClicked: card.confirming = false
                        }
                    }
                }
            }
        }

        // ------------------------------------------------------ add a bulb
        // Two forms twinned for one label column: generated fields go last in
        // the first, so their delegates can't land behind the buttons.
        Kirigami.FormLayout {
            id: addForm
            Layout.fillWidth: true
            twinFormLayouts: [actionForm]

            Kirigami.Separator {
                Kirigami.FormData.isSection: true
                Kirigami.FormData.label: page.tr("Add a bulb")
            }
            QQC2.ComboBox {
                id: kindBox
                Kirigami.FormData.label: page.tr("Kind:")
                Layout.fillWidth: true
                model: page.kinds
                textRole: "title"
                onActivated: page.resetForm()
                delegate: QQC2.ItemDelegate {
                    required property var modelData
                    required property int index
                    width: ListView.view ? ListView.view.width : implicitWidth
                    enabled: modelData.available
                    highlighted: kindBox.highlightedIndex === index
                    text: modelData.title + (modelData.available ? ""
                          : "  (" + page.tr("needs: pip install %1", (modelData.missing || []).join(" ")) + ")")
                }
            }
            QQC2.Label {
                visible: page.addBackend !== null && !page.addBackend.available
                text: page.addBackend ? page.tr("needs: pip install %1", (page.addBackend.missing || []).join(" ")) : ""
                color: Kirigami.Theme.negativeTextColor
            }

            // Mesh bulbs need the radio to themselves: adopted in a terminal.
            ColumnLayout {
                visible: page.addKind === "mesh"
                Layout.fillWidth: true
                QQC2.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: page.tr("Bluetooth-mesh bulbs are adopted in a terminal:")
                }
                Kirigami.SelectableLabel {
                    Layout.fillWidth: true
                    font.family: "monospace"
                    text: "systemctl --user stop hauslicht; hauslicht adopt"
                }
            }

            QQC2.TextField {
                visible: page.addKind !== "mesh" && page.addBackend !== null
                Kirigami.FormData.label: page.tr("Name:")
                Layout.fillWidth: true
                text: page.formName
                onTextEdited: page.formName = text
            }
            Repeater {
                model: page.addKind === "mesh" ? [] : page.addFields
                delegate: Loader {
                    required property var modelData
                    // A checkbox carries its label itself (long ones would
                    // widen the label column past the page).
                    Kirigami.FormData.label: modelData.type === "bool" ? "" : page.fieldLabel(modelData) + ":"
                    Layout.fillWidth: modelData.type !== "int"
                    sourceComponent: modelData.type === "bool" ? boolEditor
                        : modelData.type === "int" ? intEditor
                        : modelData.type === "choice" ? choiceEditor : textEditor
                }
            }
        }
        Kirigami.FormLayout {
            id: actionForm
            Layout.fillWidth: true
            twinFormLayouts: [addForm]
            visible: page.addKind !== "mesh" && page.addBackend !== null

            RowLayout {
                QQC2.Button {
                    text: page.tr("Search network")
                    icon.name: "system-search"
                    enabled: !page.searching && page.addBackend !== null && page.addBackend.available
                    onClicked: page.search()
                }
                QQC2.BusyIndicator {
                    visible: page.searching
                    running: visible
                    implicitHeight: Kirigami.Units.iconSizes.medium
                    implicitWidth: implicitHeight
                }
                QQC2.Label {
                    visible: page.searching
                    text: page.tr("Searching…")
                }
                QQC2.Label {
                    visible: page.searched && !page.searching && page.found.length === 0
                    text: page.tr("Nothing found.")
                    opacity: 0.7
                }
            }
            ColumnLayout {
                visible: page.found.length > 0
                Kirigami.FormData.label: page.tr("Found:")
                Layout.fillWidth: true
                spacing: 0
                Repeater {
                    model: page.found
                    delegate: QQC2.ItemDelegate {
                        required property var modelData
                        Layout.fillWidth: true
                        text: (modelData.name || modelData.address) + "  ·  " + modelData.address
                        icon.name: "go-next"
                        onClicked: {
                            page.formName = modelData.name || ""
                            page.formValues = Object.assign(page.defaults(), modelData.config || {})
                        }
                    }
                }
            }
            QQC2.Button {
                text: page.tr("Add")
                icon.name: "list-add"
                enabled: page.formComplete
                onClicked: page.addBulb()
            }
        }
    }

    // ------------------------------------------------- field editors
    // Loaded by the field Repeater: `parent` is the Loader carrying modelData.
    Component {
        id: textEditor
        QQC2.TextField {
            readonly property var field: parent ? parent.modelData : ({})
            echoMode: field.type === "password" ? TextInput.Password : TextInput.Normal
            placeholderText: field.required ? page.tr("required") : ""
            text: page.given(page.formValues[field.key]) ? String(page.formValues[field.key]) : ""
            onTextEdited: page.setValue(field.key, text)
        }
    }
    Component {
        id: intEditor
        QQC2.SpinBox {
            readonly property var field: parent ? parent.modelData : ({})
            from: -999999
            to: 999999
            editable: true
            value: Number(page.formValues[field.key]) || 0
            onValueModified: page.setValue(field.key, value)
        }
    }
    Component {
        id: boolEditor
        QQC2.CheckBox {
            readonly property var field: parent ? parent.modelData : ({})
            text: page.fieldLabel(field)
            checked: page.isTrue(page.formValues[field.key])
            onToggled: page.setValue(field.key, checked)
        }
    }
    Component {
        id: choiceEditor
        QQC2.ComboBox {
            readonly property var field: parent ? parent.modelData : ({})
            model: field.choices || []
            currentIndex: Math.max(0, (field.choices || []).map(String).indexOf(String(page.formValues[field.key])))
            onActivated: index => page.setValue(field.key, field.choices[index])
        }
    }
}

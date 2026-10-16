/*
 * Configure → Bulbs: how many widgets; every bulb in the global order (drag
 * to reorder), whether Haus.Licht uses it, whether this widget shows it, its
 * icons, name, profile and removal; "Add bulb…" opens the add dialog.
 * Every change goes to the daemon at once (hauslicht-ctl), nothing to apply.
 */
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM
import org.kde.iconthemes as KIconThemes

KCM.SimpleKCM {
    id: page

    // Filled by the Configure dialog from main.xml: which widget this is.
    property int cfg_widgetIndex: 1
    property int cfg_widgetIndexDefault: 1

    property HausCtl ctl: HausCtl { widget: page.cfg_widgetIndex }
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

    // Rows are keyed by id and only rebuilt when the ids or their order
    // change (not while a row is dragged), so a refresh never throws away a
    // name being typed.
    property bool dragging: false
    function syncModel() {
        if (dragging) return
        const ids = ctl.bulbs.map(b => b.id)
        const have = []
        for (let i = 0; i < bulbModel.count; i++) have.push(bulbModel.get(i).bid)
        if (ids.join("\n") === have.join("\n")) return
        bulbModel.clear()
        ids.forEach(id => bulbModel.append({"bid": id}))
    }
    function sendOrder() {
        const ids = []
        for (let i = 0; i < bulbModel.count; i++) ids.push(bulbModel.get(i).bid)
        if (ids.join("\n") !== ctl.bulbs.map(b => b.id).join("\n"))
            act(["order"].concat(ids), "", () => page.syncModel())
    }

    // This widget's list: the used bulbs it shows, in the global order.
    readonly property var inWidget: ctl.st.in_widget || []
    function setShown(id, shown) {
        const list = ctl.bulbs.filter(b => b.use !== false
                                       && (b.id === id ? shown : inWidget.includes(b.id)))
                              .map(b => b.id)
        act(list.length ? ["widget-bulbs", list.join(",")] : ["widget-bulbs"])
    }

    // The kind list only changes when the backends do: the user's choice stays.
    property var kinds: []
    Connections {
        target: page.ctl
        function onStChanged() {
            page.syncModel()
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

    // ------------------------------------------------------------ icons
    // One icon dialog for every row: iconTarget says which bulb and key.
    property var iconTarget: null
    KIconThemes.IconDialog {
        id: iconDialog
        onAccepted: page.iconChosen(iconName)
        onRejected: page.iconTarget = null
    }
    function chooseIcon(id, key) {
        iconTarget = {"id": id, "key": key}
        iconDialog.open()
    }
    function iconChosen(name) {
        const t = iconTarget
        iconTarget = null
        if (t && name) act(["bulb-set", t.id, t.key + "=" + name])
    }

    // ------------------------------------------------------- add form
    readonly property var addBackend: kinds[kindBox.currentIndex] || null
    readonly property string addKind: addBackend ? addBackend.kind : ""
    readonly property var addFields: addBackend ? addBackend.fields : []
    property string formName: ""
    property var formValues: ({})
    property var found: []
    property bool searching: false
    property bool searched: false
    property string addError: ""

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
        addError = ""
    }
    function given(value) { return value !== undefined && value !== null && String(value) !== "" }
    readonly property bool formComplete: addBackend !== null && addBackend.available
        && addKind !== "mesh" && addFields.every(f => !f.required || given(formValues[f.key]))
    function addBulb() {
        const args = ["add", addKind]
        if (formName.trim()) args.push("name=" + formName.trim())
        addFields.forEach(f => {
            const v = formValues[f.key]
            if (!given(v)) return
            args.push(f.key + "=" + (f.type === "bool" ? (isTrue(v) ? "on" : "off") : String(v)))
        })
        page.addError = ""
        ctl.run(args, reply => {
            if (reply.error) {
                page.addError = reply.error
                return
            }
            page.message = tr("Added %1.", ctl.bulbName(reply.added))
            page.messageOk = true
            if (!reply.bulbs_list) ctl.refresh()
            addDialog.close()
            resetForm()
        })
    }
    function isTrue(v) { return v === true || ["on", "true", "1", "yes"].includes(String(v)) }
    function search() {
        searching = true
        found = []
        addError = ""
        ctl.run(["discover", addKind], reply => {
            page.searching = false
            page.searched = true
            if (reply.error) page.addError = reply.error
            else page.found = reply.discovered || []
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

        // ------------------------------------------------------- widgets
        ColumnLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            RowLayout {
                QQC2.Label { text: page.tr("Widgets:") }
                QQC2.SpinBox {
                    from: 1
                    to: 8
                    enabled: page.ctl.loaded
                    value: page.ctl.st.widgets || 1
                    onValueModified: page.act(["set", "widgets=" + value])
                }
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.7
                text: page.tr("Widget 1 sits in the system tray, the others in the panel next to it.")
            }
        }

        QQC2.Label {
            visible: page.ctl.loaded && bulbModel.count === 0
            text: page.tr("No bulbs yet. Add one below.")
            opacity: 0.7
        }

        // ------------------------------------------------- one row per bulb
        ListView {
            id: bulbList
            Layout.fillWidth: true
            Layout.preferredHeight: contentHeight
            interactive: false
            spacing: Kirigami.Units.smallSpacing
            model: ListModel { id: bulbModel }

            // No required properties: the drag handle reads the context `index`.
            delegate: Item {
                id: slot
                width: ListView.view.width
                implicitHeight: card.implicitHeight

                Kirigami.AbstractCard {
                    id: card
                    readonly property string bid: model.bid
                    readonly property var bulb: page.ctl.bulbs.find(b => b.id === bid) || ({})
                    readonly property bool used: bulb.use !== false
                    property bool confirming: false
                    width: slot.width
                    onBulbChanged: if (!nameField.activeFocus) nameField.text = bulb.name || ""

                    contentItem: RowLayout {
                        spacing: Kirigami.Units.smallSpacing

                        Kirigami.ListItemDragHandle {
                            Layout.alignment: Qt.AlignVCenter
                            listItem: card
                            listView: bulbList
                            QQC2.ToolTip.text: page.tr("Drag to reorder")
                            QQC2.ToolTip.visible: dragHover.hovered && !dragActive
                            QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                            HoverHandler { id: dragHover }
                            onMoveRequested: (oldIndex, newIndex) => {
                                page.dragging = true
                                bulbModel.move(oldIndex, newIndex, 1)
                            }
                            onDropped: {
                                page.dragging = false
                                page.sendOrder()
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: Kirigami.Units.smallSpacing

                            RowLayout {
                                Repeater {
                                    model: [
                                        {"key": "icon_on", "tip": "Icon when on"},
                                        {"key": "icon_off", "tip": "Icon when off (optional)"}
                                    ]
                                    delegate: QQC2.ToolButton {
                                        id: iconButton
                                        required property var modelData
                                        readonly property string value: card.bulb[modelData.key] || ""
                                        // empty: what the widget shows instead, dimmed (no on icon: the
                                        // lighthouse; no off icon: the on icon)
                                        readonly property string shown: value !== "" ? value
                                            : modelData.key === "icon_off" ? (card.bulb.icon_on || "") : ""
                                        icon.name: shown === "" || shown.startsWith("/") ? "" : shown
                                        icon.source: shown.startsWith("/") ? "file://" + shown
                                            : shown === "" ? Qt.resolvedUrl(modelData.key === "icon_off"
                                                ? "../icons/lighthouse-off.svg" : "../icons/lighthouse-on.svg") : ""
                                        icon.width: Kirigami.Units.iconSizes.smallMedium
                                        icon.height: Kirigami.Units.iconSizes.smallMedium
                                        opacity: value === "" ? 0.5 : 1
                                        display: QQC2.AbstractButton.IconOnly
                                        text: page.tr(modelData.tip)
                                        QQC2.ToolTip.text: text
                                        QQC2.ToolTip.visible: hovered
                                        QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                                        onClicked: iconMenu.popup(iconButton, 0, iconButton.height)
                                        QQC2.Menu {
                                            id: iconMenu
                                            QQC2.MenuItem {
                                                text: page.tr("Choose…")
                                                icon.name: "document-open"
                                                onTriggered: page.chooseIcon(card.bid, iconButton.modelData.key)
                                            }
                                            QQC2.MenuItem {
                                                text: page.tr("Clear")
                                                icon.name: "edit-clear"
                                                enabled: iconButton.value !== ""
                                                onTriggered: page.act(["bulb-set", card.bid, iconButton.modelData.key + "="])
                                            }
                                        }
                                    }
                                }
                                QQC2.TextField {
                                    id: nameField
                                    Layout.fillWidth: true
                                    placeholderText: page.tr("Name")
                                    Component.onCompleted: text = card.bulb.name || ""
                                    onEditingFinished: {
                                        const name = text.trim()
                                        if (name && name !== card.bulb.name)
                                            page.act(["bulb-set", card.bid, "name=" + name])
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
                                text: page.ctl.kindTitle(card.bulb.kind) + " · " + String(card.bid).replace(/^[^:]*:/, "")
                                opacity: 0.7
                                elide: Text.ElideRight
                            }
                            RowLayout {
                                visible: !card.confirming
                                QQC2.CheckBox {
                                    text: page.tr("Use")
                                    checked: card.used
                                    onToggled: page.act(["bulb-set", card.bid, "use=" + (checked ? "on" : "off")])
                                    QQC2.ToolTip.text: page.tr("Off: Haus.Licht leaves this bulb alone. It comes on after a power cut like an ordinary bulb.")
                                    QQC2.ToolTip.visible: hovered
                                    QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                                }
                                QQC2.CheckBox {
                                    text: page.tr("Show in this widget")
                                    enabled: card.used
                                    checked: card.used && page.inWidget.includes(card.bid)
                                    onToggled: page.setShown(card.bid, checked)
                                }
                                Item { Layout.fillWidth: true }
                                QQC2.Label { text: page.tr("Profile:") }
                                QQC2.ComboBox {
                                    model: page.profileNames.map(p => page.ctl.profileLabel(p))
                                    currentIndex: Math.max(0, page.profileNames.indexOf(card.bulb.profile || "default"))
                                    onActivated: index => page.act(["bulb-set", card.bid,
                                                                    "profile=" + page.profileNames[index]])
                                }
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
                                    text: page.tr("Remove %1? Haus.Licht stops controlling it.", card.bulb.name || card.bid)
                                }
                                QQC2.Button {
                                    text: page.tr("Remove")
                                    icon.name: "edit-delete-remove"
                                    onClicked: page.act(["remove", card.bid])
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
            }
        }

        QQC2.Button {
            text: page.tr("Add bulb…")
            icon.name: "list-add"
            onClicked: {
                page.resetForm()
                addDialog.open()
            }
        }
    }

    // ------------------------------------------------------ add dialog
    Kirigami.Dialog {
        id: addDialog
        parent: QQC2.Overlay.overlay
        modal: true
        title: page.tr("Add a bulb")
        preferredWidth: Kirigami.Units.gridUnit * 27
        padding: Kirigami.Units.largeSpacing
        standardButtons: Kirigami.Dialog.NoButton
        customFooterActions: [
            Kirigami.Action {
                text: page.tr("Add")
                icon.name: "list-add"
                enabled: page.formComplete
                onTriggered: page.addBulb()
            },
            Kirigami.Action {
                text: page.tr("Cancel")
                icon.name: "dialog-cancel"
                onTriggered: addDialog.close()
            }
        ]

        ColumnLayout {
            spacing: Kirigami.Units.largeSpacing

            Kirigami.InlineMessage {
                Layout.fillWidth: true
                type: Kirigami.MessageType.Error
                visible: page.addError !== ""
                text: page.addError
            }

            // Two forms twinned for one label column: generated fields go
            // last in the first, so their delegates can't land behind the
            // search button.
            Kirigami.FormLayout {
                id: addForm
                Layout.fillWidth: true
                twinFormLayouts: [actionForm]

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
                        // widen the label column past the dialog).
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

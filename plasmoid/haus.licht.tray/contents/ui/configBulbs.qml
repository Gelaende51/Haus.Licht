/*
 * Configure → Bulbs: how many widgets; every bulb in the global order (drag
 * to reorder), whether Haus.Licht uses it, whether this widget shows it, its
 * icons (on, off, disconnected), name, device details, profile and removal;
 * "Add bulb…" opens the add dialog. Every change goes to the daemon at once
 * (hauslicht-ctl), nothing to apply, except the widget's own option at the
 * bottom (Invert scroll direction, main.xml), which Apply saves, and the
 * icon set, which applies at once.
 */
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM
import org.kde.iconthemes as KIconThemes
import org.kde.plasma.plasmoid

KCM.SimpleKCM {
    id: page

    // Filled by the Configure dialog from main.xml: which widget this is.
    property int cfg_widgetIndex: 1
    property int cfg_widgetIndexDefault: 1
    property bool cfg_invertScroll: false
    property bool cfg_invertScrollDefault: false

    property HausCtl ctl: HausCtl { widget: page.cfg_widgetIndex }
    // Icon set (main.xml iconSet): written straight to the configuration, so
    // the tray icon and the popup follow at once, without Apply.
    readonly property IconSets iconSets: IconSets {}
    readonly property string iconSet: Plasmoid.configuration.iconSet || iconSets.fallback
    // A set in the Icon set box: its on, off and disconnected icons at one
    // fixed small size (a layout may not shrink them), then its name, which
    // takes the rest and elides; the row is as high as the taller of the two.
    component SetPreview: RowLayout {
        id: preview
        required property string setId
        required property string setName
        spacing: Kirigami.Units.smallSpacing
        Repeater {
            model: ["on", "off", "disconnected"]
            delegate: Kirigami.Icon {
                required property string modelData
                Layout.alignment: Qt.AlignVCenter
                Layout.minimumWidth: Kirigami.Units.iconSizes.small
                Layout.maximumWidth: Kirigami.Units.iconSizes.small
                Layout.preferredWidth: Kirigami.Units.iconSizes.small
                Layout.minimumHeight: Kirigami.Units.iconSizes.small
                Layout.maximumHeight: Kirigami.Units.iconSizes.small
                Layout.preferredHeight: Kirigami.Units.iconSizes.small
                source: page.iconSets.url(preview.setId, modelData)
                isMask: true
            }
        }
        QQC2.Label {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.alignment: Qt.AlignVCenter
            Layout.leftMargin: Kirigami.Units.smallSpacing
            text: preview.setName
            elide: Text.ElideRight
            maximumLineCount: 1
        }
    }
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
    // The device details line: model · address · MAC · CID/PID · firmware · RSSI,
    // only what the daemon knows (`details` in get).
    function detailsText(bulb) {
        const d = bulb.details || {}
        const parts = [d.model || ctl.kindTitle(bulb.kind)]
        if (d.address) parts.push(d.address)
        if (d.mac && d.mac !== d.address) parts.push(d.mac)
        if (d.cid || d.pid) parts.push("CID/PID " + (d.cid || "?") + "/" + (d.pid || "?"))
        if (d.firmware) parts.push(tr("Firmware %1", d.firmware))
        if (typeof d.rssi === "number") parts.push(d.rssi + " dBm")
        return parts.join(" · ")
    }
    // A terminal following the service's journal: konsole, else the Debian
    // alternative, else xterm.
    function viewLogs() {
        const cmd = "journalctl --user -u hauslicht -f"
        ctl.shell(["sh", "-c", "for t in konsole x-terminal-emulator xterm; do "
                   + "if command -v \"$t\" >/dev/null 2>&1; then setsid -f \"$t\" -e " + cmd
                   + " >/dev/null 2>&1; exit 0; fi; done; exit 127"])
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
    // Discovered devices of both searches (Scan Bluetooth, the per-kind
    // button), merged by (kind, address); `misses`: scans of that kind in a
    // row that did not see it. foundErrors: kind -> message of its last scan.
    property var found: []
    property var foundErrors: ({})
    property bool searching: false
    property string searchingWhat: ""  // "bluetooth", "all" or the kind being searched
    property bool searched: false
    property int scanGen: 0  // a reply from before the dialog was reopened is dropped
    readonly property var btKinds: kinds.filter(k => k.bluetooth && k.available).map(k => k.kind)
    readonly property bool autoScan: Plasmoid.configuration.autoScanBluetooth === true
    property string addError: ""
    property bool addAccess: false   // addError is a missing device permission: offer "Grant access…"
    property var foundAccess: []     // kinds whose last scan lacked a device permission
    property bool granting: false    // our pkexec run of the root setup is up

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
        addError = ""
        addAccess = false
    }
    function clearResults() {
        found = []
        foundErrors = ({})
        foundAccess = []
        searched = false
    }
    function given(value) { return value !== undefined && value !== null && String(value) !== "" }
    // A field's `when` ({other key: [values]}): shown only while the other
    // field's value is listed; hidden fields are neither required nor sent.
    function fieldShown(f) {
        if (!f || !f.when) return true
        return Object.keys(f.when).every(k => {
            let v = formValues[k]
            if (!given(v)) {
                const other = addFields.find(o => o.key === k)
                v = other ? other.default : undefined
            }
            return (f.when[k] || []).map(String).includes(String(v))
        })
    }
    readonly property bool formComplete: addBackend !== null && addBackend.available
        && addKind !== "mesh" && addFields.every(f => !fieldShown(f) || !f.required || given(formValues[f.key]))
    function addBulb() {
        const args = ["add", addKind]
        if (formName.trim()) args.push("name=" + formName.trim())
        addFields.forEach(f => {
            const v = formValues[f.key]
            if (!given(v) || !fieldShown(f)) return
            args.push(f.key + "=" + (f.type === "bool" ? (isTrue(v) ? "on" : "off") : String(v)))
        })
        page.addError = ""
        page.addAccess = false
        ctl.run(args, reply => {
            if (reply.error) {
                page.addError = reply.error
                page.addAccess = !!reply.access
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
    function search() { scan(addKind) }
    function scanBluetooth() { scan("bluetooth") }
    // Every available kind: `discover` without a kind.
    function scanAll() { scan("all") }
    readonly property var availableKinds: kinds.filter(k => k.available).map(k => k.kind)
    function scan(what) {
        if (searching) return
        autoTimer.stop()
        searching = true
        searchingWhat = what
        addError = ""
        addAccess = false
        const gen = scanGen
        ctl.run(what === "all" ? ["discover"] : ["discover", what], reply => {
            page.searching = false
            page.searchingWhat = ""
            if (gen !== page.scanGen) return
            page.searched = true
            if (reply.error) page.addError = reply.error
            else page.merge(what === "bluetooth" ? page.btKinds : what === "all" ? page.availableKinds : [what],
                            reply.discovered || [], reply.errors || {},
                            reply.access || [])
            if (page.autoScan && addDialog.opened) autoTimer.restart()
        })
    }
    // Entries of the scanned kinds that this scan missed count a miss and go
    // after 3 in a row while auto scan is on (at once otherwise).
    function merge(scope, entries, errors, access) {
        const keyOf = e => e.kind + "\n" + String(e.address || e.name || "")
        const fresh = {}
        entries.forEach(e => { fresh[keyOf(e)] = e })
        const out = []
        found.forEach(old => {
            const k = keyOf(old)
            if (fresh[k]) {
                out.push(Object.assign({}, fresh[k], {"misses": 0}))
                delete fresh[k]
            } else if (!scope.includes(old.kind)) {
                out.push(old)
            } else if (old.misses + 1 < (autoScan ? 3 : 1)) {
                out.push(Object.assign({}, old, {"misses": old.misses + 1}))
            }
        })
        entries.forEach(e => { if (fresh[keyOf(e)]) out.push(Object.assign({}, e, {"misses": 0})) })
        found = out
        const errs = {}
        Object.keys(foundErrors).forEach(k => { if (!scope.includes(k)) errs[k] = foundErrors[k] })
        Object.keys(errors).forEach(k => { errs[k] = errors[k] })
        foundErrors = errs
        foundAccess = foundAccess.filter(k => !scope.includes(k)).concat(access || [])
    }
    // The root setup's udev rule lets the user open USB lights and serial
    // controllers: run it (pkexec in the login session, as the flick box
    // does), then scan that kind again.
    function grantAccess(kind) {
        const script = (ctl.st.setup || {}).script
        if (!script || granting) return
        granting = true
        ctl.shell(["pkexec", script], (code, out, err) => {
            page.granting = false
            if (code === 0) {
                page.addAccess = false
                page.act(["setup-status"], "", () => page.scan(kind))
            } else {
                page.addAccess = false
                page.addError = code === 126 ? page.tr("System setup cancelled.")
                              : code === 127 ? page.tr("System setup not allowed, or no password prompt available.")
                              : page.tr("System setup failed (exit %1).", code)
            }
        })
    }
    function pick(entry) {
        if (entry.known) return
        const index = kinds.findIndex(k => k.kind === entry.kind)
        if (index < 0) return
        kindBox.currentIndex = index
        formName = entry.name || ""
        formValues = Object.assign(defaults(), entry.config || {})
        addError = ""
    }
    Timer {
        id: autoTimer
        interval: 5000
        onTriggered: if (page.autoScan && addDialog.opened) page.scanBluetooth()
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

        // ------------------------------------------------------- wall-switch flick
        ColumnLayout {
            id: flickBox
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            readonly property var modes: [
                {"value": "off", "text": "Off", "hint": "The wall switch works like a normal switch: power on means light on."},
                {"value": "on", "text": "On", "hint": "Flicking the wall switch off and on toggles the light; the bulb boots into the toggled state."},
                {"value": "fast", "text": "Fast (needs system setup)", "hint": "Like On, with Bluetooth tuned to reconnect faster after a flick."},
                {"value": "exclusive", "text": "Exclusive (needs system setup)", "hint": "Like Fast, without a Bluetooth connection: Haus.Licht takes one Bluetooth adapter for itself and re-arms the bulb sooner after a flick."}
            ]
            readonly property string mode: page.ctl.st.flick || "on"
            readonly property var setup: page.ctl.st.setup || ({})
            property bool busy: false  // our pkexec run is up (its password prompt)
            property string note: ""
            function modeLabel(value) { return page.tr((modes.find(m => m.value === value) || modes[1]).text) }
            function choose(index) {
                const value = modes[index].value
                note = ""
                if (value === mode) return
                if ((value === "fast" || value === "exclusive") && setup.status !== "current" && setup.script) {
                    // pkexec here, in the login session (the polkit agent is there, the daemon is not)
                    busy = true
                    page.ctl.shell(["pkexec", setup.script], (code, out, err) => {
                        busy = false
                        if (code === 0) {
                            page.act(["setup-status"], "", () => page.act(["flick", value]))
                        } else {
                            const keep = modeLabel(mode)
                            note = code === 126 ? page.tr("System setup cancelled: wall-switch flick stays %1.", keep)
                                 : code === 127 ? page.tr("System setup not allowed, or no password prompt available: wall-switch flick stays %1.", keep)
                                 : page.tr("System setup failed (exit %2): wall-switch flick stays %1.", keep, code)
                            flickCombo.currentIndex = Qt.binding(() => flickBox.modeIndex)
                            page.ctl.refresh()
                        }
                    })
                    return
                }
                page.act(["flick", value], "", () => { flickCombo.currentIndex = Qt.binding(() => flickBox.modeIndex) })
            }
            readonly property int modeIndex: Math.max(0, modes.findIndex(m => m.value === mode))
            // Exclusive: the adapter taken from BlueZ (ctl adapters; chosen = the one it takes)
            property var adapters: []
            property string chosenAdapter: ""
            property bool othersStop: false
            readonly property var exclusive: page.ctl.st.exclusive || ({})
            function loadAdapters() {
                page.ctl.run(["adapters"], reply => {
                    if (reply.error || !reply.adapters) return
                    adapters = reply.adapters
                    chosenAdapter = reply.chosen || ""
                    othersStop = !!reply.others_stop
                    adapterCombo.currentIndex = Qt.binding(() => flickBox.adapterIndex)
                })
            }
            readonly property int adapterIndex: Math.max(0, adapters.findIndex(a => a.name === chosenAdapter))
            function adapterText(a) {
                return a.name + " · " + (a.address || "?") + " · " + page.tr(a.builtin ? "built-in" : "USB")
            }
            onModeChanged: if (mode === "exclusive") loadAdapters()
            Component.onCompleted: if (mode === "exclusive") loadAdapters()
            RowLayout {
                QQC2.Label { text: page.tr("Wall-switch flick:") }
                QQC2.ComboBox {
                    id: flickCombo
                    enabled: page.ctl.loaded && !flickBox.setup.running && !flickBox.busy
                    model: flickBox.modes.map(m => page.tr(m.text))
                    currentIndex: flickBox.modeIndex
                    onActivated: index => flickBox.choose(index)
                }
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.7
                text: page.tr((flickBox.modes.find(m => m.value === flickBox.mode) || flickBox.modes[1]).hint)
            }
            RowLayout {
                visible: flickBox.mode === "exclusive"
                QQC2.Label { text: page.tr("Adapter:") }
                QQC2.ComboBox {
                    id: adapterCombo
                    enabled: page.ctl.loaded && flickBox.adapters.length > 0
                    model: flickBox.adapters.map(a => flickBox.adapterText(a))
                    currentIndex: flickBox.adapterIndex
                    onActivated: index => {
                        const a = flickBox.adapters[index]
                        page.act(["set", "exclusive_adapter=" + (a.address || a.name)], "", () => flickBox.loadAdapters())
                    }
                }
            }
            Kirigami.InlineMessage {
                Layout.fillWidth: true
                type: Kirigami.MessageType.Warning
                visible: flickBox.mode === "exclusive" && flickBox.othersStop
                text: page.tr("Other Bluetooth devices (headphones, mice) stop working while Exclusive mode runs.")
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.7
                visible: flickBox.setup.status !== undefined
                text: (flickBox.setup.running || flickBox.busy) ? page.tr("System setup: running (enter the administrator password; Bluetooth restarts)")
                    : flickBox.setup.status === "current" ? page.tr("System setup: installed v%1", flickBox.setup.installed)
                    : (flickBox.setup.status === "outdated" ? page.tr("System setup: outdated") : page.tr("System setup: missing"))
                      + " " + page.tr("(Fast and Exclusive install it: administrator password, Bluetooth restarts)")
            }
            Kirigami.InlineMessage {
                Layout.fillWidth: true
                type: Kirigami.MessageType.Warning
                visible: !!(flickBox.note || flickBox.setup.note || (flickBox.mode === "exclusive" && flickBox.exclusive.note))
                text: flickBox.note || flickBox.setup.note || (flickBox.mode === "exclusive" && flickBox.exclusive.note) || ""
            }
            // while the password prompt is up, look more often than the 10 s poll
            Timer {
                interval: 1000
                repeat: true
                running: !!flickBox.setup.running
                onTriggered: if (page.ctl.pending === 0) page.ctl.refresh()
            }
        }

        // while an Identify or Test runs, look more often than the 10 s poll (its step, its end)
        Timer {
            interval: 1000
            repeat: true
            running: (page.ctl.st.identifying || []).length > 0
                     || Object.values(page.ctl.st.testing || {}).some(t => t.running)
            onTriggered: if (page.ctl.pending === 0) page.ctl.refresh()
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
                    readonly property bool reachable: bulb.online !== false
                    readonly property bool identifying: (page.ctl.st.identifying || []).includes(bid)
                    readonly property var testInfo: (page.ctl.st.testing || {})[bid] || null
                    readonly property bool testing: !!testInfo && testInfo.running === true
                    // the running test's step, then its measured rate
                    readonly property string testText: !testInfo ? ""
                        : testing ? page.tr("Test: %1", page.tr(({"starting": "Starting…", "bright": "Brightest",
                                                                 "fade": "Fade", "colour": "Colours",
                                                                 "strobe": "On/off, fast to slow"})[testInfo.step]
                                                               || testInfo.step))
                        : testInfo.rate !== null && testInfo.rate !== undefined
                            ? page.tr("Fastest on/off: %1 per second", testInfo.rate)
                        : testInfo.step === "stopped" ? page.tr("Test stopped") : ""
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
                                        {"key": "icon_on", "label": "On", "tip": "Icon when on"},
                                        {"key": "icon_off", "label": "Off", "tip": "Icon when off (optional)"},
                                        {"key": "icon_disconnected", "label": "Disconnected", "tip": "Icon when disconnected (optional)"}
                                    ]
                                    delegate: QQC2.ToolButton {
                                        id: iconButton
                                        required property var modelData
                                        readonly property string value: card.bulb[modelData.key] || ""
                                        readonly property bool fallbackBadge: value === "" && modelData.key === "icon_disconnected"
                                                                              && !!card.bulb.icon_on
                                        // empty: what the widget shows instead, dimmed (no on icon: the
                                        // lighthouse; no off / disconnected icon: the on icon, the
                                        // disconnected one with a badge)
                                        readonly property string shown: value !== "" ? value
                                            : modelData.key !== "icon_on" ? (card.bulb.icon_on || "") : ""
                                        icon.name: shown === "" || shown.startsWith("/") ? "" : shown
                                        icon.source: shown.startsWith("/") ? "file://" + shown
                                            : shown === "" ? page.iconSets.url(page.iconSet,
                                                modelData.key === "icon_on" ? "on" : modelData.key === "icon_off" ? "off" : "disconnected")
                                            : ""
                                        icon.width: Kirigami.Units.iconSizes.smallMedium
                                        icon.height: Kirigami.Units.iconSizes.smallMedium
                                        opacity: value === "" ? (fallbackBadge ? 0.4 : 0.5) : 1
                                        display: QQC2.AbstractButton.TextUnderIcon
                                        font: Kirigami.Theme.smallFont
                                        text: page.tr(modelData.label)
                                        Accessible.name: page.tr(modelData.tip)
                                        QQC2.ToolTip.text: page.tr(modelData.tip)
                                        QQC2.ToolTip.visible: hovered
                                        QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                                        onClicked: iconMenu.popup(iconButton, 0, iconButton.height)
                                        Kirigami.Icon {
                                            visible: iconButton.fallbackBadge
                                            // bottom right of the icon, above the label
                                            x: (iconButton.width + iconButton.icon.width) / 2 - width
                                            y: iconButton.topPadding + iconButton.icon.height - height
                                            width: Kirigami.Units.iconSizes.small / 1.5
                                            height: width
                                            source: "network-disconnect"
                                        }
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
                                text: page.detailsText(card.bulb)
                                font: Kirigami.Theme.smallFont
                                color: Kirigami.Theme.disabledTextColor
                                elide: Text.ElideRight
                                QQC2.ToolTip.text: text
                                QQC2.ToolTip.visible: truncated && detailsHover.hovered
                                QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                                HoverHandler { id: detailsHover }
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
                            // Identify and Test (daemon: identify ID on|off, test ID [stop])
                            RowLayout {
                                visible: !card.confirming
                                QQC2.ToolButton {
                                    text: page.tr("Identify")
                                    icon.name: "view-visible"
                                    display: QQC2.AbstractButton.TextBesideIcon
                                    checkable: true
                                    checked: card.identifying
                                    enabled: card.reachable && card.used && !card.testing
                                    onToggled: {
                                        page.act(["identify", card.bid, checked ? "on" : "off"])
                                        checked = Qt.binding(() => card.identifying)
                                    }
                                    Accessible.description: page.tr("Blink slowly to find this bulb")
                                    QQC2.ToolTip.text: page.tr("Blink slowly to find this bulb")
                                    QQC2.ToolTip.visible: hovered
                                    QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                                }
                                QQC2.Button {
                                    text: card.testing ? page.tr("Stop") : page.tr("Test")
                                    icon.name: card.testing ? "media-playback-stop" : "media-playback-start"
                                    enabled: card.testing || (card.reachable && card.used)
                                    onClicked: page.act(card.testing ? ["test", card.bid, "stop"] : ["test", card.bid])
                                    Accessible.description: page.tr("Brightest, a fade, colours, then on/off from fast to slow (about 25 s)")
                                    QQC2.ToolTip.text: page.tr("Brightest, a fade, colours, then on/off from fast to slow (about 25 s)")
                                    QQC2.ToolTip.visible: hovered
                                    QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                                }
                                Kirigami.Icon {
                                    source: "dialog-warning"
                                    implicitWidth: Kirigami.Units.iconSizes.small
                                    implicitHeight: Kirigami.Units.iconSizes.small
                                }
                                QQC2.Label {
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    font: Kirigami.Theme.smallFont
                                    opacity: 0.7
                                    text: page.tr("Flashes quickly: not for people sensitive to flashing light (photosensitive epilepsy)")
                                }
                            }
                            QQC2.Label {
                                visible: !card.confirming && card.testText !== ""
                                text: card.testText
                                font: Kirigami.Theme.smallFont
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
            onClicked: page.openAdd()
        }

        // ------------------------------------------------------- autoconnect
        ColumnLayout {
            id: autoBox
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            readonly property var ignored: page.ctl.st.autoconnect_ignore || []
            QQC2.Switch {
                text: page.tr("Connect new devices automatically")
                enabled: page.ctl.loaded
                checked: !!page.ctl.st.autoconnect
                onToggled: page.act(["autoconnect", checked ? "on" : "off"])
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.7
                text: page.tr("Warning: this may take over lights that belong to someone else nearby (neighbours' bulbs, shared spaces). Bluetooth mesh bulbs are only added by hand.")
            }
            QQC2.Label {
                visible: autoBox.ignored.length > 0
                text: page.tr("Removed, not connected automatically again:")
            }
            Repeater {
                model: autoBox.ignored
                RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    QQC2.Label {
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                        text: (modelData.name || modelData.id || modelData.address)
                              + (modelData.address ? "  ·  " + modelData.address : "")
                    }
                    QQC2.Button {
                        text: page.tr("Allow again")
                        icon.name: "edit-undo"
                        onClicked: page.act(["unignore", modelData.id || modelData.address])
                    }
                }
            }
        }

        // ------------------------------------------- this widget, logs
        ColumnLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            QQC2.CheckBox {
                text: page.tr("Invert scroll direction")
                checked: page.cfg_invertScroll
                onToggled: page.cfg_invertScroll = checked
                QQC2.ToolTip.text: page.tr("The mouse wheel on the tray icon and on the popup's sliders.")
                QQC2.ToolTip.visible: hovered
                QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
            }
            // Icon set: each row of the list shows the set's on, off and
            // disconnected icons; source and licence of the chosen one below.
            RowLayout {
                spacing: Kirigami.Units.smallSpacing
                QQC2.Label { text: page.tr("Icon set:") }
                QQC2.ComboBox {
                    id: iconSetBox
                    readonly property var current: page.iconSets.sets[Math.max(0, currentIndex)]
                    model: page.iconSets.sets
                    textRole: "name"
                    valueRole: "id"
                    currentIndex: Math.max(0, indexOfValue(page.iconSet))
                    onActivated: Plasmoid.configuration.iconSet = currentValue
                    Accessible.name: page.tr("Icon set")
                    QQC2.ToolTip.text: page.tr("For the tray icon and bulbs without their own icons.")
                    QQC2.ToolTip.visible: hovered
                    QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                    // The desktop style (Breeze, org.kde.desktop ComboBox.qml)
                    // paints the closed box natively: its background StyleItem
                    // draws displayText itself, at the left, under any custom
                    // contentItem; that was the name over the icons. With no
                    // displayText it draws only the frame and the arrow; the
                    // row below shows the name. Its padding is 5 on both sides,
                    // so the row would run under the arrow: keep it clear.
                    displayText: ""
                    leftPadding: padding + (mirrored ? Kirigami.Units.iconSizes.small + Kirigami.Units.smallSpacing : 0)
                    rightPadding: padding + (mirrored ? 0 : Kirigami.Units.iconSizes.small + Kirigami.Units.smallSpacing)
                    contentItem: SetPreview {
                        setId: iconSetBox.current.id
                        setName: iconSetBox.current.name
                    }
                    // Popup rows: only this contentItem draws (no text set, so
                    // the style's own label stays empty); height from the row.
                    delegate: QQC2.ItemDelegate {
                        required property var modelData
                        required property int index
                        width: ListView.view ? ListView.view.width : implicitWidth
                        highlighted: iconSetBox.highlightedIndex === index
                        Accessible.name: modelData.name
                        contentItem: SetPreview {
                            setId: modelData.id
                            setName: modelData.name
                        }
                    }
                }
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                font: Kirigami.Theme.smallFont
                opacity: 0.7
                text: page.tr("Source: %1 · Licence: %2", iconSetBox.current.source, iconSetBox.current.licence)
            }
            QQC2.Button {
                text: page.tr("View logs")
                icon.name: "utilities-terminal"
                onClicked: page.viewLogs()
                QQC2.ToolTip.text: page.tr("Shows the service's log in a terminal (journalctl).")
                QQC2.ToolTip.visible: hovered
                QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
            }
        }
    }

    // ------------------------------------------------------ add dialog
    function openAdd() {
        page.resetForm()
        page.clearResults()
        page.scanGen += 1
        addDialog.open()
    }
    // The popup's "Connect new bulb…" sets openAddBulb (main.xml) and opens
    // Configure: a new dialog starts on this page, an open one is switched
    // here by the other pages (AddBulbRedirect.qml); also when already here.
    function openAddIfAsked() {
        if (!Plasmoid.configuration.openAddBulb) return
        Plasmoid.configuration.openAddBulb = false
        openAdd()
    }
    Component.onCompleted: Qt.callLater(openAddIfAsked)
    Connections {
        target: Plasmoid.configuration
        function onValueChanged(key, value) { if (key === "openAddBulb" && value) page.openAddIfAsked() }
    }
    Kirigami.Dialog {
        id: addDialog
        parent: QQC2.Overlay.overlay
        modal: true
        title: page.tr("Add a bulb")
        preferredWidth: Kirigami.Units.gridUnit * 27
        padding: Kirigami.Units.largeSpacing
        standardButtons: Kirigami.Dialog.NoButton
        // Auto scan (main.xml autoScanBluetooth): the first scan at once.
        onOpened: if (page.autoScan) page.scanBluetooth()
        onClosed: autoTimer.stop()
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
                actions: [
                    Kirigami.Action {
                        text: page.tr("Grant access…")
                        icon.name: "security-high"
                        visible: page.addAccess
                        enabled: !page.granting
                        onTriggered: page.grantAccess(page.addKind)
                    }
                ]
            }

            // Scan every Bluetooth kind at once; Auto scan repeats it 5 s
            // after each scan while the dialog is open. Scan everything:
            // every available kind, once.
            RowLayout {
                Layout.fillWidth: true
                QQC2.Button {
                    text: page.tr("Scan Bluetooth")
                    icon.name: "network-bluetooth"
                    enabled: !page.searching && page.btKinds.length > 0
                    onClicked: page.scanBluetooth()
                }
                QQC2.Button {
                    text: page.tr("Scan everything")
                    icon.name: "system-search"
                    enabled: !page.searching && page.availableKinds.length > 0
                    onClicked: page.scanAll()
                }
                QQC2.BusyIndicator {
                    visible: page.searching && ["bluetooth", "all"].includes(page.searchingWhat)
                    running: visible
                    implicitHeight: Kirigami.Units.iconSizes.medium
                    implicitWidth: implicitHeight
                }
                QQC2.Label {
                    visible: page.searching && ["bluetooth", "all"].includes(page.searchingWhat)
                    text: page.tr("Searching…")
                }
                Item { Layout.fillWidth: true }
                QQC2.Switch {
                    text: page.tr("Auto scan")
                    enabled: page.btKinds.length > 0
                    checked: page.autoScan
                    onToggled: {
                        Plasmoid.configuration.autoScanBluetooth = checked
                        if (!checked) autoTimer.stop()
                        else if (!page.searching) page.scanBluetooth()
                    }
                }
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
                        visible: page.fieldShown(modelData)
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
                visible: page.addBackend !== null

                RowLayout {
                    visible: page.addKind !== "mesh"
                    QQC2.Button {
                        text: page.addBackend && page.addBackend.bluetooth ? page.tr("Search Bluetooth") : page.tr("Search network")
                        icon.name: "system-search"
                        enabled: !page.searching && page.addBackend !== null && page.addBackend.available
                        onClicked: page.search()
                    }
                    QQC2.BusyIndicator {
                        visible: page.searching && !["bluetooth", "all"].includes(page.searchingWhat)
                        running: visible
                        implicitHeight: Kirigami.Units.iconSizes.medium
                        implicitWidth: implicitHeight
                    }
                    QQC2.Label {
                        visible: page.searching && !["bluetooth", "all"].includes(page.searchingWhat)
                        text: page.tr("Searching…")
                    }
                }
                QQC2.Label {
                    visible: page.searched && !page.searching && page.found.length === 0
                             && Object.keys(page.foundErrors).length === 0
                    text: page.tr("Nothing found.")
                    opacity: 0.7
                }
                ColumnLayout {
                    visible: page.found.length > 0 || Object.keys(page.foundErrors).length > 0
                    Kirigami.FormData.label: page.tr("Found:")
                    Layout.fillWidth: true
                    spacing: 0
                    Repeater {
                        model: page.found
                        delegate: QQC2.ItemDelegate {
                            id: foundRow
                            required property var modelData
                            Layout.fillWidth: true
                            enabled: !modelData.known
                            icon.name: "go-next"
                            contentItem: RowLayout {
                                spacing: Kirigami.Units.smallSpacing
                                Kirigami.Icon {
                                    source: foundRow.icon.name
                                    implicitWidth: Kirigami.Units.iconSizes.small
                                    implicitHeight: implicitWidth
                                }
                                QQC2.Label {
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    text: (foundRow.modelData.name || foundRow.modelData.address) + "  ·  "
                                          + foundRow.modelData.address
                                          + (foundRow.modelData.known ? "  " + page.tr("(added)") : "")
                                }
                                QQC2.Label {
                                    text: page.ctl.kindTitle(foundRow.modelData.kind)
                                    opacity: 0.6
                                }
                            }
                            onClicked: page.pick(modelData)
                        }
                    }
                    Repeater {
                        model: Object.keys(page.foundErrors)
                        delegate: RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            opacity: 0.7
                            Kirigami.Icon {
                                source: "dialog-warning"
                                implicitWidth: Kirigami.Units.iconSizes.small
                                implicitHeight: implicitWidth
                            }
                            QQC2.Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                text: page.ctl.kindTitle(modelData) + ": " + page.tr(page.foundErrors[modelData])
                            }
                            QQC2.Button {
                                visible: page.foundAccess.includes(modelData)
                                text: page.tr("Grant access…")
                                icon.name: "security-high"
                                enabled: !page.granting && !page.searching
                                onClicked: page.grantAccess(modelData)
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
            placeholderText: field.placeholder ? String(field.placeholder) : field.required ? page.tr("required") : ""
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

/*
 * The popup's "Connect new bulb…" sets openAddBulb (main.xml) and triggers
 * Configure. Plasma's config dialog (AppletConfiguration.qml) starts on its
 * first category (Bulbs) only when it is created; an already open dialog is
 * just raised on the page it shows, and there is no API to select a page.
 * So every Configure page but Bulbs holds one of these: when openAddBulb is
 * set (on load, or while the page shows) it walks up from the page to the
 * dialog's root item and calls its open(category), the function the category
 * list itself uses. Bulbs then opens its Add dialog and clears the flag
 * (configBulbs.qml openAddIfAsked). Plasma's own pages (Keyboard Shortcuts,
 * About) have no hook: from there it lands on Bulbs the next time Configure
 * opens fresh.
 */
import QtQuick
import org.kde.plasma.plasmoid

QtObject {
    id: redirect

    property Item configPage
    readonly property url bulbsSource: Qt.resolvedUrl("configBulbs.qml")

    // AppletConfiguration's root: the ancestor with open() and currentSource.
    function dialogRoot() {
        for (let it = configPage ? configPage.parent : null; it; it = it.parent)
            if (typeof it.open === "function" && it.currentSource !== undefined) return it
        return null
    }
    // The Bulbs category as the dialog's ConfigModel lists it (name, icon,
    // source); config.qml's first entry, rebuilt if the model is not reachable.
    function bulbsCategory() {
        const model = typeof configDialog !== "undefined" && configDialog ? configDialog.configModel : null
        for (let i = 0; model && i < model.count; i++) {
            const c = model.get(i)
            if (c && String(c.source) === String(bulbsSource)) return c
        }
        return { "name": Qt.locale().name.startsWith("de") ? "Lampen" : "Bulbs",
                 "icon": "preferences-desktop-display", "source": bulbsSource }
    }
    function showBulbsIfAsked() {
        if (!Plasmoid.configuration.openAddBulb) return
        const dialog = dialogRoot()
        if (!dialog || String(dialog.currentSource) === String(bulbsSource)) return
        dialog.open(bulbsCategory())
    }

    Component.onCompleted: Qt.callLater(showBulbsIfAsked)
    property Connections watch: Connections {
        target: Plasmoid.configuration
        function onValueChanged(key, value) { if (key === "openAddBulb" && value) redirect.showBulbsIfAsked() }
    }
}

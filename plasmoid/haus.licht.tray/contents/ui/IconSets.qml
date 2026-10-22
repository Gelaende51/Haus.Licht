/*
 * The widget's icon sets (Configure → Bulbs → Icon set, main.xml iconSet):
 * one folder per set in contents/icons/, each with on.svg, off.svg and
 * disconnected.svg. Sources and licences: LICENSES.md. A bulb's own icons
 * still win over the set (main.qml iconFor).
 *
 * tests/test_iconsets.py checks that this list matches the folders.
 */
import QtQuick

QtObject {
    readonly property string fallback: "lighthouse"
    readonly property var sets: [
        { "id": "lighthouse", "name": "Haus.Licht", "source": "Haus.Licht", "licence": "AGPL-3.0" },
        { "id": "mdi-lighthouse", "name": "Material Design: Lighthouse",
          "source": "Material Design Icons (Pictogrammers)", "licence": "Apache-2.0" },
        { "id": "mdi-bulb", "name": "Material Design: Bulb",
          "source": "Material Design Icons (Pictogrammers)", "licence": "Apache-2.0" },
        { "id": "tabler-bulb", "name": "Tabler: Bulb", "source": "Tabler Icons (Paweł Kuna)", "licence": "MIT" },
        { "id": "fluent-bulb", "name": "Fluent: Bulb", "source": "Fluent UI System Icons (Microsoft)", "licence": "MIT" },
        { "id": "maki-lighthouse", "name": "Maki: Lighthouse", "source": "Maki (Mapbox)", "licence": "CC0-1.0" }
    ]
    function known(set) { return sets.some(s => s.id === set) }
    // state: on, off or disconnected; an unknown set falls back to the lighthouse.
    function url(set, state) {
        return String(Qt.resolvedUrl("../icons/" + (known(set) ? set : fallback) + "/" + state + ".svg"))
    }
}

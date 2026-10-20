/*
 * The Configure dialog: bulbs, profiles and sound (clap detection). Both pages change the daemon
 * directly through hauslicht-ctl, so there is nothing to apply.
 */
import QtQuick
import org.kde.plasma.configuration

ConfigModel {
    readonly property bool german: Qt.locale().name.startsWith("de")

    ConfigCategory {
        name: german ? "Lampen" : "Bulbs"
        icon: "preferences-desktop-display"
        source: "configBulbs.qml"
    }
    ConfigCategory {
        name: german ? "Profile" : "Profiles"
        icon: "bookmarks"
        source: "configProfiles.qml"
    }
    ConfigCategory {
        name: german ? "Ton" : "Sound"
        icon: "audio-input-microphone"
        source: "configSound.qml"
    }
}

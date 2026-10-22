#!/bin/sh
# One-time system setup for Haus.Licht (run with sudo, or with pkexec: the
# daemon does that when a flick mode needs it). Idempotent; changed system
# files keep a backup of their original. Ends by writing its version
# (install/setup-version) to /usr/local/lib/hauslicht/setup-version.
# - loads the kernel's uleds driver now and at boot, so the sync daemon can
#   create the "keyboard backlight" that Plasma's brightness applet shows;
# - lets the logged-in user open /dev/uleds (systemd uaccess, like audio devices);
# - lets the logged-in user open USB status lights and USB-serial LED
#   controllers (uaccess, rule file 70-haus-licht-devices.rules);
# - checks that BlueZ scans Bluetooth Low Energy (the bulbs speak nothing else).
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi
here=$(cd "$(dirname "$0")" && pwd)
version=$(cat "$here/setup-version")
case $version in ''|*[!0-9]*) echo "bad $here/setup-version" >&2; exit 1 ;; esac

echo uleds > /etc/modules-load.d/haus-licht.conf
echo 'KERNEL=="uleds", TAG+="uaccess"' > /etc/udev/rules.d/70-haus-licht.rules
modprobe uleds
udevadm control --reload
udevadm trigger --action=add /dev/uleds
echo "uleds ready: $(ls -l /dev/uleds)"

# USB status lights (the `usblight` kind; IDs from busylight-core's HID
# lights, tests/test_localdevices.py checks the list) and USB-serial chips of
# LED controllers (the `serial` kind): the logged-in user may open them.
# busylight-core's hidapi is the libusb backend (it opens the USB device
# node); hidraw is there for hidapi's hidraw backend. uaccess must come
# before 73-seat-late.rules, hence 70.
USBLIGHT_IDS="047f:d005 04d8:f372 04d8:f848 0e53:2516 0e53:2517 0e53:2519 1130:0001 1130:0002 1130:1e00 1395:0074 16c0:27db 20a0:41e5 20a0:42da 20a0:42db 27b8:01ed 27bb:3bca 27bb:3bcb 27bb:3bcd 27bb:3bce 27bb:3bcf 2c0d:0001 2c0d:0002 2c0d:0003 2c0d:000a 2c0d:000c 2c0d:0010 340b:f001 340b:f002"
SERIAL_IDS="1a86:7523 10c4:ea60 0403:6001 0403:6015 2341:* 303a:*"
devrules=/etc/udev/rules.d/70-haus-licht-devices.rules
set -f  # 2341:* is a udev glob, not a file name
{
    echo "# Haus.Licht (install/setup-root.sh): USB lights and LED controllers for the logged-in user"
    for id in $USBLIGHT_IDS; do
        vid=${id%:*}; pid=${id#*:}
        echo "SUBSYSTEM==\"usb\", ATTR{idVendor}==\"$vid\", ATTR{idProduct}==\"$pid\", TAG+=\"uaccess\""
        echo "SUBSYSTEM==\"hidraw\", ATTRS{idVendor}==\"$vid\", ATTRS{idProduct}==\"$pid\", TAG+=\"uaccess\""
    done
    for id in $SERIAL_IDS; do
        vid=${id%:*}; pid=${id#*:}
        echo "SUBSYSTEM==\"tty\", ATTRS{idVendor}==\"$vid\", ATTRS{idProduct}==\"$pid\", TAG+=\"uaccess\""
    done
} > "$devrules.new"
set +f
mv "$devrules.new" "$devrules"
udevadm control --reload
udevadm trigger --action=change --subsystem-match=usb --subsystem-match=hidraw --subsystem-match=tty
echo "device access rules: $devrules"

# Root helpers, runnable by the installing user via sudo without a password,
# and by nobody else:
#   conn-update: a short link timeout (fast wall-switch toggles);
#   hci-claim:   lends one adapter to the daemon for flick mode exclusive
#                (BlueZ powers it off, the daemon gets its HCI user channel;
#                given back and powered on when the daemon lets go or exits).
install -D -o root -g root -m 0755 "$here/conn-update" /usr/local/lib/hauslicht/conn-update
install -D -o root -g root -m 0755 "$here/hci-claim" /usr/local/lib/hauslicht/hci-claim
user=${SUDO_USER:-}
if [ -z "$user" ] && [ -n "${PKEXEC_UID:-}" ]; then
    user=$(id -nu "$PKEXEC_UID")
fi
if [ -n "$user" ]; then
    rule="$user ALL=(root) NOPASSWD: /usr/local/lib/hauslicht/conn-update, /usr/local/lib/hauslicht/hci-claim"
    tmp=$(mktemp)
    echo "$rule" > "$tmp"
    if visudo -cf "$tmp" >/dev/null; then
        install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/hauslicht
        echo "sudoers rule installed for $user"
    else
        echo "WARNING: sudoers rule failed validation, not installed" >&2
    fi
    rm -f "$tmp"
fi

conf=/etc/bluetooth/main.conf
if grep -q '^ControllerMode *= *bredr' "$conf" 2>/dev/null; then
    echo "WARNING: $conf has ControllerMode = bredr (no Low Energy)." >&2
    echo "         Set it to dual and restart bluetooth." >&2
fi

# BlueZ tuning for fast reconnects after a wall-switch flick (bluetoothd reads
# it at start). Each key is set in its section, replacing a commented default;
# the first change keeps a backup of the original file.
#   [LE] ScanWindowConnect = ScanIntervalConnect: scan 100 % while connecting,
#        so the bulb's first connectable advert after a reboot is caught;
#   [LE] connection interval 15-30 ms (default 30-50): faster GATT round trips
#        for the boot-count Get and the OnPowerUp Set;
#   [GATT] Cache = always: reuse the bulb's service table on reconnect;
#   [GATT] Channels = 1: no EATT, one ATT bearer (the bulb has no EATT).
set_conf() {  # section key value
    awk -v sec="[$1]" -v key="$2" -v val="$3" '
        function flush() { if (insec && !done) { print key " = " val; done = 1 } }
        /^\[/ { flush(); insec = ($0 == sec) }
        insec && !done && $0 ~ ("^#?[ \t]*" key "[ \t]*=") { print key " = " val; done = 1; next }
        { print }
        END { flush(); if (!done) { print ""; print sec; print key " = " val } }
    ' "$conf" > "$conf.hauslicht-new"
    if cmp -s "$conf" "$conf.hauslicht-new"; then
        rm -f "$conf.hauslicht-new"
        return 0
    fi
    [ -e "$conf.hauslicht-bak" ] || cp -p "$conf" "$conf.hauslicht-bak"
    cat "$conf.hauslicht-new" > "$conf"
    rm -f "$conf.hauslicht-new"
    bt_changed=1
}
bt_changed=0
if [ -e "$conf" ]; then
    set_conf LE ScanIntervalConnect 96
    set_conf LE ScanWindowConnect 96
    set_conf LE MinConnectionInterval 12
    set_conf LE MaxConnectionInterval 24
    set_conf GATT Cache always
    set_conf GATT Channels 1
    if [ "$bt_changed" = 1 ]; then
        echo "$conf tuned (backup: $conf.hauslicht-bak)."
        if systemctl is-active --quiet bluetooth 2>/dev/null; then
            systemctl restart bluetooth
            echo "bluetooth restarted to read it"
        fi
    else
        echo "$conf already tuned"
    fi
fi

# A3 (kernel auto-connect) needs nothing here; A2 (the advertising bearer) is
# flick mode exclusive through hci-claim above: see dev/flick-tuning-notes.md.

install -d -o root -g root -m 0755 /usr/local/lib/hauslicht
echo "$version" > /usr/local/lib/hauslicht/setup-version.new
chmod 0644 /usr/local/lib/hauslicht/setup-version.new
mv /usr/local/lib/hauslicht/setup-version.new /usr/local/lib/hauslicht/setup-version
echo "Haus.Licht system setup v$version installed"

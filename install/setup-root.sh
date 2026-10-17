#!/bin/sh
# One-time system setup for Haus.Licht (run with sudo, or with pkexec: the
# daemon does that when a flick mode needs it). Idempotent; changed system
# files keep a backup of their original. Ends by writing its version
# (install/setup-version) to /usr/local/lib/hauslicht/setup-version.
# - loads the kernel's uleds driver now and at boot, so the sync daemon can
#   create the "keyboard backlight" that Plasma's brightness applet shows;
# - lets the logged-in user open /dev/uleds (systemd uaccess, like audio devices);
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

# Root helper for a short link timeout (fast wall-switch toggles), runnable by
# the installing user via sudo without a password, and by nobody else.
install -D -o root -g root -m 0755 "$here/conn-update" /usr/local/lib/hauslicht/conn-update
user=${SUDO_USER:-}
if [ -z "$user" ] && [ -n "${PKEXEC_UID:-}" ]; then
    user=$(id -nu "$PKEXEC_UID")
fi
if [ -n "$user" ]; then
    rule="$user ALL=(root) NOPASSWD: /usr/local/lib/hauslicht/conn-update"
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

# A3 (kernel auto-connect) and A2 (advertising-bearer arm) need nothing here
# yet: see dev/flick-tuning-notes.md.

install -d -o root -g root -m 0755 /usr/local/lib/hauslicht
echo "$version" > /usr/local/lib/hauslicht/setup-version.new
chmod 0644 /usr/local/lib/hauslicht/setup-version.new
mv /usr/local/lib/hauslicht/setup-version.new /usr/local/lib/hauslicht/setup-version
echo "Haus.Licht system setup v$version installed"

#!/bin/sh
# One-time system setup for Licht.Haus (run with sudo).
# - loads the kernel's uleds driver now and at boot, so the sync daemon can
#   create the "keyboard backlight" that Plasma's brightness applet shows;
# - lets the logged-in user open /dev/uleds (systemd uaccess, like audio devices);
# - checks that BlueZ scans Bluetooth Low Energy (the bulbs speak nothing else).
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi

echo uleds > /etc/modules-load.d/licht-haus.conf
echo 'KERNEL=="uleds", TAG+="uaccess"' > /etc/udev/rules.d/70-licht-haus.rules
modprobe uleds
udevadm control --reload
udevadm trigger --action=add /dev/uleds
echo "uleds ready: $(ls -l /dev/uleds)"

# Root helper for a short link timeout (fast wall-switch toggles), runnable by
# the installing user via sudo without a password, and by nobody else.
here=$(cd "$(dirname "$0")" && pwd)
install -D -o root -g root -m 0755 "$here/conn-update" /usr/local/lib/lichthaus/conn-update
user=${SUDO_USER:-}
if [ -n "$user" ]; then
    rule="$user ALL=(root) NOPASSWD: /usr/local/lib/lichthaus/conn-update"
    tmp=$(mktemp)
    echo "$rule" > "$tmp"
    if visudo -cf "$tmp" >/dev/null; then
        install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/lichthaus
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

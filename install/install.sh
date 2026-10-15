#!/bin/sh
# Per-user install of Haus.Licht from this checkout (no root; for the slider
# also run install/setup-root.sh once with sudo).
set -eu
dir=$(cd "$(dirname "$0")/.." && pwd)

[ -x "$dir/.venv/bin/python" ] || python3 -m venv "$dir/.venv"
"$dir/.venv/bin/pip" install -q "btmesh[ble]"

mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
ln -sf "$dir/hauslicht" "$HOME/.local/bin/hauslicht"
ln -sf "$dir/hauslicht-ctl" "$HOME/.local/bin/hauslicht-ctl"

# replace earlier symlinks instead of writing through them into the checkout
rm -f "$HOME/.config/systemd/user/hauslicht.service"
sed "s|^ExecStart=.*|ExecStart=$dir/.venv/bin/python $dir/hauslicht.py sync|" \
    "$dir/hauslicht.service" > "$HOME/.config/systemd/user/hauslicht.service"
# Plasma tray widget (shows up in the system tray after the next Plasma start)
if command -v kpackagetool6 >/dev/null; then
    kpackagetool6 -t Plasma/Applet -u "$dir/plasmoid/haus.licht.tray" 2>/dev/null \
        || kpackagetool6 -t Plasma/Applet -i "$dir/plasmoid/haus.licht.tray"
fi

systemctl --user daemon-reload
echo "installed. Adopt a bulb (hauslicht adopt), then:"
echo "  systemctl --user enable --now hauslicht"

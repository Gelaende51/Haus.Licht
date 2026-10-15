#!/bin/sh
# Per-user install of Licht.Haus from this checkout (no root; for the slider
# also run install/setup-root.sh once with sudo).
set -eu
dir=$(cd "$(dirname "$0")/.." && pwd)

[ -x "$dir/.venv/bin/python" ] || python3 -m venv "$dir/.venv"
"$dir/.venv/bin/pip" install -q "btmesh[ble]"

mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
ln -sf "$dir/lichthaus" "$HOME/.local/bin/lichthaus"
ln -sf "$dir/lichthaus-ctl" "$HOME/.local/bin/lichthaus-ctl"

# replace earlier symlinks instead of writing through them into the checkout
rm -f "$HOME/.config/systemd/user/lichthaus.service"
sed "s|^ExecStart=.*|ExecStart=$dir/.venv/bin/python $dir/lichthaus.py sync|" \
    "$dir/lichthaus.service" > "$HOME/.config/systemd/user/lichthaus.service"
# Plasma tray widget (shows up in the system tray after the next Plasma start)
if command -v kpackagetool6 >/dev/null; then
    kpackagetool6 -t Plasma/Applet -u "$dir/plasmoid/haus.licht.tray" 2>/dev/null \
        || kpackagetool6 -t Plasma/Applet -i "$dir/plasmoid/haus.licht.tray"
fi

systemctl --user daemon-reload
echo "installed. Adopt a bulb (lichthaus adopt), then:"
echo "  systemctl --user enable --now lichthaus"

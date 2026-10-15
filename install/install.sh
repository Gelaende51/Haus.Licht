#!/bin/sh
# Per-user install of Licht.Haus from this checkout (no root; for the slider
# also run install/setup-root.sh once with sudo).
set -eu
dir=$(cd "$(dirname "$0")/.." && pwd)

[ -x "$dir/.venv/bin/python" ] || python3 -m venv "$dir/.venv"
"$dir/.venv/bin/pip" install -q "btmesh[ble]"

mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user" "$HOME/.config/autostart" \
         "$HOME/.local/share/applications"
printf '#!/bin/sh\nexec "%s/.venv/bin/python" "%s/lichthaus.py" "$@"\n' "$dir" "$dir" \
    > "$HOME/.local/bin/lichthaus"
chmod +x "$HOME/.local/bin/lichthaus"

# replace earlier symlinks instead of writing through them into the checkout
rm -f "$HOME/.config/systemd/user/lichthaus.service" "$HOME/.config/autostart/lichthaus-tray.desktop" \
      "$HOME/.local/share/applications/lichthaus-tray.desktop"
sed "s|^ExecStart=.*|ExecStart=$dir/.venv/bin/python $dir/lichthaus.py sync|" \
    "$dir/lichthaus.service" > "$HOME/.config/systemd/user/lichthaus.service"
sed "s|@DIR@|$dir|" "$dir/install/lichthaus-tray.desktop" \
    | tee "$HOME/.config/autostart/lichthaus-tray.desktop" \
    > "$HOME/.local/share/applications/lichthaus-tray.desktop"

systemctl --user daemon-reload
echo "installed. Adopt a bulb (lichthaus adopt), then:"
echo "  systemctl --user enable --now lichthaus"

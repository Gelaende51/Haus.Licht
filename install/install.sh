#!/bin/sh
# Per-user install of Haus.Licht from this checkout (no root; for the slider
# also run install/setup-root.sh once with sudo).
#
#   install/install.sh                        mesh bulbs only (Ledvance Volkslicht)
#   install/install.sh --backend wiz,hue      also the libraries these kinds need
#   install/install.sh --all-backends         the libraries of every kind
#   install/install.sh --dry-run ...          print the pip command, change nothing
set -eu
dir=$(cd "$(dirname "$0")/.." && pwd)

kinds=""
dry=""
while [ $# -gt 0 ]; do
    case "$1" in
        --backend|--backends) [ $# -ge 2 ] || { echo "$1 needs KIND[,KIND...]" >&2; exit 2; }
            kinds="${kinds:+$kinds,}$2"; shift 2 ;;
        --backend=*|--backends=*) kinds="${kinds:+$kinds,}${1#*=}"; shift ;;
        --all-backends) kinds="all"; shift ;;
        --dry-run|-n) dry=1; shift ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option $1 (try --help)" >&2; exit 2 ;;
    esac
done
case ",$kinds," in *,all,*) kinds="all" ;; esac

if [ -n "$dry" ]; then
    py=$(command -v python3)
    [ -x "$dir/.venv/bin/python" ] && py="$dir/.venv/bin/python"
    echo "pip install -r $dir/requirements.txt"
    if [ -n "$kinds" ]; then
        reqs=$("$py" "$dir/hauslicht.py" backends --requires "$kinds")
        # shellcheck disable=SC2086
        echo "pip install" $reqs
    fi
    exit 0
fi

[ -x "$dir/.venv/bin/python" ] || python3 -m venv "$dir/.venv"
"$dir/.venv/bin/pip" install -q -r "$dir/requirements.txt"
if [ -n "$kinds" ]; then
    reqs=$("$dir/.venv/bin/python" "$dir/hauslicht.py" backends --requires "$kinds")
    # shellcheck disable=SC2086
    [ -z "$reqs" ] || "$dir/.venv/bin/pip" install -q $reqs
fi

mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
ln -sf "$dir/hauslicht" "$HOME/.local/bin/hauslicht"
ln -sf "$dir/hauslicht-ctl" "$HOME/.local/bin/hauslicht-ctl"
# man pages (man hauslicht, man hauslicht-ctl; German under de), linked like the commands
mkdir -p "$HOME/.local/share/man/man1" "$HOME/.local/share/man/de/man1"
for page in hauslicht.1 hauslicht-ctl.1; do
    ln -sf "$dir/man/$page" "$HOME/.local/share/man/man1/$page"
    ln -sf "$dir/man/de/$page" "$HOME/.local/share/man/de/man1/$page"
done

# replace earlier symlinks instead of writing through them into the checkout
rm -f "$HOME/.config/systemd/user/hauslicht.service"
sed -e "s|^ExecStart=.*|ExecStart=$dir/.venv/bin/python $dir/hauslicht.py sync|" \
    -e "s|^ExecStopPost=.*|ExecStopPost=-$dir/.venv/bin/python $dir/hauslicht.py disarm --if-armed|" \
    "$dir/hauslicht.service" > "$HOME/.config/systemd/user/hauslicht.service"
# The daemon's app id for xdg-desktop-portal (follow video: the screen-cast
# permission is kept under it); hidden, not a launcher
mkdir -p "$HOME/.local/share/applications"
cp "$dir/install/io.github.gelaende51.HausLicht.desktop" "$HOME/.local/share/applications/"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$HOME/.local/share/applications" || true
# Plasma tray widget (shows up in the system tray after the next Plasma start)
if command -v kpackagetool6 >/dev/null; then
    kpackagetool6 -t Plasma/Applet -u "$dir/plasmoid/haus.licht.tray" 2>/dev/null \
        || kpackagetool6 -t Plasma/Applet -i "$dir/plasmoid/haus.licht.tray"
fi

systemctl --user daemon-reload
echo "installed. Adopt a bulb (hauslicht adopt), then:"
echo "  systemctl --user enable --now hauslicht"

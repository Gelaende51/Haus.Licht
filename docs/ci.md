# Continuous integration

Three GitHub Actions workflows in `.github/workflows/`; each can also be
started by hand on any branch: `gh workflow run <file> --ref <branch>`.

| Workflow | When | Where | What |
|---|---|---|---|
| `tests.yml` | every push and pull request to main | ubuntu-24.04 | pytest on Python 3.12, 3.13, 3.14; man pages; qmllint |
| `distros.yml` | Mondays, and by hand | Debian stable, Ubuntu 24.04, Fedora, Arch, openSUSE Tumbleweed containers | `install/install.sh --all-backends`, then pytest on the distribution's Python |
| `plasmoid.yml` | changes under `plasmoid/`, Mondays, by hand | Arch container (Plasma 6) | the widget installed with kpackagetool6 and loaded in plasmoidviewer; screenshot artifact |

## tests.yml

- **pytest**: `requirements.txt`, `requirements-dev.txt` and
  `requirements-clap.txt`, then amqtt without its websockets pin (see
  requirements-dev.txt). `-rs` lists every skip with its reason. The runner has
  no Bluetooth adapter, PipeWire or D-Bus session; the tests fake those parts
  (`HAUSLICHT_BACKENDS=mock`, fake capture and desktop), so nothing is skipped
  there. The two follow-rate tests still check the rate cap on CI but need only
  one update instead of several, since slow runners drop wakes
  (`CI` is set by GitHub).
- **man pages**: `dev/gen-man.py --check` (generated pages current), then
  `groff -man -ww -k -z` on `man/*.1` and `man/de/*.1`; any output fails.
- **qmllint**: Qt 6's qmllint in an Arch container with libplasma, kirigami,
  kcmutils, kiconthemes and plasma5support installed, so every import
  resolves and no Plasma or Kirigami warning needs filtering. Fails on an
  error, a non-zero exit, or an `[import]`, `[unresolved-type]` or
  `[missing-type]` warning; the remaining style warnings (`[unqualified]`,
  `[property-override]`) are listed by category, not failed on.

## distros.yml

Each container installs what README "Install" asks for (Python 3 with venv and
pip, BlueZ), runs `install/install.sh --all-backends` (a stand-in `systemctl`
takes its closing `daemon-reload`, as containers have no systemd user
instance), checks what it left in `$HOME` (command and man page links, the
user service, the desktop file, `hauslicht --help`), then installs the test
requirements into the same venv and runs pytest. As root in a container, two
`test_localdevices` cases (read-only files) and three `test_kde` cases (no
node) skip.

## plasmoid.yml

Plasma 6 from Arch (with plasma-desktop, whose desktop containment
plasmoidviewer shows the widget in), `kpackagetool6 -t Plasma/Applet -i plasmoid/haus.licht.tray`,
then `plasmoidviewer -a haus.licht.tray -f planar` (the full representation)
under Xvfb with Qt Quick's software renderer, in `dbus-run-session`, for 25 s. Fails when the viewer exits or its log reports a QML load error in the
package; the screenshot and the log are the `plasmoidviewer` artifact. No
daemon runs, so the widget shows its not-running state.

## The same locally

```sh
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt -r requirements-clap.txt
.venv/bin/pip install --no-deps amqtt==0.12.1
.venv/bin/python -m pytest -q -rs
.venv/bin/python dev/gen-man.py --check
for p in man/*.1 man/de/*.1; do groff -man -ww -k -z "$p"; done
(cd plasmoid/haus.licht.tray/contents && /usr/lib/qt6/bin/qmllint ui/*.qml config/*.qml)
```

A distribution run, in a copy of the checkout inside the container
(install.sh writes `.venv` there and links into the container's `$HOME`):

```sh
podman run --rm -v "$PWD":/src:ro,Z fedora:latest sh -c '
  dnf install -y python3 python3-pip bluez && cp -r /src /w && cd /w &&
  rm -rf .venv hauslicht*.json* &&
  mkdir -p /tmp/stub && printf "#!/bin/sh\n" > /tmp/stub/systemctl && chmod +x /tmp/stub/systemctl &&
  PATH=/tmp/stub:$PATH install/install.sh --all-backends &&
  .venv/bin/pip install -r requirements-dev.txt -r requirements-clap.txt &&
  .venv/bin/pip install --no-deps amqtt==0.12.1 && .venv/bin/python -m pytest -q -rs'
```

The copy drops the host's `.venv` and the state file (`hauslicht.json`,
keys) before anything runs.

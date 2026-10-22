"""The widget's icon sets (plasmoid contents/icons/<set>/): three themable
22×22 SVGs per set, nothing active or external in them, and the QML list of
sets (ui/IconSets.qml) matching the folders."""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

PLASMOID = Path(__file__).resolve().parent.parent / "plasmoid" / "haus.licht.tray" / "contents"
ICONS = PLASMOID / "icons"
STATES = ("on", "off", "disconnected")
SVG = "{http://www.w3.org/2000/svg}"
ALLOWED_PAINT = {"currentcolor", "none"}


def set_dirs():
    return sorted(p for p in ICONS.iterdir() if p.is_dir())


def qml_sets():
    text = (PLASMOID / "ui" / "IconSets.qml").read_text(encoding="utf-8")
    return re.findall(r'"id":\s*"([^"]+)"', text)


def svg_files():
    return [d / f"{s}.svg" for d in set_dirs() for s in STATES]


def test_lighthouse_is_a_set():
    assert (ICONS / "lighthouse").is_dir()


@pytest.mark.parametrize("folder", set_dirs(), ids=lambda p: p.name)
def test_set_has_three_files(folder):
    assert sorted(p.name for p in folder.glob("*.svg")) == sorted(f"{s}.svg" for s in STATES)


def test_qml_list_matches_folders():
    names = qml_sets()
    assert len(names) == len(set(names))
    assert sorted(names) == [d.name for d in set_dirs()]
    assert names[0] == "lighthouse"  # the default comes first


def test_default_is_lighthouse():
    xml = (PLASMOID / "config" / "main.xml").read_text(encoding="utf-8")
    assert re.search(r'<entry name="iconSet" type="String">\s*<default>lighthouse</default>', xml)


def paints(el):
    """fill / stroke values from attributes and the style attribute."""
    out = [el.get("fill"), el.get("stroke")]
    for decl in (el.get("style") or "").split(";"):
        if ":" in decl:
            key, value = (x.strip() for x in decl.split(":", 1))
            if key in ("fill", "stroke"):
                out.append(value)
    return [v for v in out if v is not None]


@pytest.mark.parametrize("path", svg_files(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_svg_is_themable_and_inert(path):
    text = path.read_text(encoding="utf-8")
    root = ET.fromstring(text)
    assert root.tag == f"{SVG}svg"
    assert root.get("viewBox") == "0 0 22 22"
    assert root.get("width") == "22" and root.get("height") == "22"

    style = root.find(f"{SVG}style")
    assert style is not None and style.get("id") == "current-color-scheme"
    assert ".ColorScheme-Text" in (style.text or "")

    drawn = [el for el in root.iter() if el.tag in (f"{SVG}path", f"{SVG}circle", f"{SVG}rect")]
    # Masks cut gaps with black and white; everything drawn outside them
    # follows the theme.
    masked = {el for m in root.iter(f"{SVG}mask") for el in m.iter()}
    themed = [el for el in drawn if el not in masked]
    assert themed
    for el in themed:
        assert el.get("class") == "ColorScheme-Text", ET.tostring(el)[:80]
        values = paints(el)
        assert values, ET.tostring(el)[:80]
        for value in values:
            assert value.lower() in ALLOWED_PAINT, f"{path.name}: {value}"
        assert "currentColor" in values

    assert not any(el.tag == f"{SVG}script" for el in root.iter())
    assert "<script" not in text.lower()
    assert not re.search(r'href\s*=\s*"(?!#)', text)
    assert "http://" not in text.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "https://" not in text
    assert not re.search(r"\bon[a-z]+\s*=", text)  # no event handlers

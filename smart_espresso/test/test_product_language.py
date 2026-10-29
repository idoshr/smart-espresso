"""User-facing copy follows the product language guide.

Internal ids, API keys and comments may still say pull. Strings a person
can read may not.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "smart_espresso" / "web" / "static"
PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
LOGIC = (STATIC / "dashboard_logic.js").read_text(encoding="utf-8")
MANIFEST = (STATIC / "manifest.webmanifest").read_text(encoding="utf-8")

# Whole words a person should not see. Identifiers such as used_ml stay.
FORBIDDEN = re.compile(r"\b(pulls?|pulling|poured|drawn)\b", re.I)
# Element ids are not copy. They keep the old name so the shell stays stable.
ID_TEMPLATE = re.compile(r"^pull-\$\{")


def _html_visible(page):
    stripped = re.sub(r"<script[\s\S]*?</script>", " ", page, flags=re.I)
    stripped = re.sub(r"<style[\s\S]*?</style>", " ", stripped, flags=re.I)
    texts = re.findall(r">([^<]+)<", stripped)
    attrs = re.findall(r'(?:title|aria-label|alt)="([^"]*)"', stripped)
    return [item.strip() for item in texts + attrs if item.strip()]


def _js_strings(source):
    """Quote-aware literals. Comments are skipped so they are not copy."""
    strings = []
    i = 0
    length = len(source)
    while i < length:
        char = source[i]
        if char == "/" and i + 1 < length and source[i + 1] == "/":
            newline = source.find("\n", i)
            i = length if newline < 0 else newline + 1
            continue
        if char == "/" and i + 1 < length and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            i = length if end < 0 else end + 2
            continue
        if char not in "'\"`":
            i += 1
            continue
        quote = char
        i += 1
        buf = []
        while i < length:
            current = source[i]
            if current == "\\":
                buf.append(source[i : i + 2])
                i += 2
                continue
            if (
                quote == "`"
                and current == "$"
                and i + 1 < length
                and source[i + 1] == "{"
            ):
                buf.append("${")
                i += 2
                depth = 1
                while i < length and depth:
                    if source[i] == "{":
                        depth += 1
                    elif source[i] == "}":
                        depth -= 1
                    i += 1
                buf.append("}")
                continue
            if current == quote:
                i += 1
                break
            buf.append(current)
            i += 1
        strings.append("".join(buf))
    return strings


def _script(page):
    parts = re.findall(
        r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", page, flags=re.I
    )
    return "\n".join(parts)


class TestProductLanguage:
    def test_product_name_and_known_rewrites(self):
        assert "<title>Smart Espresso</title>" in PAGE
        assert "<h1>Smart Espresso</h1>" in PAGE
        assert ">Recent shots<" in PAGE
        assert 'aria-label="Recent shots"' in PAGE
        assert ">Water used<" in PAGE
        assert ">used <" in PAGE
        assert ">Last seen —<" in PAGE
        assert ">Keep screen on<" in PAGE
        assert '"name": "Smart Espresso"' in MANIFEST
        assert "Espresso Bar Status" not in PAGE
        assert "The bar" not in _html_visible(PAGE)

    def test_ratio_is_not_rendered_until_dose_exists(self):
        # The helper stays so #26 can turn it on. Nothing calls it.
        assert "TODO(#26)" in PAGE
        assert PAGE.count("ratioText(") == 1
        assert "const DOSE_G" in PAGE

    def test_visible_copy_has_no_pull_or_retired_water_words(self):
        visible = _html_visible(PAGE)
        # The HTML escaper's character class contains quotes. It is not copy.
        script = re.sub(r"/\[&<>\"'\]/g", " ", _script(PAGE))
        strings = _js_strings(script) + _js_strings(LOGIC)
        offenders = []
        for text in visible + strings:
            if ID_TEMPLATE.match(text):
                continue
            stripped = text.strip()
            # Hyphenated ids (last-pull, pulls-note) are not copy.
            phrase = bool(re.search(r"\s", stripped)) or bool(
                FORBIDDEN.fullmatch(stripped)
            )
            if phrase and FORBIDDEN.search(stripped):
                offenders.append(stripped)
        assert offenders == []

    def test_state_labels_and_home_assistant_names(self):
        from smart_espresso.home_assistant import machine_states
        from smart_espresso.machine_state import MachineState

        labels = " ".join(MachineState.LABELS.values())
        assert not FORBIDDEN.search(labels)
        assert MachineState.LABELS[MachineState.READY] == "Ready for a shot"
        assert MachineState.LABELS[MachineState.BREWING] == "Brewing a shot"

        states = machine_states(
            {
                "state": "ready",
                "label": "Ready for a shot",
                "severity": "good",
                "shot": None,
            },
            {"remaining_ml": 1500, "used_ml": 500, "percent": 75},
        )
        assert [state.entity_id for state in states] == [
            "sensor.espresso_machine_state",
            "sensor.espresso_machine_shot",
            "sensor.espresso_water_tank",
        ]
        assert [state.attributes["friendly_name"] for state in states] == [
            "Espresso Machine",
            "Espresso Shot Timer",
            "Espresso Water Tank",
        ]
        assert MachineState.LABELS[MachineState.UNKNOWN] == "No sensor reading"

"""Contrast of the warm-neutral dashboard tokens.

Look C uses five surface colours in each scheme, plus one alert colour.
Text has to clear 4.5:1. Terracotta text sits on the card. Alert text is
the card colour in the light scheme and the page colour in the dark
scheme. Tracks are the page colour on a card on purpose, and are not
held to 3:1.
"""

import re
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
WORKER = Path(__file__).resolve().parents[1] / "web" / "static" / "sw.js"

LIGHT = {
    "bg": "#F2EBE1",
    "card": "#FBF8F3",
    "ink": "#3A2A20",
    "mute": "#735E4D",
    "acc": "#A04620",
    "alert": "#8E2F1C",
}
DARK = {
    "bg": "#1B1511",
    "card": "#271F19",
    "ink": "#F2E8DC",
    "mute": "#B5A08C",
    "acc": "#E58A5C",
    "alert": "#F08A6B",
}

# Rounded to two decimals. Card on accent is the pill. Page on accent is
# under 4.5 and must not be the pill text.
LIGHT_RATIOS = {
    ("ink", "bg"): 11.58,
    ("ink", "card"): 12.94,
    ("mute", "bg"): 5.16,
    ("mute", "card"): 5.77,
    ("card", "acc"): 5.84,
    ("card", "alert"): 7.70,
}
DARK_RATIOS = {
    ("ink", "bg"): 14.93,
    ("ink", "card"): 13.39,
    ("mute", "bg"): 7.20,
    ("mute", "card"): 6.46,
    ("card", "acc"): 6.27,
    ("bg", "alert"): 7.36,
}


def _lin(channel):
    value = channel / 255
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _luminance(color):
    hex_color = color.lstrip("#")
    red = int(hex_color[0:2], 16)
    green = int(hex_color[2:4], 16)
    blue = int(hex_color[4:6], 16)
    return 0.2126 * _lin(red) + 0.7152 * _lin(green) + 0.0722 * _lin(blue)


def contrast(foreground, background):
    lighter = max(_luminance(foreground), _luminance(background))
    darker = min(_luminance(foreground), _luminance(background))
    return (lighter + 0.05) / (darker + 0.05)


def _declarations(css, marker):
    rest = css.split(marker, 1)[1]
    start = 0
    while True:
        opened = rest.find("{", start)
        closed = rest.find("}", opened)
        body = rest[opened + 1 : closed]
        if "--ink:" in body:
            found = dict(
                (name, value.strip().rstrip(";"))
                for name, value in re.findall(
                    r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", body
                )
            )
            return found
        start = closed + 1


def _rule(css, selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, selector
    return match.group(1)


class TestDashboardContrast:
    def setup_method(self):
        self.css = PAGE.read_text(encoding="utf-8")
        self.worker = WORKER.read_text(encoding="utf-8")

    def test_each_scheme_has_the_palette_and_one_alert(self):
        light = _declarations(self.css, "/* theme:light */")
        assert light == LIGHT
        first = self.css.split("/* theme:dark */", 1)[1]
        second_at = first.find("/* theme:dark */")
        assert second_at > 0
        media = _declarations(
            "/* theme:dark */" + first[:second_at], "/* theme:dark */"
        )
        explicit = _declarations(first[second_at:], "/* theme:dark */")
        assert media == explicit == DARK
        # No sixth colour hiding in a contrast theme or a leftover hex.
        hexes = set(re.findall(r"#[0-9A-Fa-f]{3,8}", self.css))
        assert hexes == set(LIGHT.values()) | set(DARK.values())

    def test_text_pairs_clear_aa_and_match_the_palette(self):
        failures = []
        for scheme, tokens, expected in (
            ("light", LIGHT, LIGHT_RATIOS),
            ("dark", DARK, DARK_RATIOS),
        ):
            for (foreground, background), want in expected.items():
                ratio = contrast(tokens[foreground], tokens[background])
                if round(ratio, 2) != want or ratio < 4.5:
                    failures.append(
                        "%s %s on %s is %.2f (want %.2f)"
                        % (scheme, foreground, background, ratio, want)
                    )
        assert failures == []
        # Ink on the accent fails AA. The pill must not use it.
        assert contrast(LIGHT["ink"], LIGHT["acc"]) < 4.5
        assert contrast(DARK["ink"], DARK["acc"]) < 4.5

    def test_live_state_is_a_pill_and_quiet_states_are_neutral(self):
        quiet = _rule(self.css, ".state-label")
        live = _rule(self.css, ".state.live .state-label")
        assert "background: var(--bg)" in quiet
        assert "color: var(--ink)" in quiet
        assert "background: var(--acc)" in live
        assert "color: var(--card)" in live
        assert "classList.remove(kind)" in self.css
        logic = (PAGE.parent / "dashboard_logic.js").read_text(encoding="utf-8")
        assert "Pulling a shot" in logic
        assert "Steaming milk" in logic
        assert "Water running" in logic
        assert "repeating-linear-gradient" not in self.css
        assert "fonts.googleapis.com" not in self.css
        assert "box-shadow" not in self.css
        assert "#C4841A" not in self.css
        assert "#5E3A86" not in self.css
        assert "#1E2924" not in self.css

    def test_manrope_is_self_hosted_and_cached_with_the_shell(self):
        assert 'url("/fonts/manrope.woff2")' in self.css
        assert "Manrope" in self.css
        assert "fonts.googleapis.com" not in self.worker
        assert "fonts.gstatic.com" not in self.worker
        assert "/fonts/manrope.woff2" in self.worker
        assert "espresso-shell-v14" in self.worker
        font = PAGE.parent / "fonts" / "manrope.woff2"
        assert font.is_file()
        assert font.read_bytes()[:4] == b"wOF2"

    def test_labels_are_at_least_11px(self):
        sizes = [
            float(size) for size in re.findall(r"font-size:\s*([0-9.]+)px", self.css)
        ]
        assert sizes
        assert min(sizes) >= 11

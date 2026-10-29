"""Contrast of the dashboard colour tokens, light and dark.

Parses the custom properties in ``index.html`` and checks the pairs the
page actually paints. Large type is not given a lower bar: the small
labels are the ones that failed, so every text pair is held to 4.5:1
and every mark (dot, tick, arc, graph line) to 3:1.
"""

import colorsys
import re
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"

TEXT = (
    "ink",
    "ink-soft",
    "ink-faint",
    "crema-ink",
    "matcha-ink",
    "water-ink",
    "clay-ink",
    "fast-ink",
)
SURFACES = (
    "bg",
    "card",
    "crema-soft",
    "matcha-soft",
    "water-soft",
    "clay-soft",
)
MARKS = ("matcha-ink", "crema-ink", "water-ink", "clay-ink", "fast-ink", "ink-faint")
STATES = ("off", "heating", "ready", "brewing", "steaming", "overpressure", "unknown")


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


def _hsv(color):
    hex_color = color.lstrip("#")
    red = int(hex_color[0:2], 16) / 255
    green = int(hex_color[2:4], 16) / 255
    blue = int(hex_color[4:6], 16) / 255
    return colorsys.rgb_to_hsv(red, green, blue)


def states_separated(first, second):
    """Hue separates saturated bands. A grey has no hue, so lightness must."""
    hue_a, sat_a, _ = _hsv(first)
    hue_b, sat_b, _ = _hsv(second)
    hue = 0
    if sat_a >= 0.18 and sat_b >= 0.18:
        degrees = abs(hue_a - hue_b) * 360
        hue = min(degrees, 360 - degrees)
    return hue >= 40 or contrast(first, second) >= 1.5


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


def _themes(css):
    light = _declarations(css, "/* theme:light */")
    dark = dict(light)
    dark.update(_declarations(css, "/* theme:dark */"))
    contrast_light = dict(light)
    contrast_light.update(_declarations(css, "/* theme:contrast-light */"))
    contrast_dark = dict(dark)
    contrast_dark.update(_declarations(css, "/* theme:contrast-light */"))
    contrast_dark.update(_declarations(css, "/* theme:contrast-dark */"))
    return {
        "light": light,
        "dark": dark,
        "contrast-light": contrast_light,
        "contrast-dark": contrast_dark,
    }


def _pairs(theme):
    text = []
    for name in TEXT:
        for surface in SURFACES:
            text.append((name, surface, 4.5))
    for state in STATES:
        text.append(("state-%s-ink" % state, "state-%s" % state, 4.5))
    # Armed reset: page background colour sitting on the clay ink.
    text.append(("bg", "clay-ink", 4.5))
    marks = []
    for name in MARKS:
        marks.append((name, "card", 3.0))
        marks.append((name, "bg", 3.0))
    return text, marks


def _lamp_pairs(scheme):
    """Colour the brew-bar lamp actually paints on the card.

    Light schemes use the solid band. Dark schemes use the band's ink:
    the solids were picked for white type and vanish on a dark card.
    """
    pairs = []
    for state in STATES:
        if scheme in ("dark", "contrast-dark"):
            pairs.append(("state-%s-ink" % state, "card", 3.0))
        else:
            pairs.append(("state-%s" % state, "card", 3.0))
    return pairs


class TestDashboardContrast:
    def setup_method(self):
        self.css = PAGE.read_text(encoding="utf-8")
        self.themes = _themes(self.css)

    def test_dark_token_blocks_match(self):
        first = self.css.split("/* theme:dark */", 1)[1]
        # The second marker is the explicit data-theme block.
        second_at = first.find("/* theme:dark */")
        assert second_at > 0
        first_decl = _declarations(
            "/* theme:dark */" + first[:second_at], "/* theme:dark */"
        )
        second_decl = _declarations(first[second_at:], "/* theme:dark */")
        assert first_decl == second_decl

    def test_text_and_marks_in_every_scheme(self):
        failures = []
        for scheme, tokens in self.themes.items():
            text, marks = _pairs(tokens)
            for foreground, background, minimum in text + marks + _lamp_pairs(scheme):
                ratio = contrast(tokens[foreground], tokens[background])
                if ratio < minimum:
                    failures.append(
                        "%s %s on %s is %.2f (need %.1f)"
                        % (scheme, foreground, background, ratio, minimum)
                    )
        assert failures == []

    def test_state_bands_are_distinct_in_every_scheme(self):
        failures = []
        for scheme, tokens in self.themes.items():
            for index, left in enumerate(STATES):
                for right in STATES[index + 1 :]:
                    first = tokens["state-%s" % left]
                    second = tokens["state-%s" % right]
                    if not states_separated(first, second):
                        failures.append(
                            "%s %s %s vs %s %s" % (scheme, left, first, right, second)
                        )
        assert failures == []

    def test_a_missing_sensor_is_striped_and_the_lamp_is_only_a_pulse(self):
        assert '[data-state="unknown"] .state-band' in self.css
        assert "repeating-linear-gradient" in self.css
        assert ".state-band > .lamp { display: none; }" in self.css
        assert ".state-band > .lamp.pulse { display: block; }" in self.css

    def test_labels_are_at_least_11px(self):
        sizes = [
            float(size) for size in re.findall(r"font-size:\s*([0-9.]+)px", self.css)
        ]
        assert sizes
        assert min(sizes) >= 11

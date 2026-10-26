"""Pure dashboard rules shared with the page (smart_espresso/web/static)."""

import json
import subprocess
from pathlib import Path

LOGIC = Path(__file__).resolve().parents[1] / "web" / "static" / "dashboard_logic.js"


def call(expression):
    """Evaluate `expression` against the real dashboard logic module."""
    script = (
        "const logic = require(%s);\n" % json.dumps(str(LOGIC))
        + "const value = (%s);\n" % expression
        + "process.stdout.write(JSON.stringify(value));\n"
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class TestConnectionCopy:
    def test_pill_stays_connecting_until_a_reading_exists(self):
        assert call("logic.pillText(false, null)") == "connecting…"
        assert call("logic.pillText(false, null)") != "live"

    def test_pill_is_live_only_while_connected(self):
        assert call("logic.pillText(true, 1000)") == "live"

    def test_pill_is_offline_after_a_reading_was_lost(self):
        assert call("logic.pillText(false, 1000)") == "offline"

    def test_footer_never_invents_a_last_seen_time(self):
        assert call("logic.footerText(false, null, 5000)") == "never connected"

    def test_footer_counts_from_the_reading_not_from_page_load(self):
        # 12.4 s after the reading, rounded.
        assert call("logic.footerText(false, 1000, 13400)") == "last seen 12 s ago"

    def test_footer_does_not_claim_last_seen_while_connected(self):
        assert call("logic.footerText(true, 1000, 9000)") is None

    def test_a_missing_reading_is_a_gap_not_a_number(self):
        assert call("logic.formatReading(null, 1)") == "—"
        assert call("logic.formatReading(9.14, 1)") == "9.1"
        assert call('logic.readingFaulted({bar: null, health: {ok: false}}, "bar")')
        assert (
            call('logic.readingFaulted({bar: 9.1, health: {ok: true}}, "bar")') is False
        )

    def test_sensor_cards_hide_until_a_payload_names_them(self):
        assert call("logic.sensorCardVisible(false, true)") is False
        assert call("logic.sensorCardVisible(true, false)") is False
        assert call("logic.sensorCardVisible(true, true)") is True


class TestShippedShell:
    """The cached HTML is what a phone paints before the script runs."""

    def setup_method(self):
        page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        self.page = page

    def test_markup_does_not_ship_the_live_pill(self):
        assert 'id="conn">connecting…' in self.page
        assert 'id="conn">live' not in self.page
        assert 'class="dot off"' in self.page

    def test_markup_does_not_ship_a_last_seen_footer(self):
        assert 'id="footer">never connected' in self.page
        assert "let lastSeenAt = null" in self.page
        assert "let lastSeenAt = Date.now()" not in self.page

    def test_flow_and_tank_cards_start_hidden(self):
        assert 'id="flow-card" hidden' in self.page
        assert 'id="tank-card" hidden' in self.page

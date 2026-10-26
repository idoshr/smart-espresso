"""Pure dashboard rules shared with the page (smart_espresso/web/static)."""

import json
import re
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

    def test_sensor_cards_hide_until_a_payload_names_them(self):
        assert call("logic.sensorCardVisible(false, true)") is False
        assert call("logic.sensorCardVisible(true, false)") is False
        assert call("logic.sensorCardVisible(true, true)") is True


class TestOfflineRules:
    def test_banner_appears_only_after_three_misses(self):
        hidden = call("logic.offlineBanner(false, 2, null, 0)")
        assert hidden["visible"] is False
        shown = call("logic.offlineBanner(false, 3, null, 9000)")
        assert shown["visible"] is True
        assert shown["title"] == "Can't reach the machine · never connected"
        assert shown["hint"] == "Is the Pi on? Same Wi-Fi?"

    def test_banner_names_the_age_of_the_last_reading(self):
        shown = call("logic.offlineBanner(false, 3, 1000, 13000)")
        assert shown["title"] == "Can't reach the machine · last reading 12 s ago"

    def test_banner_hides_as_soon_as_a_poll_succeeds(self):
        shown = call("logic.offlineBanner(true, 5, 1000, 9000)")
        assert shown["visible"] is False

    def test_retry_resets_backoff_without_clearing_the_outage(self):
        assert call("logic.backoffAfterRetry()") == 0
        assert call("logic.nextPollDelay(0)") == 1000
        assert call("logic.nextPollDelay(1)") == 1000
        assert call("logic.nextPollDelay(2)") == 2000
        assert call("logic.nextPollDelay(6)") == 10000
        # The banner watches the miss count, which retry does not zero.
        still = call("logic.offlineBanner(false, 4, 1000, 5000)")
        assert still["visible"] is True

    def test_tank_reset_is_refused_while_offline(self):
        assert call("logic.tankPostAllowed(false)") is False
        assert call("logic.tankPostAllowed(true)") is True

    def test_reset_failure_toast_includes_the_reason(self):
        assert (
            call("logic.resetFailureToast(403, 'missing dashboard header')")
            == "Couldn't reset the tank — missing dashboard header"
        )
        assert "answered 500" in call("logic.resetFailureToast(500, null)")
        assert "network dropped" in call("logic.resetFailureToast(null, null)")

    def test_shot_timer_does_not_advance_while_offline(self):
        assert call("logic.displayedShotText(false, '12s', '13s')") == "12s"
        assert call("logic.displayedShotText(true, '12s', '13s')") == "13s"
        assert call("logic.displayedShotText(false, null, '4s')") == "4s"

    def test_skeletons_only_before_the_first_payload(self):
        assert call("logic.showSkeletons(false)") is True
        assert call("logic.showSkeletons(true)") is False


class TestGlance:
    def test_ready_and_heating_secondary_lines(self):
        ready = call(
            "logic.heroDetail({state:'ready', since_seconds:600, shot:null}, 1.1)"
        )
        assert ready == "Ready · 10 min"
        heating = call(
            "logic.heroDetail({state:'heating', since_seconds:150, shot:null}, 0.6)"
        )
        assert heating.startswith("0.6 / 0.8 bar")
        assert "min" in heating

    def test_warming_bar_and_eta_need_a_real_slope(self):
        assert call("logic.warmingProgress(0.4)") == 0.5
        assert call("logic.warmingProgress(2)") == 1
        assert call("logic.warmingEtaSeconds(0.2, 3)") is None
        assert call("logic.warmingEtaSeconds(0.16, 200)") is None
        eta = call("logic.warmingEtaSeconds(0.6, 150)")
        assert eta is not None and eta > 0

    def test_over_band_hides_until_the_reading_nears_it(self):
        assert call("logic.showOverBand(0, 8)") is False
        assert call("logic.showOverBand(7.1, 8)") is False
        assert call("logic.showOverBand(7.2, 8)") is True
        assert call("logic.showOverBand(9, 8)") is True
        assert call("logic.showOverBand(null, 8)") is False
        assert call("logic.showOverBand(1, null)") is False

    def test_glance_follows_the_wake_lock_or_a_pin(self):
        assert call("logic.glanceOn(true, false)") is True
        assert call("logic.glanceOn(false, true)") is True
        assert call("logic.glanceOn(false, false)") is False

    def test_window_cue_matches_the_buzz_and_fires_once(self):
        first = call(
            "logic.noteShotWindow({shotSeen:false, buzzedInWindow:false}, true, false)"
        )
        assert first == {
            "shotSeen": True,
            "buzzedInWindow": True,
            "buzz": False,
            "flash": False,
        }
        crossed = call(
            "logic.noteShotWindow({shotSeen:true, buzzedInWindow:false}, true, false)"
        )
        assert crossed["buzz"] is True and crossed["flash"] is True
        again = call(
            "logic.noteShotWindow(" "{shotSeen:true, buzzedInWindow:true}, true, false)"
        )
        assert again["buzz"] is False and again["flash"] is False
        quiet = call(
            "logic.noteShotWindow({shotSeen:true, buzzedInWindow:false}, true, true)"
        )
        assert quiet["buzz"] is True and quiet["flash"] is False
        assert call("logic.clearShotWindow()") == {
            "shotSeen": False,
            "buzzedInWindow": False,
        }

    def test_state_bands_are_distinct_colours(self):
        page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        colors = dict(
            re.findall(
                r"--state-(off|heating|ready|brewing|steaming):\s*(#[0-9A-Fa-f]{6})",
                page,
            )
        )
        assert set(colors) == {"off", "heating", "ready", "brewing", "steaming"}
        assert len(set(colors.values())) == 5
        glyphs = re.findall(
            r"(off|heating|ready|brewing|steaming):\s+'([^']+)'",
            page,
        )
        shapes = {name: shape for name, shape in glyphs}
        assert len(shapes) == 5
        assert len(set(shapes.values())) == 5


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

    def test_connecting_screen_has_skeletons_and_no_live_lamp(self):
        assert 'id="state-label" aria-live="polite">Connecting…' in self.page
        start = self.page.index('id="skeletons"')
        end = self.page.index("<!-- end skeletons -->")
        block = self.page[start:end]
        assert not re.search(r"\d", block)
        assert "body.stale" not in self.page

    def test_offline_banner_is_a_status_with_retry(self):
        assert 'id="offline-banner" role="status"' in self.page
        assert "Retry now" in self.page
        assert "Is the Pi on? Same Wi-Fi?" in self.page
        retry = self.page.split("getElementById('retry')", 1)[1]
        retry = retry.split("poll();", 1)[0]
        assert "backoffAfterRetry" in retry

    def test_tank_button_cannot_post_while_offline(self):
        assert 'id="tank-reset"' in self.page
        assert 'aria-disabled="true"' in self.page
        handler = self.page.split("resetButton.addEventListener", 1)[1]
        handler = handler.split("function disarmReset", 1)[0]
        assert handler.index("tankPostAllowed") < handler.index("fetch(")
        assert "resetFailureToast" in handler

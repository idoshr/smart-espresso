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
        assert call("logic.footerText(false, 1000, 13400)") == "Last seen 12 s ago"

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


class TestOfflineRules:
    def test_banner_appears_only_after_three_misses(self):
        hidden = call("logic.offlineBanner(false, 2, null, 0)")
        assert hidden["visible"] is False
        shown = call("logic.offlineBanner(false, 3, null, 9000)")
        assert shown["visible"] is True
        assert (
            shown["title"] == "Can't reach the machine yet. Is the Pi on? Same Wi-Fi?"
        )
        assert shown["hint"] == ""

    def test_banner_names_the_age_of_the_last_reading(self):
        shown = call("logic.offlineBanner(false, 3, 1000, 13000)")
        assert (
            shown["title"]
            == "Can't reach the machine. Last seen 12 s ago. Is the Pi on? Same Wi-Fi?"
        )

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
            == "Couldn't save the refill. Try again in a moment."
        )
        assert (
            call("logic.resetFailureToast(500, null)")
            == "Couldn't save the refill. Try again in a moment."
        )
        assert (
            call("logic.resetFailureToast(null, null)")
            == "Couldn't save the refill. Check the Wi-Fi and try again."
        )

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
        assert ready == "for 10 min"
        heating = call(
            "logic.heroDetail({state:'heating', since_seconds:150, shot:null}, 0.6)"
        )
        assert heating.startswith("0.6 of 0.8 bar")
        assert "Warming up" not in heating
        assert "about" in heating and "min" in heating and heating.endswith("to go")

    def test_warming_bar_and_eta_need_a_real_slope(self):
        assert call("logic.warmingProgress(0.4)") == 0.5
        assert call("logic.warmingProgress(2)") == 1
        assert call("logic.warmingEtaSeconds(0.2, 3)") is None
        assert call("logic.warmingEtaSeconds(0.16, 200)") is None
        eta = call("logic.warmingEtaSeconds(0.6, 150)")
        assert eta is not None and eta > 0

    def test_stale_tag_names_the_real_age(self):
        assert call("logic.staleTagText(1000, 13000)") == "Last seen 12 s ago"
        assert call("logic.staleTagText(0, 120000)") == "Last seen 2 min ago"
        assert call("logic.staleTagText(null, 13000)") == "Last seen"

    def test_a_flush_is_not_presented_as_a_shot(self):
        flushing = call(
            "logic.livePresentation("
            "{state:'brewing', label:'Pulling a shot', shot:{seconds:4, ml:24}}, 0.4)"
        )
        assert flushing == {
            "label": "Water running",
            "showShot": False,
            "showTarget": False,
        }
        shot = call(
            "logic.livePresentation("
            "{state:'brewing', label:'Pulling a shot', shot:{seconds:12, ml:18}}, 9)"
        )
        assert shot["label"] == "Pulling a shot"
        assert shot["showShot"] is True
        assert shot["showTarget"] is True

    def test_offline_hides_the_last_live_state(self):
        ready = call(
            "logic.shownState({state:'ready', label:'Ready to pull', shot:null},"
            " 0, false, true, 1000, 41000)"
        )
        assert ready["replace"] is True
        assert ready["showShot"] is False
        assert "Ready to pull" not in ready["label"]
        assert "Last seen 40 s ago" in ready["label"]
        brewing = call(
            "logic.shownState({state:'brewing', label:'Pulling a shot',"
            " shot:{seconds:20, ml:30}}, 9, false, true, 1000, 41000)"
        )
        assert brewing["showShot"] is False
        assert "Pulling a shot" not in brewing["label"]
        live = call(
            "logic.shownState({state:'ready', label:'Ready to pull', shot:null},"
            " 0, true, true, 1000, 41000)"
        )
        assert live["replace"] is False
        assert live["label"] == "Ready to pull"

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


class TestVerdicts:
    def test_three_verdicts_have_different_shapes_and_words(self):
        fast = call("logic.shotVerdict(22, 25, 30)")
        good = call("logic.shotVerdict(27, 25, 30)")
        edge = call("logic.shotVerdict(30, 25, 30)")
        long = call("logic.shotVerdict(33, 25, 30)")
        assert fast == {"key": "fast", "label": "short", "shape": "chevron-left"}
        assert good["label"] == "on target" and good["shape"] == "dot"
        assert edge["label"] == "on target"
        assert long == {"key": "long", "label": "long", "shape": "chevron-right"}
        assert len({fast["shape"], good["shape"], long["shape"]}) == 3
        assert len({fast["label"], good["label"], long["label"]}) == 3

    def test_nearest_tick_wins_even_when_marks_overlap(self):
        # 26 s, 27 s and 28 s on a 350 px track (scale 40 s) sit ~9 px apart.
        centers = [350 * seconds / 40 for seconds in (26, 27, 28)]
        assert call("logic.nearestTickIndex(%s, %s)" % (centers, centers[1] + 2)) == 1
        assert call("logic.nearestTickIndex(%s, %s)" % (centers, centers[0] - 3)) == 0
        # A point exactly between two ticks stays with the earlier one.
        midway = (centers[0] + centers[1]) / 2
        assert call("logic.nearestTickIndex(%s, %s)" % (centers, midway)) == 0
        assert call("logic.nearestTickIndex([], 10)") == -1


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
        assert "Is the Pi on? Same Wi-Fi?" in call("logic.offlineMessage(null, 0)")
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

    def test_verdict_is_written_and_ticks_are_not_buttons(self):
        assert 'class="pulls-legend"' in self.page
        assert "> short<" in self.page
        assert "> on target<" in self.page
        assert "> long<" in self.page
        assert "pointer-events: none" in self.page
        assert "nearestTickIndex" in self.page
        assert "verdict.label" in self.page
        assert "setAttribute('role', 'button')" not in self.page
        assert 'role="listbox"' in self.page


def _css_rule(page, selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", page)
    assert match, selector
    return match.group(1)


class TestHero:
    """The state card is the look C hero: chip, timer, verdict, window."""

    def setup_method(self):
        self.page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_chip_timer_and_window_match_the_hero(self):
        chip = _css_rule(self.page, ".state-chip")
        assert "font-size: 12.5px" in chip
        assert "font-weight: 700" in chip
        assert "var(--acc" in chip
        timer = _css_rule(self.page, ".timer b")
        assert "font-size: 62px" in timer
        assert "font-weight: 700" in timer
        assert "letter-spacing: -.05em" in timer
        unit = _css_rule(self.page, ".timer small")
        assert "font-size: 22px" in unit
        assert "font-weight: 600" in unit
        verdict = _css_rule(self.page, ".hint b")
        assert "font-size: 14px" in verdict
        assert "font-weight: 700" in verdict
        assert "var(--acc" in verdict
        track = _css_rule(self.page, ".shot-track")
        assert "height: 6px" in track
        window = _css_rule(self.page, ".shot-track u")
        assert "border: 1.5px solid var(--acc" in window
        assert "border-radius: 5px" in window
        assert 'id="shot-ml"' not in self.page
        assert 'id="shot-curve"' not in self.page
        assert "Pressure, this shot" not in self.page
        assert "This shot's pressure" in self.page
        assert 'id="shot-verdict"' in self.page
        assert ">0 s<" in self.page
        assert ">25–30 s<" in self.page
        assert ">40 s<" in self.page
        wide = self.page[self.page.find("@media (min-width: 1000px)") :]
        assert "font-size: 140px" in wide
        assert "font-size: 40px" in wide
        assert "height: 8px" in wide

    def test_aim_stays_and_the_verdict_is_its_own_line(self):
        for seconds in (12, 26, 34):
            detail = call(
                "logic.heroDetail({state:'brewing', since_seconds:1,"
                " shot:{seconds:%s, ml:30}}, null)" % seconds
            )
            assert detail == "Aiming for 25 to 30 s"
        assert call("logic.liveVerdict(12, 25, 30)") == "Short"
        assert call("logic.liveVerdict(26, 25, 30)") == "On target"
        assert call("logic.liveVerdict(34, 25, 30)") == "Long"
        pressure = call(
            "logic.heroDetail({state:'overpressure', since_seconds:4, shot:null}, 1.9)"
        )
        assert pressure == "for 4 s\nCheck the pressurestat"
        assert "Pressure high" not in pressure


class TestHeroCurve:
    """The hero curve: live tail, a reset on the next shot, last curve in Ready."""

    def setup_method(self):
        self.page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_markup_sits_under_the_axis_and_starts_hidden(self):
        axis = self.page.find('id="shot-window"')
        curve = self.page.find('id="hero-curve"')
        assert axis != -1 and curve > axis
        assert 'id="hero-curve" hidden' in self.page
        assert "This shot's pressure" in self.page
        assert ">bar<" in self.page
        label = _css_rule(self.page, "#hero-curve .lbl")
        assert "font-size: 12.5px" in label
        assert "font-weight: 600" in label
        chart = _css_rule(self.page, "#hero-curve .chart")
        assert "height: 62px" in chart
        line = _css_rule(self.page, "#hero-curve .curve-line")
        assert "stroke: var(--acc" in line
        band = _css_rule(self.page, "#hero-curve .band")
        assert "fill: var(--acc" in band
        wide = self.page[self.page.find("@media (min-width: 1000px)") :]
        assert "height: 220px" in wide

    def test_append_uses_the_after_cursor_and_skips_points_already_held(self):
        first = call(
            "logic.applyCurrentSamples({startedAt:null, samples:[]},"
            " {active:true, started_at:'shot-a',"
            " samples:[{t:0.25, head_bar:2},{t:0.5, head_bar:4}]})"
        )
        assert first["startedAt"] == "shot-a"
        assert [point["t"] for point in first["samples"]] == [0.25, 0.5]
        assert call("logic.curveRequest(%s)" % json.dumps(first)) == {"after": 0.5}
        # The server returns only t > after. The point at the cursor stays put.
        second = call(
            "logic.applyCurrentSamples(%s,"
            " {active:true, started_at:'shot-a',"
            " samples:[{t:0.5, head_bar:4},{t:0.75, head_bar:9}]})"
            % json.dumps(first)
        )
        assert [point["t"] for point in second["samples"]] == [0.25, 0.5, 0.75]
        assert [point["head_bar"] for point in second["samples"]] == [2, 4, 9]

    def test_a_new_started_at_replaces_the_curve(self):
        held = {
            "startedAt": "shot-a",
            "samples": [{"t": 26, "head_bar": 9}],
        }
        assert call(
            "logic.curveNeedsFull(%s, {active:true, started_at:'shot-b', samples:[]})"
            % json.dumps(held)
        )
        reset = call(
            "logic.applyCurrentSamples(%s,"
            " {active:true, started_at:'shot-b', samples:[{t:0.25, head_bar:1}]})"
            % json.dumps(held)
        )
        assert reset["startedAt"] == "shot-b"
        assert [point["t"] for point in reset["samples"]] == [0.25]
        assert (
            call(
                "logic.curveNeedsFull({startedAt:null, samples:[]},"
                " {active:true, started_at:'shot-b', samples:[]})"
            )
            is False
        )

    def test_ready_keeps_the_last_curve_and_an_empty_slot_stays_hidden(self):
        last = [{"t": 26, "head_bar": 9.0}]
        ready = call(
            "logic.heroCurveSlot({state:'ready', shotActive:false,"
            " liveSamples:[], lastSamples:%s})" % json.dumps(last)
        )
        assert ready["hidden"] is False
        assert ready["source"] == "last"
        assert ready["samples"] == last
        live = [{"t": 10, "head_bar": 8}]
        brewing = call(
            "logic.heroCurveSlot({state:'brewing', shotActive:true,"
            " liveSamples:%s, lastSamples:%s})"
            % (json.dumps(live), json.dumps(last))
        )
        assert brewing["hidden"] is False
        assert brewing["source"] == "live"
        assert brewing["samples"] == live
        for state in ("ready", "heating", "off"):
            empty = call(
                "logic.heroCurveSlot({state:'%s', shotActive:false,"
                " liveSamples:[], lastSamples:[]})" % state
            )
            assert empty["hidden"] is True
            assert empty["samples"] == []
        waiting = call(
            "logic.heroCurveSlot({state:'brewing', shotActive:true,"
            " liveSamples:[], lastSamples:[]})"
        )
        assert waiting["hidden"] is True

    def test_the_line_is_head_pressure_and_the_band_is_8_to_10(self):
        figure = call(
            "logic.heroCurveFigure(["
            " {t:0, head_bar:0, boiler_bar:1.2},"
            " {t:15, head_bar:9, boiler_bar:1.2},"
            " {t:26, head_bar:9, boiler_bar:1.2}])"
        )
        assert figure["yLabel"] == "9.0"
        assert figure["axis"] == ["0 s", "10", "20", "30 s"]
        assert figure["dot"]["x"] == 260
        assert figure["dot"]["y"] == 25
        assert figure["band"]["y"] == 16.67
        assert figure["band"]["height"] == 16.67
        assert "M0.00 100.00" in figure["line"]
        assert "L260.00 25.00" in figure["line"]
        # Boiler is not the drawn series: 1.2 bar would sit much lower.
        assert "L150.00 90.00" not in figure["line"]
        assert call("logic.heroCurveFigure([])") is None


class TestLastShotChip:
    """Look C drops the last-shot chip. Recent shots covers that glance."""

    def setup_method(self):
        self.page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_last_shot_chip_is_absent(self):
        assert 'id="last-pull"' not in self.page
        assert "last-pull" not in self.page
        assert "Last shot " not in self.page

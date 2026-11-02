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
        assert call("logic.pillText(false, null)") == "Connecting…"
        assert call("logic.pillText(false, null)") != "Live"

    def test_pill_is_live_only_while_connected(self):
        assert call("logic.pillText(true, 1000)") == "Live"

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
        assert call("logic.displayedShotText(false, '12 s', '13 s')") == "12 s"
        assert call("logic.displayedShotText(true, '12 s', '13 s')") == "13 s"
        assert call("logic.displayedShotText(false, null, '4 s')") == "4 s"

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
            "logic.heroDetail({state:'heating', since_seconds:30, shot:null}, 0.3)"
        )
        assert heating == "0.3 of 0.8 bar · about 2 min to go"

    def test_each_state_has_its_own_pill_class(self):
        assert call("logic.pillKind('Off')") == "off"
        assert call("logic.pillKind('Warming up')") == "heating"
        assert call("logic.pillKind('Ready to pull')") == "ready"
        assert call("logic.pillKind('Pulling a shot')") == "live"
        assert call("logic.pillKind('Steaming milk')") == "live"
        assert call("logic.pillKind('Water running')") == "live"
        assert call("logic.pillKind('No sensor reading')") == "unknown"
        assert (
            call("logic.pillKind('Pressure high. Check the pressurestat.')") == "alert"
        )
        assert call("logic.pillLabel('Pressure high. Check the pressurestat.')") == (
            "Pressure high"
        )
        assert call("logic.pillKind('connecting…')") == "quiet"

    def test_pressure_high_puts_the_check_on_its_own_line(self):
        detail = call(
            "logic.heroDetail({state:'overpressure', since_seconds:4, shot:null}, 1.9)"
        )
        assert call(
            "logic.heroDetail({state:'overpressure', since_seconds:4, shot:null}, 1.9)"
        ) == ("Check the pressurestat")
        assert call(
            "logic.heroDetail({state:'overpressure', since_seconds:59.9, shot:null}, 1.9)"
        ) == ("Check the pressurestat")
        assert (
            call(
                "logic.heroDetail({state:'overpressure', since_seconds:60, shot:null}, 1.9)"
            )
            == "for 1 min · Check the pressurestat"
        )
        assert (
            call(
                "logic.heroDetail({state:'overpressure', since_seconds:120, shot:null}, 1.9)"
            )
            == "for 2 min · Check the pressurestat"
        )
        assert "Pressure high" not in detail
        assert "pressurestat. for" not in detail

    def test_detail_never_repeats_the_pill(self):
        assert (
            call(
                "logic.heroDetail({state:'flushing', since_seconds:26, shot:null}, null)"
            )
            == "for 26 s"
        )
        assert (
            call("logic.heroDetail({state:'off', since_seconds:4, shot:null}, null)")
            == "for 4 s"
        )
        assert (
            call("logic.heroDetail({state:'steaming', since_seconds:26, shot:null}, 1)")
            == "for 26 s"
        )
        assert (
            call(
                "logic.heroDetail({state:'unknown', since_seconds:8, shot:null}, null)"
            )
            == "for 8 s"
        )
        aiming = call(
            "logic.heroDetail({state:'brewing', since_seconds:12, shot:{seconds:12}}, null)"
        )
        assert aiming == "Aiming for 25 to 30 s"
        for phrase in (
            "Water running",
            "Warming up",
            "Off for",
            "Ready for",
            "Steaming milk",
            "No sensor reading",
            "Pressure high for",
        ):
            assert phrase not in aiming

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
        assert ready["label"] == "Offline"
        assert ready["detail"] == "Readings below are from before it went offline."
        assert "Ready to pull" not in ready["label"]
        assert "Can't reach" not in ready["detail"]
        assert "Same Wi-Fi" not in ready["detail"]
        banner = call("logic.offlineBanner(false, 3, 1000, 41000)")
        assert "Can't reach the machine. Last seen 40 s ago" in banner["title"]
        assert "Same Wi-Fi" in banner["title"]
        brewing = call(
            "logic.shownState({state:'brewing', label:'Pulling a shot',"
            " shot:{seconds:20, ml:30}}, 9, false, true, 1000, 41000)"
        )
        assert brewing["showShot"] is False
        assert brewing["label"] == "Offline"
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

    def test_state_is_a_pill_and_glyphs_stay_distinct(self):
        page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        # Colour means live, not a band per state. The five glyphs still differ.
        assert "--state-off:" not in page
        assert "applyPill(card, presentation.label)" in page
        assert call("logic.pillKind('Pulling a shot')") == "live"
        assert call("logic.pillKind('Water running')") == "live"
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
        assert 'id="conn">Connecting…' in self.page
        assert 'id="conn">live' not in self.page
        assert 'class="dot off"' in self.page

    def test_markup_does_not_ship_a_last_seen_footer(self):
        # The offline banner already says Last seen. A footer would repeat it.
        assert 'id="footer"' not in self.page
        assert "dashboard up" not in self.page
        assert "let lastSeenAt = null" in self.page
        assert "let lastSeenAt = Date.now()" not in self.page

    def test_flow_and_tank_cards_start_hidden(self):
        assert 'id="flow-card" hidden' in self.page
        assert 'id="tank-card" hidden' in self.page

    def test_connecting_screen_has_skeletons_and_no_live_lamp(self):
        assert 'id="state-label" aria-live="polite"' in self.page
        assert 'id="state-label-text">Connecting…' in self.page
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

    def test_recent_tiles_open_a_shot_and_the_log(self):
        assert 'class="pulls-legend"' not in self.page
        assert 'id="pulls-track"' not in self.page
        assert 'id="recent-tiles"' in self.page
        assert 'id="all-shots"' in self.page
        assert 'id="history-card" hidden' in self.page
        assert "Yesterday" in self.page
        assert "toLocaleTimeString" in self.page
        assert "createElement('button')" in self.page
        assert "setAttribute('role', 'button')" not in self.page
        tile = _css_rule(self.page, ".recent-tile .ml")
        assert "font-size: 17px" in tile
        assert "font-weight: 700" in tile
        assert "font-variant-numeric: tabular-nums" in tile
        assert "font-variant-numeric: tabular-nums" in _css_rule(
            self.page, ".recent-tile .t"
        )
        tiles = _css_rule(self.page, ".recent-tiles")
        assert "repeat(4, minmax(0, 1fr))" in tiles
        when = _css_rule(self.page, ".recent-tile .t")
        assert "white-space: nowrap" not in when
        assert "min-width: 0" in when
        assert "white-space: nowrap" in _css_rule(self.page, ".recent-tile .t-time")
        assert "t-day" in self.page
        assert "shotWhenParts" in self.page
        assert "min-width: 0" in _css_rule(self.page, ".recent-tile")
        bar = _css_rule(self.page, ".recent-bar")
        assert "height: 4px" in bar
        assert call("logic.recentTile(26, 34, 25, 30)")["label"] == "On target"
        assert call("logic.recentTile(26, 34, 25, 30)")["bar"] == 34 / 45
        short = call("logic.recentTile(12, 22, 25, 30)")
        assert short["label"] == "Short"
        assert short["mark"] == "\u2212"
        assert call("logic.recentTile(40, 50, 25, 30)")["label"] == "Long"
        assert call("logic.recentTile(40, 90, 25, 30)")["bar"] == 1
        assert call("logic.allShotsLink(1, 4)")["hidden"] is True
        assert call("logic.allShotsLink(4, 4)")["hidden"] is True
        five = call("logic.allShotsLink(5, 4)")
        assert five["hidden"] is False
        assert five["text"] == "All 5 shots \u203A"
        tank = self.page.find('id="tank-card"')
        room = self.page.find('id="other-card"')
        recent = self.page.find('id="pulls-card"')
        assert tank < room < recent
        head = self.page[recent : self.page.find('id="recent-tiles"', recent)]
        assert 'id="all-shots"' in head
        assert "card-head .all-shots" in self.page


def _css_rule(page, selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", page)
    assert match, selector
    return match.group(1)


class TestTankLead:
    def test_zero_shots_says_the_tank_is_probably_empty(self):
        assert (
            call("logic.tankLead(0)")
            == "Tank's probably empty. Refill before the next shot."
        )
        assert call("logic.tankLead(1)") == "About 1 shot left"
        assert call("logic.tankLead(43)") == "About 43 shots left"
        assert call("logic.humidityLabel(45.2)") == "45% humidity"
        assert call("logic.humidityLabel(45.6)") == "46% humidity"
        assert call("logic.tankMeterHidden(0)") is True
        assert call("logic.tankMeterHidden(12)") is False


class TestRefillButton:
    def setup_method(self):
        self.page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_short_label_keeps_the_full_accessible_name(self):
        assert 'aria-label="I refilled the tank"' in self.page
        assert 'class="refill-long">I refilled the tank<' in self.page
        assert 'class="refill-short">I refilled it<' in self.page
        assert "@container refill" in self.page
        assert "tankMeterHidden" in self.page
        assert "Reconnect to save the refill" in self.page
        assert "Reconnect to mark the tank filled" not in self.page

    def test_normal_refill_is_short_text_and_empty_stays_a_pill(self):
        normal = call("logic.refillControl(false)")
        assert normal["pill"] is False
        assert normal["text"] == "I refilled it"
        assert normal["name"] == "I refilled the tank"
        empty = call("logic.refillControl(true)")
        assert empty["pill"] is True
        assert empty["long"] == "I refilled the tank"
        assert empty["short"] == "I refilled it"
        assert empty["name"] == "I refilled the tank"
        assert 'id="tank-used-row"' in self.page
        assert "refill-inline" in self.page
        assert "refill-pill" in self.page
        assert "min-width: 0" in _css_rule(self.page, ".pair-lead")
        assert "white-space: nowrap" not in _css_rule(self.page, ".pair-lead")
        narrow_lead = re.search(
            r"@media \(max-width: 339px\)\s*\{([^}]*)\}", self.page
        )
        assert narrow_lead is not None
        assert "font-size: 16px" in narrow_lead.group(1)
        assert "font-variant-numeric: tabular-nums" in _css_rule(self.page, ".pair-sub")
        assert "repeat(2, minmax(0, 1fr))" in _css_rule(self.page, ".pair")
        assert "white-space: normal" in _css_rule(self.page, ".pair-lead.empty")
        assert "white-space: nowrap" in _css_rule(
            self.page, "#tank-card .reset.refill-inline"
        )
        narrow = re.search(
            r"@media \(max-width: 399px\)\s*\{(?:[^{}]|\{[^{}]*\})*"
            r"#tank-card \.reset\.refill-inline(?:[^{}]|\{[^{}]*\})*\}",
            self.page,
        )
        assert narrow is not None
        assert "flex-wrap: wrap" in narrow.group(0)
        assert "text-align: left" in narrow.group(0)
        assert "pointer-events: none" in _css_rule(
            self.page, '#tank-card .reset.refill-inline[aria-disabled="true"]'
        )


class TestWaterCard:
    """The water card puts the peak in the header and shortens the chart."""

    def setup_method(self):
        self.page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_peak_header_sentence_case_and_short_chart(self):
        assert ">Now <" not in self.page
        assert ">All time <" not in self.page
        assert 'id="flow-rate"' not in self.page
        assert "3 s peak" in self.page
        assert "s peak" in self.page
        assert "−1:00" in self.page
        chart = _css_rule(self.page, "#flow-card .chart")
        assert "height: 26px" in chart
        assert "gap: 4px" in chart
        bar = _css_rule(self.page, "#flow-card .bar")
        assert "border-radius: 0" in bar
        assert "min-width: 8px" in bar
        assert "max-width: 10px" in bar
        assert len(call("logic.flowBars(Array(40).fill(1), 326)")) == 25
        wide = self.page[self.page.find("@media (min-width: 1000px)") :]
        assert "height: 170px" in wide


class TestMeters:
    """Head and boiler share one card of linear meters. The dials are gone."""

    def setup_method(self):
        self.page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_scales_and_the_head_band(self):
        head = call("logic.meterSpec('Head')")
        assert head["label"] == "Head pressure"
        assert head["max"] == 12
        assert head["band"] == [8, 10]
        boiler = call("logic.meterSpec('Boiler')")
        assert boiler == {"label": "Boiler pressure", "max": 2, "band": None}
        assert call("logic.meterFraction(9, 12)") == 0.75
        assert call("logic.meterFraction(1.2, 2)") == 0.6
        assert call("logic.meterFraction(15, 12)") == 1
        assert call("logic.meterFraction(-1, 12)") == 0
        assert call("logic.meterHot(9, [8, 10])") is True
        assert call("logic.meterHot(8, [8, 10])") is True
        assert call("logic.meterHot(10, [8, 10])") is True
        assert call("logic.meterHot(7.9, [8, 10])") is False
        assert call("logic.meterHot(10.1, [8, 10])") is False
        assert call("logic.meterHot(1.2, null)") is False

    def test_markup_is_one_card_of_linear_meters(self):
        meter = _css_rule(self.page, ".meter")
        assert "height: 6px" in meter
        number = _css_rule(self.page, ".meter-num")
        assert "font-size: 28px" in number
        assert "font-weight: 700" in number
        hot = _css_rule(self.page, ".meter i.hot")
        assert 'class="band"' not in self.page
        assert ".meter u.band" not in self.page
        assert "var(--acc" in hot
        assert "function renderRings" not in self.page
        assert 'class="ring"' not in self.page
        assert "renderMeters" in self.page
        assert "12 bar" in self.page
        assert "2 bar" in self.page
        wide = self.page[self.page.find("@media (min-width: 1000px)") :]
        assert "font-size: 38px" in wide
        assert "font-weight: 700" in _css_rule(self.page, ".meter-num b")


class TestHero:
    """The state card is the look C hero: chip, timer, verdict, window."""

    def setup_method(self):
        self.page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_chip_timer_and_window_match_the_hero(self):
        chip = _css_rule(self.page, ".state-chip")
        assert "border: none" in chip
        assert "background: transparent" in chip
        label = _css_rule(self.page, ".state-label")
        assert "font-size: 12.5px" in label
        assert "font-weight: 700" in label
        assert "border-radius: 99px" in label
        assert "letter-spacing: 0" in label
        assert "padding: 5px 12px" in label
        assert "gap: 7px" in label
        assert "var(--acc" in _css_rule(self.page, ".state.live .state-label")
        dot = _css_rule(self.page, ".state.live .state-label::before")
        assert 'content: ""' in dot
        assert "width: 6px" in dot
        assert "display: none" in _css_rule(self.page, ".state-chip .lamp")
        assert "chip.classList.toggle('offline', offline.replace)" in self.page
        assert "stateCard.classList.toggle('offline', offline.replace)" in self.page
        offline = _css_rule(self.page, ".state-chip.offline .state-label")
        assert "background: transparent" in offline
        assert "border: 1px dashed var(--mute" in offline
        assert "var(--mute" in offline
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
        assert pressure == "Check the pressurestat"
        assert "Pressure high" not in pressure

    def test_ready_detail_waits_until_a_minute(self):
        # since_seconds is the age of this Ready visit, not the last poll.
        young = "{state:'ready', since_seconds:%s, shot:null}"
        for seconds in (0, 0.4, 59, 59.9):
            assert call("logic.heroDetail(%s, 1.2)" % (young % seconds)) == ""
        assert call("logic.heroDetail(%s, 1.2)" % (young % 60)) == "for 1 min"
        assert call("logic.heroDetail(%s, 1.2)" % (young % 120)) == "for 2 min"


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
        assert 'id="hero-band"' not in self.page
        assert "#hero-curve .band" not in self.page
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
            " samples:[{t:0.5, head_bar:4},{t:0.75, head_bar:9}]})" % json.dumps(first)
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
        assert ready["label"] == "Last shot's pressure"
        assert ready["samples"] == last
        live = [{"t": 10, "head_bar": 8}]
        brewing = call(
            "logic.heroCurveSlot({state:'brewing', shotActive:true,"
            " liveSamples:%s, lastSamples:%s})" % (json.dumps(live), json.dumps(last))
        )
        assert brewing["hidden"] is False
        assert brewing["source"] == "live"
        assert brewing["label"] == "This shot's pressure"
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

    def test_the_line_is_head_pressure_on_a_40_second_scale(self):
        figure = call(
            "logic.heroCurveFigure(["
            " {t:0, head_bar:0, boiler_bar:1.2},"
            " {t:15, head_bar:9, boiler_bar:1.2},"
            " {t:26, head_bar:9, boiler_bar:1.2}])"
        )
        assert figure["yLabel"] == "9.0"
        assert figure["tMax"] == 40
        assert figure["axis"] == ["0 s", "10", "20", "30", "40 s"]
        assert figure["dot"]["x"] == 195
        assert figure["dot"]["y"] == 25
        assert "band" not in figure
        assert "M0.00 100.00" in figure["line"]
        assert "L195.00 25.00" in figure["line"]
        # Boiler is not the drawn series: 1.2 bar would sit much lower.
        assert "L150.00 90.00" not in figure["line"]
        assert call("logic.heroCurveFigure([])") is None

    def test_the_label_switches_with_the_shot(self):
        saved = [{"t": 26, "head_bar": 9}]
        running = call(
            "logic.heroCurveSlot({state:'brewing', shotActive:true,"
            " liveSamples:[{t:4, head_bar:6}], lastSamples:%s})" % json.dumps(saved)
        )
        assert running["label"] == "This shot's pressure"
        ready = call(
            "logic.heroCurveSlot({state:'ready', shotActive:false,"
            " liveSamples:[], lastSamples:%s})" % json.dumps(saved)
        )
        assert ready["label"] == "Last shot's pressure"
        nxt = call(
            "logic.heroCurveSlot({state:'brewing', shotActive:true,"
            " liveSamples:[{t:0.5, head_bar:1}], lastSamples:%s})" % json.dumps(saved)
        )
        assert nxt["label"] == "This shot's pressure"
        assert nxt["source"] == "live"

    def test_ready_curve_ends_while_water_was_still_flowing(self):
        # The head falls to 0 while the meter is still ticking. That tail
        # is not the pour. The frozen line stops at the last flow > 0
        # sample that still has pressure, and the dot sits on it.
        saved = [
            {"t": 0, "head_bar": 0.4, "flow_mls": 0.5},
            {"t": 6, "head_bar": 9, "flow_mls": 1.5},
            {"t": 26, "head_bar": 9, "flow_mls": 1.2},
            {"t": 26.13, "head_bar": 0, "flow_mls": 1.19},
            {"t": 26.43, "head_bar": 0, "flow_mls": 1.19},
            {"t": 26.63, "head_bar": 0, "flow_mls": 0},
        ]
        kept = call("logic.pourCurveSamples(%s)" % json.dumps(saved))
        assert kept[-1]["t"] == 26
        assert kept[-1]["head_bar"] == 9
        assert kept[-1]["flow_mls"] > 0
        assert all(point["head_bar"] > 0 or point["t"] == 0 for point in kept)
        assert all(
            point["head_bar"] != 0 or point["flow_mls"] <= 0 for point in kept[1:]
        )
        ready = call(
            "logic.heroCurveSlot({state:'ready', shotActive:false,"
            " liveSamples:[], lastSamples:%s})" % json.dumps(saved)
        )
        assert ready["label"] == "Last shot's pressure"
        assert ready["samples"][-1] == kept[-1]
        figure = call("logic.heroCurveFigure(%s)" % json.dumps(ready["samples"]))
        assert figure["yLabel"] == "9.0"
        assert figure["yLabelAt"] == 25
        assert figure["dot"]["x"] == 195
        assert figure["dot"]["y"] == 25
        assert "L195.00 100.00" not in figure["line"]


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


class TestTodayLine:
    """Today's tally, including the first shot while it is still running."""

    def test_idle_with_no_shots_names_the_empty_day(self):
        assert call("logic.todayLine({shots:0, ml:0}, false)") == "No shots yet today"

    def test_the_first_shot_of_the_day_leaves_the_line_empty(self):
        assert call("logic.todayLine({shots:0, ml:0}, true)") == ""

    def test_one_shot_and_two_shots(self):
        assert (
            call("logic.todayLine({shots:1, ml:39}, false)") == "1 shot · 39 ml today"
        )
        assert (
            call("logic.todayLine({shots:2, ml:68}, true)") == "2 shots · 68 ml today"
        )
        assert (
            call("logic.todayLine({shots:12, ml:1234}, false)")
            == "12 shots · 1,234 ml today"
        )

    def test_the_empty_line_is_removed_from_the_header(self):
        page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        assert "header .tagline:empty { display: none" in page


class TestWording:
    def setup_method(self):
        self.page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_units_are_spaced_and_pressure_says_bar(self):
        assert 'id="shot-seconds">0</b><small>s</small>' in self.page
        assert 'id="bb-time">0 s<' in self.page
        assert "machine.shot.seconds.toFixed(0)" in self.page
        assert ">BAR<" not in self.page
        assert "<small>bar</small>" in self.page

    def test_live_pill_and_last_shot_chip_are_sentence_case(self):
        assert "last shot ${" not in self.page
        assert call("logic.pillText(true, 1000)") == "Live"

    def test_only_the_timer_stays_centred_during_a_shot(self):
        state = _css_rule(self.page, ".state")
        assert "align-items: stretch" in state
        assert "text-align: left" in state
        assert "text-align: left" in _css_rule(self.page, ".state-detail")
        assert "justify-content: flex-start" in _css_rule(self.page, ".state-band")
        assert "text-align: left" in _css_rule(self.page, ".shot")
        assert "font-variant-numeric: tabular-nums" in _css_rule(self.page, ".timer b")
        assert "font-variant-numeric: tabular-nums" in _css_rule(
            self.page, ".meter-num"
        )
        assert "font-variant-numeric: tabular-nums" in _css_rule(self.page, ".figure b")
        # The connection pill shares the class live. Unscoped, it centers the card.
        assert re.search(r"(^|\n)\s*\.live\s*\{", self.page) is None
        assert "header .live {" in self.page

    def test_only_shot_history_shows_a_count(self):
        assert "history.length} shot" not in self.page
        assert "`${count} saved`" not in self.page
        assert ">All 0 shots ›<" in self.page
        assert call("logic.allShotsLink(6, 4)")["text"] == "All 6 shots ›"

    def test_live_pill_paints_its_dot(self):
        dot = _css_rule(self.page, ".state.live .state-label::before")
        assert 'content: ""' in dot
        assert "display: inline-flex" in dot
        assert "width: 6px" in dot
        assert "height: 6px" in dot
        assert "border-radius: 50%" in dot

    def test_warming_pill_has_an_espresso_ring(self):
        assert call("logic.pillKind('Warming up')") == "heating"
        heating = _css_rule(self.page, ".state.heating .state-label")
        assert "border-color: var(--ink)" in heating
        ring = _css_rule(self.page, ".state.heating .state-label::before")
        assert "border-radius: 50%" in ring
        assert "background: transparent" in ring

    def test_alert_mark_is_hidden_from_the_name_and_titles_are_sentence_case(self):
        assert 'class="alert-mark"' in self.page
        assert 'aria-hidden="true"' in self.page
        assert ">!</span>" in self.page
        assert "#FFB09A" in self.page
        assert "#F08A6B" not in self.page
        assert "text-transform: uppercase" not in _css_rule(self.page, ".title")
        assert 'content: "−"' in _css_rule(self.page, ".mark.fast::before")


class TestPhoneHeader:
    """On a phone the today line wraps the header to four lines."""

    def setup_method(self):
        self.page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_today_line_stays_on_the_header_row(self):
        assert 'id="tagline-card"' not in self.page
        assert "header .tagline:empty { display: none" in self.page
        title = _css_rule(self.page, "header h1")
        assert "flex: 0 0 auto" in title
        assert "text-overflow: ellipsis" not in title
        today = _css_rule(self.page, "header .tagline")
        assert "font-size: 12.5px" in today
        assert "font-weight: 500" in today
        assert "font-variant-numeric: tabular-nums" in today
        assert "min-width: 0" in today
        assert "text-overflow: ellipsis" in today
        assert "var(--mute" in today
        gap = _css_rule(self.page, "header .conn + .tagline")
        assert "margin-left: 6px" in gap
        narrow = re.search(r"@media \(max-width: 374px\)\s*\{([^}]*)\}", self.page)
        assert narrow is not None
        assert "header .tagline" in narrow.group(1)
        assert "display: none" in narrow.group(1)
        assert "todayLine(" in self.page

    def test_title_stays_whole_at_768_and_1280_with_connecting(self):
        # A labelled wake control plus "Connecting…" is about 438 px. The
        # header column is about 424, so at 768 the title was cut to
        # "Smart Espre…" and at 1280 the title row wrapped. The icon-only
        # rules live in the base sheet, so they apply at those widths and
        # not only under 480 px. The words stay for the accessible name.
        assert 'aria-label="Keep screen on"' in self.page
        assert ">Keep screen on<" in self.page
        assert 'id="conn">Connecting…' in self.page
        # Top-level rules (two-space indent), not a max-width media query.
        # A 480 px query would leave 768 and 1280 on the labelled control.
        assert re.search(r"^  #wake-text \{", self.page, re.M)
        assert re.search(r"^  #wake \{", self.page, re.M)
        hidden = _css_rule(self.page, "#wake-text")
        assert "position: absolute" in hidden
        assert "width: 1px" in hidden
        wake = _css_rule(self.page, "#wake")
        assert "width: 44px" in wake
        face = _css_rule(self.page, "#wake .pill-face")
        assert "width: 36px" in face
        assert "border-radius: 50%" in face
        assert "@media (max-width: 480px)" not in self.page

    def test_narrow_phones_do_not_scroll_sideways(self):
        # The title stays whole. A long today line ellipsizes instead.
        title = _css_rule(self.page, "header h1")
        assert "flex: 0 0 auto" in title
        assert "white-space: nowrap" in title
        today = _css_rule(self.page, "header .tagline")
        assert "min-width: 0" in today
        assert "overflow: hidden" in today
        assert "text-overflow: ellipsis" in today
        assert 'class="header-tools"' in self.page
        face = _css_rule(self.page, ".header-tools .pill-face")
        assert "height: 36px" in face
        tools = _css_rule(self.page, ".header-tools > .live")
        assert "min-height: 44px" in tools
        assert "height: 44px" in tools

    def test_wake_icon_toggles_with_aria_pressed(self):
        assert 'aria-label="Keep screen on"' in self.page
        assert 'title="Keep screen on"' in self.page
        assert 'aria-pressed="false"' in self.page
        assert 'class="wake-icon"' in self.page
        assert 'stroke="currentColor"' in self.page
        assert 'stroke-width="2"' in self.page
        icon = self.page.split('class="wake-icon"', 1)[1].split("</svg>", 1)[0]
        assert (
            'aria-hidden="true"'
            in self.page[self.page.index("<svg") : self.page.index("</svg>")]
        )
        assert "http" not in icon
        icon_box = _css_rule(self.page, ".wake-icon")
        assert "width: 20px" in icon_box
        assert "height: 20px" in icon_box
        assert "setAttribute('aria-pressed', String(on))" in self.page
        assert "classList.toggle('on', on)" in self.page
        # Tooltip follows the toggle. The accessible name does not.
        assert (
            "setAttribute('title', on ? 'Screen stays on' : 'Keep screen on')"
            in self.page
        )
        ui = self.page.split("const setUI = (on) => {", 1)[1].split("};", 1)[0]
        assert "aria-label" not in ui
        off_face = _css_rule(self.page, "#wake .pill-face")
        assert "border-color: var(--ink-faint)" in off_face
        pressed = _css_rule(self.page, "button#wake.on .pill-face")
        assert "background: var(--ink)" in pressed
        assert "color: var(--card)" in pressed
        assert "border-color: var(--ink)" in pressed


class TestDesktopGrid:
    """From 1000 px the cards form three columns that can grow to 1440."""

    def setup_method(self):
        self.page = (
            Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_three_columns_from_1000(self):
        at = self.page.find("grid-template-columns: repeat(3, minmax(280px, 1fr))")
        assert at > 0
        block = self.page[at - 250 : at + 700]
        assert "@media (min-width: 1000px)" in block
        assert "max-width: 1440px" in block
        assert "gap: 8px" in block
        assert "max-width: 1100px" not in self.page
        assert "min-width: 1024px" not in self.page
        assert (
            "#state { grid-column: 1; grid-row: 1 / span 3; align-self: stretch; }"
            in block
        )
        assert "#rings { grid-column: 2; grid-row: 1; }" in block
        assert "#flow-card { grid-column: 2; grid-row: 2; }" in block
        assert "#tank-room { display: contents; }" in block
        assert "#tank-card { grid-column: 3; grid-row: 1; }" in block
        assert "#other-card { grid-column: 3; grid-row: 2; }" in block
        assert "#pulls-card { grid-column: 3; grid-row: 3; }" in block
        assert "#history-card { grid-column" not in block

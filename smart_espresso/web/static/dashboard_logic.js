// Connection copy for the dashboard. The page paints these strings, and
// the tests call the same functions, so a "live" claim cannot sneak back
// in without a failing test. No dependencies: the file is a classic
// script in the browser and a CommonJS module under node.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.DashboardLogic = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  // The pill may say "live" only after a status payload has been applied.
  // Before that, "offline" would pretend a reading once existed.
  function pillText(connected, lastSeenAt) {
    if (connected) return "live";
    if (lastSeenAt == null) return "connecting…";
    return "offline";
  }

  // Null while the page has never applied a payload. The footer must not
  // invent a "last seen" age from the moment the document loaded.
  function footerText(connected, lastSeenAt, now) {
    if (connected) return null;
    if (lastSeenAt == null) return "never connected";
    return staleTagText(lastSeenAt, now);
  }

  // Flow and tank cards carry numbers. They stay hidden until a payload
  // says that sensor exists, so the shell's placeholder zeros never read
  // as a measurement.
  function sensorCardVisible(hasPayload, present) {
    return !!hasPayload && !!present;
  }

  // A failed sensor publishes null, not the previous number. The page
  // must show a gap rather than throwing on toFixed.
  function formatReading(value, digits) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    return value.toFixed(digits);
  }

  function readingFaulted(sensor, field) {
    if (!sensor) return true;
    if (sensor.health && sensor.health.ok === false) return true;
    return sensor[field] == null;
  }

  const POLL_FAST = 1000;
  const POLL_SLOW = 10000;
  const OFFLINE_AFTER_MISSES = 3;

  // The banner is a consequence of giving up, not of the backoff counter.
  // Retry zeroes the backoff without clearing this, so the banner stays up
  // while the immediate poll is in flight.
  function agePhrase(seconds) {
    const whole = Math.max(0, Math.round(seconds));
    if (whole < 60) return whole + " s ago";
    const minutes = Math.round(whole / 60);
    if (minutes < 60) return minutes + " min ago";
    const hours = Math.round(minutes / 60);
    return hours + " h ago";
  }

  function agePhraseFrom(lastSeenAt, now) {
    if (lastSeenAt == null || typeof now !== "number") return null;
    return agePhrase((now - lastSeenAt) / 1000);
  }

  function offlineMessage(lastSeenAt, now) {
    if (lastSeenAt == null) {
      return "Can't reach the machine yet. Is the Pi on? Same Wi-Fi?";
    }
    return (
      "Can't reach the machine. Last seen " +
      agePhraseFrom(lastSeenAt, now) +
      ". Is the Pi on? Same Wi-Fi?"
    );
  }

  function offlineBanner(connected, missed, lastSeenAt, now) {
    if (connected || missed < OFFLINE_AFTER_MISSES) {
      return { visible: false, title: "", hint: "" };
    }
    return { visible: true, title: offlineMessage(lastSeenAt, now), hint: "" };
  }

  function nextPollDelay(backoff) {
    if (backoff <= 0) return POLL_FAST;
    return Math.min(POLL_FAST * 2 ** (backoff - 1), POLL_SLOW);
  }

  function backoffAfterRetry() {
    return 0;
  }

  function tankPostAllowed(connected) {
    return !!connected;
  }

  // status is an HTTP code or null; error is the server's message or null.
  function resetFailureToast(status, error) {
    if (status || error) return "Couldn't save the refill. Try again in a moment.";
    return "Couldn't save the refill. Check the Wi-Fi and try again.";
  }

  // While offline the timer shows the last reading, even if a stale
  // payload arrives with a higher number. Online, the incoming text wins.
  function displayedShotText(online, held, incoming) {
    if (!online && held != null) return held;
    return incoming;
  }

  function showSkeletons(hasPayload) {
    return !hasPayload;
  }

  const READY_MIN_BAR = 0.8;
  const BOILER_FLOOR_BAR = 0.15;

  function boilerReading(pressure) {
    if (!Array.isArray(pressure)) return null;
    const sensor = pressure.find((item) => /boiler|steam/i.test(String(item.name || "")));
    if (!sensor || typeof sensor.bar !== "number") return null;
    return sensor.bar;
  }

  function warmingProgress(bar, readyMin) {
    const goal = readyMin || READY_MIN_BAR;
    if (typeof bar !== "number" || !(goal > 0)) return null;
    return Math.max(0, Math.min(1, bar / goal));
  }

  // Estimate only after the boiler has actually climbed. A cold start with
  // no slope yet returns null so the page shows the bar without a fake ETA.
  function warmingEtaSeconds(bar, sinceSeconds, readyMin, floor) {
    const goal = readyMin || READY_MIN_BAR;
    const base = floor == null ? BOILER_FLOOR_BAR : floor;
    if (typeof bar !== "number" || bar >= goal) return 0;
    if (typeof sinceSeconds !== "number" || sinceSeconds < 8) return null;
    const climbed = bar - base;
    if (climbed <= 0.02) return null;
    const rate = climbed / sinceSeconds;
    if (rate <= 0) return null;
    return (goal - bar) / rate;
  }

  function formatEta(seconds) {
    if (seconds == null) return null;
    if (seconds < 45) return "almost ready";
    const minutes = Math.round(seconds / 60);
    if (minutes <= 1) return "about 1 min";
    return "about " + minutes + " min";
  }

  function formatSpokenDuration(seconds) {
    const whole = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor(whole / 60) % 60;
    if (hours && minutes) return hours + " h " + minutes + " min";
    if (hours) return hours + " h";
    if (minutes) return minutes + " min";
    return whole + " s";
  }

  // One secondary line. Shot copy wins while a pull is running; heating
  // names the boiler against the ready threshold; ready names the time in
  // state. A later temperature or ready-alert can append to this string.
  const STATE_NAMES = {
    off: "Off",
    heating: "Warming up",
    ready: "Ready to pull",
    brewing: "Pulling a shot",
    steaming: "Steaming milk",
    overpressure: "Pressure high. Check the pressurestat.",
    unknown: "No sensor reading",
    flushing: "Water running",
  };

  function durationLine(state, sinceSeconds) {
    const dur = formatSpokenDuration(sinceSeconds);
    if (state === "ready") return "Ready for " + dur;
    const name = STATE_NAMES[state] || "Off";
    return name + " for " + dur;
  }

  function heroDetail(machine, boilerBar) {
    if (machine.shot) {
      const seconds = machine.shot.seconds;
      if (seconds < 25) return "Aiming for 25 to 30 s";
      if (seconds <= 30) return "On target";
      return "Running long";
    }
    if (machine.state === "heating" && typeof boilerBar === "number") {
      const eta = formatEta(warmingEtaSeconds(boilerBar, machine.since_seconds));
      const base = "Warming up · " + boilerBar.toFixed(1) + " of 0.8 bar";
      return eta ? base + " · " + eta : base;
    }
    return durationLine(machine.state, machine.since_seconds);
  }

  function glanceOn(wakeHeld, pinned) {
    return !!(wakeHeld || pinned);
  }

  // Mirrors the buzz: a window the page loaded already inside is consumed
  // (no cue), and the cue fires once on the later crossing. Flash is the
  // same edge with reduced motion taken off.
  function noteShotWindow(state, inWindow, reducedMotion) {
    let shotSeen = !!state.shotSeen;
    let buzzedInWindow = !!state.buzzedInWindow;
    let buzz = false;
    let flash = false;
    if (!shotSeen && inWindow) buzzedInWindow = true;
    if (shotSeen && inWindow && !buzzedInWindow) {
      buzzedInWindow = true;
      buzz = true;
      flash = !reducedMotion;
    }
    return {
      shotSeen: true,
      buzzedInWindow: buzzedInWindow,
      buzz: buzz,
      flash: flash,
    };
  }

  function clearShotWindow() {
    return { shotSeen: false, buzzedInWindow: false };
  }

  // The over-band arc is a warning mark. At 0 bar it is the loudest thing
  // on an idle dial, so it stays hidden until the reading nears the band.
  function showOverBand(bar, bandFrom) {
    if (typeof bar !== "number" || typeof bandFrom !== "number") return false;
    return bar >= bandFrom * 0.9;
  }

  // fast / on target / long. The shape is what remains when colour is
  // removed: an open left chevron, a filled dot, a filled right chevron.
  function shotVerdict(seconds, windowLow, windowHigh) {
    const low = typeof windowLow === "number" ? windowLow : 25;
    const high = typeof windowHigh === "number" ? windowHigh : 30;
    if (typeof seconds !== "number" || Number.isNaN(seconds)) {
      return { key: "good", label: "on target", shape: "dot" };
    }
    if (seconds < low) return { key: "fast", label: "short", shape: "chevron-left" };
    if (seconds <= high) return { key: "good", label: "on target", shape: "dot" };
    return { key: "long", label: "long", shape: "chevron-right" };
  }

  // Centers and the tap are in the same coordinate space. Equal distances
  // keep the earlier tick, so a point dead-centre never jumps to the right.
  // The stale tag names the real age of the last applied reading.
  function staleTagText(lastSeenAt, now) {
    const phrase = agePhraseFrom(lastSeenAt, now);
    if (!phrase) return "Last seen";
    return "Last seen " + phrase;
  }

  // While the machine can't be reached, the last live label and timer
  // stay off the card. Connecting… is only for a page that has no reading.
  function shownState(machine, headBar, connected, hasPayload, lastSeenAt, now) {
    if (!connected && hasPayload) {
      return {
        replace: true,
        label: offlineMessage(lastSeenAt, now),
        showShot: false,
        showTarget: false,
      };
    }
    const live = livePresentation(machine, headBar);
    return {
      replace: false,
      label: live.label,
      showShot: live.showShot,
      showTarget: live.showTarget,
    };
  }

  // A flush is water moving without brew-band head pressure. It must not
  // wear the shot label, the 25–30 s target, or the ratio.
  const BREW_HEAD_BAR = 2;

  function isFlush(machine, headBar) {
    if (!machine || machine.state !== "brewing") return false;
    if (typeof headBar !== "number" || !isFinite(headBar)) return false;
    return headBar < BREW_HEAD_BAR;
  }

  function livePresentation(machine, headBar) {
    if (isFlush(machine, headBar)) {
      return { label: "Water running", showShot: false, showTarget: false };
    }
    const shot = !!(machine && machine.shot);
    return {
      label: machine && machine.label ? machine.label : "",
      showShot: shot,
      showTarget: shot,
    };
  }

  const TABS = { now: true, shots: true, stats: true, care: true };

  function parseRoute(hash) {
    const parts = String(hash || "")
      .replace(/^#/, "")
      .split("/")
      .filter(Boolean);
    const head = parts[0];
    if (!TABS[head]) return { tab: "now", shotId: null };
    const shotId = head === "shots" && parts[1] ? String(parts[1]) : null;
    return { tab: head, shotId: shotId };
  }

  function recentShotSummary(history, limit) {
    const rows = Array.isArray(history) ? history : [];
    const count = typeof limit === "number" ? limit : 5;
    if (rows.length <= count) return rows.slice();
    return rows.slice(rows.length - count);
  }

  function careBadgeCount(tankLow, maintenanceDue) {
    return (tankLow ? 1 : 0) + (maintenanceDue ? 1 : 0);
  }

  function nearestTickIndex(centers, x) {
    if (!centers || !centers.length || typeof x !== "number") return -1;
    let best = 0;
    let bestDist = Infinity;
    for (let i = 0; i < centers.length; i++) {
      const dist = Math.abs(centers[i] - x);
      if (dist < bestDist) {
        bestDist = dist;
        best = i;
      }
    }
    return best;
  }

  return {
    pillText: pillText,
    footerText: footerText,
    sensorCardVisible: sensorCardVisible,
    formatReading: formatReading,
    readingFaulted: readingFaulted,
    offlineBanner: offlineBanner,
    nextPollDelay: nextPollDelay,
    backoffAfterRetry: backoffAfterRetry,
    tankPostAllowed: tankPostAllowed,
    resetFailureToast: resetFailureToast,
    displayedShotText: displayedShotText,
    showSkeletons: showSkeletons,
    OFFLINE_AFTER_MISSES: OFFLINE_AFTER_MISSES,
    boilerReading: boilerReading,
    warmingProgress: warmingProgress,
    warmingEtaSeconds: warmingEtaSeconds,
    formatEta: formatEta,
    heroDetail: heroDetail,
    glanceOn: glanceOn,
    noteShotWindow: noteShotWindow,
    clearShotWindow: clearShotWindow,
    showOverBand: showOverBand,
    shotVerdict: shotVerdict,
    staleTagText: staleTagText,
    offlineMessage: offlineMessage,
    isFlush: isFlush,
    livePresentation: livePresentation,
    shownState: shownState,
    nearestTickIndex: nearestTickIndex,
    parseRoute: parseRoute,
    recentShotSummary: recentShotSummary,
    careBadgeCount: careBadgeCount,
    READY_MIN_BAR: READY_MIN_BAR,
  };
});

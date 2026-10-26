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
    const ago = Math.max(0, Math.round((now - lastSeenAt) / 1000));
    return `last seen ${ago} s ago`;
  }

  // Flow and tank cards carry numbers. They stay hidden until a payload
  // says that sensor exists, so the shell's placeholder zeros never read
  // as a measurement.
  function sensorCardVisible(hasPayload, present) {
    return !!hasPayload && !!present;
  }

  const POLL_FAST = 1000;
  const POLL_SLOW = 10000;
  const OFFLINE_AFTER_MISSES = 3;

  // The banner is a consequence of giving up, not of the backoff counter.
  // Retry zeroes the backoff without clearing this, so the banner stays up
  // while the immediate poll is in flight.
  function offlineBanner(connected, missed, lastSeenAt, now) {
    if (connected || missed < OFFLINE_AFTER_MISSES) {
      return { visible: false, title: "", hint: "" };
    }
    let when = "never connected";
    if (lastSeenAt != null) {
      const ago = Math.max(0, Math.round((now - lastSeenAt) / 1000));
      when = `last reading ${ago} s ago`;
    }
    return {
      visible: true,
      title: `Can't reach the machine · ${when}`,
      hint: "Is the Pi on? Same Wi-Fi?",
    };
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
    if (error) return "Couldn't reset the tank — " + error;
    if (status) return "Couldn't reset the tank — the machine answered " + status;
    return "Couldn't reset the tank — the network dropped";
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
    if (minutes <= 1) return "~1 min";
    return "~" + minutes + " min";
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
  function heroDetail(machine, boilerBar) {
    if (machine.shot) {
      const seconds = machine.shot.seconds;
      if (seconds < 25) return "aiming for 25–30s";
      if (seconds <= 30) return "in the window";
      return "running long";
    }
    if (machine.state === "heating" && typeof boilerBar === "number") {
      const eta = formatEta(warmingEtaSeconds(boilerBar, machine.since_seconds));
      const base = boilerBar.toFixed(1) + " / 0.8 bar";
      return eta ? base + " · " + eta : base;
    }
    const dur = formatSpokenDuration(machine.since_seconds);
    if (machine.state === "ready") return "Ready · " + dur;
    return dur + " in this state";
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
    if (seconds < low) return { key: "fast", label: "fast", shape: "chevron-left" };
    if (seconds <= high) return { key: "good", label: "on target", shape: "dot" };
    return { key: "long", label: "long", shape: "chevron-right" };
  }

  // Centers and the tap are in the same coordinate space. Equal distances
  // keep the earlier tick, so a point dead-centre never jumps to the right.
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
    nearestTickIndex: nearestTickIndex,
    READY_MIN_BAR: READY_MIN_BAR,
  };
});

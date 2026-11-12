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
    if (connected) return "Live";
    if (lastSeenAt == null) return "Connecting…";
    return "offline";
  }

  function todayVolume(ml) {
    const n = Number(ml);
    if (!Number.isFinite(n)) return "0 ml";
    if (n >= 10000) return (n / 1000).toFixed(1) + " L";
    return Math.round(n).toLocaleString("en-US") + " ml";
  }

  // Saved shots only. The shot still running is not in the tally, so the
  // first one of the day leaves the line empty instead of "No shots yet".
  function todayLine(today, shotActive) {
    if (!today) return null;
    const shots = Number(today.shots) || 0;
    if (shots <= 0) return shotActive ? "" : "No shots yet today";
    const noun = shots === 1 ? "shot" : "shots";
    return shots + " " + noun + " · " + todayVolume(today.ml) + " today";
  }

  // Null while the page has never applied a payload. The banner must not
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

  // The pill already names the state. This line only says how long.
  function durationLine(state, sinceSeconds) {
    return "for " + formatSpokenDuration(sinceSeconds);
  }

  function pillLabel(label) {
    if (!label) return "";
    if (label.indexOf("Pressure high") === 0) return "Pressure high";
    return label;
  }

  function pillKind(label) {
    const text = pillLabel(label);
    if (text === "Pulling a shot" || text === "Steaming milk" || text === "Water running") {
      return "live";
    }
    if (text === "Pressure high") return "alert";
    if (text === "Ready to pull") return "ready";
    if (text === "No sensor reading") return "unknown";
    if (text === "Warming up") return "heating";
    if (text === "Off") return "off";
    return "quiet";
  }

  // The chip already names the state, so this line does not repeat it.
  // During a shot the aim stays put; the live verdict is a separate line.
  function heroDetail(machine, boilerBar) {
    if (machine.shot) return "Aiming for 25 to 30 s";
    if (machine.state === "heating" && typeof boilerBar === "number") {
      const eta = formatEta(warmingEtaSeconds(boilerBar, machine.since_seconds));
      const base = boilerBar.toFixed(1) + " of 0.8 bar";
      return eta ? base + " · " + eta + " to go" : base;
    }
    if (machine.state === "overpressure") {
      const check = "Check the pressurestat";
      if (!(Number(machine.since_seconds) >= 60)) return check;
      return "for " + formatSpokenDuration(machine.since_seconds) + " · " + check;
    }
    // since_seconds is how long the published state has been held, counted
    // from the transition into it. A duration before a minute is just
    // "for 0 s", so every state keeps that line hidden until it is real.
    if (!(Number(machine.since_seconds) >= 60)) return "";
    return durationLine(machine.state, machine.since_seconds);
  }

  // Sentence case. short / on target / long from the same window.
  function liveVerdict(seconds, windowLow, windowHigh) {
    const verdict = shotVerdict(seconds, windowLow, windowHigh);
    if (verdict.key === "fast") return "Short";
    if (verdict.key === "long") return "Long";
    return "On target";
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

  // Fixed scales from look C. Head marks 8–10 bar; boiler has no band.
  function meterSpec(name) {
    const key = String(name || "").toLowerCase();
    if (key.indexOf("boiler") >= 0 || key.indexOf("steam") >= 0) {
      return { label: "Boiler pressure", max: 2, band: null };
    }
    if (key.indexOf("head") >= 0) {
      return { label: "Head pressure", max: 12, band: [8, 10] };
    }
    return { label: name || "Pressure", max: 12, band: null };
  }

  function meterFraction(bar, max) {
    if (typeof bar !== "number" || !isFinite(bar) || !(max > 0)) return 0;
    return Math.max(0, Math.min(1, bar / max));
  }

  function meterHot(bar, band) {
    if (!band || typeof bar !== "number" || !isFinite(bar)) return false;
    return bar >= band[0] && bar <= band[1];
  }

  // The over-band arc is a warning mark. At 0 bar it is the loudest thing
  // on an idle dial, so it stays hidden until the reading nears the band.
  function showOverBand(bar, bandFrom) {
    if (typeof bar !== "number" || typeof bandFrom !== "number") return false;
    return bar >= bandFrom * 0.9;
  }

  // fast / on target / long. The shape is what remains when colour is
  // removed: an open left chevron, a filled dot, a filled right chevron.
  function humidityLabel(percent) {
    if (typeof percent !== "number" || !isFinite(percent)) return null;
    return Math.round(percent) + "% humidity";
  }

  // Normal: a short text button on the used row. Empty: the full-width pill,
  // which still shortens its own label when the card is narrow.
  function refillControl(empty) {
    const name = "I refilled the tank";
    if (empty) {
      return {
        pill: true,
        text: "",
        long: "I refilled the tank",
        short: "I refilled it",
        name: name,
      };
    }
    return { pill: false, text: "I refilled it", long: "", short: "", name: name };
  }

  function tankMeterHidden(shotsLeft) {
    return !(Number(shotsLeft) > 0);
  }

  function tankLead(shotsLeft) {
    const count = Number(shotsLeft) || 0;
    if (count <= 0) {
      return "Tank's probably empty. Refill before the next shot.";
    }
    if (count === 1) return "About 1 shot left";
    return "About " + count + " shots left";
  }

  // Four recent tiles. The bar is the yield against a 45 ml double.
  // Short carries a minus so the word is not the only signal.
  // Desktop recent rows keep the day and the time on one line. Today is
  // the locale time only. Yesterday is spelled out. Older shots are the
  // short locale date, a comma, then the time. No fixed 12h or 24h.
  function shotWhenLine(iso, now) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso || "");
    const time = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const today = now ? new Date(now) : new Date();
    if (date.toDateString() === today.toDateString()) return time;
    const yesterday = new Date(today);
    yesterday.setDate(today.getDate() - 1);
    if (date.toDateString() === yesterday.toDateString()) return "Yesterday " + time;
    const day = date.toLocaleDateString([], { month: "short", day: "numeric" });
    return day + ", " + time;
  }

  function recentTile(seconds, ml, windowLow, windowHigh) {
    const verdict = shotVerdict(seconds, windowLow, windowHigh);
    let label = "On target";
    if (verdict.key === "fast") label = "Short";
    else if (verdict.key === "long") label = "Long";
    const volume = typeof ml === "number" && isFinite(ml) ? ml : 0;
    return {
      key: verdict.key,
      label: label,
      bar: Math.max(0, Math.min(1, volume / 45)),
      mark: verdict.key === "fast" ? "\u2212" : "",
    };
  }

  // The link is only for the shots that do not fit in the four tiles.
  // How many leading rows stay. A row stays only when every content edge
  // (figure, time, bar, verdict) is at or above the card's content limit.
  // Later rows are dropped whole so a clipped timestamp is not left showing.
  function recentRowsThatFit(rows, limit) {
    const list = Array.isArray(rows) ? rows : [];
    const edge = Number(limit);
    if (!isFinite(edge)) return 0;
    let count = list.length;
    while (count > 0) {
      const bottoms = list[count - 1] && list[count - 1].bottoms;
      const parts = Array.isArray(bottoms) ? bottoms : [];
      const past = parts.some(bottom => !(Number(bottom) <= edge));
      if (!past) break;
      count -= 1;
    }
    return count;
  }

  function allShotsLink(count, shown) {
    const total = Number(count) || 0;
    const tiles = typeof shown === "number" ? shown : 4;
    const noun = total === 1 ? "shot" : "shots";
    return {
      hidden: total <= tiles,
      text: "All " + total + " " + noun + " \u203A",
    };
  }

  // About 9 px bars with 4 px gaps. Extra samples are grouped so the
  // row stays that wide instead of turning into hairlines.
  function flowBars(values, width) {
    const series = values || [];
    const n = series.length;
    if (!n) return [];
    const gap = 4;
    const bar = 9;
    const room = typeof width === "number" && width > 0 ? width : bar;
    const fit = Math.max(1, Math.floor((room + gap) / (bar + gap)));
    const count = Math.min(n, fit);
    const out = [];
    for (let i = 0; i < count; i++) {
      const start = Math.floor((i * n) / count);
      const end = Math.floor(((i + 1) * n) / count);
      let sum = 0;
      for (let j = start; j < end; j++) sum += Number(series[j]) || 0;
      out.push(sum);
    }
    return out;
  }

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
        label: "Offline",
        detail: "Readings below are from before it went offline.",
        showShot: false,
        showTarget: false,
      };
    }
    const live = livePresentation(machine, headBar);
    return {
      replace: false,
      label: live.label,
      detail: "",
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

  // Same 0–40 s scale as the shot bar, so the curve lines up under it.
  const HERO_CURVE_WIDTH = 300;
  const HERO_CURVE_HEIGHT = 100;
  const HERO_CURVE_Y_MAX = 12;
  const HERO_CURVE_T_MAX = 40;

  function curveAfter(samples) {
    if (!samples || !samples.length) return null;
    const t = Number(samples[samples.length - 1].t);
    return Number.isFinite(t) ? t : null;
  }

  // The next /api/shots/current poll. `after` is omitted until this shot
  // has a point, then it is that point's t so the server sends the tail.
  function curveRequest(state) {
    const samples = state && Array.isArray(state.samples) ? state.samples : [];
    const startedAt = state && state.startedAt ? state.startedAt : null;
    if (!startedAt || !samples.length) return { after: null };
    return { after: curveAfter(samples) };
  }

  // A cursor from the previous shot would drop the start of the new one.
  function curveNeedsFull(state, payload) {
    if (!payload || payload.active !== true) return false;
    const startedAt = state && state.startedAt;
    if (!startedAt) return false;
    return payload.started_at !== startedAt;
  }

  function applyCurrentSamples(state, payload) {
    const current = {
      startedAt: state && state.startedAt ? state.startedAt : null,
      samples: state && Array.isArray(state.samples) ? state.samples.slice() : [],
    };
    if (!payload || payload.active !== true) return current;
    const incoming = Array.isArray(payload.samples) ? payload.samples : [];
    if (current.startedAt !== payload.started_at) {
      return { startedAt: payload.started_at || null, samples: incoming.slice() };
    }
    const cursor = curveAfter(current.samples);
    const extra =
      cursor == null
        ? incoming.slice()
        : incoming.filter((point) => Number(point.t) > cursor);
    return {
      startedAt: current.startedAt,
      samples: current.samples.concat(extra),
    };
  }

  // The pour ends at the last sample that still has flow and pressure.
  // The head often falls to 0 while the meter is still ticking; that tail
  // is the pump stopping, not part of the frozen curve.
  function pourCurveSamples(samples) {
    const points = Array.isArray(samples) ? samples : [];
    let end = -1;
    for (let i = 0; i < points.length; i++) {
      const flow = Number(points[i] && points[i].flow_mls);
      const head = Number(points[i] && points[i].head_bar);
      if (flow > 0 && head > 0) end = i;
    }
    if (end < 0) return points.slice();
    return points.slice(0, end + 1);
  }

  // Live samples while a shot runs. Every other state shows the last
  // saved curve, the same way Ready does. An empty slot stays hidden.
  function heroCurveSlot(view) {
    const live = view && Array.isArray(view.liveSamples) ? view.liveSamples : [];
    const last = view && Array.isArray(view.lastSamples) ? view.lastSamples : [];
    const shotActive = !!(view && view.shotActive);
    if (shotActive) {
      if (!live.length) return { hidden: true, source: null, samples: [], label: "" };
      return {
        hidden: false,
        source: "live",
        samples: live,
        label: "This shot's pressure",
      };
    }
    const saved = pourCurveSamples(last.length ? last : live);
    if (saved.length) {
      return {
        hidden: false,
        source: last.length ? "last" : "live",
        samples: saved,
        label: "Last shot's pressure",
      };
    }
    return { hidden: true, source: null, samples: [], label: "" };
  }

  function heroCurveFigure(samples) {
    const points = (samples || []).filter(
      (sample) =>
        sample &&
        typeof sample.head_bar === "number" &&
        Number.isFinite(sample.head_bar) &&
        typeof sample.t === "number" &&
        Number.isFinite(sample.t)
    );
    if (!points.length) return null;
    const tMax = HERO_CURVE_T_MAX;
    const yOf = (bar) =>
      (1 - Math.max(0, Math.min(HERO_CURVE_Y_MAX, bar)) / HERO_CURVE_Y_MAX) *
      HERO_CURVE_HEIGHT;
    const mapped = points.map((sample) => ({
      x: (Math.max(0, Math.min(tMax, sample.t)) / tMax) * HERO_CURVE_WIDTH,
      y: yOf(sample.head_bar),
    }));
    const line = mapped
      .map(
        (point, index) =>
          (index ? "L" : "M") + point.x.toFixed(2) + " " + point.y.toFixed(2)
      )
      .join(" ");
    const first = mapped[0];
    const last = mapped[mapped.length - 1];
    const area =
      line +
      " L" +
      last.x.toFixed(2) +
      " " +
      HERO_CURVE_HEIGHT.toFixed(2) +
      " L" +
      first.x.toFixed(2) +
      " " +
      HERO_CURVE_HEIGHT.toFixed(2) +
      " Z";
    const marks = [0, 10, 20, 30, 40];
    const axis = marks.map((value, index) => {
      const text =
        Math.abs(value - Math.round(value)) < 0.05
          ? String(Math.round(value))
          : value.toFixed(0);
      return index === 0 || index === marks.length - 1 ? text + " s" : text;
    });
    return {
      width: HERO_CURVE_WIDTH,
      height: HERO_CURVE_HEIGHT,
      yMax: HERO_CURVE_Y_MAX,
      tMax: tMax,
      line: line,
      area: area,
      dot: { x: Number(last.x.toFixed(2)), y: Number(last.y.toFixed(2)) },
      // A fixed 9 bar mark, above the line. Not the live reading, so a
      // shot that has just ended cannot label the axis 0.0.
      yLabel: "9.0",
      yLabelAt: Number(yOf(9).toFixed(2)),
      axis: axis,
    };
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

  const ACCENTS = {
    gaggia: { light: "#8C4550", dark: "#D88C99", tone: "open" },
    rancilio: { light: "#2C667F", dark: "#77A8C6", tone: "tight" },
    crema: { light: "#A04620", dark: "#E58A5C", tone: "default" },
  };

  function accentTheme(id) {
    return ACCENTS[id] || ACCENTS.crema;
  }

  // Litres when the size lands on a tenth of a litre. Anything else stays ml.
  function capacityText(ml) {
    if (typeof ml !== "number" || !isFinite(ml)) return null;
    const tenth = Math.round((ml / 1000) * 10) / 10;
    if (Math.abs(ml - tenth * 1000) < 0.51) {
      const label = tenth % 1 === 0 ? String(tenth.toFixed(0)) : tenth.toFixed(1);
      return label + " L";
    }
    return Math.round(ml).toLocaleString("en-US") + " ml";
  }

  function pressureText(bar) {
    if (typeof bar !== "number" || !isFinite(bar)) return null;
    const rounded = Math.round(bar * 10) / 10;
    const label = rounded % 1 === 0 ? String(rounded.toFixed(0)) : rounded.toFixed(1);
    return label + " bar";
  }

  function isOwnMachine(profile) {
    if (!profile || !profile.id) return false;
    if (profile.id === "generic" || profile.id === "custom" || profile.custom) return true;
    return profile.vendor === "Custom" && profile.model === "Custom";
  }

  function makerModel(profile) {
    if (!profile || isOwnMachine(profile)) return "";
    return [profile.vendor, profile.model].filter(Boolean).join(" ");
  }

  function headerForLine(profile) {
    if (!profile || !profile.id || profile.status === "machine_not_set") {
      return "Machine not set";
    }
    if (isOwnMachine(profile)) return "for your machine";
    const name = makerModel(profile);
    return name ? "for " + name : "Machine not set";
  }

  function settingsSummary(profile) {
    if (!profile || !profile.id || profile.status === "machine_not_set") {
      return { title: "Machine not set", detail: "Set your machine" };
    }
    const size = capacityText(profile.tank_capacity_ml);
    const bar = pressureText(
      typeof profile.pressure_target_bar === "number" ? profile.pressure_target_bar : 9
    );
    if (isOwnMachine(profile)) {
      return {
        title: size ? "Custom machine \u00b7 " + size + " tank" : "Custom machine",
        detail: "",
      };
    }
    return {
      title: makerModel(profile),
      detail: [size ? size + " tank" : null, bar].filter(Boolean).join(" \u00b7 "),
    };
  }

  function pickerRow(profile) {
    if (!profile) return null;
    if (profile.id === "generic" || profile.id === "custom" || profile.custom) {
      return {
        id: profile.id === "custom" ? "custom" : "generic",
        title: "My model isn\u2019t listed",
        detail: "Enter the tank size yourself",
        unlisted: true,
        sourceUrl: null,
      };
    }
    const size = capacityText(profile.tank_capacity_ml);
    const confirmed = !!(profile.capacity_confirmed && profile.source_url && size);
    return {
      id: profile.id,
      title: makerModel(profile),
      detail: confirmed
        ? size + " tank \u00b7 From the maker\u2019s spec"
        : "Tank size not confirmed \u00b7 Enter yours",
      unlisted: false,
      sourceUrl: confirmed ? profile.source_url : null,
      needsSize: !confirmed,
    };
  }

  function pickerModels(profiles, query) {
    const q = String(query || "").trim().toLowerCase();
    const listed = [];
    let unlisted = null;
    (profiles || []).forEach(function (profile) {
      if (!profile || profile.id === "custom") return;
      const row = pickerRow(profile);
      if (!row) return;
      if (row.unlisted) {
        unlisted = row;
        return;
      }
      if (!q) {
        listed.push(row);
        return;
      }
      const hay = (
        row.title + " " + (profile.vendor || "") + " " + (profile.model || "")
      ).toLowerCase();
      if (hay.indexOf(q) >= 0) listed.push(row);
    });
    if (!unlisted) {
      unlisted = pickerRow({ id: "generic", vendor: "Generic", model: "Current setup" });
    }
    const rows = listed.slice();
    rows.push(unlisted);
    return {
      rows: rows,
      emptyNote: q && !listed.length ? "No listed machine matches. You can add yours." : "",
    };
  }

  function switchConfirm(current, next, shotOpen) {
    const size = capacityText(next && next.tank_capacity_ml);
    const lines = [];
    if (size) {
      lines.push(
        "The tank size changes to " + size +
          ". Fill the tank, then tap I refilled it. Your shot history stays as it is."
      );
    }
    const cur = current && typeof current.pressure_target_bar === "number"
      ? current.pressure_target_bar : 9;
    const nxt = next && typeof next.pressure_target_bar === "number"
      ? next.pressure_target_bar : 9;
    if (cur !== nxt) lines.push("Pressure target changes to " + pressureText(nxt) + ".");
    if (shotOpen) lines.push("Switches after this shot is saved.");
    const who = isOwnMachine(next) ? "your machine" : makerModel(next);
    return {
      title: "Switch to " + who + "?",
      lines: lines,
      toast: isOwnMachine(next)
        ? "Now set up for your machine."
        : "Now set up for a " + who + ".",
    };
  }

  function shotGlobals(globals) {
    const low = globals && globals.shot_time_low_s != null ? globals.shot_time_low_s : 25;
    const high = globals && globals.shot_time_high_s != null ? globals.shot_time_high_s : 30;
    const cup = globals && globals.ml_per_cup != null ? globals.ml_per_cup : 36;
    return {
      shotTime: low + " to " + high + " s",
      cupSize: cup + " ml",
      aim: "Aiming for " + low + " to " + high + " s",
      window: low + "\u2013" + high + " s",
    };
  }

  function upgradeBanner(profile) {
    if (!profile || !profile.prompt_pick_model) return null;
    return {
      text: "Which machine is this? Pick your model so the tank size and pressure target match it.",
      pick: "Pick my machine",
      later: "Not now",
    };
  }

  function shotIsOpen(machine) {
    return !!(machine && (machine.shot || machine.state === "brewing"));
  }

  function tankView(tank, profile) {
    const used = tank && typeof tank.used_ml === "number" ? Math.round(tank.used_ml) : null;
    const usedLine = used == null ? "" : used.toLocaleString("en-US") + " ml used";
    const refill = !!(tank && tank.refill_required) || !!(profile && profile.refill_required);
    const capacity = profile && typeof profile.tank_capacity_ml === "number"
      ? profile.tank_capacity_ml
      : tank && typeof tank.capacity_ml === "number" ? tank.capacity_ml : null;
    const unset = !profile || !profile.id || profile.status === "machine_not_set";
    if (refill && typeof capacity === "number") {
      return {
        mode: "refill",
        lead: "Refill needed",
        used: usedLine,
        showMeter: false,
        showRefill: true,
        note: "",
      };
    }
    if (unset || typeof capacity !== "number") {
      return {
        mode: "unset",
        lead: "Tank size not set",
        action: "Set your machine",
        used: usedLine,
        showMeter: false,
        showRefill: false,
        note: "",
      };
    }
    const shots = tank ? tank.shots_left : null;
    const over = typeof tank.used_ml === "number" && tank.used_ml > capacity;
    return {
      mode: shots === 0 ? "empty" : "ready",
      lead: shots === 0 ? tankLead(0) : null,
      shots: shots,
      used: usedLine,
      showMeter: typeof shots === "number" && shots > 0,
      percent: tank ? tank.percent : 0,
      low: !!(tank && tank.low),
      showRefill: false,
      note: over
        ? "The used amount is more than this tank\u2019s size. If the tank is full, tap I refilled it."
        : "",
    };
  }

  return {
    pillText: pillText,
    footerText: footerText,
    todayLine: todayLine,
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
    liveVerdict: liveVerdict,
    pillLabel: pillLabel,
    pillKind: pillKind,
    glanceOn: glanceOn,
    noteShotWindow: noteShotWindow,
    clearShotWindow: clearShotWindow,
    showOverBand: showOverBand,
    meterSpec: meterSpec,
    meterFraction: meterFraction,
    meterHot: meterHot,
    shotVerdict: shotVerdict,
    tankLead: tankLead,
    humidityLabel: humidityLabel,
    tankMeterHidden: tankMeterHidden,
    refillControl: refillControl,
    shotWhenLine: shotWhenLine,
    recentTile: recentTile,
    recentRowsThatFit: recentRowsThatFit,
    allShotsLink: allShotsLink,
    flowBars: flowBars,
    staleTagText: staleTagText,
    offlineMessage: offlineMessage,
    isFlush: isFlush,
    livePresentation: livePresentation,
    shownState: shownState,
    nearestTickIndex: nearestTickIndex,
    curveAfter: curveAfter,
    curveRequest: curveRequest,
    curveNeedsFull: curveNeedsFull,
    applyCurrentSamples: applyCurrentSamples,
    pourCurveSamples: pourCurveSamples,
    heroCurveSlot: heroCurveSlot,
    heroCurveFigure: heroCurveFigure,
    READY_MIN_BAR: READY_MIN_BAR,
    accentTheme: accentTheme,
    capacityText: capacityText,
    pressureText: pressureText,
    headerForLine: headerForLine,
    settingsSummary: settingsSummary,
    pickerModels: pickerModels,
    switchConfirm: switchConfirm,
    shotGlobals: shotGlobals,
    upgradeBanner: upgradeBanner,
    shotIsOpen: shotIsOpen,
    tankView: tankView,
    isOwnMachine: isOwnMachine,
  };
});

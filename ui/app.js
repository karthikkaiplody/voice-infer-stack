// The live trace page.
//
// This file draws SPANS. It receives them over SSE exactly as the pipeline
// emits them -- name, start, end, attributes -- and puts each one where its
// timestamps say it belongs. It does not know what a stage "means", does not
// infer an order, and does not time anything itself. That is the whole design:
// the page and `budget.py` are two readers of one measurement.
//
// The input meter and the reply text are the exceptions, and they are not
// timing. They are here because a silent pipeline and a wrong microphone look
// identical otherwise.

const $ = (id) => document.getElementById(id);

const LABELS = {
  turn_detection: ["Turn detection", "have you finished?"],
  stt:            ["Speech to text", "audio becomes words"],
  llm:            ["Language model", "words become a reply"],
  tts:            ["Text to speech", "the reply becomes audio"],
};

let CFG = null;
let skew = null;          // server wall clock (ms) minus performance.now()
let zero = null;          // when you stopped making noise, on the server clock
let spans = new Map();    // span_id -> {name, a, b, el, dur}
let counts = {};          // stage -> spans seen this turn
let live = false;         // a turn is in flight; keep animating
let landed = false;       // the window span arrived: stop running the clock
let view = { lo: 0, hi: 0 };

// Spans are stamped in absolute wall-clock nanoseconds on the server. To grow
// a bar that has not finished, the page needs "now" on that same axis, so
// every event carries the server clock and this tracks the offset. Smoothed,
// because one event delivered late would otherwise drag the running edge
// backwards.
function clock(ns) {
  const s = ns / 1e6 - performance.now();
  skew = skew === null ? s : skew * 0.9 + s * 0.1;
}
const now = () => performance.now() + skew;

// ---------------------------------------------------------------- layout ---

function bounds() {
  let lo = Infinity, hi = -Infinity;
  for (const s of spans.values()) {
    lo = Math.min(lo, s.a);
    hi = Math.max(hi, s.b === null ? now() : s.b);
  }
  if (!isFinite(lo)) { lo = zero === null ? now() : zero; hi = lo + 1000; }
  const org = zero === null ? lo : zero;
  if (zero !== null) {
    lo = Math.min(lo, zero);
    hi = Math.max(hi, zero + CFG.budget_ms * 1.15);
  }
  if (live) hi = Math.max(hi, now());
  // Quantised around zero so the axis steps instead of twitching, and the
  // ticks are only rebuilt when it actually steps.
  return {
    lo: org - Math.ceil((org - lo) / 250) * 250,
    hi: org + Math.ceil((hi - org) / 500) * 500,
  };
}

function ticks() {
  const box = $("ticks");
  const keep = [$("zero"), $("budget"), $("head")];
  [...box.children].forEach((c) => { if (!keep.includes(c)) c.remove(); });

  const org = zero === null ? view.lo : zero;
  const span = view.hi - view.lo;
  const step = span <= 2500 ? 250 : span <= 6000 ? 500 : 1000;
  for (let t = Math.ceil((view.lo - org) / step) * step; org + t <= view.hi; t += step) {
    if (t === 0 && zero !== null) continue;   // the zero marker already says so
    const d = document.createElement("div");
    d.className = "tick";
    d.style.left = pct(org + t);
    d.innerHTML = "<span>" + t + "</span>";
    box.appendChild(d);
  }
  if (zero !== null) {
    $("zero").style.display = "block";
    $("zero").style.left = pct(zero);
    $("budget").style.display = "block";
    $("budget").style.left = pct(zero + CFG.budget_ms);
  }
}

const pct = (t) => ((t - view.lo) / (view.hi - view.lo)) * 100 + "%";

function draw() {
  for (const s of spans.values()) {
    const end = s.b === null ? now() : s.b;
    s.el.style.left = pct(s.a);
    s.el.style.width =
      Math.max(((end - s.a) / (view.hi - view.lo)) * 100, 0.3) + "%";
    const x = ((end - view.lo) / (view.hi - view.lo)) * 100;
    s.dur.style.left = x + "%";
    s.dur.classList.toggle("flip", x > 72);
    s.dur.textContent = label(s, end);
  }
  $("head").style.left = pct(now());
}

function label(s, end) {
  const total = Math.round(end - s.a);
  // A stage that began while you were still talking is not charged for the
  // part that was free. budget.py clips to the window; this says so out loud.
  if (zero !== null && s.a < zero - 5) {
    return total + " ms  ·  " + Math.max(0, Math.round(end - zero)) + " ms after you stopped";
  }
  return total + " ms";
}

(function loop() {
  requestAnimationFrame(loop);
  if (!CFG || !spans.size) return;
  const b = bounds();
  if (b.lo !== view.lo || b.hi !== view.hi) { view = b; ticks(); }
  draw();
  if (live && !landed && zero !== null) {
    $("total").textContent = Math.round(now() - zero).toLocaleString() + " ms";
  }
})();

// ------------------------------------------------------------------ rows ---

function buildRows() {
  $("rows").innerHTML = "";
  for (const name of CFG.stages) {
    const [title, sub] = LABELS[name] || [name, ""];
    const row = document.createElement("div");
    row.className = "row";
    row.id = "r-" + name;
    row.innerHTML =
      '<div class="gut"><b>' + title + "</b><i>" + sub + "</i></div>" +
      '<div class="lane"></div>';
    const note = document.createElement("div");
    note.className = "note";
    note.id = "n-" + name;
    $("rows").append(row, note);
  }
}

function newTurn() {
  spans = new Map();
  counts = {};
  zero = null;
  live = true;
  landed = false;
  $("tlab").textContent = "since you stopped talking";
  for (const name of CFG.stages) {
    const row = $("r-" + name);
    row.className = "row";
    row.querySelector(".lane").innerHTML = "";
    $("n-" + name).textContent = "";
    $("n-" + name).className = "note";
  }
  $("zero").style.display = "none";
  $("budget").style.display = "none";
  $("head").style.display = "block";
  $("hint").textContent = "";
  $("total").textContent = "—";
}

function upsert(sp, ended) {
  if (sp.name === CFG.window) return window_(sp, ended);

  const row = $("r-" + sp.name);
  if (!row) return;
  let s = spans.get(sp.span_id);
  if (!s) {
    const el = document.createElement("div");
    el.className = "bar" + (sp.name === "turn_detection" ? " wait" : "");
    const dur = document.createElement("div");
    dur.className = "dur";
    row.querySelector(".lane").append(el, dur);
    row.classList.add("seen");
    s = { name: sp.name, a: sp.start_time_ns / 1e6, b: null, el, dur };
    spans.set(sp.span_id, s);
    counts[sp.name] = (counts[sp.name] || 0) + 1;
  }
  if (ended) {
    s.b = sp.end_time_ns / 1e6;
    s.el.classList.add("done");
    note(sp, s);
  }
  if (sp.name === "turn_detection" && zero === null) {
    zero = s.a;                    // the moment you stopped making noise
  }
  if (ended && sp.name === "turn_detection") {
    status("Your turn is over. Only now can anything downstream start.", "busy");
  }
  if (zero !== null && s.a < zero - 5) s.el.classList.add("early");
}

function note(sp, s) {
  const el = $("n-" + sp.name);
  const a = sp.attributes || {};
  if (sp.name === "turn_detection") {
    el.textContent =
      "waiting, not computing — " + Math.round(sp.duration_ms) +
      " ms of silence at vad.stop_secs=" + a["vad.stop_secs"];
  } else if (sp.name === "stt") {
    el.textContent = a.transcript || el.textContent;
  } else if (sp.name === "tts" && a["metrics.ttfb"] !== undefined) {
    el.textContent =
      "first audio " + Math.round(a["metrics.ttfb"] * 1000) +
      " ms into synthesis; the rest is still being made while it plays";
  }
  if (counts[sp.name] > 1) {
    el.className = "note warn";
    el.textContent =
      counts[sp.name] + "× in one turn — the agent answered a fragment and " +
      "threw the work away";
  }
}

function window_(sp, ended) {
  if (!ended) return;
  zero = sp.start_time_ns / 1e6;
  landed = true;
  $("tlab").textContent = "you stopped talking → first audio";
  $("total").textContent = Math.round(sp.duration_ms).toLocaleString() + " ms";
  status(
    Math.round(sp.duration_ms) + " ms from you stopping to the first sample of " +
    "the reply. It is still speaking.", "busy");
}

function status(text, cls) {
  $("stx").textContent = text;
  $("st").className = "status " + (cls || "");
}

// ---------------------------------------------------------------- events ---

$("go").onclick = async () => {
  $("go").disabled = true;
  status("Loading models. First run downloads and warms them; nothing is " +
         "listening yet.", "busy");
  const r = await (await fetch("/start", { method: "POST" })).json();
  if (!r.ok) { status(r.error, "bad"); $("go").disabled = false; }
};

$("halt").onclick = async () => {
  $("halt").disabled = true;
  status("Stopping…", "busy");
  await fetch("/stop", { method: "POST" });
};

new EventSource("/events").onmessage = (m) => {
  const e = JSON.parse(m.data);
  clock(e.now_ns);

  switch (e.kind) {
    case "hello":
      CFG = e;
      buildRows();
      $("gate").style.left = e.gate * 100 + "%";
      $("traces").textContent = e.traces;
      if (e.running) { $("go").disabled = true; $("halt").disabled = false; }
      break;

    case "level": {
      const v = Math.max(0, Math.min(1, e.v || 0));
      $("lvl").style.width = v * 100 + "%";
      // Green once you are over the gate the VAD actually compares against.
      $("lvl").style.background = v >= CFG.gate ? "var(--ok)" : "var(--run)";
      $("mic").dataset.v = v.toFixed(2);
      break;
    }

    case "devices":
      $("mic").textContent = e.name + (e.current === null ? " (default)" : "");
      break;

    case "ready":
      $("stack").textContent = e.stack;
      $("halt").disabled = false;
      status("Listening. Say something.", "live");
      break;

    case "turn_start":
      newTurn();
      status("Hearing you. Nothing downstream can start yet.", "busy");
      break;

    case "span_start":
      upsert(e.span, false);
      break;

    case "span_end":
      upsert(e.span, true);
      break;

    case "heard":
      $("n-stt").textContent = e.text;
      break;

    case "token":
      $("n-llm").textContent += e.text;
      break;

    case "turn_done":
      live = false;
      $("head").style.display = "none";
      status("Listening. Say something.", "live");
      break;

    case "error":
      status(e.text, "bad");
      break;

    case "stopped":
      live = false;
      $("go").disabled = false;
      $("halt").disabled = true;
      $("head").style.display = "none";
      status("Stopped. The spans are in " + CFG.traces +
             " — read them with budget.py.");
      break;
  }
};

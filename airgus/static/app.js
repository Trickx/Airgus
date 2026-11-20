const $ = (sel) => document.querySelector(sel);
const RULE_LABELS = {
  "runaway": "Stern läuft davon", "star-lost": "Leitstern verloren", "calibration": "Kalibrierung", "settle": "Settle-Fehler",
  "meridian-flip": "Meridian-Flip", "past-meridian": "Hinter dem Meridian", "rms": "Guiding-RMS",
  "snr-drop": "SNR-Abfall", "stale-log": "Log steht still", "tracking-off": "Nachführung aus",
  "plate-solve": "Plate Solve", "device-lost": "Gerät getrennt", "main-camera": "Hauptkamera", "focuser": "Fokussierer", "mount-home": "Home-Position", "app-error": "Aufnahmeprogramm",
  "controller-health": "ASIAIR-Stromversorgung/Temperatur", "wifi": "ASIAIR-WLAN",
  "controller-offline": "ASIAIR nicht erreichbar",
};
const STATUS_LABELS = {
  idle: "bereit", loading: "lädt …", running: "läuft", paused: "pausiert", finished: "fertig", error: "Fehler",
};
const MAX_FRAMES = 20000;

let frames = [];          // {t: ms, ra, dec}
let alerts = [];          // newest last
let last = null;          // last status snapshot
let mountView = null;

// --- helpers ------------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : res.statusText);
  return data;
}

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), 4000);
}

const fmt = (v, digits = 1, unit = "") => (v === null || v === undefined ? "–" : `${Number(v).toFixed(digits)}${unit}`);
const fmtTime = (iso) => (iso ? iso.replace("T", " ").slice(0, 19) : "–");

function mode() { return document.querySelector("input[name=mode]:checked").value; }

// --- control ------------------------------------------------------------------

async function loadFolders(selected) {
  const data = await api("/api/folders");
  const sel = $("#folder");
  sel.innerHTML = "";
  for (const f of data.folders) {
    const opt = document.createElement("option");
    opt.value = f.folder;
    opt.textContent = `${f.folder || "(Log-Wurzel)"} – ${f.files.length} Logs`;
    sel.appendChild(opt);
  }
  if (!data.folders.length) {
    sel.innerHTML = `<option value="">keine Logs unter ${data.root}</option>`;
  }
  if (selected !== undefined && [...sel.options].some((o) => o.value === selected)) sel.value = selected;
}

const locationValue = () => document.querySelector("input[name=location]:checked")?.value || "local";
let smbLabel = "";

function updateModeFields() {
  const live = mode() === "live";
  document.querySelectorAll(".replay-only").forEach((e) => (e.hidden = live));
  document.querySelectorAll(".live-only").forEach((e) => (e.hidden = !live));
  const smb = live && locationValue() === "smb";
  $("#folder-field").hidden = smb;
  $("#smb-field").hidden = !smb;
  $("#smb-field").textContent = `Liest direkt von ${smbLabel} (Einstellungen → SMB-Freigabe).`;
}

function smbBody() {
  const shares = $("#smb-shares").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const smb = { server: $("#smb-server").value, shares,
    username: $("#smb-user").value, max_age_h: Number($("#smb-age").value) };
  if ($("#smb-pass").value) smb.password = $("#smb-pass").value;
  return { smb };
}

async function saveSmb() {
  await api("/api/settings", { method: "PUT", body: smbBody() });
  $("#smb-pass").value = "";
  await loadSettings();
}

async function testSmb() {
  const list = $("#smb-result");
  list.innerHTML = "<li>verbinde …</li>";
  try {
    await saveSmb();
    const r = await api("/api/smb/test", { method: "POST" });
    list.innerHTML = "";
    for (const s of r.shares) {
      const head = document.createElement("li");
      head.className = "share-head";
      head.textContent = s.error ? `✗ ${s.location}: ${s.error}`
        : `✓ ${s.location}: ${s.total} Logdateien${s.total ? ", neueste:" : ""}`;
      list.appendChild(head);
      for (const f of s.files) {
        const li = document.createElement("li");
        li.textContent = `${f.watched ? "●" : "○"} ${f.name} · ${(f.size / 1024).toFixed(0)} KB · ${fmtTime(f.mtime)}`;
        li.title = f.watched ? "wird im Live-Modus beobachtet" : "älter als das Zeitfenster, wird ignoriert";
        list.appendChild(li);
      }
    }
  } catch (e) { list.innerHTML = ""; toast(`SMB-Test fehlgeschlagen: ${e.message}`); }
}

async function start() {
  try {
    await api("/api/run", { method: "POST", body: {
      mode: mode(), folder: $("#folder").value, speed: Number($("#speed").value),
      interval_s: Number($("#interval").value), from_start: $("#from-start").checked,
      pushover: $("#push-replay").checked, location: mode() === "live" ? locationValue() : "local",
    } });
  } catch (e) { toast(`Start fehlgeschlagen: ${e.message}`); }
}

async function pauseResume() {
  const paused = last?.run.status === "paused";
  await api(paused ? "/api/run/resume" : "/api/run/pause", { method: "POST" });
}

// --- rendering ----------------------------------------------------------------

// Values that came over a live connection which is now down stay visible but grey (last known).
const DEPENDS = {
  4700: ["#c-exposure", "#c-solve", "#c-maincam", "#c-temp", "#c-power", "#c-wifi"],
  4400: ["#c-guidecam", "#c-mount", "#s-guiding"],
  pose: ["#s-pier", "#s-ha", "#s-dec", "#s-meridian", "#mount-info", "#mount-view"],
};

function markStale(c, pose) {
  const off4700 = !!c && c.connected === false, off4400 = !!c && c.guider_connected === false;
  DEPENDS[4700].forEach((id) => $(id).classList.toggle("stale", off4700));
  DEPENDS[4400].forEach((id) => $(id).classList.toggle("stale", off4400));
  DEPENDS.pose.forEach((id) => $(id).classList.toggle("stale", off4400 && pose?.source === "live"));
  $("#s-tracking").classList.toggle("stale", off4400);
}

function renderController(c) {
  const show = !!c && c.connected !== null;
  // rows from the live ASIAIR connection are hidden without one (e.g. replaying old logs)
  document.body.classList.toggle("no-live", !show);
  if (!show) return;
  // Two connections: port 4700 (imager) and port 4400 (guider and mount; null = switched off).
  const off4700 = c.connected === false, off4400 = c.guider_connected === false;
  $("#c-conn").textContent = off4700 && off4400 ? "getrennt" : off4700 ? "Port 4700 getrennt"
    : off4400 ? "Port 4400 getrennt" : "verbunden";
  $("#c-conn").classList.toggle("bad", !!(off4700 || off4400));
  // Guiding state live from the guider while connected (otherwise the log value stays)
  const gs = { Guiding: "guidet", Stopped: "aus", Looping: "Belichtungsschleife", LostLock: "Stern verloren",
    Paused: "pausiert", Calibrating: "kalibriert", Selected: "Stern gewählt" };
  if (c.guider_state) {
    $("#s-guiding").textContent = gs[c.guider_state] || c.guider_state;
    $("#s-guiding").classList.toggle("bad", !!(c.guider_state === "LostLock"));
  }
  for (const [id, value] of [["#c-guidecam", c.guide_camera], ["#c-mount", c.mount]]) {
    $(id).textContent = !c.devices_known ? "–" : value || "nicht verbunden";
    $(id).classList.toggle("bad", !!(c.devices_known && !value));
  }
  const mo = c.mount_motion;
  if (mo && c.devices_known && c.mount) {
    const t = mo.time.slice(11, 19);
    const TEXT = { Home: { working: "fährt in Home-Position", complete: `Home-Position erreicht ${t}` },
      Goto: { working: "GoTo läuft", complete: `GoTo fertig ${t}` } };
    $("#c-mount").textContent += ` · ${TEXT[mo.kind]?.[mo.state] || `${mo.kind}: ${mo.state} ${t}`}`;
  }
  const PAGES = { preview: "Vorschau", autorun: "Sequenz", live: "Live", focus: "Fokus", plan: "Plan" };
  const EXP = { start: "läuft", downloading: "lädt herunter", complete: "fertig", fail: "fehlgeschlagen",
    cancel: "abgebrochen", stop: "gestoppt" };
  const SOLVE = { start: "startet", solving: "löst", complete: "gelöst", success: "gelöst", fail: "fehlgeschlagen" };
  const hhmm = (iso) => iso.slice(11, 19);
  const ex = c.exposure;

  // Only what the ASIAIR actually reported, always with the time of that report.
  const mc = c.main_camera_changed;
  let mcText = "–", mcBad = false;
  if (c.main_camera_connected === true || c.main_camera_connected === false) {
    mcText = `${c.main_camera_connected ? `verbunden${c.main_camera_name ? ` · ${c.main_camera_name}` : ""}`
      : "nicht verbunden"} · Stand ${hhmm(c.main_camera_fact)} (${c.main_camera_source})`;
    mcBad = !c.main_camera_connected;
  } else if (mc) {
    mcText = `Statuswechsel ${hhmm(mc)} · Zustand unbekannt`;
  }
  $("#c-maincam").textContent = mcText;
  $("#c-maincam").classList.toggle("bad", !!(mcBad));
  $("#c-exposure").textContent = !ex ? "–" : [EXP[ex.state] || ex.state, PAGES[ex.page] || ex.page,
    ex.duration_s ? `${ex.duration_s} s` : null, ex.state === "start" ? `seit ${hhmm(ex.started)}` : hhmm(ex.updated),
  ].filter(Boolean).join(" · ");
  $("#c-exposure").classList.toggle("bad", !!(ex && ["fail", "cancel"].includes(ex.state)));
  const so = c.solve;
  $("#c-solve").textContent = !so ? "–" : [SOLVE[so.state] || so.state, PAGES[so.page] || so.page,
    so.stars !== null ? `${so.stars} Sterne` : null, so.error, hhmm(so.updated)].filter(Boolean).join(" · ");
  $("#c-solve").classList.toggle("bad", !!(so && so.state === "fail"));
  $("#c-temp").textContent = fmt(c.temp_c, 0, " °C");
  const problems = [c.undervolt && "Unterspannung", c.overcurrent && "Überstrom", c.overtemp && "Überhitzung"]
    .filter(Boolean);
  $("#c-power").textContent = c.temp_c === null ? "–" : problems.length ? problems.join(", ") : "ok";
  $("#c-power").classList.toggle("bad", !!(problems.length));
  $("#c-wifi").textContent = fmt(c.wifi_dbm, 0, " dBm");
}

function fmtDuration(s) {
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

let pageCode;  // code_started of the backend this page was loaded from

function renderStatus(s) {
  // Airgus was restarted with new code: this page (HTML/JS) is outdated, load the current one.
  if (pageCode === undefined) pageCode = s.code_started;
  else if (s.code_started !== undefined && s.code_started !== pageCode) { location.reload(); return; }
  last = s;
  $("#restart-banner").hidden = s.restart_needed === false;
  const r = s.run;
  const badge = $("#run-badge");
  badge.textContent = `${r.mode === "live" ? "Live" : r.mode === "replay" ? "Replay" : ""} ${STATUS_LABELS[r.status] || r.status}`.trim();
  badge.className = `badge ${r.status}`;
  $("#btn-pause").textContent = r.status === "paused" ? "Fortsetzen" : "Pause";
  $("#btn-pause").disabled = !(r.mode === "replay" && ["running", "paused"].includes(r.status));
  $("#btn-stop").disabled = !["running", "paused", "loading"].includes(r.status);
  const pct = r.total ? (100 * r.done) / r.total : (r.status === "running" ? 100 : 0);
  $("#progress-bar").style.width = `${pct}%`;
  let info = r.folder !== null && r.folder !== undefined ? `Ordner: ${r.folder || "(Log-Wurzel)"}` : "";
  if (r.total) info += ` · ${r.done.toLocaleString("de")} / ${r.total.toLocaleString("de")} Ereignisse`;
  else if (r.mode === "live") info += ` · ${r.done.toLocaleString("de")} Ereignisse`;
  if (r.error) info += ` · Fehler: ${r.error}`;
  $("#run-info").textContent = info;

  const g = s.rig;
  $("#s-target").textContent = g.target || "–";
  $("#s-image").textContent = g.exposure_index ? `#${g.exposure_index}` : "–";
  $("#s-guiding").textContent = g.guiding ? "aktiv" : "aus";
  $("#s-guiding").classList.remove("bad");  // a live "Stern verloren" sets it again below
  $("#s-tracking").textContent = g.tracking === true ? "an" : g.tracking === false ? "aus" : "unbekannt";
  $("#s-tracking").classList.toggle("bad", !!(g.tracking === false && g.sequence_active));
  const ps = g.pose, est = ps && ps.source !== "live" ? " (geschätzt)" : "";
  $("#s-pier").textContent = ps?.pier ? `${ps.pier}${ps.source === "live" ? "" : " (PHD2-Log)"}` : "–";
  $("#s-ha").textContent = ps && ps.ha !== null ? `${fmt(ps.ha, 2)} h${est}` : "–";
  $("#s-dec").textContent = ps && ps.dec !== null ? `${fmt(ps.dec, 1)}°${ps.source === "live" ? "" : " (PHD2-Log)"}` : "–";
  $("#s-rms").textContent = g.rms_ra === null ? "–" : `${fmt(g.rms_ra, 2)}″ / ${fmt(g.rms_dec, 2)}″`;
  $("#s-rms-total").textContent = fmt(g.rms_total, 2, "″");
  $("#s-snr").textContent = fmt(g.snr, 1);
  $("#s-time").textContent = fmtTime(r.sim_time);
  // One EAF row: position from the Autorun log (the ASIAIR does not tell it live) plus the last open.
  const fo = g.focus, opened = g.controller?.focuser_opened;
  const eaf = [];
  if (fo) eaf.push(`${fo.position} · ${fo.final ? "Ergebnis Autofokus" : "während Autofokus"} ${fo.time.slice(11, 16)} (Log)`);
  if (opened) eaf.push(`zuletzt geöffnet ${opened.slice(11, 19)}`);
  $("#s-focus").textContent = eaf.join(" · ") || "–";
  renderController(g.controller);

  // Meridian countdown like the ASIAIR app: -HA from the live position, only while tracking.
  const ms = g.pose?.meridian_s;
  const mmss = (sec) => { const t = Math.round(Math.abs(sec)), h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60);
    return `${h ? `${h} h ` : ""}${m} min ${t % 60} s`; };
  $("#s-meridian").textContent = typeof ms !== "number" ? "–"
    : ms >= 0 ? `in ${mmss(ms)}` : `überschritten vor ${mmss(ms)}`;
  // 3D pose: live from the ASIAIR (as reported, with time) or estimated from the PHD2 log.
  const pose = g.pose;
  const known = !!pose && pose.ha !== null && pose.dec !== null && !!pose.pier;
  mountView?.update(known ? { ha: pose.ha, dec: pose.dec, pier: pose.pier, lat: pose.lat ?? s.site?.latitude ?? 51 }
    : { ha: null, dec: null, pier: null, lat: s.site?.latitude ?? 51 });
  $("#mount-view").classList.toggle("unknown", !known);
  markStale(g.controller, pose);
  const info3d = $("#mount-info");
  if (!known) {
    info3d.textContent = "Position unbekannt – Grundstellung nur als Platzhalter. Sie kommt live von der ASIAIR "
      + "(Port 4400) oder aus dem PHD2-Log beim Guiding-Start.";
  } else {
    const cwUp = mountView?.counterweightUp;
    const src = pose.source === "live" ? `live von der ASIAIR, Stand ${pose.time.slice(11, 19)}`
      : `geschätzt aus PHD2-Log (Guiding-Start ${pose.time.slice(11, 16)})`;
    info3d.innerHTML = `Position ${src}` +
      (cwUp ? ` · <span class="warn">Gegengewicht oben!</span>` : "");
  }
}

function addAlert(a) {
  alerts.push(a);
  renderAlerts();
}

function renderAlerts() {
  $("#alert-count").textContent = alerts.length ? `(${alerts.length})` : "";
  const ul = $("#alert-list");
  ul.innerHTML = "";
  for (const a of [...alerts].reverse()) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="time"></span><span class="sev ${a.severity}">${a.severity}</span>
      <span class="title"></span><div class="msg muted"></div>`;
    li.querySelector(".time").textContent = a.time.slice(11, 19);
    li.querySelector(".title").textContent = a.title;
    li.querySelector(".msg").textContent = a.message;
    li.title = `${fmtTime(a.time)} · ${RULE_LABELS[a.rule] || a.rule}`;
    ul.appendChild(li);
  }
  drawChart();
}

function addFrames(list) {
  for (const f of list) frames.push({ t: Date.parse(f.t), ra: f.ra, dec: f.dec });
  if (frames.length > MAX_FRAMES) frames = frames.slice(-MAX_FRAMES);
  scheduleChart();
}

let chartPending = false;
function scheduleChart() {
  if (chartPending) return;
  chartPending = true;
  requestAnimationFrame(() => { chartPending = false; drawChart(); });
}

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function drawChart() {
  const canvas = $("#chart");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const pad = { l: 40, r: 8, t: 8, b: 20 };
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  const windowMs = Number($("#window").value) * 60000;
  const tEnd = frames.length ? frames[frames.length - 1].t : Date.now();
  const tStart = tEnd - windowMs;
  let i0 = frames.length;
  while (i0 > 0 && frames[i0 - 1].t >= tStart) i0--;
  const view = frames.slice(i0);

  // y range: robust (98th percentile), at least ±4″, at most ±60″
  const abs = view.flatMap((f) => [Math.abs(f.ra), Math.abs(f.dec)]).sort((a, b) => a - b);
  const p98 = abs.length ? abs[Math.floor(abs.length * 0.98)] : 0;
  const yMax = Math.min(60, Math.max(4, Math.ceil(p98 * 1.2)));
  const x = (t) => pad.l + ((t - tStart) / windowMs) * pw;
  const y = (v) => pad.t + ph / 2 - (Math.max(-yMax, Math.min(yMax, v)) / yMax) * (ph / 2);

  ctx.font = "11px system-ui"; ctx.fillStyle = cssVar("--muted"); ctx.strokeStyle = cssVar("--grid"); ctx.lineWidth = 1;
  for (const v of [-yMax, -yMax / 2, 0, yMax / 2, yMax]) {
    ctx.beginPath(); ctx.moveTo(pad.l, y(v)); ctx.lineTo(w - pad.r, y(v)); ctx.stroke();
    ctx.fillText(`${v.toFixed(v % 1 ? 1 : 0)}″`, 2, y(v) + 4);
  }
  const step = windowMs <= 30 * 60000 ? 5 * 60000 : windowMs <= 60 * 60000 ? 10 * 60000 : 30 * 60000;
  for (let t = Math.ceil(tStart / step) * step; t <= tEnd; t += step) {
    ctx.beginPath(); ctx.moveTo(x(t), pad.t); ctx.lineTo(x(t), pad.t + ph); ctx.stroke();
    const d = new Date(t);
    ctx.fillText(`${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`, x(t) - 14, h - 5);
  }

  // alert markers
  for (const a of alerts) {
    const t = Date.parse(a.time);
    if (t < tStart || t > tEnd) continue;
    ctx.strokeStyle = cssVar(`--${a.severity.toLowerCase()}`); ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(x(t), pad.t); ctx.lineTo(x(t), pad.t + ph); ctx.stroke();
    ctx.setLineDash([]);
  }

  // series, decimated to about one point per pixel
  const stride = Math.max(1, Math.floor(view.length / pw));
  for (const [key, color] of [["ra", cssVar("--ra")], ["dec", cssVar("--dec")]]) {
    ctx.strokeStyle = color; ctx.lineWidth = 1.2; ctx.beginPath();
    let first = true, prevT = 0;
    for (let i = 0; i < view.length; i += stride) {
      const f = view[i];
      const gap = f.t - prevT > 15000;  // guiding stopped -> break the line
      if (first || gap) ctx.moveTo(x(f.t), y(f[key])); else ctx.lineTo(x(f.t), y(f[key]));
      first = false; prevT = f.t;
    }
    ctx.stroke();
  }
  if (!view.length) { ctx.fillStyle = cssVar("--muted"); ctx.fillText("Noch keine Guiding-Daten", pad.l + 10, pad.t + 20); }
}

// --- settings -----------------------------------------------------------------

async function loadSettings() {
  const [settings, schema] = await Promise.all([api("/api/settings"), api("/api/rules")]);
  const po = settings.pushover;
  $("#po-token").placeholder = po.token_set ? (po.from_env.token ? "aus Umgebungsvariable" : "gespeichert") : "nicht gesetzt";
  $("#po-user").placeholder = po.user_set ? (po.from_env.user ? "aus Umgebungsvariable" : "gespeichert") : "nicht gesetzt";
  $("#po-device").value = po.device || "";
  $("#po-min").value = po.min_severity;
  $("#push-replay").checked = !!po.in_replay;
  $("#site-lat").value = settings.site.latitude;
  $("#as-enabled").checked = settings.asiair.enabled;
  $("#as-host").value = settings.asiair.host;
  $("#as-port").value = settings.asiair.port;
  $("#as-record").checked = settings.asiair.record;
  $("#as-guider").checked = settings.asiair.guider;
  $("#as-guider-port").value = settings.asiair.guider_port;

  const smb = settings.smb;
  $("#smb-server").value = smb.server; $("#smb-shares").value = smb.shares.join("\n");
  $("#smb-user").value = smb.username; $("#smb-age").value = smb.max_age_h;
  $("#smb-pass").placeholder = smb.password_set ? "gespeichert" : "nicht gesetzt";
  smbLabel = smb.shares.map((s) => `//${smb.server}/${s}`).join(" und ");

  const src = settings.source;
  const loc = document.querySelector(`input[name=location][value=${src.location || "local"}]`);
  if (loc) loc.checked = true;
  document.querySelector(`input[name=mode][value=${src.mode}]`).checked = true;
  $("#speed").value = String(src.speed);
  if (!$("#speed").value) $("#speed").value = "60";
  $("#interval").value = src.interval_s;
  $("#from-start").checked = src.from_start;
  updateModeFields();
  await loadFolders(src.folder);

  const box = $("#rules");
  box.innerHTML = "";
  for (const rule of schema) {
    const conf = settings.rules[rule.name] || {};
    const div = document.createElement("div");
    div.className = "rule";
    div.dataset.rule = rule.name;
    div.innerHTML = `<label class="check rule-head"><input type="checkbox" data-enabled> <span></span></label>
      <div class="rule-doc"></div><div class="params"></div>`;
    div.querySelector("[data-enabled]").checked = conf.enabled !== false;
    div.querySelector(".rule-head span").textContent = RULE_LABELS[rule.name] || rule.name;
    div.querySelector(".rule-doc").textContent = rule.doc;
    for (const p of rule.params) {
      const label = document.createElement("label");
      label.textContent = p.name;
      const input = document.createElement("input");
      input.type = "number"; input.step = p.type === "int" ? "1" : "any";
      input.dataset.param = p.name;
      input.value = conf[p.name] ?? p.default;
      input.placeholder = p.default;
      label.appendChild(input);
      div.querySelector(".params").appendChild(label);
    }
    box.appendChild(div);
  }
}

async function savePushover(ev) {
  ev.preventDefault();
  const body = { pushover: { device: $("#po-device").value, min_severity: $("#po-min").value,
    in_replay: $("#push-replay").checked } };
  if ($("#po-token").value) body.pushover.token = $("#po-token").value;
  if ($("#po-user").value) body.pushover.user = $("#po-user").value;
  try {
    await api("/api/settings", { method: "PUT", body });
    $("#po-token").value = ""; $("#po-user").value = "";
    await loadSettings();
    toast("Pushover-Einstellungen gespeichert");
  } catch (e) { toast(`Speichern fehlgeschlagen: ${e.message}`); }
}

async function saveRules(ev) {
  ev.preventDefault();
  const rules = {};
  for (const div of document.querySelectorAll(".rule")) {
    const conf = { enabled: div.querySelector("[data-enabled]").checked };
    for (const input of div.querySelectorAll("[data-param]")) conf[input.dataset.param] = Number(input.value);
    rules[div.dataset.rule] = conf;
  }
  try {
    await api("/api/settings", { method: "PUT", body: { rules } });
    toast("Regeln gespeichert – gelten ab dem nächsten Start");
  } catch (e) { toast(`Speichern fehlgeschlagen: ${e.message}`); }
}

// --- streaming ----------------------------------------------------------------

function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => $("#conn").classList.add("ok");
  es.onerror = () => $("#conn").classList.remove("ok");
  es.addEventListener("status", (e) => renderStatus(JSON.parse(e.data)));
  es.addEventListener("frames", (e) => addFrames(JSON.parse(e.data)));
  es.addEventListener("alert", (e) => addAlert(JSON.parse(e.data)));
  es.addEventListener("notice", (e) => toast(JSON.parse(e.data).text));
  es.addEventListener("reset", () => { frames = []; alerts = []; renderAlerts(); });
}

async function init() {
  document.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", updateModeFields));
  $("#btn-start").addEventListener("click", start);
  $("#btn-pause").addEventListener("click", pauseResume);
  $("#btn-stop").addEventListener("click", () => api("/api/run/stop", { method: "POST" }));
  $("#speed").addEventListener("change", () => {
    if (last && ["running", "paused"].includes(last.run.status) && last.run.mode === "replay")
      api("/api/run/speed", { method: "POST", body: { speed: Number($("#speed").value) } });
  });
  $("#window").addEventListener("change", drawChart);
  $("#pushover-form").addEventListener("submit", savePushover);
  $("#po-test").addEventListener("click", async () => {
    try { await api("/api/pushover/test", { method: "POST" }); toast("Testnachricht gesendet"); }
    catch (e) { toast(`Test fehlgeschlagen: ${e.message}`); }
  });
  $("#site-save").addEventListener("click", async () => {
    try { await api("/api/settings", { method: "PUT", body: { site: { latitude: Number($("#site-lat").value) } } });
      toast("Standort gespeichert"); } catch (e) { toast(`Speichern fehlgeschlagen: ${e.message}`); }
  });
  document.querySelectorAll("input[name=location]").forEach((r) => r.addEventListener("change", updateModeFields));
  $("#smb-save").addEventListener("click", async () => {
    try { await saveSmb(); toast("SMB-Einstellungen gespeichert"); }
    catch (e) { toast(`Speichern fehlgeschlagen: ${e.message}`); }
  });
  $("#smb-test").addEventListener("click", testSmb);
  $("#as-save").addEventListener("click", async () => {
    const body = { asiair: { enabled: $("#as-enabled").checked, host: $("#as-host").value,
      port: Number($("#as-port").value), record: $("#as-record").checked,
      guider: $("#as-guider").checked, guider_port: Number($("#as-guider-port").value) } };
    try { await api("/api/settings", { method: "PUT", body });
      toast("ASIAIR-Einstellungen gespeichert – gelten ab dem nächsten Start"); }
    catch (e) { toast(`Speichern fehlgeschlagen: ${e.message}`); }
  });
  $("#rules-form").addEventListener("submit", saveRules);
  window.addEventListener("resize", drawChart);

  // three.js comes from a CDN; without internet the rest of the UI keeps working.
  import("./mount3d.js")
    .then(({ MountView }) => { mountView = new MountView($("#mount-view")); if (last) renderStatus(last); })
    .catch((e) => { $("#mount-view").textContent = "3D-Ansicht nicht verfügbar (three.js nicht geladen)"; console.error(e); });

  await loadSettings();
  const [frameList, alertList] = await Promise.all([api("/api/frames"), api("/api/alerts")]);
  addFrames(frameList);
  alerts = alertList;
  renderAlerts();
  connect();
}

init();

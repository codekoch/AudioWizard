// app.js
// ======
// Glue-Code der WebApp: Web MIDI + Web Audio einrichten, Analyse takten,
// Anzeige aktualisieren. BPM-Logik steckt in tempo.js, die Clock in
// midiclock.js.

// --- Stellschrauben (mirror der Python-Konstanten) -------------------------
const ANALYSIS_INTERVAL = 1.0;   // Sekunden zwischen zwei Tempo-Schaetzungen
const BPM_MEDIAN_LEN    = 16;    // Median ueber die letzten N Schaetzungen
const SILENCE_DB        = -55.0; // darunter: KEIN SIGNAL -> Clock haelt an
const DEFAULT_MIN_BPM   = 70;
const DEFAULT_MAX_BPM   = 140;

// --- Zustand ---------------------------------------------------------------
const clock = new MidiClock();
let midiAccess = null;
let audioCtx = null;
let workletNode = null;
let sinkNode = null;           // stumm geschalteter Gain-Knoten zum Ausgang
let mediaStream = null;
let analyzer = null;

let estimates = [];            // letzte gueltige BPM-Rohschaetzungen (Median)
let lastRaw = 0;               // letzte Rohschaetzung (Anzeige)
let levelEma = 0;              // geglaetteter Pegel (RMS)
let haveEstimate = false;
let running = false;
let analyzeTimer = null;
let lastChunkTime = 0;         // wann zuletzt Audio vom Eingang kam (Wächter)
let inputsUnlocked = false;    // volle, benannte Geraeteliste verfuegbar?
let unlocking = false;
let grantedDeviceId = '';      // zuletzt tatsaechlich freigegebenes Geraet
let grantedLabel = '';         //   (aus dem Audio-Track gelesen -- immer gueltig)

// --- DOM -------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const elMidi   = $('midiOut');
const elInput  = $('audioIn');
const elLoad   = $('loadInputs');
const elMin    = $('minBpm');
const elMax    = $('maxBpm');
const elStart  = $('startBtn');
const elBpm    = $('bpm');
const elRaw    = $('raw');
const elStatus = $('status');
const elBar    = $('levelBar');
const elDb     = $('db');
const elHint   = $('hint');

// ---------------------------------------------------------------------------
// Web MIDI
// ---------------------------------------------------------------------------
let midiRequested = false;     // MIDI-Zugriff schon angefragt?

// MIDI-Zugriff erst bei Bedarf anfragen (in-Kontext-Berechtigung), nicht
// schon beim Laden -- so erscheint der Prompt erst, wenn der Nutzer die
// MIDI-Liste oeffnet.
async function initMidi() {
  if (midiRequested) return;
  midiRequested = true;
  if (!navigator.requestMIDIAccess) {
    setHint('Web MIDI wird von diesem Browser nicht unterstuetzt. '
          + 'Empfohlen: Chrome, Edge oder Opera.', true);
    return;
  }
  try {
    midiAccess = await navigator.requestMIDIAccess({ sysex: false });
  } catch (e) {
    midiRequested = false;     // bei Ablehnung erneut versuchbar
    setHint('MIDI-Zugriff verweigert: ' + e.message, true);
    return;
  }
  refreshMidiOutputs();
  midiAccess.onstatechange = refreshMidiOutputs;
}

function refreshMidiOutputs() {
  const prev = elMidi.value;
  elMidi.innerHTML = '';
  const none = new Option('Kein MIDI (nur Anzeige)', '');
  elMidi.add(none);
  for (const out of midiAccess.outputs.values()) {
    elMidi.add(new Option(out.name, out.id));
  }
  // vorige Wahl moeglichst beibehalten
  elMidi.value = [...elMidi.options].some(o => o.value === prev) ? prev : '';
  applyMidiOutput();
}

function applyMidiOutput() {
  const id = elMidi.value;
  const out = id ? midiAccess.outputs.get(id) : null;
  clock.setOutput(out || null);
}

// ---------------------------------------------------------------------------
// Audio-Eingaenge
// ---------------------------------------------------------------------------
// Tatsaechlich freigegebenes Geraet aus dem Audio-Track lesen. Das ist
// zuverlaessiger als enumerateDevices(): manche Browser (z. B. Firefox mit
// "Dieses Mal erlauben") liefern direkt nach der Freigabe noch leere
// Geraete-IDs -- die ID/der Name des erhaltenen Tracks stimmen aber immer.
function captureGranted(stream) {
  try {
    const t = stream.getAudioTracks()[0];
    if (!t) return;
    const s = t.getSettings ? t.getSettings() : {};
    if (s.deviceId) grantedDeviceId = s.deviceId;
    if (t.label) grantedLabel = t.label;
  } catch (e) { /* ignorieren */ }
}

async function refreshAudioInputs() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return 0;
  const devices = await navigator.mediaDevices.enumerateDevices();
  const prev = elInput.value;
  elInput.innerHTML = '';
  elInput.add(new Option('Standard-Eingang', ''));   // = Browser-Standard
  const seen = new Set(['']);
  let count = 0, i = 1;

  // Das gerade freigegebene Geraet auf jeden Fall anbieten (s. captureGranted).
  if (grantedDeviceId && !seen.has(grantedDeviceId)) {
    elInput.add(new Option(grantedLabel || 'Freigegebener Eingang', grantedDeviceId));
    seen.add(grantedDeviceId);
    count++;
  }

  for (const d of devices) {
    if (d.kind !== 'audioinput') continue;
    if (!d.deviceId) continue;                       // ohne ID nicht waehlbar
    if (d.deviceId === 'communications') continue;   // Windows-Duplikat
    if (seen.has(d.deviceId)) continue;
    // Namen erscheinen erst nach erteilter Mikrofon-Freigabe; davor liefert
    // der Browser nur einen anonymen Platzhalter.
    const label = d.label || ('Eingang ' + (i++) + ' (Name nach Freigabe)');
    elInput.add(new Option(label, d.deviceId));
    seen.add(d.deviceId);
    count++;
  }

  elInput.value = [...elInput.options].some(o => o.value === prev) ? prev : '';
  return count;
}

// Mikrofon einmalig freigeben, damit der Browser die VOLLE, benannte
// Eingangsliste herausgibt (vorher zeigt er aus Datenschutzgruenden nur
// einen Platzhalter). Die temporaere Aufnahme wird sofort wieder gestoppt.
async function unlockInputs() {
  if (inputsUnlocked || unlocking) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
  unlocking = true;
  elLoad.disabled = true;
  setHint('Eingaenge werden geladen … bitte den Mikrofon-Zugriff erlauben.');
  let tmp = null;
  try {
    tmp = await navigator.mediaDevices.getUserMedia({ audio: true });
    captureGranted(tmp);
    inputsUnlocked = true;
    const n = await refreshAudioInputs();
    elLoad.textContent = 'Aktualisieren';
    setHint(n + ' Eingang/Eingaenge geladen. Gewuenschten Eingang waehlen '
          + 'und Start druecken.');
  } catch (e) {
    setHint('Mikrofon-Zugriff abgelehnt — ohne Freigabe zeigt der Browser nur '
          + 'den Standard-Eingang. (' + e.message + ')', true);
  } finally {
    if (tmp) tmp.getTracks().forEach(t => t.stop());
    unlocking = false;
    if (!running) elLoad.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Sitzung starten / stoppen
// ---------------------------------------------------------------------------
async function start() {
  const minBpm = clampBpm(parseFloat(elMin.value), DEFAULT_MIN_BPM);
  const maxBpm = clampBpm(parseFloat(elMax.value), DEFAULT_MAX_BPM);
  if (!(minBpm < maxBpm)) {
    setHint('BPM-Bereich ungueltig: von < bis (z. B. 70 bis 140).', true);
    return;
  }

  try {
    const constraints = {
      audio: {
        echoCancellation: false,        // fuer Musikanalyse stoeren diese
        noiseSuppression: false,        // "Verbesserungen" -- daher aus
        autoGainControl: false,
      },
    };
    if (elInput.value) constraints.audio.deviceId = { exact: elInput.value };
    mediaStream = await navigator.mediaDevices.getUserMedia(constraints);
  } catch (e) {
    setHint('Mikrofon-/Eingangszugriff fehlgeschlagen: ' + e.message
          + '  — anderen Eingang waehlen oder Zugriff erlauben.', true);
    setStatus('FEHLER');
    return;
  }

  // Geraetenamen sind erst nach erteilter Berechtigung sichtbar.
  captureGranted(mediaStream);
  inputsUnlocked = true;
  refreshAudioInputs();

  try {
    audioCtx = new AudioContext();
    await audioCtx.audioWorklet.addModule('capture-worklet.js');
    await audioCtx.resume();

    analyzer = new TempoAnalyzer(audioCtx.sampleRate, minBpm, maxBpm);

    const src = audioCtx.createMediaStreamSource(mediaStream);
    workletNode = new AudioWorkletNode(audioCtx, 'capture-processor');
    workletNode.port.onmessage = (ev) => onChunk(ev.data);
    src.connect(workletNode);
    // Den Worklet-Knoten in einen Pfad zum Ausgang haengen, damit der Graph
    // zuverlaessig (browserunabhaengig) gerendert wird -- manche Chrome-
    // Versionen verarbeiten einen nicht angeschlossenen Zweig nicht. Der
    // Knoten gibt selbst nur Stille aus, der Gain ist auf 0: kein Ton, keine
    // Rueckkopplung.
    sinkNode = audioCtx.createGain();
    sinkNode.gain.value = 0;
    workletNode.connect(sinkNode);
    sinkNode.connect(audioCtx.destination);
  } catch (e) {
    setHint('Audio konnte nicht gestartet werden: ' + e.message, true);
    setStatus('FEHLER');
    if (audioCtx) { try { await audioCtx.close(); } catch (_) {} audioCtx = null; }
    if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
    workletNode = null; sinkNode = null; analyzer = null;
    return;
  }

  estimates = [];
  lastRaw = 0;
  levelEma = 0;
  haveEstimate = false;

  clock.enable();
  analyzeTimer = setInterval(analyze, ANALYSIS_INTERVAL * 1000);

  lastChunkTime = performance.now();
  running = true;
  elStart.textContent = 'Stopp';
  elStart.classList.add('stop');
  setControlsDisabled(true);
  setHint('Laeuft. Tab im Vordergrund lassen, damit die Clock stabil bleibt.');
}

function stop() {
  running = false;
  if (analyzeTimer) { clearInterval(analyzeTimer); analyzeTimer = null; }
  clock.disable();
  if (workletNode) { workletNode.disconnect(); workletNode = null; }
  if (sinkNode) { sinkNode.disconnect(); sinkNode = null; }
  if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
  analyzer = null;
  haveEstimate = false;
  estimates = [];

  elStart.textContent = 'Start';
  elStart.classList.remove('stop');
  setControlsDisabled(false);
  setBpmText('—');
  elRaw.textContent = 'roh: —';
  setStatus('bereit');
  updateLevel(-60);
}

// ---------------------------------------------------------------------------
// Audio-Bloecke + Analyse
// ---------------------------------------------------------------------------
function onChunk(chunk) {
  if (!analyzer) return;
  lastChunkTime = performance.now();
  analyzer.pushChunk(chunk);
  // Pegel (RMS) fuer Anzeige und Stille-Erkennung mitfuehren.
  let sum = 0;
  for (let i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
  const rms = Math.sqrt(sum / chunk.length);
  levelEma = 0.85 * levelEma + 0.15 * rms;
}

function analyze() {
  if (!analyzer) return;
  const db = levelEma > 0 ? 20 * Math.log10(levelEma) : -120;

  if (db < SILENCE_DB) {
    // Stille -> Historie verwerfen, Clock anhalten.
    estimates = [];
    haveEstimate = false;
    clock.setTargetBpm(null);
    return;
  }

  const est = analyzer.estimate();
  if (est) {
    lastRaw = est.bpm;
    estimates.push(est.bpm);
    if (estimates.length > BPM_MEDIAN_LEN) {
      estimates.splice(0, estimates.length - BPM_MEDIAN_LEN);
    }
    const med = median(estimates);
    haveEstimate = true;
    clock.setTargetBpm(med);
  }
  // Bei schwacher Periodizitaet (est == null) bleibt die letzte gueltige
  // Schaetzung erhalten -- wie im Original wird nicht "geraten".
}

// ---------------------------------------------------------------------------
// Anzeige (~10x/s)
// ---------------------------------------------------------------------------
function render() {
  if (running) {
    const db = levelEma > 0 ? 20 * Math.log10(levelEma) : -120;
    updateLevel(db);
    // Wächter: kommen ueberhaupt Audio-Bloecke? Tote Quelle (Geraet liefert
    // nichts) von echter Stille (Quelle da, aber leise) unterscheiden.
    if (performance.now() - lastChunkTime > 1500) {
      setStatus('KEIN AUDIO – Eingang prüfen');
      setBpmText('—');
    } else if (db < SILENCE_DB) {
      setStatus('KEIN SIGNAL');
      setBpmText('—');
    } else if (!haveEstimate) {
      setStatus('analysiere …');
      setBpmText('—');
    } else {
      setStatus('laeuft');
      setBpmText(String(Math.round(median(estimates))));
    }
    elRaw.textContent = 'roh: ' + (lastRaw ? lastRaw.toFixed(1) : '—');
  }
  requestAnimationFrame(render);
}

// ---------------------------------------------------------------------------
// Hilfsfunktionen
// ---------------------------------------------------------------------------
function median(arr) {
  if (!arr.length) return 0;
  const s = [...arr].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : 0.5 * (s[m - 1] + s[m]);
}

function clampBpm(v, fallback) {
  if (!isFinite(v)) return fallback;
  return Math.min(300, Math.max(30, v));
}

function setStatus(txt) { elStatus.textContent = txt; }

function setBpmText(txt) {
  elBpm.textContent = txt;
  elBpm.classList.toggle('idle', txt === '—');   // Platzhalter gedimmt
}

function setHint(txt, isError) {
  elHint.textContent = txt;
  elHint.classList.toggle('error', !!isError);
}

function updateLevel(db) {
  // -60..0 dB auf 0..100 % abbilden.
  const pct = Math.max(0, Math.min(100, (db + 60) / 60 * 100));
  elBar.style.width = pct + '%';
  elDb.textContent = (db <= -60 ? '-60' : db.toFixed(0)) + ' dB';
}

function setControlsDisabled(on) {
  elInput.disabled = on;
  elLoad.disabled = on;
  elMin.disabled = on;
  elMax.disabled = on;
  // MIDI-Ausgang darf auch im Lauf umgestellt werden.
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
elStart.addEventListener('click', () => running ? stop() : start());
elMidi.addEventListener('change', applyMidiOutput);
elLoad.addEventListener('click', () => { inputsUnlocked = false; unlockInputs(); });
// Beim ersten Antippen der Liste die Freigabe gleich mit anbieten.
elInput.addEventListener('focus', () => { if (!inputsUnlocked) unlockInputs(); });
// MIDI-Liste beim ersten Oeffnen befuellen (fordert dann den MIDI-Zugriff an).
elMidi.addEventListener('focus', initMidi);

window.addEventListener('DOMContentLoaded', () => {
  if (!window.isSecureContext) {
    setHint('Diese Seite muss ueber http://localhost oder HTTPS laufen -- '
          + 'sonst sind Mikrofon und MIDI gesperrt. Siehe README.', true);
  }
  elMidi.add(new Option('Kein MIDI (Liste öffnen zum Verbinden)', ''));
  refreshAudioInputs();
  setBpmText('—');
  setStatus('bereit');
  updateLevel(-60);
  setHint('„Eingänge laden“ klicken, um alle Audio-Eingänge mit Namen zu sehen. '
        + 'MIDI-Liste öffnen, um den MIDI-Ausgang zu wählen.');
  requestAnimationFrame(render);
});

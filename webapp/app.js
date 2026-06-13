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
let mediaStream = null;
let analyzer = null;

let estimates = [];            // letzte gueltige BPM-Rohschaetzungen (Median)
let lastRaw = 0;               // letzte Rohschaetzung (Anzeige)
let levelEma = 0;              // geglaetteter Pegel (RMS)
let haveEstimate = false;
let running = false;
let analyzeTimer = null;

// --- DOM -------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const elMidi   = $('midiOut');
const elInput  = $('audioIn');
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
async function initMidi() {
  if (!navigator.requestMIDIAccess) {
    setHint('Web MIDI wird von diesem Browser nicht unterstuetzt. '
          + 'Empfohlen: Chrome, Edge oder Opera.', true);
    return;
  }
  try {
    midiAccess = await navigator.requestMIDIAccess({ sysex: false });
  } catch (e) {
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
async function refreshAudioInputs() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  const devices = await navigator.mediaDevices.enumerateDevices();
  const prev = elInput.value;
  elInput.innerHTML = '';
  elInput.add(new Option('Standard-Eingang', ''));
  let i = 1;
  for (const d of devices) {
    if (d.kind === 'audioinput') {
      const label = d.label || ('Eingang ' + i++);
      elInput.add(new Option(label, d.deviceId));
    }
  }
  elInput.value = [...elInput.options].some(o => o.value === prev) ? prev : '';
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
    setHint('Mikrofon-/Eingangszugriff fehlgeschlagen: ' + e.message, true);
    return;
  }

  // Geraetenamen sind erst nach erteilter Berechtigung sichtbar.
  refreshAudioInputs();

  audioCtx = new AudioContext();
  await audioCtx.audioWorklet.addModule('capture-worklet.js');
  await audioCtx.resume();

  analyzer = new TempoAnalyzer(audioCtx.sampleRate, minBpm, maxBpm);

  const src = audioCtx.createMediaStreamSource(mediaStream);
  workletNode = new AudioWorkletNode(audioCtx, 'capture-processor');
  workletNode.port.onmessage = (ev) => onChunk(ev.data);
  src.connect(workletNode);
  // Worklet muss nicht zu den Lautsprechern -- kein connect zum Ausgang
  // (verhindert Rueckkopplung/Echo).

  estimates = [];
  lastRaw = 0;
  levelEma = 0;
  haveEstimate = false;

  clock.enable();
  analyzeTimer = setInterval(analyze, ANALYSIS_INTERVAL * 1000);

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
  if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
  analyzer = null;
  haveEstimate = false;
  estimates = [];

  elStart.textContent = 'Start';
  elStart.classList.remove('stop');
  setControlsDisabled(false);
  elBpm.textContent = '—';
  elRaw.textContent = 'roh: —';
  setStatus('bereit');
  updateLevel(-60);
}

// ---------------------------------------------------------------------------
// Audio-Bloecke + Analyse
// ---------------------------------------------------------------------------
function onChunk(chunk) {
  if (!analyzer) return;
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
    if (db < SILENCE_DB) {
      setStatus('KEIN SIGNAL');
      elBpm.textContent = '—';
    } else if (!haveEstimate) {
      setStatus('analysiere …');
      elBpm.textContent = '—';
    } else {
      setStatus('laeuft');
      elBpm.textContent = String(Math.round(median(estimates)));
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
  elMin.disabled = on;
  elMax.disabled = on;
  // MIDI-Ausgang darf auch im Lauf umgestellt werden.
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
elStart.addEventListener('click', () => running ? stop() : start());
elMidi.addEventListener('change', applyMidiOutput);

window.addEventListener('DOMContentLoaded', () => {
  if (!window.isSecureContext) {
    setHint('Diese Seite muss ueber http://localhost oder HTTPS laufen -- '
          + 'sonst sind Mikrofon und MIDI gesperrt. Siehe README.', true);
  }
  initMidi();
  refreshAudioInputs();
  setStatus('bereit');
  updateLevel(-60);
  requestAnimationFrame(render);
});

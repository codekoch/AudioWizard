// midiclock.js
// ============
// Stabile MIDI-Clock (24 PPQN) ueber die Web MIDI API -- portierte Idee aus
// clock_worker() in realtime_bpm_key_midiclock.py.
//
// Der Trick fuer die Stabilitaet: Jeder Tick wird mit Web MIDI
// output.send([0xF8], zeitstempel) IM VORAUS geplant. Der Zeitstempel liegt in
// der performance.now()-Domaene (ms); das MIDI-Subsystem des Browsers/OS
// gibt die Nachricht dann zeitgenau aus -- unabhaengig davon, wann der
// JavaScript-Timer gerade aufwacht. Damit haengt die Tick-Stabilitaet nicht am
// (ruckeligen) JS-Scheduling, sondern an der OS-MIDI-Schicht.
//
// Ein Lookahead-Scheduler fuellt die Tick-Warteschlange laufend ein Stueck in
// die Zukunft. Sein Timer laeuft in einem Web Worker, damit er auch bei nicht
// fokussiertem Tab seltener gedrosselt wird als ein Haupt-Thread-setInterval.
//
// Tempofuehrung wie im Original: Totband gegen Mess-Zittern und begrenzte
// Slew-Rate (sanftes Aufholen statt Tick-Bursts).

const PPQN          = 24;     // MIDI-Clock: 24 Pulse pro Viertelnote
const LOOKAHEAD_MS  = 120;    // so weit im Voraus werden Ticks geplant
const TICK_MS       = 25;     // Aufweck-Intervall des Schedulers
const SLEW_BPM_PER_S = 8.0;   // max. Tempo-Aenderung pro Sekunde
const DEADBAND_BPM  = 0.15;   // kleinere Soll-Aenderungen werden ignoriert

// MIDI-System-Realtime-Bytes
const MSG_CLOCK = 0xF8;
const MSG_START = 0xFA;
const MSG_STOP  = 0xFC;

class MidiClock {
  constructor() {
    this.output = null;
    this.enabled = false;       // Master-Schalter (Sitzung laeuft)
    this.targetBpm = null;      // null = keine Schaetzung -> Clock haelt an
    this.curBpm = 120;
    this.running = false;       // MIDI 'start' gesendet, Ticks laufen
    this.nextTick = 0;          // naechster Tick-Zeitpunkt (performance.now ms)
    this._lastSched = 0;

    // Scheduler-Timer in einem Worker (weniger Throttling im Hintergrund).
    const code = "let t=null;onmessage=e=>{" +
      "if(e.data==='start'){if(!t)t=setInterval(()=>postMessage(0)," + TICK_MS + ");}" +
      "else{clearInterval(t);t=null;}};";
    const blob = new Blob([code], { type: 'application/javascript' });
    this.worker = new Worker(URL.createObjectURL(blob));
    this.worker.onmessage = () => this._schedule();
  }

  setOutput(output) {
    if (this.output === output) return;
    // Laufende Clock am alten Port sauber stoppen.
    if (this.running) this._send(MSG_STOP);
    this.running = false;
    this.output = output;
  }

  // Sitzung starten/stoppen (Audio laeuft / laeuft nicht).
  enable() {
    if (this.enabled) return;
    this.enabled = true;
    this._lastSched = performance.now();
    this.worker.postMessage('start');
  }

  disable() {
    this.enabled = false;
    this.worker.postMessage('stop');
    if (this.running) this._send(MSG_STOP);
    this.running = false;
  }

  // Soll-Tempo setzen; null bedeutet "keine gueltige Schaetzung".
  setTargetBpm(bpm) {
    this.targetBpm = bpm;
  }

  _send(status, timestamp) {
    if (!this.output) return;
    try {
      if (timestamp === undefined) this.output.send([status]);
      else this.output.send([status], timestamp);
    } catch (e) { /* Port verschwunden o.ae. -- still ignorieren */ }
  }

  _schedule() {
    if (!this.enabled) return;
    const now = performance.now();
    const dt = Math.max(0, (now - this._lastSched) / 1000);
    this._lastSched = now;

    // Keine Schaetzung -> Clock anhalten (MIDI 'stop').
    if (this.targetBpm == null) {
      if (this.running) { this._send(MSG_STOP); this.running = false; }
      return;
    }

    // Erste Schaetzung -> Clock direkt im erkannten Tempo starten.
    if (!this.running) {
      this.running = true;
      this.curBpm = this.targetBpm;
      this.nextTick = now + 20;            // erster Tick knapp in der Zukunft
      this._send(MSG_START);
    } else {
      // Tempo nachfuehren: Totband + begrenzte Slew-Rate.
      const diff = this.targetBpm - this.curBpm;
      if (Math.abs(diff) > DEADBAND_BPM) {
        const maxStep = SLEW_BPM_PER_S * dt;
        this.curBpm += Math.max(-maxStep, Math.min(maxStep, diff));
      }
    }

    // Nach einer Haengepartie nicht hunderte Ticks nachholen, sondern
    // sauber resynchronisieren (sanftes Aufholen statt Tick-Burst).
    if (this.nextTick < now - LOOKAHEAD_MS) {
      this.nextTick = now;
    }

    // Ticks bis zum Lookahead-Horizont im Voraus planen.
    const horizon = now + LOOKAHEAD_MS;
    while (this.nextTick < horizon) {
      this._send(MSG_CLOCK, this.nextTick);
      this.nextTick += 60000 / (this.curBpm * PPQN);
    }
  }
}

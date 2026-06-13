// capture-worklet.js
// ===================
// AudioWorklet-Prozessor: mischt den Eingang auf Mono herunter und schickt
// dem Hauptthread fortlaufend Bloecke von genau HOP Samples. Die eigentliche
// Analyse (FFT, Onset-Huellkurve, Autokorrelation) laeuft im Hauptthread --
// hier wird nur gepuffert, damit die Bloecke unabhaengig von der
// Render-Quantum-Groesse (128) eine feste Laenge haben.
//
// Laeuft im Audio-Render-Thread: keine Allokationen im heissen Pfad ausser
// dem postMessage-Transfer der fertigen Bloecke.

const HOP = 512;

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf = new Float32Array(HOP);
    this._idx = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) {
      return true;                       // kein Eingang verbunden -> weiterlaufen
    }
    const chans = input.length;
    const n = input[0].length;
    for (let i = 0; i < n; i++) {
      // Downmix auf Mono (Mittelwert ueber alle Kanaele)
      let s = 0;
      for (let c = 0; c < chans; c++) {
        s += input[c][i];
      }
      this._buf[this._idx++] = s / chans;
      if (this._idx >= HOP) {
        // Kopie senden (der interne Puffer wird sofort weiterbeschrieben)
        this.port.postMessage(this._buf.slice(0));
        this._idx = 0;
      }
    }
    return true;
  }
}

registerProcessor('capture-processor', CaptureProcessor);

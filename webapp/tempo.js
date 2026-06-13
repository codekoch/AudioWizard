// tempo.js
// ========
// BPM-Erkennung im Browser -- portierte Logik aus realtime_bpm_key_midiclock.py
// (nur der Tempo-Teil). Statt der teuren HPSS-Trennung wird eine
// Spektralfluss-Onset-Huellkurve verwendet: sie hebt breitbandige Transienten
// (Drums) von selbst hervor und ist im Browser guenstig zu rechnen. Der Rest
// folgt dem Original: Autokorrelation der Huellkurve, Kammfilter-Stuetzung der
// Kandidaten durch ihre Vielfachen, sanfter Oktav-Prior, Median ueber die
// letzten Schaetzungen.
//
// Globale Klassen (kein ES-Modul, per <script> eingebunden): FFT, TempoAnalyzer.

// --- Stellschrauben (mirror der Python-Konstanten) -------------------------
const WINDOW_SECONDS   = 8.0;    // Laenge des Analysefensters
const FFT_SIZE         = 2048;   // STFT-Fenster fuer den Spektralfluss
const HOP_SIZE         = 512;    // Hop der Onset-Huellkurve (= Worklet-Blocklaenge)
const TEMPO_PRIOR_OCT  = 0.9;    // Breite des Oktav-Priors in Oktaven
const CONF_RATIO       = 1.45;   // Mindest-Verhaeltnis Spitze/Mittel der ACF-Scores
const MIN_ONSET_SUM    = 1e-3;   // darunter gilt das Fenster als zu leise/leer

// ---------------------------------------------------------------------------
// Iterative Radix-2-FFT (in-place, reelle Eingabe ueber im=0).
// ---------------------------------------------------------------------------
class FFT {
  constructor(n) {
    this.n = n;
    this.cos = new Float32Array(n / 2);
    this.sin = new Float32Array(n / 2);
    for (let i = 0; i < n / 2; i++) {
      this.cos[i] = Math.cos(-2 * Math.PI * i / n);
      this.sin[i] = Math.sin(-2 * Math.PI * i / n);
    }
    const bits = Math.round(Math.log2(n));
    this.rev = new Uint32Array(n);
    for (let i = 0; i < n; i++) {
      let x = i, r = 0;
      for (let j = 0; j < bits; j++) { r = (r << 1) | (x & 1); x >>= 1; }
      this.rev[i] = r >>> 0;
    }
  }

  // re, im: Float32Array der Laenge n -- wird in-place ueberschrieben.
  transform(re, im) {
    const n = this.n, rev = this.rev;
    for (let i = 0; i < n; i++) {
      const j = rev[i];
      if (j > i) {
        let t = re[i]; re[i] = re[j]; re[j] = t;
        t = im[i]; im[i] = im[j]; im[j] = t;
      }
    }
    for (let size = 2; size <= n; size <<= 1) {
      const half = size >> 1;
      const step = (n / size) | 0;
      for (let i = 0; i < n; i += size) {
        for (let k = 0; k < half; k++) {
          const ci = k * step;
          const c = this.cos[ci], s = this.sin[ci];
          const a = i + k, b = a + half;
          const tre = re[b] * c - im[b] * s;
          const tim = re[b] * s + im[b] * c;
          re[b] = re[a] - tre; im[b] = im[a] - tim;
          re[a] += tre;        im[a] += tim;
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// TempoAnalyzer: nimmt fortlaufend Sample-Bloecke, fuehrt eine rollende
// Onset-Huellkurve und liefert auf Anfrage eine Tempo-Schaetzung.
// ---------------------------------------------------------------------------
class TempoAnalyzer {
  constructor(sampleRate, minBpm, maxBpm) {
    this.sr = sampleRate;
    this.minBpm = minBpm;
    this.maxBpm = maxBpm;
    this.envRate = sampleRate / HOP_SIZE;          // Huellkurven-Abtastrate (Hz)
    this.envLen = Math.round(WINDOW_SECONDS * this.envRate);
    this.centerBpm = Math.sqrt(minBpm * maxBpm);   // geometrische Mitte = Prior

    this.fft = new FFT(FFT_SIZE);
    this.win = new Float32Array(FFT_SIZE);         // Hann-Fenster
    for (let i = 0; i < FFT_SIZE; i++) {
      this.win[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (FFT_SIZE - 1));
    }
    this.frame = new Float32Array(FFT_SIZE);       // gleitendes Zeitfenster
    this.re = new Float32Array(FFT_SIZE);
    this.im = new Float32Array(FFT_SIZE);
    this.prevMag = new Float32Array(FFT_SIZE / 2);
    this.onset = [];                               // rollende Onset-Huellkurve
    this._havePrev = false;
  }

  // Einen HOP_SIZE langen Sample-Block einspeisen.
  pushChunk(chunk) {
    // Gleitendes Fenster um HOP_SIZE weiterschieben und neuen Block anhaengen.
    this.frame.copyWithin(0, HOP_SIZE);
    this.frame.set(chunk, FFT_SIZE - HOP_SIZE);

    // Fensterung + FFT
    const re = this.re, im = this.im, win = this.win;
    for (let i = 0; i < FFT_SIZE; i++) { re[i] = this.frame[i] * win[i]; im[i] = 0; }
    this.fft.transform(re, im);

    // Spektralfluss: Summe der positiven Betragszuwaechse gegenueber dem
    // vorherigen Frame (halbweg-gleichgerichtet) -> betont Onsets.
    const half = FFT_SIZE / 2;
    let flux = 0;
    const prev = this.prevMag;
    for (let k = 0; k < half; k++) {
      const mag = Math.hypot(re[k], im[k]);
      const d = mag - prev[k];
      if (d > 0) flux += d;
      prev[k] = mag;
    }
    if (this._havePrev) {
      this.onset.push(flux);
      if (this.onset.length > this.envLen) {
        this.onset.splice(0, this.onset.length - this.envLen);
      }
    }
    this._havePrev = true;
  }

  setRange(minBpm, maxBpm) {
    this.minBpm = minBpm;
    this.maxBpm = maxBpm;
    this.centerBpm = Math.sqrt(minBpm * maxBpm);
  }

  // Schaetzung aus der aktuellen Huellkurve. Rueckgabe {bpm, conf} oder null,
  // wenn zu wenig Daten / zu schwache Periodizitaet (dann lieber nichts).
  estimate() {
    const e = this.onset;
    const L = e.length;
    if (L < this.envRate * 3) return null;         // < ~3 s Material

    // Mittelwert entfernen (Hochpass), Aktivitaet pruefen.
    let mean = 0;
    for (let i = 0; i < L; i++) mean += e[i];
    mean /= L;
    if (mean < MIN_ONSET_SUM) return null;         // praktisch Stille

    const x = new Float32Array(L);
    let energy = 0;
    for (let i = 0; i < L; i++) { x[i] = e[i] - mean; energy += x[i] * x[i]; }
    if (energy <= 0) return null;

    // Such-Lags entsprechen genau dem [minBpm, maxBpm]-Bereich.
    const lagMin = Math.max(2, Math.floor(60 * this.envRate / this.maxBpm));
    const lagMax = Math.min(L - 2, Math.ceil(60 * this.envRate / this.minBpm));
    if (lagMax <= lagMin) return null;

    // Autokorrelation bis zur 4. Harmonischen (fuer die Kammfilter-Stuetzung).
    const acfMax = Math.min(L - 2, lagMax * 4);
    const acf = new Float32Array(acfMax + 1);
    for (let lag = lagMin; lag <= acfMax; lag++) {
      let s = 0;
      for (let i = 0; i + lag < L; i++) s += x[i] * x[i + lag];
      acf[lag] = s / energy;                       // normiert (~[-1,1])
    }

    // Kandidaten bewerten: Grundlag + gewichtete Vielfache, dann Oktav-Prior.
    const harmW = [1.0, 0.6, 0.4, 0.25];           // Gewichte fuer 1x..4x
    let best = -Infinity, bestLag = -1, scoreSum = 0, scoreCnt = 0;
    const priorW = TEMPO_PRIOR_OCT;
    for (let lag = lagMin; lag <= lagMax; lag++) {
      let comb = 0;
      for (let h = 1; h <= 4; h++) {
        const hl = lag * h;
        if (hl <= acfMax) comb += harmW[h - 1] * acf[hl];
      }
      const bpm = 60 * this.envRate / lag;
      const lo = Math.log2(bpm / this.centerBpm) / priorW;
      const prior = Math.exp(-0.5 * lo * lo);
      const score = comb * prior;
      scoreSum += score; scoreCnt++;
      if (score > best) { best = score; bestLag = lag; }
    }
    if (bestLag < 0) return null;

    // Vertrauen: Verhaeltnis der Spitze zum mittleren Score (skaleninvariant).
    const meanScore = scoreSum / Math.max(1, scoreCnt);
    const ratio = meanScore > 0 ? best / meanScore : 0;
    if (best <= 0 || ratio < CONF_RATIO) return null;

    // Parabel-Interpolation um das ACF-Maximum -> Sub-Lag-Genauigkeit.
    let lag = bestLag;
    const y0 = acf[bestLag - 1], y1 = acf[bestLag], y2 = acf[bestLag + 1];
    const denom = (y0 - 2 * y1 + y2);
    if (denom !== 0) {
      const delta = 0.5 * (y0 - y2) / denom;
      if (delta > -1 && delta < 1) lag = bestLag + delta;
    }
    const bpm = 60 * this.envRate / lag;
    return { bpm, conf: ratio };
  }
}

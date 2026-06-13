# Audio2Midi -- WebApp (BPM + MIDI-Clock)

Eine schlanke Browser-Variante des Projekts: **nur** Tempo-Erkennung (BPM)
mit **stabiler MIDI-Clock-Ausgabe** (24 PPQN). Keine Installation, kein
Python -- läuft direkt im Browser über die **Web Audio API** (Eingang) und
die **Web MIDI API** (Clock-Ausgabe).

Tonart, Akkorde, Loopback und die Kiosk-Oberfläche bleiben den
Python-Versionen vorbehalten; diese WebApp ist bewusst auf den Kern
reduziert.

## Voraussetzungen

- **Browser mit Web MIDI:** Chrome, Edge oder Opera (Chromium).
  Firefox unterstützt Web MIDI nur mit Erweiterung; **Safari unterstützt es
  nicht** – dort gibt es keine MIDI-Ausgabe.
- **Sicherer Kontext:** Mikrofon und MIDI funktionieren nur über
  `http://localhost` oder HTTPS. Ein direkter Doppelklick auf `index.html`
  (`file://`) reicht **nicht**.
- **Virtueller MIDI-Port**, um eine DAW/Hardware auf demselben Rechner zu
  erreichen: Windows
  [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html),
  macOS der IAC-Treiber (Audio-MIDI-Setup), Linux ALSA/`snd-virmidi`.
  Ein USB-MIDI-Interface geht direkt.

## Starten

Im Ordner `webapp/` einen lokalen Webserver starten -- z. B. mit dem schon
vorhandenen Python:

```bash
cd webapp
python -m http.server 8000
```

Dann im Browser **http://localhost:8000** öffnen.

1. **MIDI-Ausgang** wählen (z. B. „loopMIDI Port"). „Kein MIDI" zeigt nur an.
2. Optional einen **Audio-Eingang** und den **BPM-Bereich** wählen
   (Standard 70–140, genau eine Oktave – macht die Tempo-Zuordnung eindeutig).
3. **Start** drücken und den Mikrofon-Zugriff erlauben.

Die große Zahl zeigt das erkannte Tempo; die MIDI-Clock startet automatisch
mit der ersten stabilen Schätzung (MIDI `start`) und hält bei Stille an
(`stop`).

## Warum die Clock stabil ist

Jeder Clock-Tick wird mit `output.send([0xF8], zeitstempel)` **im Voraus
geplant** – der Zeitstempel liegt in der `performance.now()`-Domäne, und das
MIDI-Subsystem des Browsers/Betriebssystems gibt den Tick dann zeitgenau aus.
Die Tick-Stabilität hängt damit nicht am (ungenauen) JavaScript-Timer,
sondern an der OS-MIDI-Schicht – derselbe Gedanke wie bei CoreMIDI auf dem
Mac. Ein Lookahead-Scheduler (Timer in einem Web Worker, damit er im
Hintergrund seltener gedrosselt wird) füllt die Tick-Warteschlange laufend
ein Stück in die Zukunft. Tempoänderungen werden mit Totband (gegen
Mess-Zittern) und begrenzter Slew-Rate sanft nachgeführt – kein Tick-Burst.

**Tipp:** Den Tab im Vordergrund lassen. Hintergrund-Tabs werden vom Browser
gedrosselt; durch den Lookahead bleibt die Clock zwar eine Weile stabil,
für den Live-Betrieb sollte der Tab aber sichtbar bleiben.

## Grenzen gegenüber den Python-Versionen

- **Kein Mithören der Wiedergabe** (Spotify o. ä.): Browser dürfen die
  Ausgabe anderer Apps nicht systemweit mitschneiden. Quelle ist immer
  Mikrofon oder ein Audio-Interface (Class-Compliant, ohne Treiber).
- **Vereinfachte Analyse:** Statt der HPSS-Trennung dient eine
  Spektralfluss-Onset-Hüllkurve als Grundlage der Autokorrelation. Für
  rhythmisches Material ist das robust; bei sehr flächigem/perkussionsarmem
  Material ist die Python-Version etwas treffsicherer.
- **Nur BPM**, keine Tonart/Akkorde, kein Beat-Sync.

## Dateien

| Datei | Zweck |
|-------|-------|
| `index.html` | Oberfläche |
| `style.css` | dunkles Thema (an die Kiosk-Anzeige angelehnt) |
| `capture-worklet.js` | AudioWorklet: Mono-Downmix, liefert 512-Sample-Blöcke |
| `tempo.js` | FFT, Onset-Hüllkurve, Autokorrelation, BPM-Schätzung |
| `midiclock.js` | Lookahead-Scheduler + zeitgestempelte MIDI-Clock |
| `app.js` | Glue: Audio/MIDI einrichten, Analyse takten, Anzeige |

Die Stellschrauben (Fensterlänge, Prior, Slew-Rate, Totband …) stehen als
Konstanten am Kopf von `tempo.js`, `midiclock.js` und `app.js` und sind
bewusst nach denselben Werten wie im Python-Kern benannt.

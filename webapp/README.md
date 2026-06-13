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

Im Ordner `webapp/` den mitgelieferten kleinen Server starten:

```bash
cd webapp
python serve.py
```

Dann im Browser **http://localhost:8000** öffnen.

`serve.py` liefert `.js` zuverlässig mit JavaScript-MIME-Typ aus (manche
Systeme mappen `.js` sonst auf `text/plain`, was das Laden von AudioWorklets
verhindert) und schaltet Caching ab, damit Änderungen sofort ankommen.
`python -m http.server` funktioniert grundsätzlich auch, kann aber je nach
System genau an diesen beiden Punkten scheitern.

1. **„Eingänge laden"** klicken und den Mikrofon-Zugriff erlauben – erst
   danach zeigt der Browser alle Audio-Eingänge **mit Namen** (siehe Kasten
   unten). Dann den gewünschten **Audio-Eingang** wählen.
2. **MIDI-Ausgang**: die Liste öffnen (fragt den MIDI-Zugriff an) und einen
   Port wählen, z. B. „loopMIDI Port". „Kein MIDI" zeigt nur an.
3. Optional den **BPM-Bereich** anpassen (Standard 70–140, genau eine
   Oktave – macht die Tempo-Zuordnung eindeutig).
4. **Start** drücken.

> **Warum erst „Eingänge laden"?** Aus Datenschutzgründen gibt der Browser
> die vollständige, benannte Eingangsliste erst nach erteilter
> Mikrofon-Freigabe heraus; vorher erscheint nur ein anonymer
> „Standard-Eingang". Berechtigungen werden bewusst erst bei Bedarf
> angefragt (Mikrofon beim Laden der Eingänge, MIDI beim Öffnen der
> MIDI-Liste), nicht schon beim Seitenaufruf.
>
> **Firefox** zeigt die Geräteauswahl in seinem *eigenen* Berechtigungs­dialog
> und gibt bei „Dieses Mal erlauben" oft nur das gewählte Gerät an die Liste
> weiter – das gewünschte Mikrofon also direkt im Browser-Dialog auswählen
> (oder „Beim Besuch dieser Website merken" wählen, dann erscheint die volle
> Liste). Für die eigentliche MIDI-Ausgabe ohnehin **Chrome/Edge** verwenden;
> dort funktioniert auch die In-App-Auswahl vollständig.

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

- **Kürzere Geräteliste als die Python-Version:** Der Browser zeigt eine
  *logische* Eingangsliste – einen Eintrag pro echtem Gerät. Die Python-App
  (PortAudio) listet dasselbe Interface mehrfach, einmal pro Host-API
  (MME, DirectSound, WASAPI, WDM-KS) und Abtastrate; das macht der Browser
  bewusst nicht. Namen erscheinen zudem erst nach der Mikrofon-Freigabe
  („Eingänge laden").
- **Kein Mithören der Wiedergabe** (Spotify o. ä.): Browser dürfen die
  Ausgabe anderer Apps nicht systemweit mitschneiden, und die
  WASAPI-„Loopback"-Einträge der Windows-Version gibt es im Browser nicht.
  Quelle ist immer Mikrofon oder ein Audio-Interface (Class-Compliant,
  ohne Treiber). Ein in Windows aktiviertes „Stereomix" erscheint allerdings
  als normaler Eingang und lässt sich wählen.
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
| `tempo.js` | FFT, Onset-Hüllkurve, Autokorrelation, BPM-Schätzung |
| `midiclock.js` | Lookahead-Scheduler + zeitgestempelte MIDI-Clock |
| `app.js` | Glue: Audio/MIDI einrichten, Analyse takten, Anzeige; enthält den AudioWorklet (Mono-Downmix, 512-Sample-Blöcke) als eingebetteten Blob |

Die Stellschrauben (Fensterlänge, Prior, Slew-Rate, Totband …) stehen als
Konstanten am Kopf von `tempo.js`, `midiclock.js` und `app.js` und sind
bewusst nach denselben Werten wie im Python-Kern benannt.

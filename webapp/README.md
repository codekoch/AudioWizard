# Audio2Midi -- Webversion (BPM, MIDI-Clock & Tonart)

Eine schlanke Browser-Variante des Projekts: Tempo-Erkennung (BPM) mit
**stabiler MIDI-Clock-Ausgabe** (24 PPQN), optional auch die **Grundtonart**
(mit Paralleltonart). Keine Installation, kein Python, kein Server -- die
ganze App steckt in einer einzigen Datei: **`index.html`**.

> **Online ausprobieren:** _GitHub-Pages-Link folgt hier._

## Starten

- **Lokal:** `index.html` einfach im Browser öffnen (Doppelklick im
  Datei-Explorer oder im Browser `Strg`+`O`). Kein Server nötig.
- **Online:** über den GitHub-Pages-Link oben (siehe unten „Veröffentlichung").

Dann:

1. **„Eingänge laden"** klicken und den Mikrofon-Zugriff erlauben – erst
   danach zeigt der Browser alle Audio-Eingänge **mit Namen** (Datenschutz).
   Den gewünschten **Audio-Eingang** wählen.
2. **MIDI-Ausgang**: die Liste öffnen (fragt den MIDI-Zugriff an) und einen
   Port wählen, z. B. „loopMIDI Port". „Kein MIDI" zeigt nur an.
3. Optional **BPM-Bereich** anpassen (Standard 70–140) und über den Button
   **„Tonart"** die Tonart-Anzeige einblenden.
4. **Start** drücken.

Die große Zahl zeigt das erkannte Tempo; die MIDI-Clock startet automatisch
mit der ersten stabilen Schätzung (MIDI `start`) und hält bei Stille an
(`stop`).

## Voraussetzungen

- **Browser mit Web MIDI:** Chrome, Edge oder Opera (Chromium).
  Firefox unterstützt Web MIDI nur mit Erweiterung; **Safari unterstützt es
  nicht** – dort gibt es keine MIDI-Ausgabe (Anzeige funktioniert trotzdem).
- **Virtueller MIDI-Port**, um eine DAW/Hardware auf demselben Rechner zu
  erreichen: Windows
  [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html),
  macOS der IAC-Treiber (Audio-MIDI-Setup), Linux ALSA/`snd-virmidi`.
  Ein USB-MIDI-Interface geht direkt.

## Warum die Clock stabil ist

Jeder Clock-Tick wird mit `output.send([0xF8], zeitstempel)` **im Voraus
geplant** – der Zeitstempel liegt in der `performance.now()`-Domäne, und das
MIDI-Subsystem des Browsers/Betriebssystems gibt den Tick dann zeitgenau aus.
Die Tick-Stabilität hängt damit nicht am (ungenauen) JavaScript-Timer,
sondern an der OS-MIDI-Schicht – derselbe Gedanke wie bei CoreMIDI auf dem
Mac. Ein Lookahead-Scheduler füllt die Tick-Warteschlange laufend ein Stück
in die Zukunft. Tempoänderungen werden mit Totband (gegen Mess-Zittern) und
begrenzter Slew-Rate sanft nachgeführt – kein Tick-Burst.

**Tipp:** Den Tab im Vordergrund lassen. Hintergrund-Tabs werden vom Browser
gedrosselt; durch den Lookahead bleibt die Clock zwar eine Weile stabil,
für den Live-Betrieb sollte der Tab aber sichtbar bleiben.

## Tonart-Anzeige (optional)

Über den Button **„Tonart"** lässt sich die erkannte Grundtonart mit
Paralleltonart einblenden (z. B. „C Dur (A Moll)"). Die Erkennung ist aus dem
Python-Kern portiert: Sha'ath-Profile, Bass-Evidenz zur Unterscheidung von Dur
und Mollparallele, zweistufige Mittelung mit Hysterese; unsichere Erkennung
wird gedimmt angezeigt. Das Chroma stammt aus einer (für den Bass extra
hochaufgelösten) STFT statt der CQT des Python-Projekts.

## Technische Hinweise

- **`ScriptProcessorNode` statt `AudioWorklet`:** Der Worklet lädt ein Modul
  nach, was der Browser unter `file://` (Origin „null") blockiert. Der
  ScriptProcessor lädt nichts nach und läuft daher auch beim direkten Öffnen
  der Datei. Etwas älter/deprecated, für diese leichte Analyse aber völlig
  ausreichend; die MIDI-Clock ist davon ohnehin unabhängig.
- **MIDI-Clock-Timer im Hauptthread** (kein Blob-Worker – der wäre unter
  `file://` ebenfalls gesperrt). Tab sichtbar lassen.

## Grenzen gegenüber den Python-Versionen

- **Kürzere Geräteliste:** Der Browser zeigt eine *logische* Eingangsliste –
  einen Eintrag pro echtem Gerät. Die Python-App (PortAudio) listet dasselbe
  Interface mehrfach (MME, DirectSound, WASAPI, WDM-KS). Namen erscheinen erst
  nach der Mikrofon-Freigabe („Eingänge laden").
- **Kein Mithören der Wiedergabe** (Spotify o. ä.): Browser dürfen die Ausgabe
  anderer Apps nicht systemweit mitschneiden; die WASAPI-„Loopback"-Einträge
  der Windows-Version gibt es nicht. Quelle ist immer Mikrofon oder ein
  Audio-Interface. Ein in Windows aktiviertes „Stereomix" erscheint allerdings
  als normaler Eingang.
- **Vereinfachte Analyse:** Spektralfluss-Onset-Hüllkurve statt HPSS; Tonart
  aus STFT-Chroma statt CQT (etwas weniger treffsicher, vor allem bei der
  Dur/Moll-Unterscheidung). Keine Akkorde, kein Beat-Sync.

## Veröffentlichung über GitHub Pages

Da alles in `index.html` steckt, genügt statisches Hosting:

1. Im GitHub-Repo unter **Settings → Pages** die Quelle auf „Deploy from a
   branch", Branch `master`, Ordner `/ (root)` setzen.
2. Nach dem Build ist die App erreichbar unter
   `https://<benutzer>.github.io/<repo>/webapp/`.
3. Diesen Link oben in dieser README (und in der Haupt-README) eintragen.

> Web MIDI und Mikrofon brauchen einen sicheren Kontext – über `https://…`
> (GitHub Pages liefert HTTPS) ist das erfüllt.

# Audio2Midi -- Webversion (BPM, MIDI-Clock & Tonart)

Eine schlanke Browser-Variante des Projekts: Tempo-Erkennung (BPM) mit
**stabiler MIDI-Clock-Ausgabe** (24 PPQN), optional auch die **Grundtonart**
(mit Paralleltonart). Ein **Noten-Modus** sendet erkannte Tonhöhen direkt als
MIDI-Noten (mono- oder polyphon). Als Quelle dient ein Audio-Eingang **oder**
die **mitgehörte Wiedergabe** (Tab-/System-Audio). Keine Installation, kein
Python, kein Server -- die ganze App steckt in einer einzigen Datei:
**`index.html`**.

> **Online ausprobieren:** <a href="https://codekoch.github.io/Audio2Midi/webapp/">https://codekoch.github.io/Audio2Midi/webapp/</a>

## Starten

- **Lokal:** `index.html` einfach im Browser öffnen (Doppelklick im
  Datei-Explorer oder im Browser `Strg`+`O`). Kein Server nötig.
- **Online:** über den GitHub-Pages-Link oben (siehe unten „Veröffentlichung").

Dann:

1. **Quelle** wählen: „Audio-Eingang" (Mikrofon/Line-In) oder „Wiedergabe
   mithören" (siehe Abschnitt unten). Bei „Audio-Eingang" zusätzlich
   **„Eingänge laden"** klicken und den Mikrofon-Zugriff erlauben – erst
   danach zeigt der Browser alle Eingänge **mit Namen** (Datenschutz) – und
   den gewünschten Eingang wählen.
2. **MIDI-Ausgang**: die Liste öffnen (fragt den MIDI-Zugriff an) und einen
   Port wählen, z. B. „loopMIDI Port". „Kein MIDI" zeigt nur an.
3. Optional **BPM-Bereich** anpassen (Standard 70–140) und über den Button
   **„Tonart"** die Tonart-Anzeige einblenden.
4. **Start** drücken.

Die große Zahl zeigt das erkannte Tempo; die MIDI-Clock startet automatisch
mit der ersten stabilen Schätzung (MIDI `start`) und hält bei Stille an
(`stop`).

## Noten-Modus (Pitch → MIDI)

Über **„Modus"** lässt sich von „Tempo & MIDI-Clock" auf einen Noten-Modus
umschalten, der erkannte Tonhöhen direkt als **MIDI-Noten** (Note On/Off) an
den gewählten Ausgang sendet. In diesem Modus laufen BPM-/Tonart-/Clock-
Schritte bewusst **nicht** mit – für möglichst geringe Latenz.

- **Monophon:** erkennt jeweils EINE Note (Gesang, Bass, Lead, einzelnes
  Instrument, Pfeifen) per YIN-Tonhöhenerkennung. Geringe Latenz, gute
  Treffsicherheit – braucht aber eine klar einstimmige Quelle.
- **Polyphon:** erkennt mehrere Noten gleichzeitig per FFT-Peak-Analyse mit
  Oberton-Unterdrückung. Etwas höhere Latenz (größeres Analysefenster) und
  begrenzte Genauigkeit; bei dichter/komplexer Musik nur ein grober Eindruck.

Velocity wird aus dem Pegel abgeleitet, gesendet wird auf MIDI-Kanal 1.
Beim Stoppen/Umschalten werden alle offenen Noten beendet (Note Off).

## Wiedergabe mithören (Loopback)

Statt eines Mikrofons lässt sich auch die laufende **Wiedergabe** analysieren
(z. B. was gerade in Spotify spielt). Quelle auf **„Wiedergabe mithören"**
stellen und **Start** drücken – dann erscheint der Freigabe-Dialog des
Browsers (Screen-Capture API, `getDisplayMedia`):

- **Windows, Chrome/Edge:** „**Gesamter Bildschirm**" wählen und unten
  **„Systemaudio teilen"** ankreuzen → die komplette Systemausgabe wird
  mitgehört (auch Desktop-Apps wie Spotify). Alternativ einen **Tab** wählen
  und **„Tab-Audio teilen"** ankreuzen (z. B. den Spotify-Web-Player).
- **macOS:** System-Audio ist hier nicht erfassbar – nur **Tab-Audio**
  (Spotify/YouTube als Browser-Tab). Safari unterstützt es nicht.
- **Android/Mobil:** **nicht möglich** – mobile Browser stellen
  `getDisplayMedia` nicht bereit. Die Option wird dort automatisch
  deaktiviert; als Quelle bleibt nur das Mikrofon. (Der Noten-Modus
  monophon funktioniert auf Android mit dem Mikrofon.)

Wichtig: Das Audio-Häkchen muss aktiv sein, sonst kommt kein Ton an (die App
weist dann darauf hin). Beendet man die Freigabe über die Browser-Leiste,
stoppt die Sitzung automatisch. Die Wiedergabe ist weiterhin normal hörbar.

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
- **Mithören der Wiedergabe** ist möglich, aber über die Screen-Capture-API
  (siehe oben) statt eines WASAPI-Loopback-Geräts: Es muss pro Sitzung im
  Browser-Dialog freigegeben werden, und vollständiges **System-Audio gibt es
  nur unter Windows** (Chrome/Edge). Unter macOS nur Tab-Audio.
- **Vereinfachte Analyse:** Spektralfluss-Onset-Hüllkurve statt HPSS; Tonart
  aus STFT-Chroma statt CQT (etwas weniger treffsicher, vor allem bei der
  Dur/Moll-Unterscheidung). Keine Akkorde, kein Beat-Sync.

## Veröffentlichung über GitHub Pages

- Teste die Webversion direkt unter <a href="https://codekoch.github.io/Audio2Midi/webapp/">https://codekoch.github.io/Audio2Midi/webapp/</a>

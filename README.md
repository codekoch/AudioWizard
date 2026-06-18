# Audio2Midi

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Plattform](https://img.shields.io/badge/Plattform-Windows%20%C2%B7%20macOS%20%C2%B7%20Raspberry%20Pi%20%C2%B7%20Linux-555)
![MIDI](https://img.shields.io/badge/MIDI-Clock%2C%2024%20PPQN-1D9E75)
![Lizenz](https://img.shields.io/badge/Lizenz-GPL--3.0-orange)

**Hört Musik mit und zeigt live Tempo, Tonart und Akkorde an – und liefert
dazu eine stabile MIDI-Clock, die Drumcomputer, Sequenzer, Arpeggiatoren und
Delays synchron zum laufenden Song taktet. Zusätzlich gibt es noch zahlreiche
andere Features wie z.B. die Erstellung von Songsheets oder Stems aus Audiodateien.**

![Hauptanzeige](docs/screenshot_display.png)

Als Quelle dient wahlweise ein Audio-Eingang (Mikrofon/Line-In) oder unter
Windows direkt die **Wiedergabe selbst** (WASAPI-Loopback) – also z. B. das,
was gerade in Spotify läuft. Auf dem Raspberry Pi übernehmen die
PipeWire/Pulse-„Monitor“-Quellen dieselbe Rolle und erscheinen als normale
Eingänge; unter macOS leistet das ein virtuelles Ausgabegerät wie
[BlackHole](https://existential.audio/blackhole/).

## Funktionen

- **Tempo (BPM)** – aus dem perkussiven Anteil des Signals (HPSS-Trennung),
  per Autokorrelation mit Kammfilter-Stützung und Oktav-Prior; Median über
  die letzten Schätzungen mit Schnellumschaltung bei echten Tempowechseln.
- **Tonart** – Salience-Chroma (CQT) mit Obertongewichtung, Sha'ath-Profile,
  Bass-Evidenz zur Unterscheidung von Dur und Mollparallele. Die Stimmung
  wird je Song geschätzt und eingefroren – auch gepitchtes Material landet
  auf den richtigen Tönen. Unsichere Erkennung wird gedimmt angezeigt.
- **Akkorde** (optional) – Template-Matching mit HMM-Glättung und
  Tonart-Prior; auf Wunsch ein schneller Pfad im eigenen Thread (~0,2-s-Takt)
  mit Onset-Verankerung und Innovations-Gate für kurze Wechsel-Latenz.
  Akkordfolgen lassen sich mit Zeitstempel in eine Textdatei protokollieren.
- **MIDI-Clock (24 PPQN)** – startet erst mit der ersten echten
  Tempo-Schätzung (`start`), stoppt bei Stille/Songwechsel (`stop`).
  Eigener Thread mit hoher Priorität und 1-ms-Timerauflösung, Tempo-Totband
  gegen Mess-Zittern, sanftes Aufholen statt Tick-Bursts. Optional
  **beat-synchron**: Tick 1 von 24 rastet auf die erkannte Zählzeit ein
  (sanfte Phasenregelung, gemessen ~1–2 ms Streuung).
- **Noten-Modus (Pitch → MIDI)** (optional) – statt der Tempo-Analyse werden
  erkannte Tonhöhen direkt als MIDI gesendet: **monophon** (YIN, geringe Latenz,
  mit Halte-Hysterese gegen Neutrigger beim Ausklingen), **polyphon** (FFT-Peaks
  mit Oberton-Unterdrückung) oder **Akkorde** – aus dem Klang (z. B. Gitarre)
  wird der wahrscheinlichste Akkord erkannt und als sauberes MIDI-Voicing
  gesendet, Fehltöne fallen weg. In diesem Modus laufen die teuren
  Analyseschritte (HPSS/Chroma/Tempo/Clock) bewusst nicht mit – für möglichst
  geringe Latenz. Tracking-Parameter (Schwellen, YIN-Strenge, Entprellung,
  Polyphonie) sind kalibrierbar – in der GUI über „Noten-Kalibrierung …" im
  Einstellungsbildschirm (Slider), sonst über die Konfiguration. In Konsole und
  GUI wählbar; auch in der Webversion vorhanden.
- **Datei-Modus (Datei → MIDI-Clock, driftfrei)** (optional) – eine Audiodatei
  wird einmal vorab zu einer Beat-Map analysiert (globales Tempo → Beat-Tracker
  mit lokaler Periodenkurve, dadurch Erkennung von **konstantem vs. variablem**
  Tempo) und dann abgespielt. Die MIDI-Clock wird dabei nicht frei mitgetaktet,
  sondern streng aus der **Wiedergabeposition** abgeleitet (die Tick-Zeitpunkte
  stehen als feste Marken am Beat-Raster fest) – sie läuft daher nicht gegen den
  Song weg. Bei konstantem Tempo entsteht ein perfekt gleichmäßiges Raster über
  die ganze Datei. In der GUI über die Schaltfläche „Datei …" (auch im
  Einstellungsbildschirm), in der Konsole über `--file DATEI`. Mirror des
  gleichnamigen Modus der Webversion.
- **Aufnahme + Speichern** (optional) – das live analysierte Signal lässt sich
  mitschneiden (GUI-Schaltfläche „● Aufnahme", Konsole Taste `r`) und danach als
  WAV speichern. Enthält der Mitschnitt **mehrere Stücke** (kurze Stille +
  BPM/Tonart-Wechsel), werden sie automatisch erkannt und in einer Prüf-Liste
  getrennt angeboten – jedes mit **Namensvorschlag aus BPM und Tonart**, unsichere
  Grenzen gedimmt; „Alle speichern" legt sie in einen Ordner, der für das nächste
  Mal gemerkt wird. Mirror der Webversion.
- **DJ-Modus** (optional) – zwei Decks nebeneinander laden und analysieren (auch
  während eines läuft), in **einem** Ausgabe-Stream gemischt. Ein Klick aufs Deck
  (oder der Crossfader) blendet per **Equal-Power-Crossfade** über; die MIDI-Clock
  **folgt automatisch dem lauteren Deck** (driftfrei aus dessen Wiedergabeposition,
  Tempowechsel beim Überblenden inklusive). In der GUI über die Schaltfläche „DJ",
  in der Konsole über `--dj DATEI_A DATEI_B` (Tasten `a`/`b` zum Überblenden).
  Mirror der Webversion. Je Deck gibt es zusätzlich einen **EQ-Isolator**
  (Bass/Mitte/Höhen stufenlos über senkrechte Slider regeln, mit dB-Wertanzeige
  und Doppelklick = zurück auf 0 dB – echtzeitfähige Frequenzfilterung als
  schlanker Stem-Ersatz, kein echtes Trennen einzelner Instrumente) und einen
  **Tempo-Sync** („Sync"): das Deck rastet **tonhöhen-erhaltend** auf das Tempo
  des anderen Decks ein (Beat-Phasen-Ausrichtung; die MIDI-Clock bleibt beim
  Überblenden konstant). Die Zeitdehnung läuft **in Echtzeit** (WSOLA, in der
  WebApp als AudioWorklet) und wirkt **sofort** – ohne Vorberechnung. Mit
  **„Übergang"** gleitet ein Deck stattdessen vom Master-Tempo allmählich auf
  sein **Eigentempo**; die WSOLA-Rate wird live gerampt und die MIDI-Clock
  gleitet automatisch mit.
- **Stem-Trennung (lokales KI-Modell)** (optional) – ein Stück lässt sich lokal
  und offline per **Demucs** (`htdemucs`) in **Drums, Bass, Gesang, Rest**
  zerlegen. Im **DJ-Modus** trennt die Schaltfläche „Stems" je Deck und öffnet
  einen Stem-Mischer (Pegel je Instrument, **in Echtzeit** mischbar – echte
  Instrument-Isolation statt bloßer Frequenzfilterung; lässt sich mit dem
  Tempo-Sync kombinieren). Auch eine **Aufnahme** lässt sich direkt zerlegen:
  im Speichern-Fenster „In Stems trennen & abspielen" öffnet einen kleinen
  **Stem-Player** (Pegel-Fader je Spur, Play/Pause). Zum reinen **Export** gibt es
  „Stems exportieren …" im Einstellungsbildschirm bzw. `--stems DATEI [--out ORDNER]`
  in der Konsole (speichert die Spuren als einzelne WAVs). Braucht das zusätzliche Paket **`demucs`**
  (`pip install demucs`, zieht PyTorch); ohne bleibt das Feature einfach aus. Die
  KI-Trennung läuft offline und kann je nach CPU einige Minuten je Stück dauern.
  Während der Trennung öffnet sich ein eigenes **Fortschritts-/Log-Fenster**: es
  zeigt live, welcher Schritt gerade läuft (Modell laden, Audio laden, Trennung,
  Speichern) und im Fehlerfall die **vollständige Fehlermeldung** – so lässt sich
  beurteilen, was passiert. Das Laden der Audiodatei läuft bewusst **über librosa**
  statt über torchaudio; dadurch funktioniert die Trennung auch dann, wenn die
  Installation kein `torchcodec` mitbringt (neuere torchaudio-Versionen verlangen
  es zum Laden) oder die `demucs.api` fehlt – ist die installierte Demucs-/
  PyTorch-Version unvollständig, weicht das Programm automatisch auf einen anderen
  Weg aus.
- **Song-Sheet (Text + Akkorde)** (optional) – aus einer Datei entsteht ein
  **Chord-Sheet wie bei Ultimate Guitar**: die Akkorde stehen über den jeweiligen
  Wörtern des gesungenen Textes. Ablauf komplett **lokal/offline**: Demucs trennt
  zuerst den **Gesang** heraus (das verbessert die Transkription deutlich), eine
  lokale **Whisper-KI** transkribiert den Gesang mit Wort-Zeitstempeln, und die
  **Begleitung** (alles außer Gesang) geht in die vorhandene **Akkord-Erkennung**
  (über je zwei Beats ein Akkord, auf die gängigen Typen Dur/Moll/7/m7 beschränkt
  und leitereigene Akkorde der erkannten Tonart leicht bevorzugt – das hält das
  Sheet ruhig). Im Einstellungsbildschirm „Song-Sheet …" (ein kleiner Dialog
  fragt **Sprache** und **Modellgröße** ab) bzw. `--sheet DATEI [--out ORDNER]
  [--lang de|en] [--whisper small|medium|large-v3]` in der Konsole. Das Ergebnis
  wird in einem Fenster angezeigt und lässt sich als **Textdatei** und als
  **ChordPro** (`.chordpro`, transponier-/druckbar) speichern.
  Braucht zusätzlich **`faster-whisper`** (`pip install faster-whisper`; lädt beim
  ersten Mal ein Sprachmodell) sowie `demucs`; ohne bleibt das Feature aus. Es
  wird ein **mehrsprachiges** Modell genutzt (Deutsch und Englisch gleichermaßen),
  Standard ist „medium". **Tipp:** Bei bekanntem Gesang die Sprache fest wählen –
  die automatische Spracherkennung liegt bei Musik gern daneben (ein deutsches
  Lied wird sonst als Englisch „übersetzt"). Hinweise: gesungener Text wird „gut,
  aber nicht fehlerfrei" erkannt, die Akkorde sind eine Approximation (kein
  Profi-Transkriptor), und der Lauf kann je nach CPU einige Minuten dauern (eher
  ein PC- als ein Pi-Feature).
- **Zwei Oberflächen** – Konsolen-Version (`realtime_bpm_key_midiclock.py`)
  und Touch-taugliche Kiosk-GUI (`bpm_key_display.py`) für ein 7-Zoll-Display
  am Raspberry Pi; unter Windows und macOS läuft sie im Fenster.
- **Praxis-Helfer** – Hold-Funktion für Stücke mit langen Breaks (Anzeige
  friert ein, Clock läuft konstant weiter), manueller Analyse-Neustart für
  Songwechsel ohne Pause, Pegelanzeige, Watchdog und Logdatei für den
  Kiosk-Betrieb.

## Schnellstart (Windows)

```
pip install -r requirements.txt
python bpm_key_display.py
```

Beim ersten Start erscheint der Einstellungsbildschirm: Audio-Quelle (auch
„Loopback: …“-Einträge zum Mithören der Wiedergabe) und MIDI-Ausgang wählen,
Start drücken. Die Wahl wird gespeichert, danach startet das Programm direkt
in die Anzeige. Alternativ die Konsolen-Version:

```
python realtime_bpm_key_midiclock.py
```

Eine Audiodatei statt einer Live-Quelle abspielen (Datei-Modus, driftfreie
Clock zur Wiedergabe) – in der Konsole:

```
python realtime_bpm_key_midiclock.py --file "C:\Pfad\zum\song.mp3"
```

Für die MIDI-Ausgabe an Software auf demselben Rechner braucht es unter
Windows einen virtuellen MIDI-Port, z. B.
[loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html).

## macOS

```
python3 -m pip install -r requirements.txt
python3 bpm_key_display.py
```

Läuft wie unter Windows im Fenster; Bedienung und Konsolen-Version sind
identisch. Drei Besonderheiten:

- **Mikrofon-Berechtigung:** Beim ersten Zugriff auf einen Audio-Eingang
  fragt macOS nach der Mikrofon-Freigabe für das Terminal (bzw. die
  Python-App) – einmal erlauben.
- **MIDI-Ausgang:** In der MIDI-Liste gibt es den Eintrag „Virtueller Port
  ‚Audio2Midi Clock‘ erzeugen“ (CoreMIDI) – der Port erscheint dann in der
  DAW als MIDI-Eingang, ganz ohne IAC-Treiber. Alternativ funktioniert
  natürlich auch der IAC-Bus aus dem Audio-MIDI-Setup oder ein
  USB-MIDI-Interface.
- **Wiedergabe mithören:** WASAPI-Loopback gibt es nur unter Windows. Auf
  dem Mac ein virtuelles Ausgabegerät wie
  [BlackHole](https://existential.audio/blackhole/) installieren (kostenlos),
  die Wiedergabe dorthin routen (z. B. per „Gerät mit mehreren Ausgängen“
  im Audio-MIDI-Setup, damit weiterhin etwas zu hören ist) – BlackHole
  erscheint dann als normaler Audio-Eingang in der Quellenliste.

## Raspberry Pi (Kiosk-Betrieb)

Installation (mit Desktop oder als Minimal-Variante auf Pi OS Lite),
Kiosk-Autostart, Overlay-Dateisystem für den Bühnenbetrieb (robust gegen
hartes Ausschalten) und Performance-Tipps stehen in
[README_RaspberryPi.md](README_RaspberryPi.md).

## Webversion (Browser)

Eine schlanke Browser-Variante ohne Installation liegt als **einzelne Datei**
in [webapp/index.html](webapp/index.html): BPM-Erkennung mit stabiler
MIDI-Clock und optionaler Tonart-Anzeige über die Web Audio und Web MIDI API
(Chrome/Edge). Als Quelle dient ein Audio-Eingang oder die mitgehörte
Wiedergabe (Tab-/System-Audio über die Screen-Capture-API). Zusätzlich gibt es
einen **Datei-/Aufnahme-Modus**, der eine Audiodatei – oder einen Mitschnitt
(mitgehörte Wiedergabe oder Audio-Eingang, manuell aufgenommen) – vorab zu einer
Beat-Map analysiert und ihre MIDI-Clock **driftfrei** zur Wiedergabe ausgibt; die
Aufnahme lässt sich – auch in mehrere erkannte Stücke getrennt – als Datei mit
BPM/Tonart-Namensvorschlag speichern. Außerdem gibt es
einen **Noten-Modus** (Pitch → MIDI, mono-/polyphon), einen
**Akkord-Modus**, der angeschlagene Akkorde (z. B. Gitarre) erkennt und als
sauberen MIDI-Akkord sendet, sowie einen **DJ-Modus** (zwei Decks nebeneinander,
Audio-Crossfade, MIDI-Clock folgt dem überblendeten Track). Einfach im Browser
öffnen – kein Server nötig.

👉 **Online ausprobieren:** <a href="https://codekoch.github.io/Audio2Midi/webapp/">https://codekoch.github.io/Audio2Midi/webapp/</a>

Details in [webapp/README.md](webapp/README.md).

## Wie es funktioniert

Die Analyse läuft auf einem rollenden 8-Sekunden-Fenster, das jede Sekunde
neu ausgewertet wird (librosa, 22 050 Hz):

1. **HPSS-Zerlegung** trennt das Signal nach Struktur: kurze, breitbandige
   Transienten (Drums) gehen in die Tempo-Schätzung, der harmonische Rest
   (Flächen, Bass, Gesang) in Tonart und Akkorde.
2. **Tempo**: Autokorrelation der Onset-Hüllkurve, jeder Kandidat wird durch
   seine Vielfachen und das Achtelraster gestützt (Kammfilter); ein sanfter
   Prior löst die Oktav-Mehrdeutigkeit. Schwache Periodizität wird verworfen
   statt geraten.
3. **Tonart**: Chroma aus einer 7-Oktaven-CQT mit Obertongewichtung
   (Salience) und Log-Kompression, korreliert mit Dur-/Moll-Profilen;
   ein Bass-Chroma liefert die Grundton-Evidenz. Zweistufige Mittelung
   (schnelle EMA + Gesamtmittel) plus Hysterese gegen Flackern.
4. **MIDI-Clock**: eigener Echtzeit-Thread, der dem Tempo-Median mit
   Totband und begrenzter Slew-Rate folgt; im Beat-Sync-Modus zieht eine
   Regelschleife die Tick-Phase mit max. 1,5 ms pro Tick auf das geglättete
   Beat-Raster.

Viele Stellschrauben sind direkt im Quelltext dokumentiert – inklusive der
Messwerte, die zur jeweiligen Einstellung geführt haben, und der Ansätze,
die gemessen und verworfen wurden.

## Qualität ist nachmessbar

- `eval_detection.py` – nach wie vielen Sekunden stehen BPM und Tonart
  dauerhaft korrekt? (Testdateien nach dem Muster `<BPM>BPM_<Tonart>.mp3`
  in den Projektordner legen; aus Urheberrechtsgründen liegen keine im Repo.)
- `eval_chords.py` – Diatonik-Quote und Wechselrate der Akkorderkennung,
  inklusive CPU-Zeit pro Analyse.
- `eval_clock_sync.py` – End-to-End-Test der MIDI-Clock gegen einen
  synthetischen Klicktrack: misst Phasen-Streuung und Tempo-Stabilität
  der Beat-Ticks.

## Abhängigkeiten

numpy, librosa, soundfile, sounddevice, mido, python-rtmidi – und nur unter
Windows zusätzlich soundcard für den Loopback (`requirements.txt`).
python-rtmidi nutzt je nach Plattform WinMM, CoreMIDI (macOS) oder ALSA.

## Lizenz

[GPL-3.0](LICENSE) – frei nutzbar und veränderbar; Weitergaben (auch
veränderte) müssen unter derselben Lizenz quelloffen bleiben.

---

*Autoren: codekoch / claude · © 2026 codekoch*

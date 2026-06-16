# Audio2Midi

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Plattform](https://img.shields.io/badge/Plattform-Windows%20%C2%B7%20macOS%20%C2%B7%20Raspberry%20Pi%20%C2%B7%20Linux-555)
![MIDI](https://img.shields.io/badge/MIDI-Clock%2C%2024%20PPQN-1D9E75)
![Lizenz](https://img.shields.io/badge/Lizenz-GPL--3.0-orange)

**Hört Musik mit und zeigt live Tempo, Tonart und Akkorde an – und liefert
dazu eine stabile MIDI-Clock, die Drumcomputer, Sequenzer, Arpeggiatoren und
Delays synchron zum laufenden Song taktet.**

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
  geringe Latenz. Tracking-Parameter (Schwellen, YIN-Strenge, Entprellung) sind
  über die Konfiguration kalibrierbar. In Konsole und GUI wählbar; auch in der
  Webversion vorhanden.
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

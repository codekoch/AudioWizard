# Audio2Midi

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Plattform](https://img.shields.io/badge/Plattform-Windows%20%C2%B7%20Raspberry%20Pi%20%C2%B7%20Linux-555)
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
Eingänge.

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
- **Zwei Oberflächen** – Konsolen-Version (`realtime_bpm_key_midiclock.py`)
  und Touch-taugliche Kiosk-GUI (`bpm_key_display.py`) für ein 7-Zoll-Display
  am Raspberry Pi; unter Windows läuft sie im Fenster.
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

## Raspberry Pi (Kiosk-Betrieb)

Installation, Autostart und Performance-Tipps für den Pi stehen in
[README_RaspberryPi.md](README_RaspberryPi.md).

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

## Lizenz

[GPL-3.0](LICENSE) – frei nutzbar und veränderbar; Weitergaben (auch
veränderte) müssen unter derselben Lizenz quelloffen bleiben.

---

*Autoren: codekoch / claude · © 2026 codekoch*

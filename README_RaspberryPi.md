# BPM- & Tonart-Anzeige auf dem Raspberry Pi (7"-Display)

`bpm_key_display.py` zeigt BPM und Tonart bildschirmfüllend an (für 800x600,
skaliert aber mit jeder Auflösung) und gibt parallel die MIDI-Clock aus.
Der Analyse-Kern wird aus `realtime_bpm_key_midiclock.py` importiert –
beide Dateien müssen also im selben Ordner liegen.

## Hardware

- Raspberry Pi 5 (empfohlen) oder Pi 4, **64-bit** Raspberry Pi OS –
  wahlweise mit Desktop (einfachster Weg) oder als Lite-Variante ohne
  Desktop (siehe „Minimal-Installation“ unten)
- 7"-Display (HDMI oder DSI), idealerweise mit Touch
- USB-Audio-Interface für den Eingang (der Pi hat keinen Line-In)
- USB-MIDI-Interface für die Clock (entfällt, wenn nur angezeigt werden soll)

## Installation (Raspberry Pi OS mit Desktop)

Der einfachste Weg; wer einen dedizierten Bühnen-Pi möglichst schlank
aufsetzen will, springt zur „Minimal-Installation“ weiter unten.

```bash
sudo apt update
sudo apt install -y python3-venv python3-tk libportaudio2 libsndfile1 \
                    libasound2-dev build-essential

python3 -m venv ~/audio2midi-env
source ~/audio2midi-env/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Hinweise:

- `soundcard` (Windows-Loopback) wird auf dem Pi **nicht** gebraucht.
  Wer die Systemausgabe analysieren will: Unter PipeWire/PulseAudio
  erscheint die Wiedergabe als Eingang „Monitor of …“ direkt in der
  Geräteliste des Auswahlbildschirms. (Unter Windows zeigt derselbe
  Auswahlbildschirm dafür zusätzliche „Loopback: …“-Einträge, sofern
  `soundcard` installiert ist.)
- Die erste Analyse nach dem Start dauert ein paar Sekunden
  (numba kompiliert einmalig) – das Skript wärmt das beim Start vor und
  zeigt solange „INITIALISIERE ANALYSE …“.

## Starten

```bash
source ~/audio2midi-env/bin/activate
python bpm_key_display.py
```

- Erster Start: Auswahlbildschirm für Audio-Eingang und MIDI-Ausgang.
  Die Wahl wird in `display_config.json` gespeichert; danach bootet das
  Programm direkt in die Anzeige.
- Tasten/Optionen:
  - `F11` Vollbild umschalten, `Esc` beenden
  - `--windowed` / `--fullscreen` / `--setup` (Auswahl erzwingen)
- Unter Linux startet das Programm automatisch im Vollbild,
  unter Windows im 800x600-Fenster (zum Testen).

## Optionen im Einstellungsbildschirm

- **BPM mit Nachkommastelle**: Standardmäßig wird das Tempo ganzzahlig
  angezeigt (ruhigere Anzeige); die MIDI-Clock läuft intern immer mit
  voller Auflösung.
- **Beat-synchrone Clock (experimentell)**: Die MIDI-Clock rastet
  zusätzlich auf die erkannten Zählzeiten ein (Tick 1 von 24 liegt auf
  dem Beat), statt nur im richtigen Tempo frei zu laufen. Die Phase wird
  sanft nachgeregelt (max. 1,5 ms pro Tick), sodass die Clock nie springt.
- Die MIDI-Clock läuft grundsätzlich erst, sobald ein Tempo erkannt
  wurde (vorher wäre es ein fiktives Tempo): Bei der ersten Schätzung
  wird MIDI-`start` gesendet -- mit Beat-Sync exakt auf dem nächsten
  Beat --, bei Stille/Songwechsel stoppt die Clock (`stop`) und startet
  beim nächsten Stück neu.
- **BPM-Bereich** (Standard 70–140): Suchbereich der Tempo-Erkennung.
  Genau eine Oktave (Faktor 2) macht die Oktav-Zuordnung eindeutig;
  ein breiterer Bereich (z. B. 60–180) erfasst auch sehr langsame oder
  schnelle Stücke, kann dann aber halbes/doppeltes Tempo verwechseln.
- Die Tonart wird **gedimmt** angezeigt, solange die Erkennung noch
  unsicher ist (kleiner Vorsprung vor dem zweitbesten Kandidaten), und
  leuchtet erst bei klarer Entscheidung voll auf. In der Konsolen-Version
  markiert ein `?` dahinter dieselbe Unsicherheit.
- **Analyse anhalten** (Button unten links bzw. Leertaste): friert die
  aktuellen Ergebnisse bewusst ein -- für Stücke mit langen Breaks. Die
  MIDI-Clock läuft konstant im gehaltenen Tempo weiter, und die Stille
  des Breaks löst keinen Reset aus. Erneutes Drücken setzt die Analyse
  fort (die Historie des Stücks bleibt dabei erhalten). Der Status zeigt
  währenddessen „ANGEHALTEN · CLOCK LAEUFT".

## Diagnose und Wartung

- Fehler und Neustarts der Analyse landen in `audio2midi.log` neben den
  Skripten -- im Kiosk-Betrieb ohne Konsole die erste Anlaufstelle.
- Der Analyse-Thread ist doppelt abgesichert: Er startet sich nach einem
  unerwarteten Fehler selbst neu, und die GUI überwacht ihn zusätzlich
  (Watchdog). Die Audio-Queue ist begrenzt, damit ein Hänger nicht den
  Speicher füllt.
- `eval_detection.py` misst die Erkennungsqualität über Testdateien im
  Projektordner (Namensmuster `<BPM>BPM_<Tonart>.mp3`, z. B.
  `106BPM_C_Dur.mp3`): `python eval_detection.py` zeigt, nach wie vielen
  Sekunden BPM und Tonart dauerhaft korrekt stehen. Damit lässt sich
  jede Änderung an den Stellschrauben nachmessen.

## Autostart (Desktop-Variante)

Auf Raspberry Pi OS mit Desktop genügt eine Autostart-Datei:

```bash
mkdir -p ~/.config/autostart
nano ~/.config/autostart/bpm-display.desktop
```

Inhalt (Pfade ggf. anpassen):

```ini
[Desktop Entry]
Type=Application
Name=BPM Display
Exec=/home/pi/audio2midi-env/bin/python /home/pi/Audio2Midi/bpm_key_display.py
X-GNOME-Autostart-enabled=true
```

Optional in `raspi-config`: Auto-Login auf den Desktop aktivieren und den
Bildschirmschoner/Blanking abschalten.

## Minimal-Installation: Pi OS Lite ohne Desktop (Kiosk)

Für einen dedizierten Bühnen-Pi reicht **Raspberry Pi OS Lite (64-bit)**:
Statt des kompletten Desktops startet ein minimaler X-Server direkt in die
Anzeige. Das spart rund zwei Drittel Platz auf der Karte, bootet schneller,
und außer Kernel, X und Python läuft praktisch nichts. Sound- und
MIDI-Treiber stecken bereits im Kernel (ALSA, `snd-usb-audio`) –
USB-Audio- und USB-MIDI-Interfaces funktionieren ohne weiteres Zutun.

```bash
sudo apt update
sudo apt install -y python3-venv python3-tk libportaudio2 libsndfile1 \
                    xserver-xorg-core xinit xserver-xorg-input-libinput \
                    matchbox-window-manager x11-xserver-utils \
                    fonts-dejavu-core

python3 -m venv ~/audio2midi-env
source ~/audio2midi-env/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Hinweise:

- `xserver-xorg-input-libinput` wird für den Touchscreen gebraucht;
  `matchbox-window-manager` (winziger Fenstermanager, ~100 KB), damit das
  Tk-Vollbild zuverlässig funktioniert – ganz ohne Fenstermanager
  ignoriert X das Vollbild-Attribut.
- Falls pip für `python-rtmidi` kein fertiges Wheel findet und kompilieren
  will: vorher `sudo apt install -y build-essential libasound2-dev`,
  hinterher mit `sudo apt autoremove build-essential libasound2-dev`
  wieder entfernbar.
- Ohne PipeWire/PulseAudio gibt es **keine „Monitor of …“-Quellen**
  (Wiedergabe mithören) – nur echte Eingänge wie USB-Interface oder
  Mikrofon. Für einen reinen Analyse-Pi mit Line-In ist das egal; wer die
  Systemwiedergabe analysieren will, bleibt bei der Desktop-Variante.

### Autostart (Lite-Variante)

Konsolen-Autologin aktivieren:

```bash
sudo raspi-config   # System Options -> Boot / Auto Login -> Console Autologin
```

`~/.xinitrc` anlegen (Pfade ggf. anpassen):

```bash
#!/bin/sh
xset s off -dpms            # Bildschirmschoner und Blanking aus
matchbox-window-manager -use_cursor no &
exec $HOME/audio2midi-env/bin/python \
     $HOME/Audio2Midi/bpm_key_display.py --fullscreen
```

Und am Ende von `~/.bash_profile` (startet X nur auf der ersten Konsole,
nicht in SSH-Sitzungen):

```bash
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec startx
fi
```

So verhält sich der Pi wie ein Gerät: einschalten, Anzeige kommt.
`Esc` beendet die Anzeige – durch den Autologin startet sie sofort neu
(gewollt im Kiosk-Betrieb). Zum Arbeiten am Gerät per SSH anmelden oder
mit `Strg+Alt+F2` auf eine zweite Konsole wechseln.

## Robust gegen hartes Ausschalten (Overlay-Dateisystem)

Auf der Bühne wird der Pi meist einfach vom Strom getrennt – auf Dauer
riskiert das ein korruptes Dateisystem auf der SD-Karte. Abhilfe schafft
das Overlay-Dateisystem (funktioniert mit Desktop- und Lite-Variante):
Root wird read-only eingehängt, alle Schreibzugriffe landen nur noch im
RAM, und hartes Ausschalten ist jederzeit folgenlos.

```bash
sudo raspi-config   # Performance Options -> Overlay File System -> Enable
```

Wichtig:

- **Vorher** einmal normal starten und im Auswahlbildschirm Audio-Eingang,
  MIDI-Ausgang und Optionen festlegen, damit `display_config.json`
  geschrieben ist – mit aktivem Overlay gehen Änderungen daran beim
  Ausschalten verloren.
- Logs (`audio2midi.log`, optional `akkorde.txt`) landen dann ebenfalls
  nur noch flüchtig im RAM. Für die Fehlersuche das Overlay vorübergehend
  deaktivieren.
- Für Updates (git pull, pip install, Einstellungs-Änderungen) das Overlay
  in `raspi-config` deaktivieren, neu starten, ändern, wieder aktivieren.

## Wenn der Pi 4 nicht hinterherkommt

Die Analyse (HPSS + Chroma-CQT, jede Sekunde auf 8 s Audio) ist der teure
Teil. Falls die Anzeige zu träge aktualisiert, in
`realtime_bpm_key_midiclock.py` anpassen:

- `ANALYSIS_INTERVAL = 2.0` (statt 1.0) – halbiert die Last, kaum spürbar
- `BASS_TONIC_WEIGHT = 0` – schaltet das zusätzliche Bass-Chroma ab
  (spart ein zweites CQT pro Analyse; die Tonart-Erkennung verliert dann
  die Bass-Evidenz, die Dur von der Mollparallele unterscheidet)
- In `chroma_pcp()` notfalls `librosa.feature.chroma_stft` statt
  `chroma_cqt` verwenden (deutlich schneller, etwas ungenauere Tonart)

Die MIDI-Clock läuft davon unabhängig in ihrem eigenen Thread und bleibt
auch bei langsamer Analyse stabil.

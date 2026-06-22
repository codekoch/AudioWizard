#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realtime_bpm_key_midiclock_loopback.py
======================================

Echtzeit-Analyse von Audio (BPM + Tonart) mit stabiler MIDI-Clock-Ausgabe.
Laeuft unter Windows, macOS und Linux (inkl. Raspberry Pi).

NEU gegenueber der Grundversion:
  Du kannst als Quelle entweder
    (1) einen normalen Audio-Eingang / ein Mikrofon (ueber sounddevice)
        ODER
    (2) nur Windows: die Lautsprecher-/Kopfhoerer-AUSGABE mithoeren
        (Loopback, ueber soundcard / WASAPI) -- z. B. um zu analysieren,
        was Spotify gerade ueber deinen Kopfhoerer-Ausgang abspielt.
  waehlen.

Wiedergabe mithoeren auf den anderen Plattformen:
  macOS: ein virtuelles Ausgabegeraet wie BlackHole installieren
         (https://existential.audio/blackhole/) -- es erscheint dann als
         normaler Audio-Eingang in Modus (1).
  Linux/Raspberry Pi: die PipeWire/Pulse-"Monitor"-Quellen erscheinen
         direkt als normale Eingaenge in Modus (1).

Wichtig zum Loopback: Es wird ALLES erfasst, was an den gewaehlten
Ausgang geht (also auch Windows-Systemklaenge, Benachrichtigungen usw.),
nicht nur Spotify allein.

MIDI-Ausgang: Unter Windows braucht es einen vorhandenen Port (z. B.
loopMIDI). macOS/Linux koennen zusaetzlich einen eigenen virtuellen Port
erzeugen (CoreMIDI/ALSA) -- der erscheint dann in DAW & Co. als Eingang.

Installation:
    pip install -r requirements.txt

('soundcard' wird nur unter Windows fuer den Loopback-Modus gebraucht.)

Beenden mit Strg+C.
"""

import os
import sys
import math
import time
import queue
import warnings
import threading
from collections import deque

import numpy as np

# Die soundcard-Loopback-Aufnahme meldet bei jeder Leerlauf-/Stille-Phase des
# Ausgabegeraets "data discontinuity in recording". Das ist hier harmlos, wuerde
# aber die Statuszeile zumuellen -- daher gezielt nur diese Meldung stummschalten.
warnings.filterwarnings("ignore", message="data discontinuity in recording")

# ---------------------------------------------------------------------------
# Abhaengigkeiten
# ---------------------------------------------------------------------------
try:
    import sounddevice as sd
except ImportError:
    sd = None  # nur fuer den Eingangs-Modus noetig

try:
    import msvcrt  # Windows: Tastenabfrage waehrend des Laufs (Quelle/Ausgang wechseln)
except ImportError:
    msvcrt = None

try:
    import mido
except ImportError:
    sys.exit("Fehlt: 'mido'. Installiere mit: pip install mido python-rtmidi")

try:
    import librosa
except ImportError:
    sys.exit("Fehlt: 'librosa'. Installiere mit: pip install librosa")

try:
    import soundfile as sf  # nur fuer den Datei-Modus (verlustfreies Laden zur Wiedergabe)
except ImportError:
    sf = None

try:
    import scipy.signal as _sps  # nur fuer den DJ-EQ-Isolator (Biquad-Filter)
except ImportError:
    _sps = None


# ===========================================================================
# Konfiguration
# ===========================================================================
WINDOW_SECONDS        = 8.0     # Laenge des Analysefensters
ANALYSIS_INTERVAL     = 1.0     # wie oft (Sek.) neu analysiert wird
ANALYSIS_SR           = 22050   # Analyse-Abtastrate (Fenster wird ggf. heruntergerechnet)
ONSET_HOP             = 256     # Hop der Onset-Huellkurve (kleiner = feineres Tempo-Raster)
CHROMA_HOP            = 512     # Hop des Chromagramms (chroma_cqt-Standard)
PPQN                  = 24      # MIDI-Clock: 24 Pulse pro Viertelnote

VIRTUAL_MIDI          = "__virtual__"       # Sentinel statt Portname: eigenen
                                            #   virtuellen Port erzeugen (nur
                                            #   macOS/Linux, s. open_midi_output)
VIRTUAL_MIDI_NAME     = "Audio2Midi Clock"  # Name des erzeugten Ports

INPUT_SR              = 22050   # Wunschrate fuer den Eingangs-Modus (sounddevice)
LOOPBACK_SR           = 48000   # Aufnahmerate fuer Loopback (Windows-Mixer ist meist 48 kHz)
LOOPBACK_CHUNK        = 4096    # Frames pro Loopback-Lesevorgang

MIN_BPM               = 70.0    # Tempo-Suchbereich (genau eine Oktave: 70..140) -- so
MAX_BPM               = 140.0   #   ist die Oktav-Zuordnung eindeutig und das doppelte
                                #   Tempo (z. B. 144 statt 72) wird nicht mehr gewaehlt.
TEMPO_CENTER_BPM      = 100.0   # Mittelpunkt des Tempo-Priors (loest Oktav-Mehrdeutigkeit)
TEMPO_PRIOR_OCT       = 0.9     # Breite des Priors in Oktaven (groesser = schwaecher)
BPM_MEDIAN_LEN        = 16      # Median ueber die letzten N (~Sekunden) Tempo-Schaetzungen (robust)
TEMPO_MIN_CORR        = 0.08    # min. Autokorrelations-Koeffizient am besten Lag --
                                #   darunter ist keine klare Periodizitaet im Signal
                                #   und die Schaetzung wird verworfen (statt den
                                #   Median mit Rauschen zu fuellen)
TEMPO_FLUSH_DEV       = 0.05    # stimmen die juengsten Schaetzungen untereinander
                                #   ueberein, weichen aber > 5 % vom Median ab, hat
                                #   sich das Tempo wirklich geaendert -> alte
                                #   Schaetzungen verwerfen (schnellere Anpassung)
TEMPO_CONTINUITY      = 0.15    # leichter Score-Bonus (15 %) fuer Kandidaten nahe
                                #   am aktuellen Tempo -> unterdrueckt kurze
                                #   4/3- und 3/2-Aliase (z. B. 96 statt 72), ohne
                                #   echte Tempowechsel zu blockieren
KEY_TUNE_LOCK_N       = 5       # Stimmung des Stuecks ueber so viele Analysen
                                #   schaetzen (Median), dann bis zum naechsten
                                #   Song-Reset einfrieren. Faengt Material ab,
                                #   das nicht auf A440 liegt (gepitchte Tracks,
                                #   aeltere Aufnahmen) -- mit fest 0.0 landet
                                #   dessen Energie zwischen den Chroma-Bins.
                                #   0 = aus (feste Standardstimmung).
CHROMA_LOG_COMP       = 10.0    # Log-Kompression des Chromas: log1p(K*chroma).
                                #   Staucht laute Teiltoene, hebt leise an --
                                #   das Tonprofil haengt dann weniger an der
                                #   Abmischung (Standard in der Literatur). 0 = aus.
CHROMA_SALIENCE       = True    # Obertongewichtung vor der Chroma-Faltung
                                #   (Salience nach Gomez' HPCP-Idee): jeder
                                #   CQT-Bin wird durch die Energie auf seinen
                                #   Vielfachen gestuetzt, Nicht-Peaks fallen
                                #   weg. Daempft die Obertoene gespielter
                                #   Toene (h3 = Quinte, h5 = grosse Terz!),
                                #   die sonst das Tonprofil verschmieren.
                                #   Nebeneffekt: EIN CQT fuer Gesamt- UND
                                #   Bass-Chroma (statt zwei).
SAL_HARMONICS         = (1, 2, 3, 4)        # gestuetzt durch diese Vielfachen
SAL_WEIGHTS           = (1.0, 0.5, 0.33, 0.25)  # ... mit diesen Gewichten
KEY_EMA_SEC           = 15.0    # Zeitkonstante der schnellen Chroma-Mittelung;
                                #   die Tonart-Entscheidung nutzt 50 % davon und
                                #   50 % Gesamtmittel seit Songbeginn -> reagiert
                                #   anfangs schnell, wird mit der Zeit stabiler
BASS_TONIC_WEIGHT     = 0.30    # Bonus fuer Tonarten, deren Grundton den Bass
                                #   dominiert (unterscheidet Dur von der
                                #   Mollparallele -- gleiches Tonmaterial!)
KEY_SWITCH_CONFIRM    = 2       # Tonartwechsel erst nach N uebereinstimmenden
                                #   Folge-Schaetzungen anzeigen (gegen Flackern)
KEY_CONFIDENT_MARGIN  = 0.12    # Mindestvorsprung des besten Tonart-Kandidaten
                                #   vor dem zweitbesten, damit die Tonart als
                                #   "sicher" gilt (Anzeige sonst gedimmt).
                                #   Skala haengt an der Chroma-Aufbereitung
                                #   (Log-Kompression, Salience) -- nach
                                #   Aenderungen dort per Sweep in
                                #   eval_detection.py neu kalibrieren.
KEY_CONFIDENT_MIN_N   = 16      # Mindestzahl Analysen (~Sekunden), bevor die
                                #   Tonart ueberhaupt als "sicher" gelten darf.
                                #   16 schneidet die hochmargigen Fruehfehler
                                #   (harmonisch anders zentrierte Intros) weg,
                                #   die keine Vorsprung-Huerde erwischt, und
                                #   kostet korrekte Stuecke nur wenige Sekunden
                                #   "sicher"-Anzeige (per Sweep nachgemessen).
CHORD_ENABLED         = False   # Akkorderkennung an/aus (GUI-Option). Der
                                #   Akkord kommt aus dem juengsten Stueck des
                                #   OHNEHIN berechneten Chromagramms -- kostet
                                #   praktisch keine zusaetzliche CPU, folgt
                                #   aber nur im Analyse-Takt (~1 s).
CHORD_TAIL_SEC        = 2.5     # so viele juengste Sekunden des Chromagramms
                                #   bestimmen den aktuellen Akkord. 1,5 s war
                                #   im eval_chords-Proxy klar schlechter
                                #   (rauschigeres Chroma -> mehr leiterfremde
                                #   Deutungen und mehr Flackern).
CHORD_TAIL_BEAT       = False   # Akkord-Fenster an der letzten Beat-Grenze
                                #   ausrichten (Laenge = Zeit seit letztem Beat
                                #   + CHORD_TAIL_BEATS Perioden). GETESTET UND
                                #   VERWORFEN: bei gleicher effektiver Laenge
                                #   schlechter als das feste Fenster -- die
                                #   Beat-Phase aus dem 8-s-Fenster jittert zu
                                #   stark, um als Fensteranker zu taugen.
CHORD_TAIL_BEATS      = 3       # ganze Beat-Perioden des beat-synchronen
                                #   Fensters (nur bei CHORD_TAIL_BEAT)
CHORD_TAIL_MIN        = 0.6     # Klemmen der beat-synchronen Fensterlaenge:
CHORD_TAIL_MAX        = 2.5     #   nie kuerzer als ~1 Beat Chroma-Substanz,
                                #   nie laenger als ein typischer Akkord
CHORD_BASS_WEIGHT     = 0.4     # Bonus, wenn der Akkord-Grundton den Bass
                                #   dominiert -- trennt Umkehrungen und ton-
                                #   verwandte Deutungen (C-Dur vs. Am7)
CHORD_STICKY          = 0.02    # kleiner Score-Bonus fuer den zuletzt
                                #   erkannten Akkord (nur noch fuer die
                                #   Einzelbild-Funktion classify_chord; der
                                #   Worker glaettet stattdessen per HMM, s. u.)
CHORD_SELF_P          = 0.85    # HMM-Glaettung (ChordTracker): Wahrschein-
                                #   lichkeit, dass der Akkord von einer Analyse
                                #   (~1 s) zur naechsten derselbe bleibt --
                                #   hoeher = traeger, weniger Flackern.
                                #   (0.85 lag im eval_chords-Proxy gleichauf
                                #   mit 0.9, reagiert aber schneller auf
                                #   echte Wechsel.)
CHORD_TEMP            = 10.0    # Schaerfe der Softmax, die Template-Scores in
                                #   Beobachtungswahrscheinlichkeiten uebersetzt
                                #   (hoeher = Beobachtung schlaegt Traegheit
                                #   schneller)
KEY_CHORD_PRIOR       = 0.05    # Score-Bonus fuer leitereigene Akkorde der
                                #   aktuell erkannten Tonart -- unterdrueckt
                                #   exotische Fehldeutungen (0 = aus)
CHORD_FAST            = False   # Schneller Akkord-Pfad (GUI-Option): eigene,
                                #   leichte Analyse NUR fuer den Akkord im
                                #   CHORD_FAST_INTERVAL-Takt auf den juengsten
                                #   CHORD_FAST_WIN Sekunden -- der Akkord folgt
                                #   dann ~3x pro Sekunde statt im 1-s-Takt der
                                #   grossen Analyse. Kostet zusaetzliche CPU;
                                #   auf schwacher Hardware (Pi) aus lassen,
                                #   dann laeuft der bisherige 1-s-Pfad.
CHORD_FAST_INTERVAL   = 0.2     # Abstand der schnellen Akkord-Analysen (Sek.)
                                #   -- bei ~18 ms je Analyse (12-Bin-CQT)
                                #   bleibt das unter 10 % eines Kerns; das
                                #   Innovations-Gate braucht 2 Bestaetigungen,
                                #   profitiert also direkt vom kurzen Takt
CHORD_FAST_WIN        = 2.0     # so viele juengste Sekunden verarbeitet der
                                #   schnelle Pfad (= das Akkordfenster; eine
                                #   eigene Tail-Mittelung entfaellt). Kuerzer
                                #   als die 2,5 s des 1-s-Pfads: die ~3x
                                #   hoehere Beobachtungsrate mittelt das
                                #   Rauschen ueber die ueberlappenden Fenster
                                #   weg -- Qualitaet hielt im eval_chords-
                                #   Proxy stand, 1,5 s war zu kurz.
CHORD_FAST_HOP        = 1024    # groesserer Chroma-Hop im schnellen Pfad:
                                #   das Fenster wird ohnehin gemittelt,
                                #   weniger Frames = weniger CQT/Salience-CPU
CHORD_FAST_OCTAVES    = 5       # CQT-Umfang des schnellen Pfads, ab C2:
                                #   die sehr langen Filter der C1-Oktave
                                #   lohnen auf 2,5 s nicht; Bass-Profil aus
                                #   den unteren 2 Oktaven derselben CQT
CHORD_FAST_SAL_PEAKS  = True    # Salience-Peak-Filterung im schnellen Pfad.
                                #   GEMESSEN: ohne sie klar schlechter (D-Moll
                                #   -13 pp) -- sie uebernimmt hier die Drum-
                                #   Unterdrueckung, denn der schnelle Pfad
                                #   hat keine HPSS-Trennung.
CHORD_FAST_BINS_OCT   = 12      # CQT-Aufloesung des schnellen Pfads. 12 statt
                                #   36 Bins/Oktave: das Feinraster diente nur
                                #   der Tuning-Robustheit, die Stimmung ist
                                #   aber pro Song geschaetzt und eingefroren.
                                #   Kuerzere Filter (C2: 0,26 statt 0,79 s!)
                                #   = bessere Zeitaufloesung im Bass und
                                #   ~3x weniger CQT-CPU (Unschaerfeprinzip:
                                #   Frequenz- gegen Zeitaufloesung).
                                #   GEMESSENER TRADEOFF: 12 Bins ~0,9 s
                                #   Wechsel-Latenz bei 92 % Diatonik-Proxy
                                #   (E-Dur-Testdatei); 36 Bins ~1,9 s bei
                                #   95 % -- die langen Bassfilter
                                #   verschmieren auch den Onset-Anker.
CHORD_FAST_HALF_LIFE  = 1.0     # Recency-Gewichtung im schnellen Fenster:
                                #   Frame-Gewicht halbiert sich je so viele
                                #   Sekunden Alter -- ein neuer Akkord
                                #   dominiert das Profil frueher, der alte
                                #   Schwanz stabilisiert weiter (0 = aus).
                                #   1,0 s: gleiche Diatonik-Quote wie der
                                #   1-s-Pfad bei ~1,8 s Wechsel-Latenz
                                #   (statt ~2-3 s); 0,7 s war minimal
                                #   schneller, aber messbar flackriger.
                                #   Greift nur als RUECKFALL, wenn kein
                                #   Onset-Anker gefunden wird (s. u.).
CHORD_ONSET_ANCHOR    = True    # Akkordfenster am letzten starken Onset
                                #   verankern: Akkordwechsel liegen auf
                                #   Anschlaegen -- ab dem Anker zaehlt nur
                                #   noch, was DANACH klang (onset-synchrone
                                #   Chroma-Mittelung, Bello & Pickens 2005).
                                #   Onsets sind lokale Ereignisse, anders
                                #   als die (verworfene) extrapolierte
                                #   Beat-Phase jittert hier nichts.
CHORD_ONSET_STD       = 1.5     # Onset-Schwelle: Spektralfluss-Frames ueber
                                #   Mittel + so viele Standardabweichungen
                                #   gelten als Anschlag
CHORD_ONSET_MIN_TAIL  = 0.3     # Mindestmaterial NACH dem Anker (Sek.) --
                                #   sonst gilt der vorherige Anker bzw. der
                                #   Recency-Rueckfall (zu kurzes Segment
                                #   liefert nur Rauschen)
CHORD_ANCHOR_NOV      = 0.3     # Harmonie-Neuheit (1 - Pearson zwischen dem
                                #   Profil vor und nach dem Onset), damit ein
                                #   Onset das Fenster neu verankern darf
                                #   (Harmonic-Change-Idee, Harte et al. 2006).
                                #   Ohne diese Pruefung stutzt JEDER Drum-
                                #   Schlag das Fenster auf den letzten Beat
                                #   -> rauschige Profile, Flackern (gemessen).
CHORD_ANCHOR_PRE      = 1.0     # so viel Kontext (Sek.) VOR dem Onset geht
                                #   in den Neuheits-Vergleich -- ein kurzes
                                #   Vorher-Fenster ist selbst zu verrauscht,
                                #   um Harmoniewechsel von Beats zu trennen
CHORD_ALT_MARGIN      = 0.40    # Mindestabstand des Kurzsegment-Vorschlags
                                #   zum besten Akkord mit ANDEREM GRUNDTON,
                                #   damit er als Wechsel-Verdacht zaehlt.
                                #   Bewusst weder der Abstand zum Zweit-
                                #   platzierten (bei Verwandten wie F vs.
                                #   Fmaj7 prinzipiell winzig, sagt nichts
                                #   ueber den Wechsel) noch zum aktuellen
                                #   Akkord (nach einem Fehl-Kipp kaskadiert
                                #   das; beides gemessen). Ein klarer neuer
                                #   Akkord liegt ~0,5 ueber fremden Grund-
                                #   toenen, Melodie-Phrasen deutlich tiefer.
                                #   (Das Segment auch als HAUPT-Beobachtung
                                #   zu nutzen wurde gemessen und verworfen:
                                #   Post-Onset-Segmente sind melodie-
                                #   dominiert und druecken die Qualitaet --
                                #   als reiner Wechsel-DETEKTOR ist es
                                #   dagegen unschaedlich.)
CHORD_GATE_SELF_P     = 0.2     # Traegheit waehrend des Gate-Updates: schlaegt
                                #   das Kurzsegment 2x in Folge denselben
                                #   anderen Akkord vor, wird einmalig mit
                                #   dieser (niedrigen) Selbstuebergangs-
                                #   wahrscheinlichkeit aktualisiert -> der
                                #   Tracker kippt sofort, statt ~0,7 s zu
                                #   warten (Innovation-Gating)
CHORD_LOG_PATH        = None    # Textdatei fuers Akkord-Protokoll (GUI-
                                #   Option); None = kein Protokoll
ANALYSIS_QUEUE_MAX    = 256     # max. gepufferte Bloecke fuer die Analyse --
                                #   verhindert unbegrenztes Speicherwachstum,
                                #   falls die Analyse haengt (aeltester fliegt)
RESAMPLE_CTX          = 2048    # Roh-Samples Kontext fuers blockweise Resampling
                                #   (vermeidet Filterartefakte an den Nahtstellen)
BEAT_VALID_SEC        = 8.0     # so lange gilt ein Beat-Anker aus der Analyse
BEAT_NUDGE_MAX        = 0.0015  # max. Phasenkorrektur der Clock pro Tick (Sek.)
BEAT_NUDGE_GAIN       = 0.1     # Anteil des Phasenfehlers, der pro Tick
                                #   korrigiert wird (sanfte Regelschleife)
BEAT_ANCHOR_EMA       = 0.3     # Glaettung des Beat-Ankers ueber die Analysen:
                                #   Anteil, mit dem eine neue Phasenmessung in
                                #   den gefilterten Anker eingeht. Einzelne
                                #   Messungen rauschen (~10-20 ms, Phasen-
                                #   Histogramm); ohne Glaettung jagt die
                                #   Nudge-Schleife der Clock jedem Messwert
                                #   einzeln nach -> hoerbares Phasen-Zappeln.
                                #   1.0 = aus (jede Messung gilt sofort).

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "audio2midi.log")
CHORD_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "akkorde.txt")


def log_message(text):
    """Zeile mit Zeitstempel an die Logdatei anhaengen (Fehler ignorieren).
    Im Kiosk-Betrieb (GUI-Autostart auf dem Pi) gibt es keine Konsole --
    ohne Logdatei waeren Fehler dort unsichtbar."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + text + "\n")
    except Exception:
        pass


def feed_analysis(audio_q, block):
    """Block samt Capture-Zeitstempel in die Analyse-Queue legen; bei Stau
    aeltesten Block verwerfen.

    Der Zeitstempel (perf_counter beim Eintreffen) datiert das LETZTE Sample
    des Blocks und dient dem Analyse-Worker als Wanduhr-Zeit des Pufferendes
    (Beat-Anker). Wuerde der Worker stattdessen beim ABHOLEN stempeln, kaeme
    -- je nachdem, wann er nach einer ~0,5-s-Analyse die Queue leert -- bis
    zu eine Chunk-Laenge (~85 ms) Jitter in den Anker, dem die beat-synchrone
    Clock dann hoerbar hinterherregeln muesste."""
    try:
        if audio_q.qsize() >= ANALYSIS_QUEUE_MAX:
            try:
                audio_q.get_nowait()
            except queue.Empty:
                pass
        audio_q.put_nowait((block, time.perf_counter()))
    except Exception:
        pass
SILENCE_DB            = -50.0   # Pegel darunter gilt als Stille
SILENCE_RESET_SEC     = 2.0     # so lange Stille (Pause/Songwechsel) -> Analyse zuruecksetzen
CLOCK_SLEW_BPM_PER_S  = 4.0     # max. Tempoaenderung der Clock pro Sekunde
CLOCK_DEADBAND_FRAC   = 0.002   # Totband der Clock: weicht das Zieltempo um
                                #   weniger als so viel (relativ, 0,2 %) vom
                                #   Clock-Tempo ab, bleibt die Clock konstant.
                                #   Der BPM-Median wackelt von Analyse zu
                                #   Analyse um ~+-0,1 BPM (Mess-Quantisierung)
                                #   -- ohne Totband faehrt die Clock jedes
                                #   Wackeln per Slew nach, ein konstantes
                                #   Stueck bekommt so nie ein konstantes
                                #   Tempo. Der stehenbleibende Restfehler
                                #   (max. 0,2 %) ist unhoerbar; im Beat-Sync-
                                #   Modus raeumt ihn die Nudge-Schleife ab.
CLOCK_JUMP_FRAC       = 0.20    # weicht das Zieltempo um mehr als 20 % von der
                                #   Clock ab, sofort springen statt slewen --
                                #   das Slewen wuerde sonst ~15+ s dauern, in
                                #   denen Clock und BPM-Anzeige sichtbar
                                #   auseinanderlaufen (bis Faktor ~2)
INITIAL_BPM           = 120.0

AUDIO_BLOCKSIZE       = 2048    # Blockgroesse fuer den Eingangs-Modus

MONITOR_QUEUE_MAX     = 8       # max. gepufferte Bloecke beim Mithören (begrenzt die Latenz)


# Tonprofile (Index 0 = Grundton). Sha'ath-Profile (wie in "KeyFinder"):
# unterscheiden Dur und seine Moll-Parallele zuverlaessiger als Krumhansl-Kessler
# -- im Test gegen echte Stuecke deutlich treffsicherer. Auch die
# Albrecht-Shanahan-Korpusprofile (2013) wurden per eval_detection.py
# gemessen und VERWORFEN: auf Pop-/Dance-Material klar schlechter
# (Paralleltonart-Verwechslung; sie stammen aus Klassik-Korpora).
KS_MAJOR = np.array([6.6, 2.0, 3.5, 2.3, 4.6, 4.0,
                     2.5, 5.2, 2.4, 3.7, 2.3, 3.4])
KS_MINOR = np.array([6.5, 2.7, 3.5, 5.4, 2.6, 3.5,
                     2.5, 5.2, 4.0, 2.7, 4.3, 3.2])
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F',
              'F#', 'G', 'G#', 'A', 'A#', 'B']

# Akkord-Schablonen: Intervalle (Halbtoene ueber dem Grundton) -> Gewicht.
# Septimen schwaecher gewichtet: der Vierklang gewinnt nur, wenn die Septime
# wirklich klar im Signal liegt, sonst bleibt es der Dreiklang. sus2 fehlt
# bewusst (Csus2 = gleiche Tonklassen wie Gsus4), aug ebenso (grossterz-
# symmetrisch, Grundton nicht bestimmbar).
CHORD_TYPES = [
    ("",     {0: 1.0, 4: 1.0, 7: 1.0}),            # Dur
    ("m",    {0: 1.0, 3: 1.0, 7: 1.0}),            # Moll
    ("7",    {0: 1.0, 4: 1.0, 7: 1.0, 10: 0.7}),   # Dominantseptakkord
    ("maj7", {0: 1.0, 4: 1.0, 7: 1.0, 11: 0.7}),   # grosse Septime
    ("m7",   {0: 1.0, 3: 1.0, 7: 1.0, 10: 0.7}),   # Mollseptakkord
    ("dim",  {0: 1.0, 3: 1.0, 6: 1.0}),            # vermindert
    ("sus4", {0: 1.0, 5: 1.0, 7: 1.0}),            # Quartvorhalt
]


def _build_chord_templates():
    """(namen, matrix): je Grundton x Akkordtyp eine zentrierte, normierte
    12er-Schablone. Damit liefert matrix @ chroma_zentriert_normiert die
    Pearson-Korrelation ALLER Akkorde in einem Schritt."""
    names, rows = [], []
    for i in range(12):
        for suffix, ivs in CHORD_TYPES:
            t = np.zeros(12)
            for iv, w in ivs.items():
                t[(i + iv) % 12] = w
            t -= t.mean()
            rows.append(t / np.linalg.norm(t))
            names.append(NOTE_NAMES[i] + suffix)
    return names, np.vstack(rows)


CHORD_NAMES, _CHORD_MAT = _build_chord_templates()
_CHORD_IDX = {n: k for k, n in enumerate(CHORD_NAMES)}


# ===========================================================================
# Gemeinsamer Zustand
# ===========================================================================
class Shared:
    def __init__(self):
        self.lock = threading.Lock()
        self.target_bpm = INITIAL_BPM
        self.raw_bpm = 0.0
        self.key = "—"
        self.key_confident = False  # Tonart-Vorsprung gross genug? (Anzeige)
        self.chord = "—"          # aktueller Akkord (nur bei CHORD_ENABLED)
        self.level = 0.0          # aktueller Eingangspegel (RMS, linear)
        self.level_time = 0.0     # perf_counter des letzten Pegel-Updates
        self.capture_sr = float(ANALYSIS_SR)  # aktuelle Aufnahmerate (live aenderbar)
        self.have_estimate = False
        self.hold = False         # Analyse eingefroren (lange Breaks):
                                  # Ergebnisse bleiben stehen, kein
                                  # Stille-Reset, Clock laeuft konstant
        self.reset_request = False  # manueller Neustart der Analyse (GUI):
                                  # der Worker leert Puffer und Historie
                                  # und beginnt von vorn; die Clock stoppt
                                  # bis zur naechsten echten Schaetzung
        self.fast_buf = None      # Kopie der juengsten Audio-Sekunden
                                  # (ANALYSIS_SR) fuer den schnellen
                                  # Akkord-Thread; legt der Analyse-Worker ab
        self.tuning = 0.0         # eingefrorene Stimmung des Stuecks
                                  # (TuningEstimator), fuer den Akkord-Thread
        self.chord_epoch = 0      # zaehlt Analyse-Resets: der schnelle
                                  # Akkord-Thread verwirft seinen Zustand,
                                  # sobald sich der Wert aendert
        self.chord_logged = False # seit letzter Trennmarke Akkorde im
                                  # Protokoll? (je nach Modus schreibt der
                                  # Worker ODER der Akkord-Thread Akkorde;
                                  # die Trennmarken schreibt der Worker)
        self.beat_sync = False    # Clock auf den Beat einrasten (GUI-Option)
        self.beat_anchor = 0.0    # perf_counter-Zeit eines erkannten Beats
        self.beat_period = 0.0    # Beat-Abstand in Sekunden
        self.beat_valid_time = 0.0  # wann der Anker zuletzt erneuert wurde
        self.note_display = "—"   # aktuelle Note(n) im Noten-Modus (Anzeige)
        self.rec_active = False   # Aufnahme laeuft: der Capture sammelt die
                                  # analysierten Mono-Bloecke (capture_sr)
        self.rec_blocks = []      # gesammelte Mono-Bloecke (unter shared.lock)


# ===========================================================================
# Analyse: Tempo und Tonart
# ===========================================================================
def estimate_tempo(y, sr, prev_bpm=0.0):
    """Schaetzt das Tempo direkt aus der Autokorrelation der Onset-Huellkurve.

    Am besten bekommt diese Funktion den PERKUSSIVEN Anteil des Signals
    (siehe split_harmonic_percussive): dann bestimmen die Drums das Tempo,
    und dazukommende Instrumente/Flaechen koennen die Onset-Kurve nicht
    verschmutzen.

    Statt sich auf librosas grobe (und durch einen 120-BPM-Prior verzerrte)
    tempo()-Schaetzung zu verlassen, wird die staerkste Periodizitaet direkt im
    erlaubten Bereich [MIN_BPM, MAX_BPM] gesucht:
      1. Onset-Huellkurve -> Autokorrelation. (Aggregation ueber die
         Mel-Baender per Mittelwert: im Test auf dem perkussiven Anteil
         und auch auf dem Voll-Mix treffsicherer als der Median.)
      2. Autokorrelation pro Lag auf die Zahl der Summanden normieren --
         die rohe Autokorrelation faellt linear mit dem Lag und bevorzugt
         sonst systematisch hohe BPM.
      3. Kammfilter: jeder Lag-Kandidat wird durch seine 2- und 3-fache
         Periode sowie das Achtelraster der halben Periode gestuetzt.
      4. Sanfter Tempo-Prior (log-normal um TEMPO_CENTER_BPM), um Oktav-
         Mehrdeutigkeiten (halbes/doppeltes Tempo) aufzuloesen.
      5. Peak parabolisch interpolieren -> sub-Frame-genaues Tempo.
         Ist die Periodizitaet zu schwach (< TEMPO_MIN_CORR), wird 0
         geliefert -- besser keine Schaetzung als eine zufaellige.
    """
    try:
        onset_env = librosa.onset.onset_strength(y=y, sr=sr,
                                                 hop_length=ONSET_HOP)
    except Exception:
        return 0.0
    return _tempo_from_onset_env(onset_env, sr / ONSET_HOP, prev_bpm)


def _tempo_from_onset_env(onset_env, fr, prev_bpm=0.0):
    """Tempo-Scoring (Schritte 2-5 von estimate_tempo) auf einer fertigen
    Onset-Huellkurve mit fr Frames pro Sekunde."""
    try:
        if not np.any(onset_env):
            return 0.0

        oe = onset_env - onset_env.mean()
        n = len(oe)
        ac = np.correlate(oe, oe, mode='full')[n - 1:]
        if len(ac) < 4:
            return 0.0
        acn = ac / np.maximum(1.0, n - np.arange(n))   # pro Lag entzerren
        var = acn[0]
        if var <= 0:
            return 0.0
        acn = acn / var                            # -> Koeffizienten (max. 1)
        acn[0] = 0.0                               # Null-Lag ignorieren

        min_lag = max(1, int(round(60.0 * fr / MAX_BPM)))
        max_lag = min(n - 2, int(round(60.0 * fr / MIN_BPM)))
        if max_lag <= min_lag:
            return 0.0

        def comb(k):                               # Stuetzung durch Vielfache
            s = acn[k]
            h = k // 2                             # ... und durch die halbe
            s = s + 0.3 * max(acn[h], acn[h + 1])  # Periode (Achtelraster):
            if 2 * k < n:                          # das echte Tempo hat sie,
                s = s + 0.5 * acn[2 * k]           # ein 4/3-Alias (96 statt
            if 3 * k < n:                          # 72) nicht
                s = s + 0.25 * acn[3 * k]
            return s

        lags = np.arange(min_lag, max_lag + 1)
        score = acn[lags].copy()
        lh = lags // 2
        score += 0.3 * np.maximum(acn[lh], acn[lh + 1])
        l2 = 2 * lags
        m = l2 < n
        score[m] += 0.5 * acn[l2[m]]
        l3 = 3 * lags
        m = l3 < n
        score[m] += 0.25 * acn[l3[m]]
        bpms = 60.0 * fr / lags
        prior = np.exp(-0.5 * (np.log2(bpms / TEMPO_CENTER_BPM) / TEMPO_PRIOR_OCT) ** 2)
        score *= prior
        if prev_bpm > 0:
            # Kontinuitaet: Kandidaten nahe am bisherigen Tempo leicht bevorzugen
            score *= 1.0 + TEMPO_CONTINUITY * np.exp(
                -0.5 * (np.log2(bpms / prev_bpm) / 0.1) ** 2)

        k = min_lag + int(np.argmax(score))       # bester Lag im Bereich
        if acn[k] < TEMPO_MIN_CORR:               # keine klare Periodizitaet
            return 0.0
        a, b, c = comb(k - 1), comb(k), comb(k + 1)
        denom = a - 2.0 * b + c
        offset = 0.5 * (a - c) / denom if denom != 0 else 0.0
        offset = float(np.clip(offset, -0.5, 0.5))
        lag = k + offset
        if lag <= 0:
            return 0.0
        return 60.0 * fr / lag
    except Exception:
        return 0.0


FOLD_EDGE_TOL = 1.04    # Schaetzungen knapp (< 4 %) ausserhalb des BPM-Bereichs
                        #   sind DOPPELDEUTIG: z. B. 141 kann Mess-Jitter eines
                        #   140-BPM-Stuecks sein (richtig: 140) ODER das
                        #   Doppeltempo eines 70.5-BPM-Stuecks (richtig: 70.5).
                        #   Stures Oktav-Falten liess die Anzeige bei
                        #   Grenz-Tempi aufs halbe Tempo kippen, waehrend die
                        #   Clock nur langsam nachzog ("fast doppelt so
                        #   schnell") -> mit bisherigem Tempo als Kontext
                        #   gewinnt der Kandidat, der ihm naeher liegt.


def fold_bpm(bpm, prev=0.0):
    """BPM per Oktav-Falten in [MIN_BPM, MAX_BPM] bringen. Liegt der Wert nur
    knapp ausserhalb (FOLD_EDGE_TOL) und gibt es ein bisheriges Tempo (prev),
    entscheidet die Naehe zu prev zwischen Bereichsgrenze und Oktav-Faltung."""
    if bpm <= 0:
        return bpm
    edge = 0.0
    if MAX_BPM < bpm <= MAX_BPM * FOLD_EDGE_TOL:
        edge = MAX_BPM
    elif MIN_BPM > bpm >= MIN_BPM / FOLD_EDGE_TOL:
        edge = MIN_BPM
    while bpm < MIN_BPM:
        bpm *= 2.0
    while bpm > MAX_BPM:
        bpm /= 2.0
    if edge > 0 and prev > 0 and \
            abs(math.log2(edge / prev)) < abs(math.log2(bpm / prev)):
        return edge
    return bpm


def split_harmonic_percussive(y):
    """EINE HPSS-Zerlegung fuer beide Analyse-Pfade: (y_harm, y_perc).

    Der harmonische Anteil (Flaechen, Bass, Gesang) geht in die Tonart-
    Erkennung, der perkussive (Drums: kurze, breitbandige Transienten)
    in die Tempo-Erkennung. Die Trennung erfolgt nach Signalstruktur,
    nicht nach Frequenz -- ein Basslauf im Kick-Bereich landet trotzdem
    im harmonischen Teil. Bei Fehlern wird (y, y) geliefert."""
    try:
        D = librosa.stft(y)
        H, P = librosa.decompose.hpss(D, margin=4.0)
        y_h = librosa.istft(H, length=len(y))
        y_p = librosa.istft(P, length=len(y))
        return y_h, y_p
    except Exception:
        return y, y


class TuningEstimator:
    """Stimmung (Abweichung von A440) des laufenden Stuecks schaetzen.

    Die ersten KEY_TUNE_LOCK_N Analysen wird die Stimmung aus dem
    harmonischen Anteil geschaetzt und der Median gebildet, danach ist
    der Wert bis reset() eingefroren. So bleibt die Chroma-Zuordnung
    innerhalb eines Stuecks stabil (das Problem der frueheren
    Pro-Fenster-Schaetzung), aber nicht-A440-Material (gepitchte Tracks,
    aeltere Aufnahmen) landet trotzdem auf den richtigen Bins.
    Einheit: Bruchteile eines CQT-Bins bei 36 Bins/Oktave -- direkt an
    chroma_cqt(tuning=...) durchreichbar."""

    def __init__(self):
        self.hist = []
        self.value = 0.0

    def reset(self):
        self.hist = []
        self.value = 0.0

    def update(self, y_harm, sr):
        """Naechstes Analysefenster einarbeiten; liefert die aktuelle
        (ggf. schon eingefrorene) Stimmung."""
        if len(self.hist) >= KEY_TUNE_LOCK_N:
            return self.value
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # "empty frequency set" u. ae.
                t = float(librosa.estimate_tuning(y=y_harm, sr=sr,
                                                  bins_per_octave=36))
            if math.isfinite(t):
                self.hist.append(t)
                self.value = float(np.median(self.hist))
        except Exception:
            pass
        return self.value


def chroma_pcp(y, sr, y_harm=None, tail_sec=0.0, tuning=0.0):
    """Chroma-Gesamtprofil + Bass-Profil: (pcp, bass) mit je 12 Werten
    (auf Summe 1 normiert) oder None.

    Mit tail_sec > 0 kommen zwei weitere Profile dazu -- (pcp, bass,
    tail, tail_bass) -- die nur die juengsten tail_sec Sekunden des
    Chromagramms mitteln: "was klingt gerade" fuer die Akkorderkennung.
    Das Chromagramm liegt fuer die Tonart ohnehin vor, der Schwanz kostet
    also nur zwei kleine Mittelwerte extra.

    Vor der Chroma-Berechnung wird der harmonische Anteil isoliert (HPSS).
    So verwaschen Schlagzeug/Perkussion das Tonprofil nicht -- gerade bei
    Pop-/Dance-Material (z. B. Spotify ueber Loopback) verbessert das die
    Tonart-Erkennung deutlich. chroma_cqt korreliert ausserdem besser mit den
    Krumhansl-Profilen als das geglaettete/quantisierte chroma_cens.

    Das Bass-Profil (nur C1..H3, also ~33..247 Hz) liefert zusaetzliche
    Evidenz fuer den Grundton: Die Tonika liegt ueberproportional oft im
    Bass auf schweren Zaehlzeiten. classify_key() nutzt das vor allem, um
    Dur von der Mollparallele zu unterscheiden (gleiches Tonmaterial!).
    """
    try:
        if y_harm is None:      # kein vorab getrennter Anteil uebergeben
            try:
                y_harm = librosa.effects.harmonic(y, margin=4.0)
            except Exception:
                y_harm = y
        # tuning: FESTER Wert je Stueck (TuningEstimator) statt der
        # chroma_cqt-eigenen Schaetzung pro Fenster. Die warnt bei tonlosen
        # Fenstern ("empty frequency set"), kostet CPU und laesst die
        # Chroma-Zuordnung zwischen den Analysefenstern springen.
        fmin = librosa.note_to_hz('C1')
        bchroma = None
        if CHROMA_SALIENCE:
            # CQT einmal ueber 7 Oktaven, Obertongewichtung, dann Faltung;
            # das Bass-Chroma kommt aus den unteren 3 Oktaven DERSELBEN CQT.
            n_bins = 7 * 36
            C = np.abs(librosa.cqt(y_harm, sr=sr, fmin=fmin, n_bins=n_bins,
                                   bins_per_octave=36, tuning=tuning,
                                   hop_length=CHROMA_HOP))
            freqs = librosa.cqt_frequencies(n_bins=n_bins, fmin=fmin,
                                            bins_per_octave=36)
            C = librosa.salience(C, freqs=freqs, harmonics=SAL_HARMONICS,
                                 weights=SAL_WEIGHTS, fill_value=0.0)
            chroma = librosa.feature.chroma_cqt(
                C=C, sr=sr, fmin=fmin, n_octaves=7, bins_per_octave=36,
                hop_length=CHROMA_HOP)
            if BASS_TONIC_WEIGHT > 0:
                bchroma = librosa.feature.chroma_cqt(
                    C=C[:3 * 36], sr=sr, fmin=fmin, n_octaves=3,
                    bins_per_octave=36, hop_length=CHROMA_HOP)
        else:
            chroma = librosa.feature.chroma_cqt(y=y_harm, sr=sr,
                                                tuning=tuning,
                                                hop_length=CHROMA_HOP)
            if BASS_TONIC_WEIGHT > 0:   # = 0 spart das zweite CQT
                try:
                    bchroma = librosa.feature.chroma_cqt(
                        y=y_harm, sr=sr, fmin=fmin,
                        n_octaves=3, tuning=tuning, hop_length=CHROMA_HOP)
                except Exception:
                    bchroma = None
        if CHROMA_LOG_COMP > 0:
            chroma = np.log1p(CHROMA_LOG_COMP * chroma)
            if bchroma is not None:
                bchroma = np.log1p(CHROMA_LOG_COMP * bchroma)
        pcp = chroma.mean(axis=1)
        s = pcp.sum()
        if s <= 0:
            return None
        pcp = pcp / s             # Normierung -> laute Stellen dominieren nicht
        bass = np.zeros(12)
        if bchroma is not None:
            bass = bchroma.mean(axis=1)
            bs = bass.sum()
            bass = bass / bs if bs > 0 else np.zeros(12)
        if tail_sec <= 0:
            return pcp, bass
        k = max(1, int(round(tail_sec * sr / CHROMA_HOP)))
        tail = chroma[:, -k:].mean(axis=1)
        ts = tail.sum()
        tail = tail / ts if ts > 0 else None
        tail_bass = np.zeros(12)
        if bchroma is not None:
            tail_bass = bchroma[:, -k:].mean(axis=1)
            bts = tail_bass.sum()
            tail_bass = tail_bass / bts if bts > 0 else np.zeros(12)
        return pcp, bass, tail, tail_bass
    except Exception:
        return None


def chroma_pcp_fast(y, sr, tuning=0.0):
    """Leichtgewichtiges Chroma NUR fuer den schnellen Akkord-Pfad:
    (pcp, bass, alt_pcp, alt_bass) oder None.

    pcp/bass: stabile Hauptbeobachtung (Recency-gewichtetes Fenster bzw.
    onset-verankertes Segment, wenn es lang genug ist). alt_pcp/alt_bass:
    Profil des onset-verankerten KURZsegments als Wechsel-Verdacht fuer
    das Innovations-Gate des ChordTracker -- None, wenn kein harmonisch
    neuer Onset gefunden wurde.

    Gegenueber chroma_pcp eingespart: die HPSS-Trennung (die Salience-
    Peak-Filterung daempft breitbandige Drums bereits), groeberer Hop
    (CHORD_FAST_HOP statt CHROMA_HOP -- das Fenster wird ohnehin
    gemittelt) und 5 statt 7 Oktaven ab C2 (CHORD_FAST_OCTAVES).
    Aufbereitung sonst wie im grossen Pfad: Salience-Obertongewichtung,
    Log-Kompression, Bass-Profil aus den unteren 2 Oktaven derselben CQT."""
    try:
        if y is None or not np.any(y):
            return None
        bpo = CHORD_FAST_BINS_OCT
        fmin = librosa.note_to_hz('C2')
        n_bins = CHORD_FAST_OCTAVES * bpo
        # tuner.value ist in Bruchteilen eines 36er-Bins (Schaetzung mit
        # bins_per_octave=36) -> auf das Raster des schnellen Pfads umrechnen.
        C = np.abs(librosa.cqt(y, sr=sr, fmin=fmin, n_bins=n_bins,
                               bins_per_octave=bpo,
                               tuning=tuning * bpo / 36.0,
                               hop_length=CHORD_FAST_HOP))
        # Spektralfluss aus der ohnehin berechneten CQT (faellt fast
        # gratis ab) -- Grundlage fuer den Onset-Anker weiter unten.
        flux = None
        if CHORD_ONSET_ANCHOR and C.shape[1] >= 8:
            flux = np.maximum(0.0, np.diff(C, axis=1)).sum(axis=0)
        freqs = librosa.cqt_frequencies(n_bins=n_bins, fmin=fmin,
                                        bins_per_octave=bpo)
        C = librosa.salience(C, freqs=freqs, harmonics=SAL_HARMONICS,
                             weights=SAL_WEIGHTS, fill_value=0.0,
                             filter_peaks=CHORD_FAST_SAL_PEAKS)
        chroma = librosa.feature.chroma_cqt(
            C=C, sr=sr, fmin=fmin, n_octaves=CHORD_FAST_OCTAVES,
            bins_per_octave=bpo, hop_length=CHORD_FAST_HOP)
        bchroma = librosa.feature.chroma_cqt(
            C=C[:2 * bpo], sr=sr, fmin=fmin, n_octaves=2,
            bins_per_octave=bpo, hop_length=CHORD_FAST_HOP)
        if CHROMA_LOG_COMP > 0:
            chroma = np.log1p(CHROMA_LOG_COMP * chroma)
            bchroma = np.log1p(CHROMA_LOG_COMP * bchroma)
        # Mittelung: bevorzugt ab dem letzten starken Onset (Akkordwechsel
        # liegen auf Anschlaegen -- alles davor gehoert zum alten Akkord),
        # sonst Recency-gewichtet ueber das ganze Fenster.
        n = chroma.shape[1]
        anchor = -1
        if flux is not None and len(flux) >= 4:
            thr = flux.mean() + CHORD_ONSET_STD * flux.std()
            min_tail = max(2, int(round(
                CHORD_ONSET_MIN_TAIL * sr / CHORD_FAST_HOP)))
            pre_k = max(min_tail, int(round(
                CHORD_ANCHOR_PRE * sr / CHORD_FAST_HOP)))
            cand = np.flatnonzero(flux > thr) + 1   # Fluss i: Frame i -> i+1
            cand = cand[(cand <= n - min_tail) & (cand >= min_tail)]
            # Nur Onsets, an denen sich die HARMONIE aendert, duerfen
            # verankern: Profil vor/nach dem Kandidaten vergleichen --
            # reine Schlagzeug-Anschlaege veraendern das Chroma kaum.
            # Vom juengsten Kandidaten rueckwaerts, der erste neue gewinnt.
            for a in cand[::-1][:3]:
                post = chroma[:, a:].mean(axis=1)
                pre = chroma[:, max(0, a - pre_k):a].mean(axis=1)
                pc = post - post.mean()
                qc = pre - pre.mean()
                den = float(np.linalg.norm(pc) * np.linalg.norm(qc))
                if den <= 1e-12:
                    continue
                if 1.0 - float(pc @ qc) / den >= CHORD_ANCHOR_NOV:
                    anchor = int(a)
                    break
        alt_pcp = alt_bass = None
        if anchor > 0:
            alt_pcp = chroma[:, anchor:].mean(axis=1)
            alt_bass = bchroma[:, anchor:].mean(axis=1)
            asum = alt_pcp.sum()
            if asum > 0:
                alt_pcp = alt_pcp / asum
                absum = alt_bass.sum()
                alt_bass = alt_bass / absum if absum > 0 else np.zeros(12)
            else:
                alt_pcp = alt_bass = None
        if CHORD_FAST_HALF_LIFE > 0 and n > 1:
            age = (n - 1 - np.arange(n)) * (CHORD_FAST_HOP / sr)
            w = 0.5 ** (age / CHORD_FAST_HALF_LIFE)
            w /= w.sum()
            pcp = chroma @ w
            bass = bchroma @ w
        else:
            pcp = chroma.mean(axis=1)
            bass = bchroma.mean(axis=1)
        s = pcp.sum()
        if s <= 0:
            return None
        bs = bass.sum()
        return (pcp / s, (bass / bs if bs > 0 else np.zeros(12)),
                alt_pcp, alt_bass)
    except Exception:
        return None


def level_bar(rms, width=12, floor_db=-60.0):
    """Pegel als (dBFS, ASCII-Balken) -- zur schnellen Sichtkontrolle."""
    db = 20.0 * math.log10(rms) if rms > 1e-9 else floor_db
    frac = max(0.0, min(1.0, (db - floor_db) / (0.0 - floor_db)))
    n = int(round(frac * width))
    return db, "#" * n + "-" * (width - n)


def classify_key(pcp, bass=None, with_margin=False):
    """Krumhansl-Schmuckler: bestes Dur-/Moll-Profil zum Chroma finden.

    Zusaetzlich bekommt jeder Tonart-Kandidat einen Bonus, wenn sein
    Grundton (und schwaecher: seine Quinte) den Bass dominiert. Das
    entscheidet vor allem zwischen Dur und seiner Mollparallele, die vom
    Tonmaterial her identisch sind (z. B. C-Dur vs. A-Moll: liegt vor
    allem C im Bass, ist es C-Dur; liegt A im Bass, A-Moll).

    with_margin=True liefert (name, vorsprung, zweiter) -- der Vorsprung des
    besten vor dem zweitbesten Kandidaten ist ein brauchbares Konfidenzmass,
    und WER der Zweite ist, zeigt beim Nachmessen (eval_detection.py), worin
    die Ambiguitaet besteht (z. B. Paralleltonart vs. Quint-Nachbar)."""
    if pcp is None or not np.any(pcp):
        return ("—", 0.0, "—") if with_margin else "—"
    use_bass = bass is not None and np.any(bass)
    scores = []
    for i in range(12):
        bonus = 0.0
        if use_bass:
            bonus = BASS_TONIC_WEIGHT * (bass[i] + 0.5 * bass[(i + 7) % 12])
        maj = np.corrcoef(pcp, np.roll(KS_MAJOR, i))[0, 1] + bonus
        mino = np.corrcoef(pcp, np.roll(KS_MINOR, i))[0, 1] + bonus
        scores.append((maj, f"{NOTE_NAMES[i]} Dur"))
        scores.append((mino, f"{NOTE_NAMES[i]} Moll"))
    scores.sort(key=lambda t: t[0], reverse=True)
    if with_margin:
        return scores[0][1], scores[0][0] - scores[1][0], scores[1][1]
    return scores[0][1]


def estimate_key(y, sr, with_margin=False):
    """Tonart eines Mono-Signals offline schaetzen -- inklusive Bass-Evidenz,
    die Dur von der Mollparallele trennt. WICHTIG: chroma_pcp liefert ein TUPEL
    (pcp, bass); genau dieses Auspacken wurde an mehreren Offline-Stellen
    vergessen (das Tupel landete als 'pcp' in classify_key und ergab konstant
    'C Dur'). Rueckgabe: Name oder (name, margin)."""
    try:
        res = chroma_pcp(y, sr)
    except Exception:
        res = None
    if not res:
        return ("—", 0.0) if with_margin else "—"
    pcp, bass = res[0], res[1]
    if with_margin:
        name, margin, _second = classify_key(pcp, bass, with_margin=True)
        return name, margin
    return classify_key(pcp, bass)



def chord_tail_sec(onset_env, fr, bpm):
    """Laenge des Akkord-Fensters in Sekunden (fuer chroma_pcp/tail_sec).

    Mit bekanntem Tempo wird das Fenster an der letzten Beat-Grenze
    ausgerichtet: Zeit seit dem letzten Beat plus eine Beat-Periode, geklemmt
    auf [CHORD_TAIL_MIN, CHORD_TAIL_MAX]. Akkordwechsel liegen auf
    Zaehlzeiten -- ein beat-ausgerichtetes Fenster mittelt deshalb nicht
    ueber den Wechsel hinweg, das feste 1,5-s-Fenster tat das regelmaessig.
    Ohne Tempo oder Beat-Phase: feste CHORD_TAIL_SEC."""
    if not CHORD_TAIL_BEAT or bpm <= 0:
        return CHORD_TAIL_SEC
    offs = _beat_phase_from_onset_env(onset_env, fr, bpm)
    if offs is None:
        return CHORD_TAIL_SEC
    return float(min(CHORD_TAIL_MAX,
                     max(CHORD_TAIL_MIN,
                         offs + CHORD_TAIL_BEATS * 60.0 / bpm)))


def chord_scores(pcp, bass=None, bass_weight=None):
    """Rohe Template-Scores fuer ALLE Akkorde auf einem Chroma-Profil:
    Pearson-Korrelation je Schablone plus Bass-Bonus. None, wenn das
    Profil leer/unbrauchbar ist.

    Der Bass-Bonus entscheidet zwischen tonverwandten Deutungen (C-Dur
    und Am7 teilen drei Toene): Grundton (und schwaecher: Quinte) im
    Bass spricht fuer den Akkord auf diesem Grundton. bass_weight ueber-
    schreibt CHORD_BASS_WEIGHT (z. B. hoeher, wenn der Bass aus einem
    SAUBEREN Bass-Stem statt aus dem Tiefband eines Mixes kommt)."""
    if pcp is None or not np.any(pcp):
        return None
    p = pcp - pcp.mean()
    n = float(np.linalg.norm(p))
    if n <= 1e-9:
        return None
    bw = CHORD_BASS_WEIGHT if bass_weight is None else bass_weight
    scores = _CHORD_MAT @ (p / n)          # Pearson je Schablone
    if bass is not None and np.any(bass):
        root_bonus = bw * (bass + 0.5 * np.roll(bass, -7))
        scores = scores + np.repeat(root_bonus, len(CHORD_TYPES))
    return scores


def classify_chord(pcp, bass=None, prev=None):
    """Einzelbild-Klassifikation: bester Akkord zum Chroma (ohne HMM).
    Der kleine Bonus fuer den bisherigen Akkord (prev) verhindert Flackern
    an der Kippgrenze. Rueckgabe: Akkordname ('C', 'Am7', ...) oder '—'.
    Im Worker laeuft stattdessen ChordTracker (HMM-Glaettung)."""
    scores = chord_scores(pcp, bass)
    if scores is None:
        return "—"
    if prev is not None:
        k = _CHORD_IDX.get(prev)
        if k is not None:
            scores[k] += CHORD_STICKY
    return CHORD_NAMES[int(np.argmax(scores))]


_MAJ_SCALE = (0, 2, 4, 5, 7, 9, 11)
_MIN_SCALE = (0, 2, 3, 5, 7, 8, 10, 11)   # natuerlich + Leitton (V/V7 in Moll)
_diatonic_masks = {}                       # Tonartname -> Bool-Maske (Cache)


def _diatonic_mask(key):
    """Bool-Maske ueber CHORD_NAMES: liegt der Akkord vollstaendig im
    Tonmaterial der Tonart ('C Dur', 'D Moll', ...)? None bei unbekanntem
    Tonartnamen. Moll enthaelt zusaetzlich den erhoehten Leitton, damit
    die in Moll uebliche Dur-Dominante (A7 in d-Moll) leitereigen zaehlt."""
    mask = _diatonic_masks.get(key)
    if mask is not None:
        return mask
    try:
        note, mode = key.rsplit(" ", 1)
        root = NOTE_NAMES.index(note)
    except (AttributeError, ValueError):
        return None
    scale = {(root + i) % 12
             for i in (_MAJ_SCALE if mode == "Dur" else _MIN_SCALE)}
    mask = np.zeros(len(CHORD_NAMES), dtype=bool)
    k = 0
    for r in range(12):
        for _suffix, ivs in CHORD_TYPES:
            mask[k] = all((r + iv) % 12 in scale for iv in ivs)
            k += 1
    _diatonic_masks[key] = mask
    return mask


class ChordTracker:
    """Online-Glaettung der Akkorderkennung: HMM-Forward-Algorithmus.

    Es wird eine Wahrscheinlichkeitsverteilung ueber alle Akkorde
    mitgefuehrt. Pro Analyse erst der Uebergang -- mit CHORD_SELF_P bleibt
    der Akkord derselbe, der Rest verteilt sich gleichmaessig --, dann die
    Beobachtung (Softmax der Template-Scores). Anders als ein fester
    Sticky-Bonus ist die Traegheit damit evidenz-abhaengig: klare neue
    Akkorde setzen sich sofort durch, bei mehrdeutigem Chroma bleibt der
    bisherige stehen. Das ist das Standard-Glaettungsmodell der Literatur
    (Sheh & Ellis 2003); mehr als dieses simple Modell bringt kaum etwas
    (Cho & Bello 2014, Korzeniowski & Widmer 2017).

    Liegt eine erkannte Tonart vor, bekommen deren leitereigene Akkorde
    zusaetzlich einen kleinen Score-Bonus (KEY_CHORD_PRIOR)."""

    def __init__(self):
        self.belief = None
        self.chord = "—"
        self.alt_pending = None   # Wechsel-Verdacht aus dem Kurzsegment

    def reset(self):
        self.belief = None
        self.chord = "—"
        self.alt_pending = None

    def update(self, pcp, bass=None, key=None, dt=None, alt=None):
        """Neues Chroma-Profil einarbeiten; liefert den aktuellen Akkord.

        dt = Abstand zur vorigen Beobachtung in Sekunden. CHORD_SELF_P und
        CHORD_TEMP sind auf den 1-s-Analysetakt bezogen und werden auf dt
        umgerechnet (Traegheit p^(dt/1s), Evidenzgewicht temp*dt/1s --
        Likelihood-Potenzierung): pro SEKUNDE wirken Traegheit und Evidenz
        dann gleich stark, egal ob der langsame 1-s-Pfad oder der schnelle
        0,3-s-Pfad fuettert. Ohne die Evidenz-Skalierung integriert der
        schnelle Pfad das ~3-fache Evidenzgewicht und flackert (gemessen:
        Wechselrate etwa verdoppelt).

        alt = (pcp, bass) des onset-verankerten Kurzsegments (schneller
        Pfad): schlaegt es zweimal in Folge denselben ANDEREN Akkord mit
        klarem Vorsprung (CHORD_ALT_MARGIN) vor, folgt ein einmaliges
        Gate-Update mit niedriger Traegheit (CHORD_GATE_SELF_P) und voller
        Evidenz -- der Tracker kippt am Akkordwechsel sofort, waehrend die
        normale Traegheit im Gleichlauf unangetastet bleibt (Innovation-
        Gating, analog zur Innovationspruefung beim Kalman-Filter)."""
        scores = chord_scores(pcp, bass)
        if scores is None:
            return self.chord
        if KEY_CHORD_PRIOR > 0 and key:
            mask = _diatonic_mask(key)
            if mask is not None:
                scores = scores + KEY_CHORD_PRIOR * mask
        if dt is None:
            dt = ANALYSIS_INTERVAL
        rel = max(0.05, dt) / ANALYSIS_INTERVAL
        emis = np.exp(CHORD_TEMP * rel * (scores - scores.max()))
        s = emis.sum()
        if not np.isfinite(s) or s <= 0:
            return self.chord
        emis /= s
        p_stay = CHORD_SELF_P ** rel
        if self.belief is None:
            belief = emis
        else:
            pred = p_stay * self.belief + (1.0 - p_stay) / len(emis)
            belief = pred * emis
            s = belief.sum()
            belief = belief / s if s > 0 else emis
        self.belief = belief
        self.chord = CHORD_NAMES[int(np.argmax(belief))]

        # ---- Innovations-Gate (nur schneller Pfad, alt-Profil) ----
        if alt is not None and alt[0] is not None:
            ascores = chord_scores(alt[0], alt[1])
        else:
            ascores = None
        if ascores is None:
            self.alt_pending = None
            return self.chord
        if KEY_CHORD_PRIOR > 0 and key:
            mask = _diatonic_mask(key)
            if mask is not None:
                ascores = ascores + KEY_CHORD_PRIOR * mask
        best = int(np.argmax(ascores))
        alt_best = CHORD_NAMES[best]
        # Margin gegen den besten Akkord mit ANDEREM Grundton (Varianten
        # desselben Grundtons wie F/Fmaj7/F7 zaehlen nicht als Rivalen).
        nt = len(CHORD_TYPES)
        root = best // nt
        rival = np.delete(ascores, slice(root * nt, (root + 1) * nt))
        fam_margin = float(ascores[best] - rival.max())
        if alt_best == self.chord or fam_margin < CHORD_ALT_MARGIN:
            self.alt_pending = None
        elif alt_best != self.alt_pending:
            self.alt_pending = alt_best        # erster Verdacht: merken
        else:
            # Verdacht bestaetigt -> Gate: einmaliges Update mit niedriger
            # Traegheit und voller (nicht dt-skalierter) Evidenz.
            emis = np.exp(CHORD_TEMP * (ascores - ascores.max()))
            s = emis.sum()
            if np.isfinite(s) and s > 0:
                emis /= s
                pred = CHORD_GATE_SELF_P * self.belief \
                    + (1.0 - CHORD_GATE_SELF_P) / len(emis)
                belief = pred * emis
                s = belief.sum()
                if s > 0:
                    self.belief = belief / s
                    self.chord = CHORD_NAMES[int(np.argmax(self.belief))]
            self.alt_pending = None
        return self.chord


_chord_log_header = False   # Sitzungs-Kopfzeile schon geschrieben?


def chord_log(line):
    """Zeile ans Akkord-Protokoll (CHORD_LOG_PATH) anhaengen; vor dem ersten
    Eintrag der Sitzung eine Kopfzeile mit Datum. Kein Pfad gesetzt = aus;
    Schreibfehler werden wie bei log_message ignoriert."""
    global _chord_log_header
    path = CHORD_LOG_PATH
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            if not _chord_log_header:
                fh.write("\n=== Sitzung "
                         + time.strftime("%Y-%m-%d %H:%M:%S") + " ===\n")
                _chord_log_header = True
            fh.write(line + "\n")
    except Exception:
        pass


def estimate_beat_phase(y, sr, bpm):
    """Zeitpunkt des letzten Beats im Fenster, als Sekunden VOR Fensterende.
    None, wenn keine brauchbare Phase bestimmbar ist. Wie estimate_tempo
    arbeitet die Funktion am besten auf dem perkussiven Anteil des Signals."""
    try:
        oe = librosa.onset.onset_strength(y=y, sr=sr, hop_length=ONSET_HOP)
    except Exception:
        return None
    return _beat_phase_from_onset_env(oe, sr / ONSET_HOP, bpm)


def _beat_phase_from_onset_env(oe, fr, bpm):
    """Beat-Phase (Schritt 2 von estimate_beat_phase) auf einer fertigen
    Onset-Huellkurve mit fr Frames pro Sekunde.

    Die Onset-Huellkurve wird auf die Beat-Periode gefaltet (Histogramm der
    Phasenlage, spaete Frames staerker gewichtet, damit die Phase zum
    aktuellen Fensterende passt); der staerkste Phasen-Bin ist der Beat."""
    try:
        if bpm <= 0 or oe is None or not np.any(oe):
            return None
        period = 60.0 * fr / bpm              # Beat-Abstand in Frames
        if period < 4 or len(oe) < 2 * period:
            return None
        nb = 24                                # Phasen-Aufloesung (Bins)
        idx = np.arange(len(oe), dtype=np.float64)
        w = 0.3 + 0.7 * idx / (len(oe) - 1)    # juengere Frames zaehlen mehr
        bins = ((idx % period) / period * nb).astype(int) % nb
        hist = np.bincount(bins, weights=oe * w, minlength=nb)
        b = int(np.argmax(hist))
        a, m, c = hist[(b - 1) % nb], hist[b], hist[(b + 1) % nb]
        den = a - 2.0 * m + c
        off = 0.5 * (a - c) / den if den != 0 else 0.0
        off = float(np.clip(off, -0.5, 0.5))
        phase = ((b + 0.5 + off) / nb) * period   # Beat-Phase in Frames
        last = len(oe) - 1
        k = math.floor((last - phase) / period)
        beat_frame = phase + k * period           # letzter Beat im Fenster
        return max(0.0, (last - beat_frame) / fr)
    except Exception:
        return None


def analysis_worker(shared, audio_q, stop_event):
    with shared.lock:
        capture_sr = shared.capture_sr
    # Der Analysepuffer laeuft IMMER mit ANALYSIS_SR: ankommende Bloecke
    # werden (falls noetig) sofort blockweise heruntergerechnet, statt jede
    # Sekunde das komplette 8-s-Fenster neu zu resampeln.
    win = int(WINDOW_SECONDS * ANALYSIS_SR)
    buf = np.zeros(0, dtype=np.float32)
    res_tail = np.zeros(0, dtype=np.float32)  # Roh-Kontext fuers Resampling
    buf_end_wall = 0.0          # Wanduhr-Zeit des Pufferendes (Beat-Anker)
    b_anchor_f = 0.0            # geglaetteter Beat-Anker (BEAT_ANCHOR_EMA)
    b_anchor_t = 0.0            # wann er zuletzt aktualisiert wurde
    last_run = 0.0
    # Tonart-Integration, zweistufig: schnelle EMA (reagiert in ~KEY_EMA_SEC)
    # plus Gesamtmittel seit Songbeginn (wird mit der Laufzeit immer stabiler).
    key_ema_a = min(1.0, ANALYSIS_INTERVAL / KEY_EMA_SEC)
    ema_pcp = ema_bass = None
    cum_pcp = np.zeros(12)
    cum_bass = np.zeros(12)
    cum_n = 0
    bpm_hist = deque(maxlen=BPM_MEDIAN_LEN)  # letzte Tempo-Schaetzungen (Median)
    key_disp = "—"              # aktuell angezeigte Tonart (mit Hysterese)
    key_pend = None             # Wechsel-Kandidat + Zaehler
    key_pend_n = 0
    chord_disp = "—"            # aktuell erkannter Akkord
    chord_tracker = ChordTracker()  # HMM-Glaettung ueber die Analysen
    tuner = TuningEstimator()   # Stimmung des Stuecks (wird eingefroren)
    silence_rms = 10.0 ** (SILENCE_DB / 20.0)
    silent_since = None
    err_shown = False           # Analyse-Fehler nur einmal melden

    def reset_analysis(mark, clear_shared=True):
        """Analysezustand komplett leeren -- gemeinsamer Kern fuer
        Stille-Reset, Quellenwechsel und manuellen Neustart. mark ist die
        Trennmarke fuers Akkord-Protokoll. Mit clear_shared werden auch
        die Anzeige-Ergebnisse geloescht (haelt die Clock an, bis eine
        neue Schaetzung vorliegt)."""
        nonlocal buf, res_tail, ema_pcp, ema_bass, cum_pcp, cum_bass
        nonlocal cum_n, key_disp, key_pend, key_pend_n, chord_disp
        nonlocal b_anchor_t
        buf = np.zeros(0, dtype=np.float32)
        res_tail = np.zeros(0, dtype=np.float32)
        b_anchor_t = 0.0        # Anker-Glaettung neu beginnen (neues Stueck)
        ema_pcp = ema_bass = None
        cum_pcp = np.zeros(12)
        cum_bass = np.zeros(12)
        cum_n = 0
        bpm_hist.clear()
        key_disp, key_pend, key_pend_n = "—", None, 0
        chord_disp = "—"
        chord_tracker.reset()
        tuner.reset()
        with shared.lock:
            logged = shared.chord_logged
            shared.chord_logged = False
            shared.chord_epoch += 1     # Akkord-Thread: Zustand verwerfen
            shared.fast_buf = None
            shared.tuning = 0.0
        if logged:
            chord_log("--- " + mark + " ---")
        if clear_shared:
            with shared.lock:
                shared.have_estimate = False
                shared.raw_bpm = 0.0
                shared.key = "—"
                shared.key_confident = False
                shared.chord = "—"

    while not stop_event.is_set():
        t_block = 0.0           # Capture-Zeit des juengsten Blocks
        try:
            block, t_block = audio_q.get(timeout=0.1)
            blocks = [block]
            # Alle weiteren wartenden Bloecke mitnehmen, damit die Analyse nicht
            # hinterherhinkt, falls ein Durchlauf laenger gedauert hat.
            try:
                while True:
                    block, t_block = audio_q.get_nowait()
                    blocks.append(block)
            except queue.Empty:
                pass
        except queue.Empty:
            blocks = []

        # ---- Manueller Neustart (GUI-Button "Analyse neu starten")? ----
        # Wie ein Stille-Reset, nur auf Wunsch: z. B. wenn ein Songwechsel
        # ohne Pause die Historie mit dem alten Stueck gefuellt hat.
        with shared.lock:
            want_reset = shared.reset_request
            shared.reset_request = False
        if want_reset:
            reset_analysis("Reset " + time.strftime("%H:%M:%S"))

        # ---- Analyse bewusst angehalten (langer Break)? ----
        # Bloecke verwerfen, Ergebnisse/Clock unveraendert lassen und den
        # Stille-Reset unterdruecken. Der Audio-Puffer wird geleert, damit
        # nach dem Fortsetzen nicht Altes mit Neuem verklebt wird --
        # die BPM-/Tonart-Historie bleibt erhalten (gleiches Stueck).
        with shared.lock:
            hold = shared.hold
        if hold:
            buf = np.zeros(0, dtype=np.float32)
            res_tail = np.zeros(0, dtype=np.float32)
            silent_since = None
            continue

        # ---- Stille/Pause erkennen -> Analyse zuruecksetzen ----
        # (Pegel kommt Producer-seitig; bei stehengebliebenen Bloecken klingt er ab.)
        with shared.lock:
            lvl = shared.level
            lvt = shared.level_time
        age = time.perf_counter() - lvt
        eff = lvl * (math.exp(-(age - 0.3) / 0.4) if age > 0.3 else 1.0)
        now0 = time.perf_counter()
        if eff < silence_rms:
            if silent_since is None:
                silent_since = now0
            elif (now0 - silent_since) >= SILENCE_RESET_SEC and (cum_n or bpm_hist):
                # Pause/Songwechsel: alles leeren, naechstes Stueck startet frisch.
                reset_analysis("Stille " + time.strftime("%H:%M:%S"))
        else:
            silent_since = None

        if not blocks:
            continue

        # Aufnahmerate kann zur Laufzeit wechseln (Quellenwechsel) -> Puffer
        # und Schaetzungen zuruecksetzen, damit nichts vermischt wird.
        with shared.lock:
            new_sr = shared.capture_sr
        if new_sr != capture_sr:
            capture_sr = new_sr
            # Anzeige/Clock NICHT leeren: beim Quellenwechsel laeuft die
            # Clock auf dem alten Tempo weiter, bis die neue Quelle eine
            # Schaetzung liefert (bisheriges Verhalten).
            reset_analysis("Quellenwechsel", clear_shared=False)

        new_raw = blocks[0] if len(blocks) == 1 else np.concatenate(blocks)
        if capture_sr != ANALYSIS_SR:
            # Nur die NEUEN Samples herunterrechnen; etwas Roh-Kontext
            # mitschleifen, damit an der Naht keine Filterartefakte
            # entstehen (die Kontext-Ausgabe wird wieder verworfen).
            chunk = np.concatenate([res_tail, new_raw])
            try:
                res = librosa.resample(chunk, orig_sr=capture_sr,
                                       target_sr=ANALYSIS_SR)
                skip = int(round(len(res_tail) * ANALYSIS_SR / capture_sr))
                new = np.asarray(res[skip:], dtype=np.float32)
            except Exception:
                new = np.zeros(0, dtype=np.float32)
            res_tail = chunk[-RESAMPLE_CTX:]
        else:
            new = new_raw
        buf = np.concatenate([buf, new])
        if len(buf) > win:
            buf = buf[-win:]
        # Pufferende mit der CAPTURE-Zeit des juengsten Blocks datieren,
        # nicht mit "jetzt": der Worker leert die Queue oft erst nach einer
        # ~0,5-s-Analyse stapelweise -- "jetzt" laege dann mal Millisekunden,
        # mal eine Chunk-Laenge hinter der Aufnahme, und genau dieser Jitter
        # ginge 1:1 in den Beat-Anker der Clock.
        buf_end_wall = t_block
        now = time.perf_counter()

        # ---- Schneller Akkord-Pfad (Option CHORD_FAST) ----
        # Die eigentliche Akkord-Analyse laeuft in einem EIGENEN Thread
        # (fast_chord_worker): die ~0,5 s Rechenzeit der grossen Analyse
        # wuerde die kurzen Akkord-Schritte sonst blockieren und die
        # Wechsel-Latenz um bis zu eine halbe Sekunde wuerfeln. Hier wird
        # nur der juengste Audio-Schnitt fuer den Thread abgelegt.
        if CHORD_ENABLED and CHORD_FAST and len(buf) > 0:
            snap = buf[-int(CHORD_FAST_WIN * ANALYSIS_SR):].copy()
            with shared.lock:
                shared.fast_buf = snap

        if len(buf) < win * 0.5 or (now - last_run) < ANALYSIS_INTERVAL:
            continue
        last_run = now

        # Heavy lifting absichern: ein Fehler darf den Thread nicht killen.
        try:
            y = buf                 # liegt bereits in ANALYSIS_SR vor
            sr = ANALYSIS_SR
            # Drums von Flaechen/Bass/Gesang trennen: das Tempo kommt aus
            # dem perkussiven, die Tonart aus dem harmonischen Anteil.
            y_harm, y_perc = split_harmonic_percussive(y)
            prev = float(np.median(bpm_hist)) if bpm_hist else 0.0
            # Onset-Huellkurve des perkussiven Anteils EINMAL berechnen --
            # Tempo, Beat-Phase und Akkord-Fenster nutzen alle dieselbe.
            env_fr = sr / ONSET_HOP
            try:
                perc_env = librosa.onset.onset_strength(
                    y=y_perc, sr=sr, hop_length=ONSET_HOP)
            except Exception:
                perc_env = None
            bpm = 0.0
            if perc_env is not None:
                bpm = fold_bpm(_tempo_from_onset_env(perc_env, env_fr, prev),
                               prev)
            if bpm <= 0:
                # kaum Perkussives (z. B. Ballade) -> Voll-Mix versuchen
                bpm = fold_bpm(estimate_tempo(y, sr, prev), prev)
            tail = 0.0
            if CHORD_ENABLED and not CHORD_FAST:
                tail = chord_tail_sec(perc_env, env_fr,
                                      prev if prev > 0 else bpm)
            tuning = tuner.update(y_harm, sr)
            chroma_res = chroma_pcp(y, sr, y_harm=y_harm, tail_sec=tail,
                                    tuning=tuning)
        except Exception as e:
            if not err_shown:
                msg = f"[Analyse-Fehler: {type(e).__name__}: {e}]"
                print("\n" + msg)
                log_message(msg)
                err_shown = True
            continue

        # Tonart: 50 % schnelle EMA + 50 % Gesamtmittel seit Songbeginn.
        # Die EMA laesst die Anzeige zuegig auf gefestigte neue Evidenz
        # reagieren (z. B. Stueck beginnt auf der Mollparallele), das
        # Gesamtmittel verhindert, dass einzelne Akkordwechsel sie kippen.
        if chroma_res is not None:
            p, b = chroma_res[0], chroma_res[1]
            ema_pcp = p if ema_pcp is None else \
                (1.0 - key_ema_a) * ema_pcp + key_ema_a * p
            ema_bass = b if ema_bass is None else \
                (1.0 - key_ema_a) * ema_bass + key_ema_a * b
            cum_pcp = cum_pcp + p
            cum_bass = cum_bass + b
            cum_n += 1
        if cum_n > 0:
            prof = 0.5 * ema_pcp + 0.5 * (cum_pcp / cum_n)
            bprof = 0.5 * ema_bass + 0.5 * (cum_bass / cum_n)
            cand, cand_margin, cand_2nd = classify_key(prof, bprof,
                                                       with_margin=True)
        else:
            cand, cand_margin, cand_2nd = "—", 0.0, "—"

        # Hysterese: die erste Schaetzung sofort anzeigen, danach erst
        # wechseln, wenn der neue Kandidat KEY_SWITCH_CONFIRM-mal in Folge
        # gewinnt -> kein Hin- und Herflackern an der Kippgrenze.
        if cand != key_disp:
            if key_disp == "—" or cand == "—":
                key_disp, key_pend, key_pend_n = cand, None, 0
            elif cand == key_pend:
                key_pend_n += 1
                if key_pend_n >= KEY_SWITCH_CONFIRM:
                    key_disp, key_pend, key_pend_n = cand, None, 0
            else:
                key_pend, key_pend_n = cand, 1
        else:
            key_pend, key_pend_n = None, 0
        key = key_disp
        # "Sicher" = Anzeige und aktuelle Klassifikation stimmen ueberein,
        # der Vorsprung ist deutlich und es gibt schon etwas Historie.
        confident = (cand == key_disp and cand != "—"
                     and cand_margin >= KEY_CONFIDENT_MARGIN
                     and cum_n >= KEY_CONFIDENT_MIN_N)

        # Akkord: Template-Matching auf dem juengsten Stueck des Chromagramms
        # (faellt bei der Tonart-Berechnung mit ab, siehe chroma_pcp/tail_sec),
        # ueber die Analysen per HMM geglaettet (ChordTracker); die erkannte
        # Tonart gibt leitereigenen Akkorden einen kleinen Vorsprung.
        # Jeder Wechsel geht -- falls aktiviert -- mit Uhrzeit ins Protokoll.
        # Pegel-Gate (eff): beim Ausklingen in die Stille -- bevor der
        # Stille-Reset greift -- liefert das fast leere Chroma sonst noch
        # 1-2 Zufallsakkorde, die Anzeige und Protokoll verschmutzen.
        if CHORD_ENABLED and chroma_res is not None and len(chroma_res) > 2 \
                and eff >= silence_rms:
            cand_chord = chord_tracker.update(chroma_res[2], chroma_res[3],
                                              key=key_disp)
            if cand_chord != "—" and cand_chord != chord_disp:
                chord_disp = cand_chord
                chord_log(time.strftime("%H:%M:%S") + "  " + cand_chord)
                with shared.lock:
                    shared.chord_logged = True

        # Tempo: Median der letzten Schaetzungen -> robust gegen Ausreisser.
        if bpm > 0:
            bpm_hist.append(bpm)
            # Echten Tempowechsel erkennen: stimmen die letzten 5 Schaetzungen
            # eng untereinander ueberein (< 3 % Streuung), weichen aber deutlich
            # vom bisherigen Median ab, alte Schaetzungen verwerfen -- so
            # springt die Anzeige in ~5 s auf das neue Tempo statt in ~16 s.
            # Liegt der Sprung aber auf einem typischen Alias-Verhaeltnis
            # (4/3, 3/2, 2/1 bzw. deren Kehrwerte), ist es fast sicher ein
            # Schaetzfehler-Lauf und kein echter Wechsel -> nicht verwerfen,
            # der Median uebersteht solche Laeufe. (2/1 faengt vor allem
            # Oktav-Kipper an den Bereichsgrenzen ab, z. B. 140 <-> 70.)
            if len(bpm_hist) >= 10:
                recent = list(bpm_hist)[-5:]
                rmed = float(np.median(recent))
                omed = float(np.median(bpm_hist))
                ratio = rmed / omed
                alias = any(abs(ratio / h - 1.0) < 0.04
                            for h in (4 / 3, 3 / 2, 2 / 3, 3 / 4, 2.0, 0.5))
                if (max(recent) / min(recent) - 1.0) < 0.03 and \
                        abs(rmed - omed) / omed > TEMPO_FLUSH_DEV and \
                        not alias:
                    while len(bpm_hist) > 5:
                        bpm_hist.popleft()
        target = float(np.median(bpm_hist)) if bpm_hist else 0.0

        # Beat-Phase bestimmen (nur wenn die Beat-Synchronisation an ist):
        # Zeitpunkt des letzten Beats im Fenster -> Anker fuer die Clock.
        with shared.lock:
            want_beat = shared.beat_sync
        beat_update = None
        if want_beat and target > 0:
            offs = _beat_phase_from_onset_env(perc_env, env_fr, target)
            if offs is not None:
                anchor = buf_end_wall - offs
                period = 60.0 / target
                # Anker glaetten: den bisherigen Anker aufs Beat-Raster der
                # neuen Messung projizieren (round = naechstgelegener Beat)
                # und nur um BEAT_ANCHOR_EMA des Phasenfehlers nachziehen.
                # Die Nudge-Schleife der Clock folgt dann dem gefilterten
                # Raster statt dem Messrauschen jeder Einzelanalyse.
                if b_anchor_t > 0 and (now - b_anchor_t) < BEAT_VALID_SEC:
                    pred = b_anchor_f + period * round(
                        (anchor - b_anchor_f) / period)
                    anchor = pred + BEAT_ANCHOR_EMA * (anchor - pred)
                b_anchor_f, b_anchor_t = anchor, now
                beat_update = (anchor, period)

        with shared.lock:
            if target > 0:
                shared.target_bpm = target
                shared.have_estimate = True
            if bpm > 0:
                shared.raw_bpm = bpm
            shared.key = key
            shared.key_confident = confident
            shared.tuning = tuner.value
            if not (CHORD_ENABLED and CHORD_FAST):
                # im schnellen Modus gehoert shared.chord dem Akkord-Thread
                shared.chord = chord_disp if CHORD_ENABLED else "—"
            if beat_update is not None:
                shared.beat_anchor = beat_update[0]
                shared.beat_period = beat_update[1]
                shared.beat_valid_time = time.perf_counter()


def fast_chord_worker(shared, stop_event):
    """Eigener Thread fuer den schnellen Akkord-Pfad (Option CHORD_FAST).

    Laeuft PARALLEL zur grossen Analyse: deren ~0,5 s Rechenzeit pro
    Durchlauf wuerde die kurzen Akkord-Schritte sonst blockieren und die
    Wechsel-Latenz um bis zu eine halbe Sekunde wuerfeln. numpy/librosa
    geben waehrend der grossen Transformationen den GIL frei, der Thread
    rechnet also tatsaechlich nebenher. Solange CHORD_FAST aus ist,
    schlaeft er nur (kostet nichts -- Pi-tauglich).

    Audio kommt als Kopie der juengsten Sekunden aus shared.fast_buf
    (legt der Analyse-Worker ab), der Akkord geht nach shared.chord und
    ins Protokoll. Meldet der Worker einen Analyse-Reset (chord_epoch),
    verwirft der Thread seinen Zustand."""
    tracker = ChordTracker()
    chord_disp = "—"
    epoch = None
    last_obs = 0.0
    silence_rms = 10.0 ** (SILENCE_DB / 20.0)
    while not stop_event.is_set():
        if not (CHORD_ENABLED and CHORD_FAST):
            time.sleep(0.25)
            continue
        t0 = time.perf_counter()
        with shared.lock:
            buf = shared.fast_buf
            ep = shared.chord_epoch
            key = shared.key
            tuning = shared.tuning
            lvl = shared.level
            lvt = shared.level_time
            hold = shared.hold
        if epoch != ep:
            epoch = ep
            tracker.reset()
            chord_disp = "—"
            last_obs = 0.0
        # Pegel-Gate wie im Worker (abklingend, falls keine Updates kommen)
        age = t0 - lvt
        eff = lvl * (math.exp(-(age - 0.3) / 0.4) if age > 0.3 else 1.0)
        if hold or buf is None or eff < silence_rms \
                or len(buf) < int(CHORD_FAST_WIN * ANALYSIS_SR * 0.9):
            time.sleep(CHORD_FAST_INTERVAL)
            continue
        try:
            fres = chroma_pcp_fast(buf, ANALYSIS_SR, tuning=tuning)
        except Exception:
            fres = None
        if fres is not None:
            dt = (t0 - last_obs) if last_obs > 0 else None
            last_obs = t0
            alt = (fres[2], fres[3]) if fres[2] is not None else None
            cand = tracker.update(fres[0], fres[1], key=key, dt=dt, alt=alt)
            if cand != "—" and cand != chord_disp:
                chord_disp = cand
                chord_log(time.strftime("%H:%M:%S") + "  " + cand)
                with shared.lock:
                    shared.chord = chord_disp
                    shared.chord_logged = True
        d = t0 + CHORD_FAST_INTERVAL - time.perf_counter()
        if d > 0:
            time.sleep(d)


def analysis_worker_safe(shared, audio_q, stop_event):
    """analysis_worker mit Absturzschutz: ein unerwarteter Fehler wird
    geloggt und der Worker neu gestartet, statt die Analyse dauerhaft zu
    verlieren (die Anzeige wuerde sonst stumm einfrieren). Startet auch
    den Thread des schnellen Akkord-Pfads (fast_chord_worker) -- der
    haengt am selben stop_event und uebersteht Worker-Neustarts."""
    threading.Thread(target=fast_chord_worker, args=(shared, stop_event),
                     daemon=True).start()
    while not stop_event.is_set():
        try:
            analysis_worker(shared, audio_q, stop_event)
            return                              # regulaer beendet
        except Exception as e:
            msg = f"[analysis_worker abgestuerzt, Neustart: {type(e).__name__}: {e}]"
            print("\n" + msg)
            log_message(msg)
            time.sleep(1.0)


# ===========================================================================
# Noten-Modus: Pitch -> MIDI (monophon: YIN, polyphon: FFT-Peaks)
# ===========================================================================
# Sendet erkannte Tonhoehen direkt als MIDI-Noten. In diesem Modus laufen die
# teuren Analyseschritte (HPSS/Chroma/Tempo/Clock) bewusst NICHT mit, damit
# die Latenz so gering wie moeglich bleibt.
NOTE_CHANNEL     = 0       # MIDI-Kanal 1
NOTE_MIN_MIDI    = 36      # C2
NOTE_MAX_MIDI    = 96      # C7
NOTE_SILENCE_RMS = 0.004   # ~ -48 dBFS: darunter keine Note
YIN_THRESHOLD    = 0.15    # YIN-Schwelle (kleiner = strenger)
NOTE_WIN_MONO    = 2048    # YIN-Analysefenster
NOTE_WIN_POLY    = 4096    # FFT-Fenster fuer den polyphonen Pfad
NOTE_BLOCKSIZE   = 512     # kleine Capture-Bloecke -> geringe Latenz (Eingang)
NOTE_MAX_POLY    = 6       # max. gleichzeitige Noten
NOTE_SUSTAIN_RMS = 0.0015  # gehaltene Note/Akkord bleibt an, solange Pegel darueber
NOTE_OFF_FRAMES  = 3       # leise Frames bis Note-Off (mono, entprellt)
# Akkord-Trigger-Modus (sauberer MIDI-Akkord aus dem Klang, z. B. Gitarre)
CHORD_TRIG_MIN_SCORE = 0.50   # Mindest-Korrelation Chroma<->Akkord-Schablone
CHORD_TRIG_MARGIN    = 0.05   # Abstand zum besten Akkord mit anderem Grundton
CHORD_TRIG_CONFIRM   = 3      # Frames Bestaetigung vor Akkordwechsel
CHORD_TRIG_OFF_FRAMES = 6     # leise Frames bis Akkord-Off (Akkorde klingen aus)


def midi_name(m):
    return NOTE_NAMES[m % 12] + str(m // 12 - 1)


def vel_from_level(level):
    db = 20.0 * math.log10(level) if level > 0 else -90.0
    return int(max(1, min(127, round((db + 54.0) / 48.0 * 127.0))))


def yin_pitch(buf, sr, threshold=YIN_THRESHOLD):
    """Monophone Tonhoehe per YIN. Rueckgabe Frequenz in Hz oder 0.0.

    Schnelle, vektorisierte Differenzfunktion ueber die Autokorrelation:
    d(tau) = sum(x[i]^2) + sum(x[i+tau]^2) - 2*sum(x[i]*x[i+tau]).
    """
    n = len(buf)
    W = n // 2
    x = buf.astype(np.float64)
    pe = np.concatenate(([0.0], np.cumsum(x * x)))
    t1 = pe[W] - pe[0]
    taus = np.arange(W)
    t2 = pe[taus + W] - pe[taus]
    size = 1
    while size < n + W:
        size <<= 1
    fa = np.fft.rfft(x[:W], size)
    fx = np.fft.rfft(x, size)
    corr = np.fft.irfft(np.conj(fa) * fx, size)
    t3 = corr[:W]
    d = t1 + t2 - 2.0 * t3
    np.clip(d, 0.0, None, out=d)
    cmnd = np.empty(W)
    cmnd[0] = 1.0
    csum = np.cumsum(d[1:])
    cmnd[1:] = d[1:] * np.arange(1, W) / np.where(csum > 0, csum, 1.0)
    below = np.where(cmnd[2:] < threshold)[0]
    if below.size == 0:
        return 0.0
    tau = int(below[0]) + 2
    while tau + 1 < W and cmnd[tau + 1] < cmnd[tau]:
        tau += 1
    bt = float(tau)
    if 1 < tau < W - 1:
        s0, s1, s2 = cmnd[tau - 1], cmnd[tau], cmnd[tau + 1]
        denom = 2.0 * (2.0 * s1 - s2 - s0)
        if denom != 0:
            bt = tau + (s2 - s0) / denom
    return sr / bt if bt > 0 else 0.0


class NoteDetector:
    """Haelt ein rollendes Fenster, erkennt Tonhoehe(n) und meldet die noetigen
    MIDI-Noten-Ereignisse ueber die uebergebene send(status, note, vel)."""

    def __init__(self, sr, mode):
        self.sr = float(sr)
        self.mode = mode                       # 'mono' | 'poly' | 'chord'
        self.poly = (mode == "poly")
        use_fft = mode in ("poly", "chord")
        self.win = NOTE_WIN_POLY if use_fft else NOTE_WIN_MONO
        self.buf = np.zeros(self.win, dtype=np.float32)
        self.filled = 0
        self.cur = -1
        self.cand = -1
        self.cand_n = 0
        self.off_n = 0
        self.active = {}                       # midi -> Frames ohne Beleg
        self.han = np.hanning(self.win).astype(np.float32) if use_fft else None
        self.display = "—"
        # Kalibrierbare Parameter (Default = Konstanten; die GUI kann sie setzen).
        self.silence_rms = NOTE_SILENCE_RMS
        self.sustain_rms = NOTE_SUSTAIN_RMS
        self.off_frames = NOTE_OFF_FRAMES
        self.yin_threshold = YIN_THRESHOLD
        self.change_frames = 2
        self.max_poly = NOTE_MAX_POLY
        # Akkord-Trigger-Zustand
        self.chord_notes = []
        self.chord_idx = -1
        self.chord_cand = -1
        self.chord_cand_n = 0
        if mode == "chord":                    # Bin -> Tonklasse (70..1100 Hz)
            freqs = np.arange(self.win // 2 + 1) * self.sr / self.win
            self._chroma_pc = np.full(freqs.shape, -1, dtype=np.int64)
            band = (freqs >= 70.0) & (freqs <= 1100.0)
            with np.errstate(divide="ignore"):
                midi = np.round(12 * np.log2(np.where(freqs > 0, freqs, 1.0) / 440.0) + 69)
            self._chroma_pc[band] = (midi[band].astype(np.int64) % 12)

    def push(self, x):
        h = len(x)
        if h >= self.win:
            self.buf[:] = x[-self.win:]
            self.filled = self.win
        else:
            self.buf[:-h] = self.buf[h:]
            self.buf[-h:] = x
            self.filled = min(self.win, self.filled + h)

    def process(self, level, send):
        if self.filled < self.win:
            return
        if self.mode == "mono":
            self._mono(level, send)
        elif self.mode == "poly":
            self._poly(level, send)
        else:
            self._chord(level, send)

    def _mono(self, level, send):
        note = -1
        # Zum HALTEN genuegt ein niedrigerer Pegel (Hysterese) -> ein ausklingender
        # Ton reisst nicht ab und wird nicht neu getriggert.
        gate = self.sustain_rms if self.cur != -1 else self.silence_rms
        if level > gate:
            f = yin_pitch(self.buf, self.sr, self.yin_threshold)
            if f > 0:
                m = int(round(69 + 12 * math.log2(f / 440.0)))
                if NOTE_MIN_MIDI <= m <= NOTE_MAX_MIDI:
                    note = m
        if note == self.cur and self.cur != -1:        # klar gehalten
            self.off_n = 0
            self.cand = -1
            self.cand_n = 0
            return
        if note == -1:
            if self.cur != -1 and level > self.sustain_rms:
                self.off_n = 0                          # klingt noch -> halten
            elif self.cur != -1:
                self.off_n += 1
                if self.off_n >= self.off_frames:
                    send(0x80, self.cur, 0)
                    self.cur = -1
                    self.off_n = 0
                    self.display = "—"
            self.cand = -1
            self.cand_n = 0
            return
        self.off_n = 0
        if note == self.cand:
            self.cand_n += 1
        else:
            self.cand = note
            self.cand_n = 1
        need = 1 if self.cur == -1 else self.change_frames
        if self.cand_n >= need:
            if self.cur != -1:
                send(0x80, self.cur, 0)
            send(0x90, note, vel_from_level(level))
            self.cur = note
            self.cand = -1
            self.cand_n = 0
            self.display = midi_name(note)

    def _chord(self, level, send):
        # Akkord aus dem Chroma (FFT-Magnitude in 12 Tonklassen, 70..1100 Hz) mit
        # den vorhandenen Akkord-Schablonen erkennen; Fehltoene fallen weg, der
        # Akkord wird als sauberes Voicing gesendet und GEHALTEN.
        gate = self.sustain_rms if self.chord_idx != -1 else self.silence_rms
        if level <= gate:
            if self.chord_idx != -1:
                self.off_n += 1
                if self.off_n >= CHORD_TRIG_OFF_FRAMES:
                    self._chord_off(send)
            self.chord_cand = -1
            self.chord_cand_n = 0
            return
        self.off_n = 0
        mag = np.abs(np.fft.rfft(self.buf * self.han))
        chroma = np.zeros(12)
        pc = self._chroma_pc
        valid = pc >= 0
        np.add.at(chroma, pc[valid], mag[valid])
        scores = chord_scores(chroma)
        if scores is None:
            self.chord_cand = -1
            self.chord_cand_n = 0
            return
        nt = len(CHORD_TYPES)
        order = np.argsort(scores)[::-1]
        best = int(order[0])
        best_score = float(scores[best])
        best_root = best // nt
        second = -1.0
        for k in order[1:]:                            # bester mit ANDEREM Grundton
            if int(k) // nt != best_root:
                second = float(scores[int(k)])
                break
        if best_score < CHORD_TRIG_MIN_SCORE or (best_score - second) < CHORD_TRIG_MARGIN:
            self.chord_cand = -1
            self.chord_cand_n = 0
            return
        if best == self.chord_idx:                     # gleicher Akkord -> halten
            self.chord_cand = -1
            self.chord_cand_n = 0
            return
        if best == self.chord_cand:
            self.chord_cand_n += 1
        else:
            self.chord_cand = best
            self.chord_cand_n = 1
        need = 1 if self.chord_idx == -1 else CHORD_TRIG_CONFIRM
        if self.chord_cand_n >= need:
            self._chord_off(send)
            notes = self._chord_voicing(best)
            vel = vel_from_level(level)
            for m in notes:
                send(0x90, m, vel)
            self.chord_notes = notes
            self.chord_idx = best
            self.chord_cand = -1
            self.chord_cand_n = 0
            self.display = CHORD_NAMES[best] + "  " + " ".join(midi_name(m) for m in notes)

    def _chord_voicing(self, idx):
        nt = len(CHORD_TYPES)
        root = idx // nt
        ivs = sorted(CHORD_TYPES[idx % nt][1].keys())
        base = 48                                       # Register-Anker (C3)
        root_midi = root + 12 * int(round((base - root) / 12.0))
        if root_midi > base:
            root_midi -= 12
        while root_midi < NOTE_MIN_MIDI:
            root_midi += 12
        out = []
        for iv in ivs:
            m = root_midi + iv
            while m > NOTE_MAX_MIDI:
                m -= 12
            if m not in out:
                out.append(m)
        return sorted(out)

    def _chord_off(self, send):
        for m in self.chord_notes:
            send(0x80, m, 0)
        self.chord_notes = []
        self.chord_idx = -1
        self.off_n = 0
        self.display = "—"

    def _poly(self, level, send):
        detected = set()
        if level > self.silence_rms:
            mag = np.abs(np.fft.rfft(self.buf * self.han))
            mx = float(mag.max()) if mag.size else 0.0
            if mx > 0:
                thr = mx * 0.12
                inner = mag[1:-1]
                idx = np.where((inner > thr) & (inner > mag[:-2]) &
                               (inner >= mag[2:]))[0] + 1
                peaks = []
                for k in idx:
                    a, b, c = mag[k - 1], mag[k], mag[k + 1]
                    denom = a - 2.0 * b + c
                    delta = 0.5 * (a - c) / denom if denom != 0 else 0.0
                    freq = (k + delta) * self.sr / self.win
                    peaks.append((float(b), float(freq)))
                peaks.sort(reverse=True)
                accepted = []
                for _b, freq in peaks:
                    if len(accepted) >= self.max_poly:
                        break
                    if freq <= 0:
                        continue
                    m = int(round(69 + 12 * math.log2(freq / 440.0)))
                    if not (NOTE_MIN_MIDI <= m <= NOTE_MAX_MIDI) or m in detected:
                        continue
                    harmonic = False               # Oberton eines staerkeren Grundtons?
                    for af in accepted:
                        r = freq / af
                        if r > 1.5 and abs(r - round(r)) < 0.04:
                            harmonic = True
                            break
                    if harmonic:
                        continue
                    accepted.append(freq)
                    detected.add(m)
        vel = vel_from_level(level)
        for m in detected:
            if m not in self.active:
                send(0x90, m, vel)
            self.active[m] = 0
        for m in list(self.active):                # Note-Off mit 1 Frame Nachsicht
            if m in detected:
                continue
            if self.active[m] >= 1:
                send(0x80, m, 0)
                del self.active[m]
            else:
                self.active[m] += 1
        on = sorted(m for m, miss in self.active.items() if miss == 0)
        self.display = " ".join(midi_name(m) for m in on) if on else "—"

    def all_off(self, send):
        if self.cur != -1:
            send(0x80, self.cur, 0)
            self.cur = -1
        for m in list(self.active):
            send(0x80, m, 0)
        self.active.clear()
        for m in self.chord_notes:
            send(0x80, m, 0)
        self.chord_notes = []
        self.chord_idx = -1
        self.display = "—"


def note_worker(shared, audio_q, midi_out, stop_event, mode, calib=None):
    """Verbraucht die Capture-Bloecke (wie der Analyse-Worker) und sendet
    erkannte Tonhoehen/Akkorde als MIDI -- ohne die teure Tempo-/Tonart-Analyse.
    mode: 'mono' | 'poly' | 'chord'. calib: optionales dict mit Tracking-Parametern."""
    with shared.lock:
        sr = shared.capture_sr
    det = NoteDetector(sr, mode)
    if calib:
        for k, v in calib.items():
            if hasattr(det, k):
                setattr(det, k, v)

    def send(status, note, vel):
        if midi_out is None:
            return
        try:
            if status == 0x90:
                midi_out.send(mido.Message('note_on', channel=NOTE_CHANNEL,
                                           note=note, velocity=vel))
            else:
                midi_out.send(mido.Message('note_off', channel=NOTE_CHANNEL,
                                           note=note, velocity=0))
        except Exception:
            pass

    try:
        while not stop_event.is_set():
            try:
                block = audio_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if block is None or len(block) == 0:
                continue
            rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
            det.push(np.asarray(block, dtype=np.float32))
            det.process(rms, send)
            with shared.lock:
                shared.note_display = det.display
    finally:
        det.all_off(send)


# ===========================================================================
# Praezises Warten + MIDI-Clock
# ===========================================================================
def precise_sleep_until(target_perf, stop_event):
    """Bis target_perf warten: grob per time.sleep mit Sicherheitsmarge
    (der Scheduler weckt auch bei 1-ms-Timeraufloesung gern ~1 ms zu spaet),
    die letzten ~2 ms Spin auf perf_counter fuer tickgenaues Senden."""
    while True:
        if stop_event.is_set():
            return
        remaining = target_perf - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.002:
            time.sleep(remaining - 0.0015)


def clock_worker(shared, midi_out, stop_event):
    """MIDI-Clock-Thread. Die Clock laeuft NUR, wenn eine echte Tempo-
    Schaetzung vorliegt: vorher waere es ein fiktives Tempo (INITIAL_BPM).
    Bei Stille/Reset stoppt sie (MIDI 'stop') und startet beim naechsten
    Stueck neu ('start') -- im Beat-Sync-Modus exakt auf dem naechsten
    erkannten Beat."""
    winmm = None
    if sys.platform == 'win32':
        # Tick-Timing braucht beides: 1-ms-Timeraufloesung (sonst schlaeft
        # time.sleep in ~15-ms-Schritten; die GUI setzt timeBeginPeriod --
        # anders als das CLI -- sonst nirgends) und hohe Thread-Prioritaet,
        # damit Analyse-Rechnerei und GUI-Rendering die Ticks nicht
        # verdraengen. timeBeginPeriod ist refcounted, der doppelte Aufruf
        # im CLI-Main schadet nicht.
        try:
            import ctypes
            try:
                winmm = ctypes.windll.winmm
                winmm.timeBeginPeriod(1)
            except Exception:
                winmm = None
            try:
                # Windows 11 IGNORIERT timeBeginPeriod fuer Prozesse ohne
                # Vordergrund-Fenster (Timer-Throttling) -- Ticks kaemen
                # dann in ~15-ms-Schritten, sobald ein anderes Fenster den
                # Fokus hat. Das Throttling hier explizit abschalten:
                # PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION (0x4)
                # im ControlMask, StateMask 0 = Aufloesung immer ehren.
                class _PowerThrottling(ctypes.Structure):
                    _fields_ = [("Version", ctypes.c_ulong),
                                ("ControlMask", ctypes.c_ulong),
                                ("StateMask", ctypes.c_ulong)]
                st = _PowerThrottling(1, 0x4, 0)
                ctypes.windll.kernel32.SetProcessInformation(
                    ctypes.windll.kernel32.GetCurrentProcess(),
                    4,          # ProcessPowerThrottling
                    ctypes.byref(st), ctypes.sizeof(st))
            except Exception:
                pass
            try:
                ctypes.windll.kernel32.SetThreadPriority(
                    ctypes.windll.kernel32.GetCurrentThread(),
                    15)         # THREAD_PRIORITY_TIME_CRITICAL
            except Exception:
                pass
        except Exception:
            pass

    clock_msg = mido.Message('clock')
    running = False
    cur_bpm = INITIAL_BPM
    next_tick = time.perf_counter()
    last_loop = next_tick
    t_sent = 0.0                # wann der letzte Tick tatsaechlich rausging
    tick_in_beat = 0            # 0..PPQN-1; Tick 0 soll auf dem Beat liegen

    while not stop_event.is_set():
        with shared.lock:
            target = shared.target_bpm
            have = shared.have_estimate
            bs = shared.beat_sync
            b_anchor = shared.beat_anchor
            b_period = shared.beat_period
            b_valid = shared.beat_valid_time

        if not have:
            # Kein Signal / noch keine Schaetzung -> Clock anhalten.
            if running:
                running = False
                try:
                    if midi_out is not None:
                        midi_out.send(mido.Message('stop'))
                except Exception:
                    break
            time.sleep(0.05)
            last_loop = time.perf_counter()
            continue

        if not running:
            # Schaetzung da -> Clock (wieder) starten; nicht von 120
            # hochslewen, sondern direkt im erkannten Tempo loslegen.
            running = True
            cur_bpm = max(20.0, min(400.0, target))
            now = time.perf_counter()
            next_tick = now
            tick_in_beat = 0
            if bs and b_period > 0 and (now - b_valid) < BEAT_VALID_SEC:
                # Beat-Sync: ersten Tick auf den naechsten Beat legen,
                # damit die Clock von Anfang an auf der Zaehlzeit liegt.
                m = math.ceil((now - b_anchor) / b_period)
                next_tick = b_anchor + m * b_period
            last_loop = now
            try:
                if midi_out is not None:
                    midi_out.send(mido.Message('start'))
            except Exception:
                break
            precise_sleep_until(next_tick, stop_event)
            try:
                if midi_out is not None:
                    midi_out.send(clock_msg)
            except Exception:
                break
            t_sent = time.perf_counter()
            tick_in_beat = 1
            continue

        now = time.perf_counter()
        dt = now - last_loop
        last_loop = now

        max_step = CLOCK_SLEW_BPM_PER_S * dt
        diff = target - cur_bpm
        dead = cur_bpm * CLOCK_DEADBAND_FRAC
        if abs(diff) > cur_bpm * CLOCK_JUMP_FRAC:
            cur_bpm = target            # grosser Sprung: sofort uebernehmen
        elif abs(diff) > dead:
            # Nur bis an den Rand des Totbands heranfahren (Hysterese):
            # das +-0,1-BPM-Wackeln des Medians erreicht die Clock so gar
            # nicht, echte Tempoaenderungen folgen weiter per Slew.
            cur_bpm += math.copysign(min(max_step, abs(diff) - dead), diff)
        cur_bpm = max(20.0, min(400.0, cur_bpm))

        interval = 60.0 / (cur_bpm * PPQN)
        next_tick += interval

        # Beat-Synchronisation: sanfte Regelschleife, die die Tick-Zeiten
        # auf das Beat-Raster aus der Analyse zieht. Tick 'tick_in_beat'
        # gehoert auf (Anker + m*Periode + tick_in_beat/PPQN*Periode);
        # vom Phasenfehler wird pro Tick nur ein kleiner, begrenzter
        # Anteil korrigiert -> kein Stottern der Clock.
        if bs and b_period > 0 and (now - b_valid) < BEAT_VALID_SEC:
            grid = b_anchor + (tick_in_beat / PPQN) * b_period
            rel = (next_tick - grid) / b_period
            err = (rel - round(rel)) * b_period      # Sek., +-Periode/2
            nudge = max(-BEAT_NUDGE_MAX,
                        min(BEAT_NUDGE_MAX, err * BEAT_NUDGE_GAIN))
            next_tick -= nudge

        if now - next_tick > 0.05:
            # Langer Aussetzer: neu aufsetzen; den Phasenversatz holt im
            # Beat-Sync-Modus die Nudge-Schleife wieder ein.
            next_tick = now + interval
        elif next_tick < t_sent + 0.5 * interval:
            # Kurzer Rueckstand (Scheduler/GIL): aufholen, aber hoechstens
            # mit doppeltem Tempo. Ein Burst von Ticks im Null-Abstand
            # laesst Empfaenger, die das Tempo aus den Tick-Abstaenden
            # mitteln, kurz zappeln.
            next_tick = t_sent + 0.5 * interval

        precise_sleep_until(next_tick, stop_event)
        try:
            if midi_out is not None:
                midi_out.send(clock_msg)
        except Exception:
            break
        t_sent = time.perf_counter()
        tick_in_beat = (tick_in_beat + 1) % PPQN

    try:
        if midi_out is not None and running:
            midi_out.send(mido.Message('stop'))
    except Exception:
        pass
    if winmm is not None:
        try:
            winmm.timeEndPeriod(1)
        except Exception:
            pass


# ===========================================================================
# Datei-Modus: Offline-Beat-Map + driftfreie Wiedergabe-Clock
# ===========================================================================
# Mirror der WebApp ("Datei/Aufnahme -> MIDI-Clock (driftfrei)"): Eine Datei
# wird einmal vorab zu einer Beat-Map analysiert (globales Tempo -> Beat-Tracker
# -> konstant/variabel). Bei der Wiedergabe taktet die MIDI-Clock NICHT frei
# mit, sondern wird aus der Wiedergabeposition abgeleitet -- die Tick-Zeitpunkte
# stehen als feste Sekunden-Marken (24 PPQN am Beat-Raster) fest und werden zu
# genau der Zeit gesendet, zu der die Wiedergabe sie erreicht. Driftfrei, weil
# die Position aus dem Geraete-Takt (ausgegebene Frames) kommt.
FILE_CONST_DRIFT = 0.015    # <=1,5% Tempo-Unterschied 1./2. Haelfte -> "konstant"
FILE_FOLD_REL    = 0.006    # Feinsuche der Periode: +-0,6% um den Median-Beat-Abstand
FILE_FOLD_MIN_R  = 0.45     # ab dieser Phasenkohaerenz wird die verfeinerte Periode genutzt
FILE_ONSET_HOP   = 256      # Onset-Hop fuer die Offline-Beat-Map (~11,6 ms @ 22,05 kHz)


def _refine_beats_to_onset(frames, oe):
    """Beat-Frames auf die naechste Onset-Spitze ziehen und sub-Frame-genau
    parabolisch interpolieren (reduziert die Frame-Quantisierung)."""
    L = len(oe)
    r = 2
    out = np.empty(len(frames), dtype=np.float64)
    for n, bf in enumerate(frames):
        b = int(round(bf))
        best_i = b
        best_v = oe[b] if 0 <= b < L else -1e18
        for j in range(-r, r + 1):
            k = b + j
            if 0 <= k < L and oe[k] > best_v:
                best_v = oe[k]
                best_i = k
        frac = 0.0
        if 0 < best_i < L - 1:
            y0, y1, y2 = oe[best_i - 1], oe[best_i], oe[best_i + 1]
            den = y0 - 2.0 * y1 + y2
            if den != 0:
                d = 0.5 * (y0 - y2) / den
                if -1.0 < d < 1.0:
                    frac = d
        out[n] = best_i + frac
    return out


def _local_period_curve(onset, env_rate, tau0, min_bpm, max_bpm):
    """Zeitlich variable Periodenkurve (Frames/Beat), oktav-fest um tau0. Pro
    halbe Sekunde wird die lokale Autokorrelation (6-s-Fenster) ausgewertet,
    mit sanftem Prior auf tau0, dann ueber 3 Knoten geglaettet und auf jeden
    Frame linear interpoliert. Mirror der WebApp-localPeriodCurve."""
    L = len(onset)
    win_len = max(int(round(6 * env_rate)), int(round(5 * tau0)))
    hop = max(1, int(round(0.5 * env_rate)))
    lag_lo = max(2, int(round(tau0 * 0.7)))
    lag_hi = int(round(tau0 * 1.45))
    lag_lo = max(lag_lo, int(math.floor(60 * env_rate / max_bpm)))
    lag_hi = min(lag_hi, int(math.ceil(60 * env_rate / min_bpm)))
    if lag_hi <= lag_lo:
        return np.full(L, float(tau0))
    half_w = win_len >> 1
    lags = np.arange(lag_lo, lag_hi + 1)
    prior = np.exp(-0.5 * (np.log(lags / tau0) / 0.35) ** 2)
    knot_x, knot_lag = [], []
    for c in range(0, L, hop):
        s = max(0, c - half_w)
        e = min(L, c + half_w)
        nn = e - s
        if nn <= lag_hi + 2:
            knot_x.append(c)
            knot_lag.append(float(tau0))
            continue
        x = onset[s:e] - onset[s:e].mean()
        energy = float(np.dot(x, x))
        best_lag = float(tau0)
        if energy > 0:
            scores = np.empty(len(lags), dtype=np.float64)
            for li, lag in enumerate(lags):
                acc = float(np.dot(x[:nn - lag], x[lag:nn]))
                scores[li] = acc / energy * prior[li]
            best_lag = float(lags[int(np.argmax(scores))])
        knot_x.append(c)
        knot_lag.append(best_lag)
    kl = np.array(knot_lag, dtype=np.float64)
    sm = kl.copy()
    for i in range(len(kl)):
        sm[i] = kl[max(0, i - 1):min(len(kl), i + 2)].mean()
    if len(knot_x) == 1:
        return np.full(L, sm[0])
    return np.interp(np.arange(L), np.array(knot_x, dtype=np.float64), sm)


def _dp_beats(local, period, tightness):
    """Dynamische Programmierung (Ellis): beste Beat-Folge bei lokal erwarteter
    Periode. Mirror der WebApp-dpBeats; innere Schleife vektorisiert."""
    L = len(local)
    cum = np.zeros(L, dtype=np.float64)
    back = np.full(L, -1, dtype=np.int64)
    for i in range(L):
        p = period[i] if period[i] > 1 else 1.0
        lo = max(1, int(round(p * 0.5)))
        hi = int(round(p * 2.0))
        if hi < lo:
            cum[i] = local[i]
            continue
        lagvals = np.arange(lo, hi + 1)
        js = i - lagvals
        ok = js >= 0
        if not ok.any():
            cum[i] = local[i]
            continue
        lv = lagvals[ok]
        jj = js[ok]
        r = np.log(lv / p)
        sc = cum[jj] - tightness * (r * r)
        b = int(np.argmax(sc))
        cum[i] = local[i] + sc[b]
        back[i] = jj[b]
    p_last = period[L - 1] if period[L - 1] > 1 else 1.0
    tail_start = max(0, L - int(round(p_last)))
    seg = cum[tail_start:]
    if len(seg) == 0:
        return []
    best_end = tail_start + int(np.argmax(seg))
    beats = []
    i = best_end
    while i >= 0:
        beats.append(i)
        i = int(back[i])
    beats.reverse()
    return beats


def _fold_period(beats, p0, rel):
    """Phasenfaltung: feine Periodensuche eng um p0 (Sek.). Faltet alle Beats
    auf ihre Phase (Beat modulo P) und sucht die Periode mit der staerksten
    Buendelung (Resultierenden-Laenge R, 0..1). Unempfindlich gegen
    ausgelassene/doppelte Beats. Rueckgabe (P, R)."""
    beats = np.asarray(beats, dtype=np.float64)
    m = len(beats)

    def coh_grid(ps):
        a = (2.0 * np.pi) * (beats[None, :] / ps[:, None])
        return np.hypot(np.cos(a).sum(axis=1), np.sin(a).sum(axis=1)) / m

    lo, hi = p0 * (1.0 - rel), p0 * (1.0 + rel)
    best_p, best_r = p0, -1.0
    for _ in range(3):                       # grob -> fein um das Maximum
        steps = 400
        ps = np.linspace(lo, hi, steps + 1)
        rs = coh_grid(ps)
        bi = int(np.argmax(rs))
        best_p, best_r = float(ps[bi]), float(rs[bi])
        span = (hi - lo) / steps * 4.0
        lo, hi = best_p - span, best_p + span
    return best_p, best_r


def _grid_phase(beats, p):
    """Zirkulaeres Mittel der Beat-Phasen modulo P -> bester globaler Anker (s)
    in [0, P). Robust gegen Ausreisser (Vektormittel)."""
    beats = np.asarray(beats, dtype=np.float64)
    a = (2.0 * np.pi) * (beats / p)
    ph = math.atan2(float(np.sin(a).sum()), float(np.cos(a).sum())) / (2.0 * np.pi) * p
    ph %= p
    if ph < 0:
        ph += p
    return ph


def _build_tick_times(beats, duration):
    """24 PPQN am Beat-Raster. Ist ein Beat-Abstand ~doppelt so lang wie seine
    Nachbarn (vom Tracker uebersprungener Beat), bekommt er 48 Ticks (mult=2) ->
    die Clock-Geschwindigkeit bleibt durchgehend korrekt und phasenrichtig.
    Ueber den letzten Beat hinaus wird mit dem letzten Intervall bis Dateiende
    verlaengert."""
    beats = np.asarray(beats, dtype=np.float64)
    m = len(beats)
    if m < 2:
        return np.array([], dtype=np.float64)
    ibi = np.diff(beats)

    def local_med(k):
        w = ibi[max(0, k - 2):min(len(ibi), k + 3)]
        return float(np.median(w)) if len(w) else 0.0

    ticks = []
    for k in range(m - 1):
        t0 = beats[k]
        d = beats[k + 1] - beats[k]
        med = local_med(k) or d
        mult = int(round(d / med)) if med > 0 else 1
        mult = min(4, max(1, mult))
        sub = PPQN * mult
        for j in range(sub):
            ticks.append(t0 + d * (j / sub))
    last_ivl = beats[m - 1] - beats[m - 2]
    if last_ivl > 0:
        t = beats[m - 1]
        while t <= duration + 1e-6:
            for j in range(PPQN):
                tt = t + last_ivl * (j / PPQN)
                if tt <= duration + 1e-6:
                    ticks.append(tt)
            t += last_ivl
    else:
        ticks.append(beats[m - 1])
    return np.array(ticks, dtype=np.float64)


def file_bpm_at(beats, pos, fallback=0.0):
    """Momentantempo (BPM) an Wiedergabeposition pos (s) aus dem Beat-Raster."""
    b = np.asarray(beats, dtype=np.float64)
    if b is None or len(b) < 2:
        return fallback
    if pos <= b[0]:
        d = b[1] - b[0]
        return 60.0 / d if d > 0 else fallback
    if pos >= b[-1]:
        d = b[-1] - b[-2]
        return 60.0 / d if d > 0 else fallback
    i = int(np.searchsorted(b, pos)) - 1
    i = min(max(i, 0), len(b) - 2)
    d = b[i + 1] - b[i]
    return 60.0 / d if d > 0 else fallback


def analyze_file_beatmap(y, sr, min_bpm=MIN_BPM, max_bpm=MAX_BPM):
    """Offline-Beat-Map aus Mono-Audio y@sr. Rueckgabe-dict oder None:
        beats      np.ndarray  Beat-Zeiten (s)
        ticks      np.ndarray  Tick-Zeiten (s, 24 PPQN am Raster)
        constant   bool        konstantes Tempo erkannt (perfektes Raster)
        bpm        float       (mittleres) Tempo
        bpm_min/max float      bei variablem Tempo die Spanne (10/90-Perzentil)
        duration   float       Laenge (s)
    """
    if y is None or len(y) < sr:                  # < 1 s -> keine sinnvolle Schaetzung
        return None
    duration = len(y) / float(sr)
    hop = FILE_ONSET_HOP
    try:
        oe = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    except Exception:
        return None
    if oe is None or not np.any(oe):
        return None
    fr = sr / hop
    g_bpm = _tempo_from_onset_env(oe, fr, 0.0)    # globales Tempo (Prior + Bereich)
    if not g_bpm or not np.isfinite(g_bpm):
        return None
    tau0 = 60.0 * fr / g_bpm                       # Frames pro Beat (Startwert)
    # Lokale Periodenkurve (folgt Tempowechseln) -> DP-Beat-Tracker. Anders als
    # librosas konstant-Tempo-Tracker erlaubt das, variables Tempo zu erkennen.
    period = _local_period_curve(oe, fr, tau0, min_bpm, max_bpm)
    std = float(oe.std())
    local = oe / std if std > 0 else oe
    beat_idx = _dp_beats(local, period, 100.0)     # FILE_TIGHTNESS = 100
    if len(beat_idx) < 2:
        return None
    beat_frames = _refine_beats_to_onset(np.array(beat_idx, dtype=np.float64), oe)
    beats = beat_frames * hop / float(sr)
    # sanitisieren: streng monoton steigend
    keep = [beats[0]]
    for t in beats[1:]:
        if np.isfinite(t) and t > keep[-1] + 1e-4:
            keep.append(t)
    beats = np.array(keep, dtype=np.float64)
    if len(beats) < 2:
        return None

    ibis = np.diff(beats)
    ibis = ibis[ibis > 0]
    if len(ibis) < 2:
        return None
    med_ivl = float(np.median(ibis))
    mid = len(beats) // 2

    def med_half(lo, hi):
        w = np.diff(beats[lo:hi])
        w = w[w > 0]
        return float(np.median(w)) if len(w) else 0.0

    mh1, mh2 = med_half(0, mid + 1), med_half(mid, len(beats))
    drift = (abs(mh2 - mh1) / (0.5 * (mh1 + mh2))) if (mh1 > 0 and mh2 > 0) else 1.0

    if med_ivl > 0 and drift < FILE_CONST_DRIFT:
        # konstantes Tempo: Periode per Phasenfaltung verfeinern, globales
        # Raster ueber die volle Laenge legen -> perfekt driftfrei.
        fold_p, fold_r = _fold_period(beats, med_ivl, FILE_FOLD_REL)
        period = fold_p if fold_r >= FILE_FOLD_MIN_R else med_ivl
        ph = _grid_phase(beats, period)
        arr = []
        k = math.ceil((0.0 - ph) / period - 1e-9)
        while True:
            t = ph + period * k
            if t > duration:
                break
            if t >= 0:
                arr.append(t)
            k += 1
        if len(arr) >= 2:
            grid = np.array(arr, dtype=np.float64)
            bpm = 60.0 / period
            return {"beats": grid, "ticks": _build_tick_times(grid, duration),
                    "constant": True, "bpm": bpm, "bpm_min": bpm, "bpm_max": bpm,
                    "duration": duration}

    # variabel: robuste Kennzahlen
    sorted_ibi = np.sort(ibis)
    q10 = float(sorted_ibi[int(round(0.1 * (len(sorted_ibi) - 1)))])
    q90 = float(sorted_ibi[int(round(0.9 * (len(sorted_ibi) - 1)))])
    return {"beats": beats, "ticks": _build_tick_times(beats, duration),
            "constant": False, "bpm": 60.0 / med_ivl,
            "bpm_min": 60.0 / q90 if q90 > 0 else 0.0,
            "bpm_max": 60.0 / q10 if q10 > 0 else 0.0,
            "duration": duration}


def load_audio_file(path):
    """Laedt eine Audiodatei zweifach: Mono @ ANALYSIS_SR fuer die Analyse und
    (verlustfrei, native Rate, alle Kanaele) fuer die Wiedergabe. Rueckgabe
    (y_analyse, audio_play[frames,ch] float32, sr_play) oder wirft."""
    y_an, _ = librosa.load(path, sr=ANALYSIS_SR, mono=True)
    if sf is not None:
        audio, sr_play = sf.read(path, dtype='float32', always_2d=True)
    else:
        # Fallback ueber librosa (resampelt ggf.); native Rate beibehalten
        y2, sr_play = librosa.load(path, sr=None, mono=False)
        audio = y2.T if y2.ndim > 1 else y2.reshape(-1, 1)
        audio = np.ascontiguousarray(audio, dtype=np.float32)
    return y_an, audio, int(sr_play)


def _realtime_timer_begin():
    """Win11: 1-ms-Timeraufloesung + Timer-Throttling aus + hohe Thread-
    Prioritaet fuer tickgenaues Senden. Rueckgabe winmm-Handle oder None."""
    if sys.platform != 'win32':
        return None
    winmm = None
    try:
        import ctypes
        try:
            winmm = ctypes.windll.winmm
            winmm.timeBeginPeriod(1)
        except Exception:
            winmm = None
        try:
            class _PowerThrottling(ctypes.Structure):
                _fields_ = [("Version", ctypes.c_ulong),
                            ("ControlMask", ctypes.c_ulong),
                            ("StateMask", ctypes.c_ulong)]
            st = _PowerThrottling(1, 0x4, 0)
            ctypes.windll.kernel32.SetProcessInformation(
                ctypes.windll.kernel32.GetCurrentProcess(),
                4, ctypes.byref(st), ctypes.sizeof(st))
        except Exception:
            pass
        try:
            ctypes.windll.kernel32.SetThreadPriority(
                ctypes.windll.kernel32.GetCurrentThread(), 15)
        except Exception:
            pass
    except Exception:
        pass
    return winmm


def _realtime_timer_end(winmm):
    if winmm is not None:
        try:
            winmm.timeEndPeriod(1)
        except Exception:
            pass


class FilePlayer:
    """Spielt einen dekodierten Audiopuffer ueber sounddevice ab und stellt der
    Clock eine driftfreie Wiedergabeposition bereit. Die Position kommt aus dem
    GERAETE-Takt (ausgegebene Frames), nicht aus perf_counter -- deshalb laeuft
    eine daraus getaktete MIDI-Clock nicht gegen die Wiedergabe weg. Zwischen
    zwei Callbacks wird linear interpoliert (Bloecke sind klein/gleichmaessig)."""

    def __init__(self, audio, sr, device=None, blocksize=1024):
        if audio.ndim == 1:
            audio = audio.reshape(-1, 1)
        self.audio = np.ascontiguousarray(audio, dtype=np.float32)
        self.frames_total = self.audio.shape[0]
        self.channels = self.audio.shape[1]
        self.sr = int(sr)
        self.device = device
        self.blocksize = blocksize
        self.lock = threading.Lock()
        self.pos = 0                       # naechster auszugebender Frame
        self.anchor_pos = 0                # Frames vor dem letzten Callback
        self.anchor_perf = time.perf_counter()
        self.latency = blocksize / float(sr)   # Ausgabe-Latenz (s), s. start()
        self.finished = False
        self.playing = False
        self.stream = None

    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            self.anchor_pos = self.pos
            self.anchor_perf = time.perf_counter()
        start = self.pos
        end = min(start + frames, self.frames_total)
        n = end - start
        if n > 0:
            outdata[:n] = self.audio[start:end]
            self.pos = end
        if n < frames:
            outdata[n:] = 0
            self.finished = True
            raise sd.CallbackStop

    def start(self):
        if sd is None:
            raise RuntimeError("sounddevice nicht verfuegbar")
        self.stream = sd.OutputStream(
            samplerate=self.sr, channels=self.channels, blocksize=self.blocksize,
            device=self.device, dtype='float32', callback=self._callback)
        self.stream.start()
        try:
            self.latency = float(self.stream.latency)
        except Exception:
            self.latency = self.blocksize / float(self.sr)
        self.playing = True

    def play_pos(self):
        """Aktuelle Wiedergabeposition (s), driftfrei aus dem Geraete-Takt."""
        with self.lock:
            ap, aperf = self.anchor_pos, self.anchor_perf
        return ap / float(self.sr) - self.latency + (time.perf_counter() - aperf)

    def is_done(self):
        return self.finished or self.pos >= self.frames_total

    def stop(self):
        self.playing = False
        st, self.stream = self.stream, None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass


def file_clock_worker(shared, player, ticks, midi_out, stop_event):
    """Treibt die MIDI-Clock streng aus der Wiedergabeposition des FilePlayer.
    Die Tick-Zeitpunkte (Sek.) stehen vorab fest; jeder Tick wird zu der
    perf_counter-Zeit gesendet, zu der die Wiedergabe diese Position erreicht
    -- daher driftfrei. Bei Aussetzer/Seek wird der Tick-Index neu auf die
    aktuelle Position gesetzt (kein Tick-Burst)."""
    winmm = _realtime_timer_begin()
    clock_msg = mido.Message('clock')
    ticks = np.asarray(ticks, dtype=np.float64)
    n = len(ticks)
    started = False
    try:
        if midi_out is not None:
            midi_out.send(mido.Message('start'))
        started = True
    except Exception:
        pass

    i = int(np.searchsorted(ticks, max(0.0, player.play_pos()))) if n else 0
    while not stop_event.is_set():
        if player.is_done() or i >= n:
            break
        target = float(ticks[i])
        pos = player.play_pos()
        wait = target - pos
        if wait > 0.0:
            precise_sleep_until(time.perf_counter() + wait, stop_event)
            if stop_event.is_set():
                break
        try:
            if midi_out is not None:
                midi_out.send(clock_msg)
        except Exception:
            break
        i += 1
        # bei grossem Rueckstand (Aussetzer/Seek) neu einrasten statt Burst
        pos2 = player.play_pos()
        if i < n and pos2 - target > 0.25:
            i = int(np.searchsorted(ticks, pos2))

    try:
        if midi_out is not None and started:
            midi_out.send(mido.Message('stop'))
    except Exception:
        pass
    _realtime_timer_end(winmm)


# ===========================================================================
# Aufnahme: Segmentierung in Stuecke + Speichern (Mirror der WebApp)
# ===========================================================================
REC_GAP_S = 1.2     # Stille-Laenge (s), ab der eine Stueck-Grenze vermutet wird
REC_MIN_S = 15.0    # kuerzere Segmente werden zum Nachbarn gemerged


def _to_analysis_sr(y, sr):
    """Mono-Signal fuer die Analyse auf ANALYSIS_SR bringen (Tempo/Tonart sind
    darauf abgestimmt); gespeichert wird spaeter in voller Aufnahmerate."""
    if int(sr) == ANALYSIS_SR:
        return y
    try:
        return librosa.resample(np.asarray(y, dtype=np.float32),
                                orig_sr=sr, target_sr=ANALYSIS_SR)
    except Exception:
        return y


def _seg_bpm(y, sr, min_bpm, max_bpm):
    ya = _to_analysis_sr(y, sr)
    if len(ya) < ANALYSIS_SR:
        return 0.0
    try:
        info = analyze_file_beatmap(ya, ANALYSIS_SR, min_bpm, max_bpm)
    except Exception:
        info = None
    return info["bpm"] if info else 0.0


def _seg_key(y, sr):
    ya = _to_analysis_sr(y, sr)
    try:
        name, margin = estimate_key(ya, ANALYSIS_SR, with_margin=True)
        return name, margin
    except Exception:
        return "", 0.0


def suggest_seg_name(seg):
    """Namensvorschlag aus BPM + Tonart, z. B. '120BPM_C_Dur'."""
    parts = []
    if seg.get("bpm"):
        parts.append(f"{int(round(seg['bpm']))}BPM")
    if seg.get("key"):
        parts.append(seg["key"].replace(" ", "_"))
    return "_".join(parts) if parts else "Aufnahme"


def _merge_short(segs, min_samples):
    changed = True
    while changed and len(segs) > 1:
        changed = False
        for k in range(len(segs)):
            if segs[k]["end"] - segs[k]["start"] < min_samples:
                if k > 0:
                    segs[k - 1]["end"] = segs[k]["end"]
                    segs.pop(k)
                else:
                    segs[k + 1]["start"] = segs[k]["start"]
                    segs.pop(k)
                changed = True
                break
    return segs


def segment_recording(y, sr, min_bpm=MIN_BPM, max_bpm=MAX_BPM):
    """Zerlegt eine Aufnahme (Mono y@sr) an kurzer Stille + BPM/Tonart-Wechsel
    in einzelne Stuecke. Rueckgabe: Liste von dicts mit start/end (Samples in
    y), bpm, key, key_margin, confident, name. Mirror der WebApp."""
    y = np.asarray(y, dtype=np.float32)
    n = len(y)
    whole = {"start": 0, "end": n, "bpm": 0.0, "key": "", "key_margin": 0.0,
             "confident": True, "name": "Aufnahme"}
    if n < sr:                                     # < 1 s -> ein Stueck
        return [whole]
    fr = 2048
    nf = n // fr
    if nf < 2:
        return [whole]
    frames = y[:nf * fr].reshape(nf, fr).astype(np.float64)
    rms = np.sqrt((frames * frames).mean(axis=1))
    peak = float(rms.max())
    thr = max(peak * 0.06, 1e-4)                   # ~ -24 dB unter Peak = "still"
    gap_frames = math.ceil(REC_GAP_S * sr / fr)
    cuts = []
    i = 0
    while i < nf:
        if rms[i] < thr:
            j = i
            while j < nf and rms[j] < thr:
                j += 1
            if j - i >= gap_frames:
                mid = ((i + j) // 2) * fr
                if fr * 8 < mid < n - fr * 8:
                    cuts.append(mid)
            i = j
        else:
            i += 1
    bounds = [0] + cuts + [n]
    segs = [{"start": bounds[k], "end": bounds[k + 1]}
            for k in range(len(bounds) - 1)]
    segs = _merge_short(segs, int(REC_MIN_S * sr))

    out = []
    for s in segs:
        if s["end"] - s["start"] < sr:             # < 1 s ueberspringen
            continue
        sub = y[s["start"]:s["end"]]
        bpm = _seg_bpm(sub, sr, min_bpm, max_bpm)
        key, margin = _seg_key(sub, sr)
        out.append({"start": s["start"], "end": s["end"], "bpm": bpm,
                    "key": key, "key_margin": margin})
    if not out:
        return [whole]
    for k, seg in enumerate(out):
        if k == 0:
            seg["confident"] = True
        else:
            a, b = out[k - 1], seg
            bpm_diff = (abs(a["bpm"] - b["bpm"]) / ((a["bpm"] + b["bpm"]) / 2)
                        if (a["bpm"] and b["bpm"]) else 0.0)
            seg["confident"] = (bpm_diff > 0.04
                                or (bool(a["key"]) and bool(b["key"])
                                    and a["key"] != b["key"]))
        seg["name"] = suggest_seg_name(seg)
    return out


def sanitize_filename(name):
    """Unzulaessige Dateinamenzeichen durch '_' ersetzen."""
    out = []
    for ch in (name or "Aufnahme"):
        out.append("_" if ch in '\\/:*?"<>|' else ch)
    return "".join(out).strip() or "Aufnahme"


def save_wav_slice(audio, sr, s0, s1, path):
    """Schreibt audio[s0:s1] als 16-bit-PCM-WAV (mono oder stereo)."""
    if sf is None:
        raise RuntimeError("soundfile nicht verfuegbar (pip install soundfile)")
    seg = np.asarray(audio)[s0:s1]
    sf.write(path, seg, int(sr), subtype='PCM_16')


# ===========================================================================
# Stem-Trennung (lokales KI-Modell Demucs) -- optional
# ===========================================================================
# Trennt ein Stueck in Instrumentengruppen (Drums/Bass/Gesang/Rest). Laeuft
# komplett lokal und OFFLINE ueber Demucs (PyTorch); langsam, daher als
# Hintergrundschritt gedacht. Optionale Abhaengigkeit -- ohne Demucs bleibt das
# Feature einfach aus. torch wird ERST beim ersten Trennen importiert (sonst
# wuerde es jeden Programmstart spuerbar verlangsamen).
STEM_NAMES = ("drums", "bass", "other", "vocals")
STEM_LABELS = {"drums": "Drums", "bass": "Bass", "vocals": "Vocals", "other": "Rest"}
_demucs_sep = None
_demucs_mod = None
_demucs_model = None      # gecachtes Modell fuer den direkten Weg (ohne torchaudio)


def demucs_available():
    """Schnelltest (ohne torch zu importieren), ob Demucs nutzbar waere."""
    import importlib.util as _u
    return (_u.find_spec("demucs") is not None
            and _u.find_spec("torch") is not None)


def _emit(log, msg):
    """Ruft den optionalen Fortschritts-/Log-Callback sicher auf.
    Fehler im Callback werden geschluckt, damit die Trennung nie daran scheitert."""
    if log:
        try:
            log(str(msg))
        except Exception:
            pass


def _get_separator(model="htdemucs", log=None):
    global _demucs_sep, _demucs_mod
    if _demucs_mod is None:
        _emit(log, "Lade PyTorch + Demucs (einmalig) …")
        try:
            import torch
            from demucs.api import Separator
            _demucs_mod = (torch, Separator)
        except Exception as e:
            raise RuntimeError("Demucs/torch nicht installiert "
                               "(pip install demucs): " + str(e))
    torch, Separator = _demucs_mod
    if _demucs_sep is None or getattr(_demucs_sep, "_a2m_model", None) != model:
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        _emit(log, f"Initialisiere Modell '{model}' auf {dev.upper()} "
                   "(beim ersten Mal wird das Modell heruntergeladen) …")
        _demucs_sep = Separator(model=model, device=dev)
        _demucs_sep._a2m_model = model
        _emit(log, "Modell bereit.")
    return _demucs_sep


def _separate_stems_api(path, model="htdemucs", log=None):
    """Trennung ueber die demucs.api (demucs >= 4)."""
    sep = _get_separator(model, log=log)
    _emit(log, f"Trennung laeuft (Python-API) fuer {os.path.basename(path)} … "
               "das kann je nach Laenge und CPU einige Minuten dauern.")
    _origin, separated = sep.separate_audio_file(path)
    sr = int(getattr(sep, "samplerate", DJ_SR))
    out = {}
    for name, tensor in separated.items():
        arr = tensor.detach().cpu().numpy()        # (channels, samples)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        out[name] = np.ascontiguousarray(arr.T, dtype=np.float32)  # (samples, ch)
    _emit(log, "Stems erhalten: " + ", ".join(out.keys()))
    return out, sr


def _get_model_direct(model="htdemucs", log=None):
    """Laedt das Demucs-Modell ueber die Low-Level-API (pretrained/apply).
    Unabhaengig von demucs.api UND von torchaudio -- noetig, weil neuere
    torchaudio-Versionen zum Laden 'torchcodec' verlangen und die demucs.api
    in manchen Installationen fehlt."""
    global _demucs_model
    try:
        import torch  # noqa: F401  (nur Verfuegbarkeit pruefen / spaeter genutzt)
        from demucs.pretrained import get_model
        from demucs.apply import apply_model  # noqa: F401
    except Exception as e:
        raise RuntimeError("Demucs/torch nicht nutzbar (pip install demucs): " + str(e))
    if _demucs_model is None or getattr(_demucs_model, "_a2m_name", None) != model:
        _emit(log, f"Lade Modell '{model}' (direkter Weg) – beim ersten Mal "
                   "wird es heruntergeladen …")
        m = get_model(model)
        m.cpu()
        m.eval()
        m._a2m_name = model
        _demucs_model = m
        _emit(log, "Modell bereit.")
    return _demucs_model


def _load_audio_for_demucs(path, target_sr, channels):
    """Laedt eine Audiodatei OHNE torchaudio (per librosa/soundfile), resamplet
    auf target_sr und bringt sie auf 'channels' Spuren. Rueckgabe: np.ndarray
    (channels, samples) float32."""
    wav, _sr = librosa.load(path, sr=target_sr, mono=False)
    wav = np.asarray(wav, dtype=np.float32)
    if wav.ndim == 1:                      # mono -> (1, samples)
        wav = wav[None, :]
    if wav.shape[0] == 1 and channels >= 2:
        wav = np.repeat(wav, channels, axis=0)
    elif wav.shape[0] > channels:
        wav = wav[:channels]
    elif wav.shape[0] < channels:
        wav = np.repeat(wav[:1], channels, axis=0)
    return np.ascontiguousarray(wav, dtype=np.float32)


def _separate_stems_direct(path, model="htdemucs", log=None):
    """Trennung ueber die Low-Level-API von Demucs, mit eigenem Audio-Laden
    (librosa) statt torchaudio. Robust gegen fehlende demucs.api/torchcodec."""
    import torch
    from demucs.apply import apply_model
    m = _get_model_direct(model, log=log)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    _emit(log, f"Lade Audio: {os.path.basename(path)} …")
    wav = _load_audio_for_demucs(path, int(m.samplerate), int(m.audio_channels))
    wt = torch.from_numpy(wav)
    ref = wt.mean(0)                       # demucs-Normierung (wie in separate.py)
    wt = (wt - ref.mean()) / (ref.std() + 1e-8)
    _emit(log, f"Trennung laeuft (direkt, {dev.upper()}) … "
               "das kann je nach Laenge und CPU einige Minuten dauern.")
    with torch.no_grad():
        sources = apply_model(m, wt[None], device=dev, progress=False)[0]
    sources = sources * ref.std() + ref.mean()
    out = {}
    for name, src in zip(m.sources, sources):
        arr = src.detach().cpu().numpy()   # (channels, samples)
        out[name] = np.ascontiguousarray(arr.T, dtype=np.float32)  # (samples, ch)
    _emit(log, "Stems erhalten: " + ", ".join(out.keys()))
    return out, int(m.samplerate)


def _separate_stems_cli(path, model="htdemucs", log=None):
    """Fallback ueber die Demucs-Kommandozeile (versionsrobust): schreibt die
    Stems in einen Temp-Ordner und liest sie zurueck."""
    import subprocess
    import tempfile
    import shutil
    if sf is None:
        raise RuntimeError("soundfile fehlt")
    tmp = tempfile.mkdtemp(prefix="a2m_stems_")
    try:
        _emit(log, "Trennung laeuft (Kommandozeile: python -m demucs) …")
        proc = subprocess.run(
            [sys.executable, "-m", "demucs", "-n", model, "-o", tmp, path],
            capture_output=True, text=True)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            # nur die letzten Zeilen, damit das Log lesbar bleibt
            tail = "\n".join(detail.splitlines()[-12:]) if detail else "(keine Ausgabe)"
            raise RuntimeError(f"demucs (CLI) Exit-Code {proc.returncode}:\n{tail}")
        base = os.path.splitext(os.path.basename(path))[0]
        stem_dir = os.path.join(tmp, model, base)
        out, sr = {}, DJ_SR
        for name in list(STEM_NAMES) + ["guitar", "piano"]:
            f = os.path.join(stem_dir, name + ".wav")
            if os.path.exists(f):
                data, srr = sf.read(f, dtype='float32', always_2d=True)
                out[name] = np.ascontiguousarray(data, dtype=np.float32)
                sr = int(srr)
        if not out:
            raise RuntimeError(f"keine Stem-Dateien gefunden (erwartet in {stem_dir})")
        _emit(log, "Stems erhalten: " + ", ".join(out.keys()))
        return out, sr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def separate_stems(path, model="htdemucs", log=None):
    """Trennt eine Audiodatei lokal per Demucs. Rueckgabe (dict {name:
    (frames, ch) float32}, sr). OFFLINE und langsam (KI-Modell).

    Drei Wege, vom robustesten zum ausweichendsten:
      1) direkt (eigenes Audio-Laden per librosa, ohne torchaudio) -- laeuft
         auch, wenn demucs.api fehlt oder torchaudio 'torchcodec' verlangt,
      2) demucs.api (falls vorhanden),
      3) Kommandozeile (python -m demucs).
    Mit optionalem log(text)-Callback fuer Fortschrittsmeldungen."""
    errors = []
    for label, fn in (("direkt", _separate_stems_direct),
                      ("API", _separate_stems_api),
                      ("CLI", _separate_stems_cli)):
        try:
            return fn(path, model, log=log)
        except Exception as e:
            errors.append(f"{label}: {e}")
            _emit(log, f"Weg '{label}' fehlgeschlagen ({e}).")
    raise RuntimeError("Stem-Trennung fehlgeschlagen – " + "; ".join(errors))


def write_stems_to_files(stems, sr, out_dir, base="stems", log=None):
    """Schreibt BEREITS getrennte Stems als einzelne WAVs (PCM_16) in out_dir.
    Rueckgabe: Liste der geschriebenen Pfade. So muss bei kombinierten Aktionen
    (Export + Sheet + Abspielen) die Trennung nur einmal laufen."""
    if sf is None:
        raise RuntimeError("soundfile nicht verfuegbar (pip install soundfile)")
    base = sanitize_filename(base or "stems")
    _emit(log, f"Speichere Stems nach {out_dir} …")
    # bekannte Stems zuerst, dann evtl. zusaetzliche (6s-Modell: guitar/piano)
    order = list(STEM_NAMES) + [n for n in stems if n not in STEM_NAMES]
    written = []
    for name in order:
        if name not in stems:
            continue
        p = os.path.join(out_dir, f"{base}_{name}.wav")
        sf.write(p, stems[name], sr, subtype='PCM_16')
        written.append(p)
        _emit(log, "  ✓ " + os.path.basename(p))
    _emit(log, f"Fertig – {len(written)} Datei(en) gespeichert.")
    return written


def separate_stems_to_files(path, out_dir, model="htdemucs", base=None, log=None):
    """Trennt eine Datei und speichert die Spuren als einzelne WAVs.
    Rueckgabe: Liste der geschriebenen Pfade."""
    if sf is None:
        raise RuntimeError("soundfile nicht verfuegbar (pip install soundfile)")
    stems, sr = separate_stems(path, model, log=log)
    base = base or os.path.splitext(os.path.basename(path))[0]
    return write_stems_to_files(stems, sr, out_dir, base=base, log=log)


def separate_stems_array(audio, sr, model="htdemucs", log=None):
    """Wie separate_stems, aber fuer ein In-Memory-Signal (z. B. eine Aufnahme):
    schreibt eine Temp-WAV und trennt diese. Rueckgabe (dict, sr)."""
    if sf is None:
        raise RuntimeError("soundfile nicht verfuegbar (pip install soundfile)")
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        _emit(log, "Schreibe Aufnahme in eine temporaere WAV-Datei …")
        sf.write(tmp, np.asarray(audio, dtype=np.float32), int(sr), subtype='PCM_16')
        return separate_stems(tmp, model, log=log)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


# ======================================================================
# Song-Sheet: gesungener Text (lokale Whisper-KI) + Akkorde -> Chord-Sheet
# (Akkorde ueber den Woertern, wie bei Ultimate Guitar). OFFLINE.
# Bauteile: Demucs trennt den Gesang heraus (bessere Transkription),
# der Rest (Begleitung) geht in die vorhandene Akkord-Erkennung.
# ======================================================================
_whisper_model = None
_whisper_kind = None      # "faster" | "openai"
# WhisperX wird nur fuers Forced Alignment genutzt (Transkription macht
# faster-whisper). Cache: Sprache -> (align_model, metadata).
_whisperx_align = {}


def whisper_available():
    """Schnelltest, ob eine lokale Whisper-Variante installiert ist."""
    import importlib.util as _u
    return (_u.find_spec("faster_whisper") is not None
            or _u.find_spec("whisper") is not None)


def _get_whisper(size="small", log=None):
    """Laedt (und cached) ein Whisper-Modell. Bevorzugt faster-whisper
    (schnell auf CPU, int8), faellt auf openai-whisper zurueck."""
    global _whisper_model, _whisper_kind
    import importlib.util as _u
    if _whisper_model is not None and getattr(_whisper_model, "_a2m_size", None) == size:
        return _whisper_model, _whisper_kind
    # Windows: HuggingFace warnt sonst lautstark ueber fehlende Symlink-Rechte
    # (Cache funktioniert trotzdem) -- diese Warnung abschalten.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    if _u.find_spec("faster_whisper") is not None:
        _emit(log, f"Lade Whisper-Modell '{size}' (faster-whisper) … "
                   "beim ersten Mal wird es heruntergeladen.")
        from faster_whisper import WhisperModel
        m = WhisperModel(size, device="cpu", compute_type="int8")
        m._a2m_size = size
        _whisper_model, _whisper_kind = m, "faster"
    elif _u.find_spec("whisper") is not None:
        _emit(log, f"Lade Whisper-Modell '{size}' (openai-whisper) …")
        import whisper
        m = whisper.load_model(size)
        m._a2m_size = size
        _whisper_model, _whisper_kind = m, "openai"
    else:
        raise RuntimeError("Keine Whisper-Installation gefunden "
                           "(pip install faster-whisper).")
    _emit(log, "Whisper-Modell bereit.")
    return _whisper_model, _whisper_kind


def transcribe_segments(audio, sr, size="medium", language=None, log=None,
                        lang_out=None):
    """Transkribiert (gesungenen) Text mit Wort-Zeitstempeln. audio: np.ndarray
    (mono oder (frames, ch)) -- wird zu Mono/16 kHz gewandelt. Rueckgabe: Liste
    von Zeilen [{'text', 'words': [{'word','start','end'}, ...]}, ...]; die
    Zeilen entsprechen den Whisper-Segmenten (natuerliche Gesangs-Phrasen).
    lang_out: optionale Liste -- die erkannte Sprache wird angehaengt (fuer
    Aufrufer, die den Sprachcode brauchen, z. B. das Forced Alignment).

    language: ISO-Code ('de', 'en', ...) erzwingt die Sprache -- bei Gesang
    DRINGEND empfohlen, weil die automatische Spracherkennung an Musik gerne
    danebenliegt (ein deutsches Lied wird sonst als Englisch 'uebersetzt').
    None/''/'auto' = automatisch erkennen.

    Fuer Lieder getunt: VAD ueberspringt Instrumental-/Stille-Passagen (weniger
    erfundener Text), und condition_on_previous_text=False verhindert, dass sich
    der Refrain in Endlosschleifen wiederholt."""
    if language in ("", "auto", None):
        language = None
    m, kind = _get_whisper(size, log=log)
    y = np.asarray(audio, dtype=np.float32)
    if y.ndim == 2:
        y = y.mean(axis=1)
    if int(sr) != 16000:
        y = librosa.resample(y, orig_sr=int(sr), target_sr=16000)
    y = np.ascontiguousarray(y, dtype=np.float32)
    _emit(log, "Transkribiere Gesang … (kann je nach Laenge dauern).")
    lines = []
    if kind == "faster":
        segments, info = m.transcribe(
            y, word_timestamps=True, language=language,
            vad_filter=True, condition_on_previous_text=False)
        lang = getattr(info, "language", None)
        if lang:
            if lang_out is not None:
                lang_out.append(lang)
            _emit(log, f"Sprache: {lang}"
                       + ("" if language else " (automatisch erkannt)"))
        for seg in segments:                # Generator -> hier laeuft die KI
            words = [{"word": (w.word or "").strip(),
                      "start": float(w.start), "end": float(w.end)}
                     for w in (seg.words or []) if (w.word or "").strip()]
            if words:
                lines.append({"text": seg.text.strip(), "words": words})
    else:
        res = m.transcribe(y, word_timestamps=True, language=language,
                           condition_on_previous_text=False)
        if res.get("language"):
            if lang_out is not None:
                lang_out.append(res["language"])
            _emit(log, f"Sprache: {res['language']}"
                       + ("" if language else " (automatisch erkannt)"))
        for seg in res.get("segments", []):
            words = [{"word": (w.get("word") or "").strip(),
                      "start": float(w.get("start", 0.0)),
                      "end": float(w.get("end", 0.0))}
                     for w in seg.get("words", []) if (w.get("word") or "").strip()]
            if words:
                lines.append({"text": (seg.get("text") or "").strip(),
                              "words": words})
    nwords = sum(len(l["words"]) for l in lines)
    _emit(log, f"{nwords} Woerter in {len(lines)} Zeilen erkannt.")
    return lines


def whisperx_available():
    """WhisperX (Forced Alignment per wav2vec2) installiert? -> praezisere
    Wort-Zeitstempel als die rohen Whisper-Zeiten."""
    import importlib.util as _u
    return _u.find_spec("whisperx") is not None


def _fill_word_times(words, seg_start, seg_end):
    """Loecher in den Wort-Zeiten schliessen (WhisperX richtet einzelne Woerter
    wie Zahlen/Satzzeichen nicht immer aus) -- damit kein Wort verloren geht."""
    n = len(words)
    for i, w in enumerate(words):
        if w.get("start") is None:
            w["start"] = words[i - 1]["end"] if i > 0 and words[i - 1].get("end") \
                is not None else seg_start
        if w.get("end") is None:
            nxt = None
            for j in range(i + 1, n):
                if words[j].get("start") is not None:
                    nxt = words[j]["start"]
                    break
            w["end"] = nxt if nxt is not None else seg_end
        if w["end"] < w["start"]:
            w["end"] = w["start"]
    return words


def transcribe_aligned(audio, sr, size="medium", language=None, log=None):
    """Transkription MIT Forced Alignment -- kombiniert die Staerken beider Tools:
      * die SEGMENTIERUNG (Phrasen-Zeilen) kommt von faster-whisper, das sauber an
        gesungenen Phrasen schneidet (gut lesbares Sheet),
      * das praezise WORT-TIMING vom WhisperX/wav2vec2-Alignment, das die Woerter
        jeder Phrase exakt an der Audiozeit ausrichtet (Akkorde ueber den Silben).
    (WhisperX' eigenes transcribe wuerde grobe VAD-Bloecke liefern -> unlesbar.)
    Rueckgabe wie transcribe_segments(). Faellt bei Problemen NICHT still aus --
    der Aufrufer soll dann auf transcribe_segments() zurueckgreifen."""
    global _whisperx_align
    import whisperx
    import torch
    # 1) Transkription + natuerliche Phrasen-Segmente von faster-whisper
    lang_box = []
    lines = transcribe_segments(audio, sr, size=size, language=language,
                                log=log, lang_out=lang_box)
    if not lines:
        return lines
    lang = (language if language not in ("", "auto", None)
            else (lang_box[0] if lang_box else None) or "en")
    # 2) Audio fuers Alignment auf Mono/16 kHz bringen
    y = np.asarray(audio, dtype=np.float32)
    if y.ndim == 2:
        y = y.mean(axis=1)
    if int(sr) != 16000:
        y = librosa.resample(y, orig_sr=int(sr), target_sr=16000)
    y = np.ascontiguousarray(y, dtype=np.float32)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    # 3) Ausrichtmodell (je Sprache, gecacht)
    _emit(log, f"Richte Woerter aus (Forced Alignment, '{lang}') …")
    if lang not in _whisperx_align:
        _emit(log, f"Lade Ausrichtmodell fuer '{lang}' (einmalig, ~300-400 MB) – "
                   "das kann je nach Verbindung einige Minuten dauern. "
                   "Es haengt NICHT, der Download laeuft im Hintergrund.")
        _whisperx_align[lang] = whisperx.load_align_model(language_code=lang,
                                                          device=dev)
        _emit(log, "Ausrichtmodell geladen (ab jetzt aus dem Cache).")
    align_model, metadata = _whisperx_align[lang]
    # 4) faster-whisper-Segmente -> Align-Eingabe (Phrasen bleiben Zeilen)
    segs = [{"start": ln["words"][0]["start"], "end": ln["words"][-1]["end"],
             "text": ln["text"]} for ln in lines if ln["words"]]
    aligned = whisperx.align(segs, align_model, metadata, y, dev,
                             return_char_alignments=False)
    # 5) Zeilen mit den praezisen Wortzeiten neu aufbauen (Segment = Zeile)
    out = []
    for seg in aligned.get("segments", []):
        raw = [w for w in seg.get("words", []) if (w.get("word") or "").strip()]
        raw = _fill_word_times(raw, float(seg.get("start", 0.0)),
                               float(seg.get("end", 0.0)))
        words = [{"word": w["word"].strip(), "start": float(w["start"]),
                  "end": float(w["end"])} for w in raw]
        if words:
            out.append({"text": (seg.get("text") or "").strip(), "words": words})
    if not out:
        _emit(log, "Alignment ohne Woerter – nutze faster-whisper-Zeiten.")
        return lines
    nwords = sum(len(l["words"]) for l in out)
    _emit(log, f"{nwords} Woerter in {len(out)} Phrasen-Zeilen ausgerichtet.")
    return out


def snap_words_to_onsets(lines, vocals, sr, tol=0.25, log=None):
    """Korrigiert die Whisper-Wortzeiten, indem jeder Wortanfang auf den naechsten
    ECHTEN Einsatz (Onset) im sauberen Gesang-Stem gezogen wird (innerhalb +-tol s).
    Whisper markiert gesungene Wortanfaenge oft etwas daneben; die Onsets der
    isolierten Stimme liegen naeher am tatsaechlichen Silbeneinsatz -> die Akkorde
    sitzen danach genauer ueber den Silben. Reihenfolge bleibt monoton.
    Veraendert 'lines' in place und gibt es zurueck."""
    try:
        v = np.asarray(vocals, dtype=np.float32)
        if v.ndim == 2:
            v = v.mean(axis=1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            onsets = librosa.onset.onset_detect(y=v, sr=int(sr), units="time",
                                                backtrack=True)
    except Exception as e:
        _emit(log, f"Onset-Erkennung uebersprungen ({e}).")
        return lines
    onsets = np.asarray(onsets, dtype=float)
    if onsets.size == 0:
        return lines
    moved, last = 0, -1.0
    for ln in lines:
        for w in ln["words"]:
            st = w["start"]
            i = int(np.searchsorted(onsets, st))
            cand = []
            if i < len(onsets):
                cand.append(onsets[i])
            if i > 0:
                cand.append(onsets[i - 1])
            if not cand:
                last = max(last, st)
                continue
            near = min(cand, key=lambda o: abs(o - st))
            if abs(near - st) <= tol and near >= last:
                delta = near - st
                w["start"] = float(near)
                w["end"] = float(w["end"] + delta)
                if abs(delta) > 1e-3:
                    moved += 1
                last = w["start"]
            else:
                last = max(last, st)
    _emit(log, f"Wortzeiten an Gesang-Onsets justiert ({moved} verschoben, "
               f"{len(onsets)} Onsets).")
    return lines


# Akkorde fuers Sheet um diesen Betrag (Sekunden) nach vorne ziehen -- gleicht den
# leichten, systematischen Versatz aus, mit dem Whisper gesungene Wortanfaenge oft
# etwas zu spaet markiert. Der Rest schwankt je Lauf/Song -> im Sheet-Fenster live
# nachregelbar ("Akkorde frueher/spaeter").
CHORD_LEAD = 0.15

# Fuer ein lesbares Chord-Sheet nur die gaengigen Akkordtypen zulassen --
# dim/maj7/sus4 flackern auf der gesangslosen Begleitung pro Beat zu stark.
_SHEET_SUFFIXES = ("", "m", "7", "m7")


def _suffix_mask(allowed):
    """Bool-Maske ueber CHORD_NAMES (gleiche Reihenfolge wie _build_chord_templates):
    True, wenn der Akkordtyp in 'allowed' liegt."""
    mask = np.zeros(len(CHORD_NAMES), dtype=bool)
    k = 0
    for _i in range(12):
        for suffix, _ivs in CHORD_TYPES:
            mask[k] = suffix in allowed
            k += 1
    return mask


_SHEET_MASK = _suffix_mask(_SHEET_SUFFIXES)
# Noch strenger: nur Dur-/Moll-DREIKLAENGE. Auf der gesangslosen Begleitung sind
# Septakkorde (Em7/Am7 ...) kaum sauseinanderzuhalten und verwaschen das Sheet --
# fuer ein Ultimate-Guitar-Sheet sind saubere Dreiklaenge meist das Richtige.
_TRIAD_MASK = _suffix_mask(("", "m"))


def _merge_equal_chords(seq):
    """Benachbarte gleiche Akkorde zu einem Abschnitt verschmelzen."""
    out = []
    for s in seq:
        if out and out[-1]["chord"] == s["chord"]:
            out[-1]["end"] = s["end"]
        else:
            out.append(dict(s))
    return out


def _merge_short_chords(seq, min_dur):
    """Zu kurze Abschnitte (< min_dur s) im Nachbarn aufgehen lassen -- entfernt
    einzelne Ausreisser, damit das Sheet ruhig bleibt."""
    changed = True
    while changed and len(seq) > 1:
        changed = False
        for i, s in enumerate(seq):
            if s["end"] - s["start"] < min_dur:
                if i > 0:
                    seq[i - 1]["end"] = s["end"]
                else:
                    seq[i + 1]["start"] = s["start"]
                seq.pop(i)
                changed = True
                break
        seq = _merge_equal_chords(seq)
    return seq


def chord_sequence(y, sr, key=None, bass_audio=None, beat_times=None,
                   win_beats=2, min_dur=1.2, triads_only=True, simple=True,
                   key_bias=0.12, bass_weight=None, log=None):
    """Offline-Akkordfolge eines ganzen Stuecks (ueber die vorhandene Erkennung
    chroma_pcp + chord_scores). Fuer ein lesbares Sheet stabilisiert:
      * je 'win_beats' Beats EIN Akkord (Chroma ueber das Fenster gemittelt),
      * 'triads_only': nur Dur-/Moll-Dreiklaenge (sonst 'simple': +7/m7),
      * 'min_dur': zu kurze Abschnitte gehen im Nachbarn auf.
    'bass_audio' (z. B. der getrennte Bass-Stem) liefert einen SAUBEREN Grundton
    -- das entscheidet zwischen tonverwandten Akkorden (C vs. Am, G vs. Em) und
    behebt die haeufigste Fehlerkennung. 'bass_weight' steuert dessen Gewicht.
    Mit bekannter Tonart 'key' werden leitereigene Akkorde leicht bevorzugt.
    Rueckgabe: Liste {'start','end','chord'} (Sekunden)."""
    y = np.asarray(y, dtype=np.float32)
    if y.ndim == 2:
        y = y.mean(axis=1)
    dur = len(y) / float(sr)
    if beat_times is None:
        _emit(log, "Suche Beats / Taktraster …")
        try:
            _tempo, frames = librosa.beat.beat_track(y=y, sr=sr)
            beat_times = librosa.frames_to_time(frames, sr=sr)
        except Exception:
            beat_times = None
    bt = np.asarray(beat_times, dtype=float) if beat_times is not None else None
    if bt is None or bt.size < 2:
        bt = np.arange(0.0, dur, 0.5)       # Ausweich: feste 0,5-s-Fenster
    # Beats zu Fenstern von win_beats gruppieren (weniger, ruhigere Akkorde)
    edges = list(bt[::max(1, int(win_beats))])
    if edges[-1] < bt[-1]:
        edges.append(float(bt[-1]))
    _emit(log, "Trenne harmonischen Anteil (einmalig) …")
    try:
        yh = librosa.effects.harmonic(y, margin=4.0)
    except Exception:
        yh = y
    # Sauberes Bass-Chromagramm aus dem Bass-Stem (falls vorhanden)
    bass_cg = None
    if bass_audio is not None:
        ba = np.asarray(bass_audio, dtype=np.float32)
        if ba.ndim == 2:
            ba = ba.mean(axis=1)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                bass_cg = librosa.feature.chroma_cqt(
                    y=ba, sr=sr, fmin=librosa.note_to_hz('C1'),
                    n_octaves=4, hop_length=CHROMA_HOP)
        except Exception:
            bass_cg = None
    if bass_weight is None:
        bass_weight = 0.9 if bass_cg is not None else None
    diatonic = _diatonic_mask(key) if key else None
    allowed = _TRIAD_MASK if triads_only else (_SHEET_MASK if simple else None)
    _emit(log, f"Bestimme Akkorde ({max(0, len(edges) - 1)} Fenster) …")
    raw, prev = [], None
    minlen = int(0.05 * sr)
    # Kurze Akkordfenster lassen die CQT "n_fft zu gross"-Warnungen fluten
    # (harmlos, nur Zero-Padding) -- hier lokal unterdruecken.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for i in range(len(edges) - 1):
            a, b = int(edges[i] * sr), int(edges[i + 1] * sr)
            if b - a < minlen:
                continue
            res = chroma_pcp(yh[a:b], sr, y_harm=yh[a:b])
            ch = prev or "—"
            if res:
                pcp, bass = res[0], res[1]
                if bass_cg is not None:                # sauberer Stem-Bass
                    f0 = int(edges[i] * sr / CHROMA_HOP)
                    f1 = max(f0 + 1, int(edges[i + 1] * sr / CHROMA_HOP))
                    bv = bass_cg[:, f0:min(f1, bass_cg.shape[1])].mean(axis=1)
                    bs = bv.sum()
                    bass = bv / bs if bs > 0 else bass
                scores = chord_scores(pcp, bass, bass_weight=bass_weight)
                if scores is not None:
                    if prev is not None:
                        k = _CHORD_IDX.get(prev)
                        if k is not None:
                            scores[k] += CHORD_STICKY
                    if diatonic is not None:
                        scores = scores + key_bias * diatonic
                    if allowed is not None:
                        scores = np.where(allowed, scores, -1e9)
                    ch = CHORD_NAMES[int(np.argmax(scores))]
            raw.append({"start": float(edges[i]), "end": float(edges[i + 1]),
                        "chord": ch})
            prev = ch
    seq = _merge_equal_chords(raw)
    seq = _merge_short_chords(seq, min_dur)
    _emit(log, f"{len(seq)} Akkord-Abschnitte.")
    return seq


def _chord_at(chords, t):
    """Welcher Akkord klingt zur Zeit t? (lineare Suche, Listen sind kurz)."""
    last = None
    for c in chords:
        if c["start"] <= t:
            last = c["chord"]
        else:
            break
    return last


def _chords_in_range(chords, t0, t1):
    """Akkorde im Zeitfenster [t0, t1) als Folge von Labels (aufeinanderfolgende
    Wiederholungen zusammengefasst) -- fuer Instrumental-/Intro-Zeilen."""
    out = []
    for c in chords:
        if c["end"] > t0 and c["start"] < t1:
            if not out or out[-1] != c["chord"]:
                out.append(c["chord"])
    return out


def _wrap_words(words, width):
    """Woerter (mit Zeitstempeln) auf Teilzeilen <= width Zeichen umbrechen
    (an Wortgrenzen). Rueckgabe: Liste von Wortlisten."""
    subs, cur, ln = [], [], 0
    for w in words:
        wlen = len(w["word"]) + 1
        if cur and ln + wlen > width:
            subs.append(cur)
            cur, ln = [], 0
        cur.append(w)
        ln += wlen
    if cur:
        subs.append(cur)
    return subs


def build_chord_sheet(lines, chords, title="", key="", bpm=0.0, width=84,
                      gap_instr=4.0, chord_lead=CHORD_LEAD, with_map=False):
    """Baut aus Text-Zeilen (mit Wort-Zeitstempeln) und der Akkordfolge ein
    Chord-Sheet im Ultimate-Guitar-Stil. Rueckgabe (text, chordpro) -- bzw.
    (text, chordpro, linemap), wenn with_map=True.
      * text: Akkordzeile ueber der Textzeile (Monospace),
      * chordpro: ChordPro ([C]Wort), transponier-/druckbar,
      * linemap: Liste {chord_row, lyric_row, start, end} (Tk-Zeilen, 1-basiert)
        zum Mitlauf-Markieren der aktuellen Stelle beim Abspielen.
    Eigenschaften: lange Gesangszeilen werden umbrochen (width); zu Beginn jeder
    Zeile steht der gerade klingende Akkord, danach nur die Wechsel; Intro-,
    Zwischen- und Schluss-Instrumentalteile bekommen eine eigene Akkordzeile
    (nur, wenn die Pause >= gap_instr Sekunden ist).

    chord_lead (Sekunden) zieht ALLE Akkorde um diesen Betrag nach vorne. Whisper
    setzt gesungene Wortanfaenge tendenziell etwas zu spaet, wodurch die Akkorde
    optisch hinter der richtigen Silbe landen -- ein kleiner Vorlauf rueckt sie auf
    die passende Silbe. (Die Wort-/Mitlauf-Zeiten bleiben davon unberuehrt.)"""
    if chord_lead:
        chords = [{"start": c["start"] - chord_lead,
                   "end": c["end"] - chord_lead, "chord": c["chord"]}
                  for c in chords]
    valid = [c for c in chords if c["chord"] and c["chord"] != "—"]
    head = []
    if title:
        head.append(title)
    meta = []
    if key:
        meta.append(f"Tonart: {key}")
    if bpm and bpm > 0:
        meta.append(f"Tempo: {bpm:.0f} BPM")
    if meta:
        head.append("  ·  ".join(meta))
    if head:
        head.append("")

    text_lines, cp_lines, lmap = [], [], []

    def _emit_instr(t0, t1):
        seq = _chords_in_range(valid, t0, t1)
        if seq:
            row = len(text_lines)
            text_lines.append(" ".join(seq))
            text_lines.append("")
            cp_lines.append(" ".join(f"[{c}]" for c in seq))
            lmap.append({"chord_row": row, "lyric_row": None,
                         "start": float(t0), "end": float(t1), "words": []})

    first_t = next((ln["words"][0]["start"] for ln in lines if ln["words"]), None)
    if first_t is not None and first_t > gap_instr:
        _emit_instr(0.0, first_t)            # Intro

    prev_end = None
    for ln in lines:
        words = ln["words"]
        if not words:
            continue
        if prev_end is not None and words[0]["start"] - prev_end >= gap_instr:
            _emit_instr(prev_end, words[0]["start"])   # Instrumental-Zwischenteil
        cp = ""
        cp_running = None
        for sub in _wrap_words(words, width):
            lyric, chordline = "", ""
            line_running = None              # Akkord am Anfang JEDER Zeile zeigen
            wspans = []
            for w in sub:
                active = _chord_at(valid, w["start"])
                if active and active != line_running:
                    col = len(lyric)
                    if chordline and col <= len(chordline):
                        pad = len(chordline) - col + 1
                        lyric += " " * pad
                        col = len(lyric)
                    chordline = chordline.ljust(col) + active
                    line_running = active
                if active and active != cp_running:
                    cp += f"[{active}]"
                    cp_running = active
                c0 = len(lyric)              # Spalten des Wortes (fuer Wort-Marker)
                lyric += w["word"] + " "
                wspans.append({"c0": c0, "c1": c0 + len(w["word"]),
                               "start": float(w["start"]), "end": float(w["end"])})
                cp += w["word"] + " "
            crow = len(text_lines)
            text_lines.append(chordline.rstrip())
            text_lines.append(lyric.rstrip())
            text_lines.append("")
            lmap.append({"chord_row": crow, "lyric_row": crow + 1,
                         "start": float(sub[0]["start"]),
                         "end": float(sub[-1]["end"]), "words": wspans})
        cp_lines.append(cp.rstrip())
        prev_end = words[-1]["end"]

    if valid and prev_end is not None:       # Outro
        last_t = max(c["end"] for c in valid)
        if last_t - prev_end >= gap_instr:
            _emit_instr(prev_end, last_t)

    text = "\n".join(head + text_lines).rstrip() + "\n"
    cp_head = []
    if title:
        cp_head.append(f"{{title: {title}}}")
    if key:
        cp_head.append(f"{{key: {key}}}")
    if bpm and bpm > 0:
        cp_head.append(f"{{tempo: {bpm:.0f}}}")
    if cp_head:
        cp_head.append("")
    chordpro = "\n".join(cp_head + cp_lines).rstrip() + "\n"
    if not with_map:
        return text, chordpro
    # Zeilen-Indizes auf die Tk-Zeilen des Gesamttexts umrechnen (1-basiert) und
    # die Abdeckung luekenlos machen (jede Stelle gehoert genau einer Zeile)
    off = len(head)
    out = []
    for e in lmap:
        out.append({
            "chord_row": e["chord_row"] + off + 1,
            "lyric_row": (e["lyric_row"] + off + 1) if e["lyric_row"] is not None else None,
            "start": e["start"], "end": e["end"], "words": e.get("words", [])})
    out.sort(key=lambda e: e["start"])
    for i in range(len(out) - 1):
        out[i]["end"] = max(out[i]["end"], out[i + 1]["start"])
    return text, chordpro, out


def accompaniment_from_stems(stems):
    """Begleitung = Summe aller Nicht-Gesang-Stems (fuer die Akkorde)."""
    acc = None
    for name, a in stems.items():
        if name == "vocals":
            continue
        a = np.asarray(a, dtype=np.float32)
        if acc is None:
            acc = a.copy()
        else:
            n = min(len(acc), len(a))
            acc = acc[:n] + a[:n]
    return acc


def song_sheet_from_stems(stems, sr, title="", whisper_size="medium",
                          language=None, snap=False, log=None):
    """Chord-Sheet aus BEREITS getrennten Stems bauen (Schritte 2-4). So muss
    die teure Stem-Trennung nur einmal laufen, wenn neben dem Sheet z. B. auch
    der Stem-Export gewuenscht ist. Rueckgabe wie song_sheet().
    snap=True zieht die Whisper-Wortzeiten auf die naechsten Gesang-Onsets. Tests
    zeigten KEINE verlaessliche Verbesserung (Gesang hat sehr dichte Onsets ~3/s,
    'naechster Onset' ist eher Rauschen) -> standardmaessig AUS. Wortgenaue
    Alignment braucht eher Forced Alignment (z. B. WhisperX/wav2vec2)."""
    vocals = stems.get("vocals")
    if vocals is None:
        raise RuntimeError("Kein Gesang-Stem erhalten (Modell ohne 'vocals'?).")
    acc = accompaniment_from_stems(stems)
    if acc is None:
        acc = np.asarray(vocals, dtype=np.float32)   # Notnagel

    _emit(log, "== Gesangstext transkribieren ==")
    lines = None
    if whisperx_available():
        # Forced Alignment -> praezise Wortzeiten (Akkorde genauer ueber Silben)
        try:
            lines = transcribe_aligned(vocals, sr, size=whisper_size,
                                       language=language, log=log)
        except Exception as e:
            _emit(log, f"WhisperX fehlgeschlagen ({e}) – nutze faster-whisper.")
            lines = None
    if lines is None:
        lines = transcribe_segments(vocals, sr, size=whisper_size,
                                    language=language, log=log)
        if snap:
            snap_words_to_onsets(lines, vocals, sr, log=log)

    _emit(log, "== Tonart + Akkorde bestimmen ==")
    acc_mono = acc.mean(axis=1) if acc.ndim == 2 else acc
    bass_stem = stems.get("bass")
    bass_mono = None
    if bass_stem is not None:
        b = np.asarray(bass_stem, dtype=np.float32)
        bass_mono = b.mean(axis=1) if b.ndim == 2 else b
    try:
        key = estimate_key(acc_mono, sr)
    except Exception:
        key = ""
    try:
        bpm = float(estimate_tempo(acc_mono, sr) or 0.0)
    except Exception:
        bpm = 0.0
    chords = chord_sequence(acc_mono, sr, key=key, bass_audio=bass_mono, log=log)

    _emit(log, "== Chord-Sheet zusammensetzen ==")
    text, chordpro = build_chord_sheet(lines, chords, title=title,
                                       key=key, bpm=bpm)
    _emit(log, "Fertig – Chord-Sheet steht.")
    return {"text": text, "chordpro": chordpro, "key": key, "bpm": bpm,
            "lines": lines, "chords": chords, "title": title}


def song_sheet(path, model="htdemucs", whisper_size="medium", language=None,
               log=None):
    """Komplettpipeline: Datei -> Stems (Gesang isolieren) -> Text (Whisper) +
    Akkorde (Begleitung) -> Chord-Sheet. Rueckgabe-dict mit 'text', 'chordpro',
    'key', 'bpm', 'lines', 'chords'. OFFLINE, kann einige Minuten dauern."""
    _emit(log, "== Gesang per KI heraustrennen ==")
    stems, sr = separate_stems(path, model, log=log)
    title = os.path.splitext(os.path.basename(path))[0]
    return song_sheet_from_stems(stems, sr, title=title,
                                 whisper_size=whisper_size,
                                 language=language, log=log)


class StemPlayer:
    """Spielt mehrere Stem-Spuren gemischt ab -- Pegel je Spur live regelbar,
    eigener sounddevice-OutputStream (unabhaengig vom DJ-Modus). Fuer den
    Aufnahme->Stems-Ablauf."""

    def __init__(self, stems, sr, names=None, device=None, blocksize=1024):
        self.sr = int(sr)
        self.device = device
        self.blocksize = int(blocksize)
        prepared = []
        for s in stems:
            a = np.asarray(s, dtype=np.float32)
            if a.ndim == 1:
                a = a.reshape(-1, 1)
            prepared.append(a)
        self.channels = max(1, min(2, max((a.shape[1] for a in prepared), default=1)))
        adj = []
        for a in prepared:
            if a.shape[1] == 1 and self.channels == 2:
                a = np.repeat(a, 2, axis=1)
            elif a.shape[1] > self.channels:
                a = a[:, :self.channels]
            adj.append(np.ascontiguousarray(a, dtype=np.float32))
        self.stems = adj
        self.names = list(names) if names else [f"Stem {i+1}" for i in range(len(adj))]
        self.total = min((a.shape[0] for a in self.stems), default=0)
        self.gain = [1.0] * len(self.stems)
        self.pos = 0
        self.playing = False
        self.stream = None
        self.lock = threading.Lock()

    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            out = np.zeros((frames, self.channels), dtype=np.float32)
            if self.playing and self.pos < self.total:
                s = self.pos
                e = min(s + frames, self.total)
                n = e - s
                for k, a in enumerate(self.stems):
                    out[:n] += a[s:e] * self.gain[k]
                self.pos = e
                if self.pos >= self.total:
                    self.playing = False
            outdata[:] = out

    def start_stream(self):
        if sd is None:
            raise RuntimeError("sounddevice nicht verfuegbar")
        self.stream = sd.OutputStream(
            samplerate=self.sr, channels=self.channels, blocksize=self.blocksize,
            device=self.device, dtype='float32', callback=self._callback)
        self.stream.start()

    def toggle(self):
        with self.lock:
            if self.playing:
                self.playing = False
            else:
                if self.pos >= self.total:
                    self.pos = 0
                self.playing = True
            return self.playing

    def play(self):
        with self.lock:
            if self.pos >= self.total:
                self.pos = 0
            self.playing = True

    def pause(self):
        with self.lock:
            self.playing = False

    def seek(self, sec):
        with self.lock:
            self.pos = max(0, min(self.total, int(float(sec) * self.sr)))

    def set_gain(self, k, g):
        with self.lock:
            if 0 <= k < len(self.gain):
                self.gain[k] = float(max(0.0, g))

    def position(self):
        with self.lock:
            return self.pos / float(self.sr), self.total / float(self.sr)

    def is_playing(self):
        with self.lock:
            return self.playing

    def stop(self):
        with self.lock:
            self.playing = False
        st, self.stream = self.stream, None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass


def basic_pitch_available():
    """basic-pitch (Audio->MIDI) installiert? Wird fuers Bass->MIDI gebraucht.
    Auf Win/Py3.12 ohne TensorFlow nutzbar (ONNX): siehe README/requirements."""
    import importlib.util as _u
    return _u.find_spec("basic_pitch") is not None


def bass_to_midi_notes(bass_audio, sr, max_freq=600.0, min_freq=30.0,
                       min_note_ms=130.0, onset_thresh=0.5, log=None):
    """Wandelt den (sauberen) Bass-Stem per basic-pitch in eine Notenliste.
    Auf Bass getunt: tiefe Frequenzgrenzen + laengere Mindestnote (weniger
    Geister-/Kurznoten). Rueckgabe: Liste (start_s, end_s, pitch, velocity).
    basic-pitch erwartet eine Datei -> der Stem wird mono in eine temporaere
    WAV geschrieben. Nutzt automatisch das ONNX-Modell, wenn TensorFlow fehlt."""
    import tempfile
    from basic_pitch.inference import predict
    from basic_pitch import ICASSP_2022_MODEL_PATH
    y = np.asarray(bass_audio, dtype=np.float32)
    if y.ndim == 2:
        y = y.mean(axis=1)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    notes = []
    try:
        sf.write(tmp.name, y, int(sr), subtype="PCM_16")
        _emit(log, "Bass -> MIDI (basic-pitch) … das kann einen Moment dauern.")
        _model_out, _midi, events = predict(
            tmp.name, ICASSP_2022_MODEL_PATH,
            onset_threshold=onset_thresh, minimum_note_length=min_note_ms,
            minimum_frequency=min_freq, maximum_frequency=max_freq,
            melodia_trick=True)
        for ev in events:
            start, end, pitch = float(ev[0]), float(ev[1]), int(ev[2])
            amp = float(ev[3]) if len(ev) > 3 else 0.7
            vel = int(min(127, max(1, round(amp * 127))))
            notes.append((start, end, pitch, vel))
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
    notes.sort(key=lambda n: n[0])
    _emit(log, f"{len(notes)} Bass-Noten erkannt.")
    return notes


class MidiNotePlayer:
    """Sendet eine Notenliste (start, end, pitch, velocity) zeitsynchron ueber
    einen mido-Ausgang. Die aktuelle Abspielzeit liefert position_fn() (z. B. der
    StemPlayer) -- so laufen die Bass-MIDI-Noten exakt zur Stem-Wiedergabe mit;
    pausiert die Wiedergabe (is_playing_fn -> False), schweigt auch das MIDI, und
    ein Sprung in der Position (Seek) wird sauber neu synchronisiert. Start/Stop
    ueber start()/stop(); stop() schickt fuer alle klingenden Noten ein note_off."""

    def __init__(self, notes, midi_out, position_fn, is_playing_fn=None,
                 channel=NOTE_CHANNEL):
        self.notes = sorted(notes, key=lambda n: n[0])
        self.out = midi_out
        self.position_fn = position_fn
        self.is_playing_fn = is_playing_fn
        self.channel = int(channel)
        self._stop = threading.Event()
        self._thread = None
        self._active = {}                      # pitch -> end_s der klingenden Note

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _send(self, kind, pitch, vel=0):
        try:
            self.out.send(mido.Message(kind, channel=self.channel,
                                       note=int(pitch), velocity=int(vel)))
        except Exception:
            pass

    def _all_off(self):
        for pitch in list(self._active):
            self._send('note_off', pitch, 0)
        self._active.clear()

    def _run(self):
        # Zustandsbasiert: jede Runde bestimmen, WELCHE Noten zur Zeit t klingen
        # sollen (start <= t < end), und die Differenz zum aktuellen Klang als
        # note_on/note_off senden. Das behandelt normales Abspielen, Pause und
        # Seek (vor/zurueck) einheitlich -- ohne fragile Sprung-Heuristik.
        while not self._stop.is_set():
            t = float(self.position_fn())
            playing = self.is_playing_fn() if self.is_playing_fn else True
            if not playing:
                if self._active:
                    self._all_off()
                time.sleep(0.02)
                continue
            desired = {}                       # pitch -> (end, velocity)
            for s, e, p, v in self.notes:
                if s > t:
                    break                      # Liste ist nach Startzeit sortiert
                if e > t:
                    desired[p] = (e, v)
            for pitch in list(self._active):   # nicht mehr gewollte -> aus
                if pitch not in desired:
                    self._send('note_off', pitch, 0)
                    del self._active[pitch]
            for pitch, (end, vel) in desired.items():   # neue -> an
                if pitch not in self._active:
                    self._send('note_on', pitch, vel)
                    self._active[pitch] = end
            time.sleep(0.01)
        self._all_off()

    def stop(self):
        self._stop.set()
        th = self._thread
        if th is not None:
            th.join(timeout=0.5)
        self._all_off()

    def is_active(self):
        return self._thread is not None and self._thread.is_alive()


# ===========================================================================
# DJ-Modus: zwei Decks, Equal-Power-Crossfade, Clock folgt dem Ziel-Deck
# ===========================================================================
# Mirror der WebApp: zwei Audiodateien werden nebeneinander geladen/analysiert
# (auch waehrend eine laeuft) und in EINEM Ausgabe-Stream gemischt. Ein Klick
# blendet zum jeweiligen Deck (Equal-Power: A=cos, B=sin), und die MIDI-Clock
# folgt automatisch dem dominierenden Deck (driftfrei aus dessen Position).
DJ_SR       = 44100      # gemeinsame Ausgabe-/Mischrate (Decks werden umgesampelt)
DJ_FADE_TAU = 0.18       # Zeitkonstante (s) der Crossfade-Glaettung
DJ_GLIDE_S  = 8.0        # Dauer (s) des Tempo-Uebergangs (Master-Tempo -> Eigentempo)
# EQ-Isolator (3 Baender pro Deck). Kill = Band auf DJ_EQ_KILL_DB absenken.
DJ_EQ_LOW_HZ  = 250.0
DJ_EQ_MID_HZ  = 1000.0
DJ_EQ_HIGH_HZ = 3500.0
DJ_EQ_KILL_DB = -40.0


def _rbj_biquad(kind, f0, db, q, sr):
    """RBJ-Cookbook-Biquad (low shelf / peak / high shelf) als sos-Zeile."""
    A = 10.0 ** (db / 40.0)
    w0 = 2.0 * math.pi * f0 / sr
    cw, sw = math.cos(w0), math.sin(w0)
    al = sw / (2.0 * q)
    if kind == 'peak':
        b0, b1, b2 = 1 + al * A, -2 * cw, 1 - al * A
        a0, a1, a2 = 1 + al / A, -2 * cw, 1 - al / A
    elif kind == 'low':
        s = 2.0 * math.sqrt(A) * al
        b0 = A * ((A + 1) - (A - 1) * cw + s)
        b1 = 2 * A * ((A - 1) - (A + 1) * cw)
        b2 = A * ((A + 1) - (A - 1) * cw - s)
        a0 = (A + 1) + (A - 1) * cw + s
        a1 = -2 * ((A - 1) + (A + 1) * cw)
        a2 = (A + 1) + (A - 1) * cw - s
    else:  # high shelf
        s = 2.0 * math.sqrt(A) * al
        b0 = A * ((A + 1) + (A - 1) * cw + s)
        b1 = -2 * A * ((A - 1) + (A + 1) * cw)
        b2 = A * ((A + 1) + (A - 1) * cw - s)
        a0 = (A + 1) - (A - 1) * cw + s
        a1 = 2 * ((A - 1) - (A + 1) * cw)
        a2 = (A + 1) - (A - 1) * cw - s
    return [b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]


class _WSOLA:
    """Streaming-Zeitdehnung (WSOLA), tonhoehen-erhaltend und echtzeitfaehig.
    pull(n) liefert n Ausgabesamples und verbraucht dabei ~n*rate Quellsamples
    (rate>1 = schneller = hoeheres Tempo). Guenstig genug fuer den Audio-Callback.
    Die Rate kann jederzeit gesetzt werden (auch fuer einen Tempo-Glide)."""

    def __init__(self, audio, start_frame=0, frame=2048, hop=512, search=200):
        a = np.asarray(audio, dtype=np.float32)
        if a.ndim == 1:
            a = a.reshape(-1, 1)
        self.x = a
        self.N, self.ch = a.shape
        self.frame, self.Hs, self.search = frame, hop, search
        self.win = np.hanning(frame).astype(np.float32)
        self.xm = a.mean(axis=1).astype(np.float32)     # Mono fuer die Suche
        self.rate = 1.0
        self.acc = np.zeros((frame, self.ch), dtype=np.float32)
        self.wacc = np.zeros(frame, dtype=np.float32)
        self.out = np.zeros((0, self.ch), dtype=np.float32)
        self.finished = False
        start = max(0, min(int(start_frame), max(0, self.N - frame)))
        self.nominal = float(start)
        self._place(start)

    def set_rate(self, r):
        self.rate = float(max(0.25, min(4.0, r)))

    def _frame_at(self, a):
        seg = np.zeros((self.frame, self.ch), dtype=np.float32)
        a = max(0, a)
        e = min(self.N, a + self.frame)
        if e > a:
            seg[:e - a] = self.x[a:e]
        return seg

    def _place(self, a):
        self.acc += self._frame_at(a) * self.win[:, None]
        self.wacc += self.win
        self.ia = a

    def _hop(self):
        w = np.maximum(self.wacc[:self.Hs], 1e-6)
        chunk = (self.acc[:self.Hs] / w[:, None]).copy()
        self.acc = np.roll(self.acc, -self.Hs, axis=0); self.acc[-self.Hs:] = 0
        self.wacc = np.roll(self.wacc, -self.Hs); self.wacc[-self.Hs:] = 0
        self.nominal += self.Hs * self.rate
        base = int(round(self.nominal))
        nat_a = self.ia + self.Hs                        # natuerliche Fortsetzung
        d = 0
        if self.search > 0 and 0 <= nat_a <= self.N - self.frame:
            lo = max(0, base - self.search)
            hi = min(self.N - self.frame, base + self.search)
            if hi > lo:
                nat = self.xm[nat_a:nat_a + self.frame]
                region = self.xm[lo:hi + self.frame]
                corr = np.correlate(region, nat, 'valid')
                d = lo + int(np.argmax(corr)) - base
        a = max(0, min(max(0, self.N - self.frame), base + d))
        if a >= self.N - self.frame:
            self.finished = True
        self._place(a)
        return chunk

    def pull(self, n):
        while self.out.shape[0] < n and not self.finished:
            self.out = np.concatenate([self.out, self._hop()], axis=0)
        if self.out.shape[0] >= n:
            o = self.out[:n]
            self.out = self.out[n:]
            return o
        o = np.zeros((n, self.ch), dtype=np.float32)
        o[:self.out.shape[0]] = self.out
        self.out = self.out[:0]
        return o


def _glide_ticks(orig_ticks, src0_sec, r0, r1, glide_dur):
    """Ausgabe-Zeit-Tickraster fuer den Tempo-Glide: die Rate gleitet in der
    AUSGABEZEIT linear r0->r1 ueber glide_dur, danach konstant r1. Quelle->Ausgabe
    wird analytisch invertiert (Quadratik), damit die Clock automatisch mitgleitet."""
    if orig_ticks is None:
        return None
    t = np.asarray(orig_ticks, dtype=np.float64)
    t = t[t >= src0_sec]
    if len(t) == 0:
        return np.array([], dtype=np.float64)
    a = (r1 - r0) / (2.0 * glide_dur) if glide_dur > 0 else 0.0
    src_glide_end = src0_sec + glide_dur * (r0 + r1) / 2.0
    out = np.empty(len(t), dtype=np.float64)
    for i, ts in enumerate(t):
        if ts <= src_glide_end:
            if abs(a) < 1e-12:
                out[i] = (ts - src0_sec) / r0
            else:
                disc = r0 * r0 + 4.0 * a * (ts - src0_sec)
                out[i] = (-r0 + math.sqrt(max(0.0, disc))) / (2.0 * a)
        else:
            out[i] = glide_dur + (ts - src_glide_end) / r1
    return out


def _dj_eq_sos(low_db, mid_db, high_db, sr=DJ_SR):
    """sos-Kaskade fuer den 3-Band-EQ; None, wenn alle Baender neutral (0 dB)."""
    if abs(low_db) < 0.01 and abs(mid_db) < 0.01 and abs(high_db) < 0.01:
        return None
    return np.array([
        _rbj_biquad('low', DJ_EQ_LOW_HZ, low_db, 0.7, sr),
        _rbj_biquad('peak', DJ_EQ_MID_HZ, mid_db, 0.8, sr),
        _rbj_biquad('high', DJ_EQ_HIGH_HZ, high_db, 0.7, sr),
    ], dtype=np.float64)


class DJDeck:
    def __init__(self):
        self.audio = None          # (frames, ch) float32 @ DJ_SR
        self.frames_total = 0
        self.beats = None
        self.ticks = None
        self.info = None
        self.key = ""
        self.name = ""
        self.pos = 0               # aktueller Frame
        self.playing = False
        self.anchor_pos = 0
        self.anchor_perf = 0.0
        self.level = 0.0           # RMS des zuletzt ausgegebenen Blocks (Anzeige)
        self.eq_db = [0.0, 0.0, 0.0]   # Baender low/mid/high (dB; 0 = neutral)
        self.eq_sos = None             # Biquad-Kaskade oder None (neutral)
        self.eq_zi = None              # Filterzustand (Block-Kontinuitaet)
        # Tempo-Sync/-Glide (tonhoehen-erhaltend, ECHTZEIT via _WSOLA):
        self.native_bpm = 0.0          # Eigentempo des Stuecks
        self.orig_audio = None         # Originalpuffer (Quelle fuer den Stretcher)
        self.orig_beats = None
        self.orig_ticks = None
        self.synced = False            # spielt gerade im Master-Tempo?
        self.sync_ratio = 1.0          # aktuelle Dehnungsrate (Anzeige)
        self.gliding = False           # Tempo-Uebergang laeuft?
        self.stretcher = None          # _WSOLA, wenn Tempo veraendert wird
        self.tempo_mode = "none"       # 'none' | 'sync' | 'glide'
        self.glide_r0 = 1.0            # Start-/Zielrate + Startzeit (Ausgabe) des Glide
        self.glide_r1 = 1.0
        self.glide_t0 = 0.0
        self.glide_dur = DJ_GLIDE_S
        # Stems (KI-getrennte Instrumentenspuren, in Echtzeit mischbar):
        self.stems = None              # Liste [(frames,ch) float32] in stem_names-Reihenfolge
        self.stem_names = None         # Namensliste (z. B. drums/bass/other/vocals)
        self.stem_gain = None          # Liste float je Stem (0..1.x), live regelbar
        self.stem_str = None           # je Stem ein _WSOLA, wenn Tempo veraendert wird


class DJEngine:
    """Zwei Decks in einem gemischten OutputStream. Threadsicher (ein Lock):
    der Audio-Callback mischt, die Steuer-/Clock-Threads lesen Position und
    Dominanz."""

    def __init__(self, channels=2, device=None, blocksize=1024):
        self.decks = [DJDeck(), DJDeck()]
        self.channels = int(channels)
        self.device = device
        self.blocksize = int(blocksize)
        self.lock = threading.Lock()
        self.cross = 0.0           # 0 = A, 1 = B (aktuell)
        self.cross_target = 0.0
        self.latency = blocksize / float(DJ_SR)
        self.stream = None

    def load(self, idx, audio, sr, info, key, name=""):
        """Deck mit (bereits analysiertem) Audio belegen. audio (frames[,ch])
        wird auf DJ_SR und die Engine-Kanalzahl gebracht. Beats/Ticks sind
        zeitbasiert und bleiben nach dem Resampling gueltig."""
        a = np.asarray(audio, dtype=np.float32)
        if a.ndim == 1:
            a = a.reshape(-1, 1)
        if int(sr) != DJ_SR:
            try:
                a = librosa.resample(a.T, orig_sr=int(sr), target_sr=DJ_SR).T
                a = np.ascontiguousarray(a, dtype=np.float32)
            except Exception:
                pass
        if a.shape[1] == 1 and self.channels >= 2:
            a = np.repeat(a, self.channels, axis=1)
        elif a.shape[1] > self.channels:
            a = np.ascontiguousarray(a[:, :self.channels])
        elif a.shape[1] < self.channels:
            a = np.repeat(a[:, :1], self.channels, axis=1)
        with self.lock:
            d = self.decks[idx]
            d.playing = False
            d.audio = a
            d.frames_total = a.shape[0]
            d.pos = 0
            d.info = info
            d.beats = info["beats"] if info else None
            d.ticks = info["ticks"] if info else None
            d.key = key
            d.name = name
            # Originale fuer Tempo-Sync merken; Sync zuruecksetzen
            d.orig_audio = a
            d.orig_beats = d.beats
            d.orig_ticks = d.ticks
            d.native_bpm = float(info["bpm"]) if info else 0.0
            d.synced = False
            d.sync_ratio = 1.0
            d.gliding = False
            d.stretcher = None
            d.tempo_mode = "none"
            d.stems = None
            d.stem_names = None
            d.stem_gain = None
            d.stem_str = None

    def _adapt(self, audio, sr):
        """Puffer auf DJ_SR + Engine-Kanalzahl bringen (Resampling/Kanaladaption)."""
        a = np.asarray(audio, dtype=np.float32)
        if a.ndim == 1:
            a = a.reshape(-1, 1)
        if int(sr) != DJ_SR:
            try:
                a = np.ascontiguousarray(
                    librosa.resample(a.T, orig_sr=int(sr), target_sr=DJ_SR).T,
                    dtype=np.float32)
            except Exception:
                pass
        if a.shape[1] == 1 and self.channels >= 2:
            a = np.repeat(a, self.channels, axis=1)
        elif a.shape[1] > self.channels:
            a = np.ascontiguousarray(a[:, :self.channels])
        elif a.shape[1] < self.channels:
            a = np.repeat(a[:, :1], self.channels, axis=1)
        return a

    def load_stems(self, idx, stems_dict, sr):
        """KI-getrennte Spuren als Stems eines bereits geladenen Decks setzen.
        Reihenfolge: bekannte STEM_NAMES zuerst, weitere angehaengt. Live ueber
        set_stem_gain mischbar (Pegel je Instrument)."""
        names = ([n for n in STEM_NAMES if n in stems_dict]
                 + [n for n in stems_dict if n not in STEM_NAMES])
        prepared = [self._adapt(stems_dict[n], sr) for n in names]
        if not prepared:
            return []
        L = min(p.shape[0] for p in prepared)
        prepared = [p[:L] for p in prepared]
        with self.lock:
            d = self.decks[idx]
            d.stems = prepared
            d.stem_names = names
            d.stem_gain = [1.0] * len(prepared)
            d.stem_str = None
            if d.pos >= L:
                d.pos = 0
        return names

    def clear_stems(self, idx):
        with self.lock:
            d = self.decks[idx]
            d.stems = d.stem_names = d.stem_gain = d.stem_str = None

    def set_stem_gain(self, idx, k, gain):
        with self.lock:
            d = self.decks[idx]
            if d.stem_gain is not None and 0 <= k < len(d.stem_gain):
                d.stem_gain[k] = float(max(0.0, gain))

    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            block_dt = frames / float(DJ_SR)
            if abs(self.cross - self.cross_target) > 1e-4:
                coeff = 1.0 - math.exp(-block_dt / DJ_FADE_TAU)
                self.cross += (self.cross_target - self.cross) * coeff
                if abs(self.cross - self.cross_target) < 1e-3:
                    self.cross = self.cross_target
            x = min(1.0, max(0.0, self.cross))
            gains = (math.cos(x * math.pi / 2), math.sin(x * math.pi / 2))
            out = np.zeros((frames, self.channels), dtype=np.float32)
            for i, d in enumerate(self.decks):
                if d.playing and d.stems is not None:
                    # KI-Stems: je Spur ein Pegel, in Echtzeit gemischt. Bei Tempo-
                    # aenderung je Stem ein eigener Stretcher (gleiche Rate/Position).
                    d.anchor_pos = d.pos
                    d.anchor_perf = time.perf_counter()
                    block = np.zeros((frames, self.channels), dtype=np.float32)
                    if d.stem_str is not None:
                        if d.tempo_mode == "glide":
                            to = d.pos / float(DJ_SR) - d.glide_t0
                            f = min(1.0, max(0.0, to / d.glide_dur)) if d.glide_dur > 0 else 1.0
                            r = d.glide_r0 + (d.glide_r1 - d.glide_r0) * f
                            for st in d.stem_str:
                                st.set_rate(r)
                        for k, st in enumerate(d.stem_str):
                            block += st.pull(frames) * d.stem_gain[k]
                        d.pos += frames
                        if all(st.finished and st.out.shape[0] == 0 for st in d.stem_str):
                            d.playing = False
                    else:
                        total = d.stems[0].shape[0]
                        s = d.pos
                        e = min(s + frames, total)
                        n = e - s
                        if n > 0:
                            for k, st in enumerate(d.stems):
                                block[:n] += st[s:e] * d.stem_gain[k]
                            d.pos = e
                        if e >= total:
                            d.playing = False
                    if d.eq_sos is not None and _sps is not None:
                        block = self._deck_eq(d, block)
                    out += block * gains[i]
                    d.level = float(np.sqrt(np.mean(block * block))) * gains[i]
                elif d.playing and d.stretcher is not None:
                    # Tempo veraendert -> Echtzeit-Zeitdehnung. d.pos = AUSGABE-Frames.
                    d.anchor_pos = d.pos
                    d.anchor_perf = time.perf_counter()
                    if d.tempo_mode == "glide":
                        to = d.pos / float(DJ_SR) - d.glide_t0
                        f = min(1.0, max(0.0, to / d.glide_dur)) if d.glide_dur > 0 else 1.0
                        d.stretcher.set_rate(d.glide_r0 + (d.glide_r1 - d.glide_r0) * f)
                    block = d.stretcher.pull(frames)
                    if d.eq_sos is not None and _sps is not None:
                        block = self._deck_eq(d, block)
                    out += block * gains[i]
                    d.level = float(np.sqrt(np.mean(block * block))) * gains[i]
                    d.pos += frames
                    if d.stretcher.finished and d.stretcher.out.shape[0] == 0:
                        d.playing = False
                elif d.playing and d.audio is not None:
                    d.anchor_pos = d.pos
                    d.anchor_perf = time.perf_counter()
                    s = d.pos
                    e = min(s + frames, d.frames_total)
                    n = e - s
                    if n > 0:
                        block = d.audio[s:e]
                        if d.eq_sos is not None and _sps is not None:
                            block = self._deck_eq(d, block)
                        out[:n] += block * gains[i]
                        d.level = float(np.sqrt(np.mean(block * block))) * gains[i]
                        d.pos = e
                    if e >= d.frames_total:
                        d.playing = False
                else:
                    d.level = 0.0
            outdata[:] = out

    def _deck_eq(self, d, block):
        """3-Band-EQ auf einen Block anwenden (Filterzustand bleibt erhalten)."""
        if d.eq_zi is None or d.eq_zi.shape[-1] != block.shape[1]:
            d.eq_zi = np.zeros((d.eq_sos.shape[0], 2, block.shape[1]),
                               dtype=np.float64)
        out, d.eq_zi = _sps.sosfilt(d.eq_sos, block, axis=0, zi=d.eq_zi)
        return out.astype(np.float32)

    def set_eq(self, idx, low_db, mid_db, high_db):
        """EQ-Baender eines Decks setzen (dB; 0 = neutral, DJ_EQ_KILL_DB = kill)."""
        sos = _dj_eq_sos(low_db, mid_db, high_db) if _sps is not None else None
        with self.lock:
            d = self.decks[idx]
            d.eq_db = [low_db, mid_db, high_db]
            d.eq_sos = sos
            d.eq_zi = None             # Zustand fuer die neue Kaskade zuruecksetzen

    def _cur_src_sec(self, d):
        """Aktuelle Quellposition (s): bei aktivem Stretcher dessen Leseposition,
        sonst die direkte Ausgabeposition."""
        with self.lock:
            if d.stem_str:
                return d.stem_str[0].ia / float(DJ_SR)
            return (d.stretcher.ia / float(DJ_SR)) if d.stretcher is not None \
                else (d.pos / float(DJ_SR))

    def _phase_align_src(self, idx, src0, period_master):
        """Quellposition src0 (s) so verschieben, dass die Beat-Phase von Deck idx
        zur Beat-Phase des Master-Decks passt. Da eine Zeitdehnung Beats auf Beats
        abbildet, ist die Beat-Phase erhalten -- Ausrichten in der Quelle genuegt."""
        d = self.decks[idx]
        m = self.decks[1 - idx]
        if (d.orig_beats is None or len(d.orig_beats) < 2 or m.beats is None
                or len(m.beats) < 2 or m.native_bpm <= 0 or d.native_bpm <= 0
                or not m.playing):
            return src0
        P = 60.0 / d.native_bpm                       # B Quell-Beat-Periode
        m_pos = self.play_pos(1 - idx)                # nimmt self.lock selbst
        m_phase = ((m_pos - float(m.beats[0])) / period_master) % 1.0
        b_phase = ((src0 - float(d.orig_beats[0])) / P) % 1.0
        dphi = (m_phase - b_phase + 0.5) % 1.0 - 0.5
        return max(0.0, src0 + dphi * P)

    def set_sync(self, idx, on, status_cb=None):
        """Deck idx in ECHTZEIT (tonhoehen-erhaltend, WSOLA) auf das Tempo des
        anderen Decks einrasten bzw. wieder loesen -- ohne Vorberechnung, also
        sofort. status_cb(idx, ok) optional fuer die GUI."""
        d = self.decks[idx]
        other = self.decks[1 - idx]
        if on:
            if d.orig_audio is None or other.native_bpm <= 0 or d.native_bpm <= 0:
                return False
            r = other.native_bpm / d.native_bpm
            src0 = self._cur_src_sec(d)
            src0 = self._phase_align_src(idx, src0, 60.0 / other.native_bpm)
            sf0 = int(max(0.0, src0 * DJ_SR))
            stem_str = st = None
            if d.stems is not None:
                stem_str = [_WSOLA(s, start_frame=sf0) for s in d.stems]
                for s in stem_str:
                    s.set_rate(r)
            else:
                st = _WSOLA(d.orig_audio, start_frame=sf0)
                st.set_rate(r)
            ticks = beats = None
            if d.orig_ticks is not None:
                ot = np.asarray(d.orig_ticks, dtype=np.float64)
                ticks = (ot[ot >= src0] - src0) / r
            if d.orig_beats is not None:
                ob = np.asarray(d.orig_beats, dtype=np.float64)
                beats = (ob[ob >= src0] - src0) / r
            with self.lock:
                d.stretcher = st
                d.stem_str = stem_str
                d.tempo_mode = "sync"
                d.pos = 0
                d.ticks = ticks
                d.beats = beats
                d.eq_zi = None
                d.synced = True
                d.gliding = False
                d.sync_ratio = r
            if status_cb:
                status_cb(idx, True)
            return True
        else:
            with self.lock:
                if d.stretcher is None and not d.stem_str:
                    d.synced = d.gliding = False
                    d.tempo_mode = "none"
                    return False
                src0f = d.stem_str[0].ia if d.stem_str else d.stretcher.ia
                d.stretcher = None
                d.stem_str = None
                d.tempo_mode = "none"
                if d.stems is not None:
                    d.pos = min(max(0, src0f), max(0, d.stems[0].shape[0] - 1))
                else:
                    d.audio = d.orig_audio
                    d.frames_total = d.orig_audio.shape[0]
                    d.pos = min(max(0, src0f), max(0, d.frames_total - 1))
                d.beats = d.orig_beats
                d.ticks = d.orig_ticks
                d.eq_zi = None
                d.synced = d.gliding = False
                d.sync_ratio = 1.0
            return True

    def set_glide(self, idx, glide_s=DJ_GLIDE_S, status_cb=None):
        """Tempo-Uebergang in ECHTZEIT: Deck idx startet im Master-Tempo und
        gleitet ueber glide_s Sekunden auf sein Eigentempo. Die WSOLA-Rate wird
        im Callback gerampt; die analytisch berechneten Tick-Zeiten lassen die
        Clock automatisch mitgleiten. Sofort wirksam, ohne Vorberechnung."""
        d = self.decks[idx]
        other = self.decks[1 - idx]
        if d.orig_audio is None or other.native_bpm <= 0 or d.native_bpm <= 0:
            return False
        r0 = other.native_bpm / d.native_bpm          # Start: Master-Tempo
        r1 = 1.0                                       # Ziel: Eigentempo
        src0 = self._cur_src_sec(d)
        src0 = self._phase_align_src(idx, src0, 60.0 / other.native_bpm)
        sf0 = int(max(0.0, src0 * DJ_SR))
        stem_str = st = None
        if d.stems is not None:
            stem_str = [_WSOLA(s, start_frame=sf0) for s in d.stems]
            for s in stem_str:
                s.set_rate(r0)
        else:
            st = _WSOLA(d.orig_audio, start_frame=sf0)
            st.set_rate(r0)
        ticks = _glide_ticks(d.orig_ticks, src0, r0, r1, glide_s)
        beats = _glide_ticks(d.orig_beats, src0, r0, r1, glide_s)
        with self.lock:
            d.stretcher = st
            d.stem_str = stem_str
            d.tempo_mode = "glide"
            d.pos = 0
            d.glide_r0, d.glide_r1 = r0, r1
            d.glide_t0, d.glide_dur = 0.0, glide_s
            d.ticks = ticks
            d.beats = beats
            d.eq_zi = None
            d.synced = False
            d.gliding = True
            d.sync_ratio = r0
        if status_cb:
            status_cb(idx, True)
        return True

    def start_stream(self):
        if sd is None:
            raise RuntimeError("sounddevice nicht verfuegbar")
        self.stream = sd.OutputStream(
            samplerate=DJ_SR, channels=self.channels, blocksize=self.blocksize,
            device=self.device, dtype='float32', callback=self._callback)
        self.stream.start()
        try:
            self.latency = float(self.stream.latency)
        except Exception:
            self.latency = self.blocksize / float(DJ_SR)

    def play(self, idx):
        with self.lock:
            d = self.decks[idx]
            if d.audio is None:
                return
            if d.pos >= d.frames_total:
                d.pos = 0
            d.anchor_pos = d.pos
            d.anchor_perf = time.perf_counter()
            d.playing = True

    def stop(self, idx):
        with self.lock:
            self.decks[idx].playing = False

    def fade_to(self, idx):
        """Zu Deck idx ueberblenden (ggf. starten); die Clock folgt automatisch."""
        if self.decks[idx].audio is None:
            return
        if not self.decks[idx].playing:
            self.play(idx)
        with self.lock:
            self.cross_target = 1.0 if idx else 0.0

    def play_pos(self, idx):
        with self.lock:
            d = self.decks[idx]
            ap, aperf, playing = d.anchor_pos, d.anchor_perf, d.playing
            pos_frame = d.pos
        if not playing:
            return pos_frame / float(DJ_SR)
        return ap / float(DJ_SR) - self.latency + (time.perf_counter() - aperf)

    def dominant(self):
        with self.lock:
            return 1 if self.cross >= 0.5 else 0

    def any_playing(self):
        with self.lock:
            return self.decks[0].playing or self.decks[1].playing

    def teardown(self):
        for i in (0, 1):
            self.stop(i)
        st, self.stream = self.stream, None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass


def dj_clock_worker(engine, midi_out, stop_event):
    """MIDI-Clock fuer den DJ-Modus: folgt dem dominierenden Deck und sendet
    dessen 24-PPQN-Ticks driftfrei aus der Wiedergabeposition. Beim Deck-Wechsel
    rastet der Tick-Index auf die aktuelle Position des neuen Decks ein."""
    winmm = _realtime_timer_begin()
    clock_msg = mido.Message('clock')
    started = False
    cur_deck = -1
    i = 0
    while not stop_event.is_set():
        if not engine.any_playing():
            if started:
                try:
                    if midi_out is not None:
                        midi_out.send(mido.Message('stop'))
                except Exception:
                    pass
                started = False
            cur_deck = -1
            time.sleep(0.03)
            continue
        dom = engine.dominant()
        d = engine.decks[dom]
        ticks = d.ticks
        if ticks is None or len(ticks) == 0 or not d.playing:
            time.sleep(0.02)
            continue
        if not started:
            try:
                if midi_out is not None:
                    midi_out.send(mido.Message('start'))
                started = True
            except Exception:
                pass
        pos = engine.play_pos(dom)
        if dom != cur_deck:
            cur_deck = dom
            i = int(np.searchsorted(ticks, max(0.0, pos)))
        if i >= len(ticks):
            time.sleep(0.02)
            continue
        target = float(ticks[i])
        wait = target - pos
        if wait > 0.0:
            precise_sleep_until(time.perf_counter() + wait, stop_event)
            if stop_event.is_set():
                break
        try:
            if midi_out is not None:
                midi_out.send(clock_msg)
        except Exception:
            break
        i += 1
        pos2 = engine.play_pos(dom)
        if i < len(ticks) and pos2 - target > 0.25:
            i = int(np.searchsorted(ticks, pos2))
    try:
        if midi_out is not None and started:
            midi_out.send(mido.Message('stop'))
    except Exception:
        pass
    _realtime_timer_end(winmm)


# ===========================================================================
# Quellen-Auswahl
# ===========================================================================
def choose_capture_mode():
    if sys.platform != 'win32':
        # Loopback (Ausgabe mithoeren) gibt es nur unter Windows (WASAPI).
        # Auf den anderen Plattformen laeuft das Mithoeren ueber Geraete,
        # die als normale Eingaenge erscheinen -- darum hier keine Auswahl.
        if sys.platform == 'darwin':
            print("\nHinweis: Zum Mithoeren der Wiedergabe (z. B. Spotify) unter")
            print("macOS ein virtuelles Ausgabegeraet wie BlackHole installieren")
            print("(https://existential.audio/blackhole/) -- es erscheint dann")
            print("unten als normaler Audio-Eingang.")
        else:
            print("\nHinweis: Zum Mithoeren der Wiedergabe die PipeWire/Pulse-")
            print("'Monitor'-Quelle waehlen -- sie erscheint unten als normaler")
            print("Audio-Eingang.")
        return "1"
    print("\nAufnahmequelle waehlen:")
    print("  [1] Audio-Eingang / Mikrofon")
    print("  [2] Ausgabe mithoeren (Loopback) -- z. B. Spotify ueber Kopfhoerer")
    while True:
        sel = input("Modus (1/2): ").strip()
        if sel in ("1", "2"):
            return sel
        print("Bitte 1 oder 2 eingeben.")


def _list_io_devices(kind):
    """kind='in'|'out'. Liste (sd_index, beschriftung) ueber ALLE Host-APIs
    (so tauchen auch WASAPI-Endpunkte wie Kopfhoerer mit vollem Namen auf)."""
    devices = sd.query_devices()
    try:
        apis = sd.query_hostapis()
    except Exception:
        apis = []
    key = 'max_input_channels' if kind == 'in' else 'max_output_channels'
    out = []
    for i, d in enumerate(devices):
        if d.get(key, 0) > 0:
            api = apis[d['hostapi']]['name'] if d['hostapi'] < len(apis) else '?'
            out.append((i, f"{d['name']}  [{api}, {int(d['default_samplerate'])} Hz, "
                           f"{d[key]} ch]"))
    return out


def _measure_input_rms(device_index, seconds=0.5):
    """Kurz aufnehmen und RMS messen (fuer den Signal-Scan). None bei Fehler."""
    try:
        sr = int(sd.query_devices(device_index)['default_samplerate'])
        sr = min(max(sr, 8000), 48000)
        frames = int(seconds * sr)
        rec = sd.rec(frames, samplerate=sr, channels=1, dtype='float32',
                     device=device_index)
        sd.wait()
        return float(np.sqrt(np.mean(np.square(rec[:, 0], dtype=np.float64))))
    except Exception:
        return None


def scan_input_levels(seconds=0.5):
    """Misst nacheinander den Pegel jedes Eingangs -> zeigt, wo Signal anliegt."""
    if sd is None:
        print("(Scan nicht moeglich: 'sounddevice' fehlt.)")
        return
    inputs = _list_io_devices('in')
    if not inputs:
        print("Keine Audio-Eingaenge gefunden.")
        return
    print("\nSignal-Scan der Eingaenge (je ~0,5 s) -- spiele/sende dabei Audio:")
    for n, (idx, label) in enumerate(inputs):
        rms = _measure_input_rms(idx, seconds)
        if rms is None:
            print(f"  [{n}] {'(nicht lesbar)':>16}   {label}")
        else:
            db, bar = level_bar(rms)
            mark = "  <== SIGNAL" if db > -45.0 else ""
            print(f"  [{n}] {db:5.0f}dB [{bar}]   {label}{mark}")


def play_test_tone(device_index):
    """Kurzer 440-Hz-Ton auf das Geraet -- zum Identifizieren per Gehoer."""
    if sd is None:
        return
    try:
        sr = int(sd.query_devices(device_index)['default_samplerate'])
    except Exception:
        sr = 48000
    t = np.arange(int(0.6 * sr)) / sr
    tone = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    fade = max(1, int(0.01 * sr))
    env = np.ones(len(tone), dtype=np.float32)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    tone *= env
    try:
        sd.play(tone, samplerate=sr, device=device_index, blocking=True)
        print("    (Testton abgespielt.)")
    except Exception as e:
        print(f"    Testton fehlgeschlagen: {e}")
    finally:
        try:
            sd.stop()
        except Exception:
            pass


def choose_audio_input():
    if sd is None:
        sys.exit("Fehlt: 'sounddevice'. Installiere mit: pip install sounddevice")
    inputs = _list_io_devices('in')
    if not inputs:
        sys.exit("Kein Audio-Eingang gefunden.")
    print("\nVerfuegbare Audio-Eingaenge:")
    for n, (i, label) in enumerate(inputs):
        print(f"  [{n}] {label}")
    print("  [s] Signal-Scan (zeigt, auf welchem Eingang gerade etwas ankommt)")
    while True:
        raw = input("Eingang waehlen (Nummer / s = scannen): ").strip().lower()
        if raw == "s":
            scan_input_levels()
            continue
        try:
            return inputs[int(raw)][0]
        except (ValueError, IndexError):
            print("Ungueltige Eingabe, bitte erneut.")


def choose_loopback_speaker():
    try:
        import soundcard as sc
    except ImportError:
        sys.exit("Fehlt: 'soundcard' (fuer Loopback). Installiere mit: pip install soundcard")
    # soundcard setzt beim Import 'always' fuer SoundcardRuntimeWarning und
    # ueberschreibt damit unseren Filter -> hier erneut stummschalten.
    warnings.filterwarnings("ignore", message="data discontinuity in recording")
    print("\nVerfuegbare Ausgaenge (deren Ausgabe mitgehoert werden kann):")
    print("  WICHTIG: Waehle genau das Geraet, auf dem die Musik tatsaechlich")
    print("  laeuft (das Windows-Standard-Wiedergabegeraet). Ein anderes,")
    print("  leerlaufendes Geraet liefert nur Stille/Glitches.")
    speakers = sc.all_speakers()
    if not speakers:
        sys.exit("Kein Ausgabegeraet gefunden.")
    default_name = ""
    default_idx = 0
    try:
        default_name = sc.default_speaker().name
    except Exception:
        pass
    for n, sp in enumerate(speakers):
        is_default = sp.name == default_name
        if is_default:
            default_idx = n
        tag = "  <- Standard (empfohlen)" if is_default else ""
        print(f"  [{n}] {sp.name}{tag}")
    while True:
        try:
            raw = input(f"Ausgang waehlen (Nummer, Enter = [{default_idx}] Standard): ").strip()
            sel = default_idx if raw == "" else int(raw)
            chosen = speakers[sel]
            # Loopback-"Mikrofon" zu diesem Ausgang holen
            mic = sc.get_microphone(id=str(chosen.name), include_loopback=True)
            return mic
        except (ValueError, IndexError):
            print("Ungueltige Eingabe, bitte erneut.")
        except Exception as e:
            sys.exit(f"Konnte Loopback nicht oeffnen: {e}")


def choose_midi_output():
    print("\nVerfuegbare MIDI-Ausgaenge:")
    names = mido.get_output_names()
    # CoreMIDI (macOS) und ALSA (Linux) koennen eigene virtuelle Ports
    # erzeugen -- so braucht es kein IAC-/loopMIDI-Gegenstueck. Die
    # Windows-MultiMedia-API kann das nicht, dort bleibt alles wie gehabt.
    allow_virtual = sys.platform != 'win32'
    if not names and not allow_virtual:
        print("  Kein MIDI-Ausgang gefunden (wird uebersprungen).")
        return None

    for n, name in enumerate(names):
        print(f"  [{n}] {name}")
    if allow_virtual:
        print(f"  [v] Virtuellen MIDI-Port '{VIRTUAL_MIDI_NAME}' erzeugen")
    print("  [x] Ueberspringen (kein MIDI)")

    choices = "Nummer, 'v' oder 'x'" if allow_virtual else "Nummer oder 'x'"
    while True:
        try:
            raw = input(f"MIDI-Ausgang waehlen ({choices}): ").strip().lower()
            if raw == 'x':
                return None
            if allow_virtual and raw == 'v':
                return VIRTUAL_MIDI
            sel = int(raw)
            return names[sel]
        except (ValueError, IndexError):
            print("Ungueltige Eingabe, bitte erneut.")


def open_midi_output(midi_name):
    """MIDI-Ausgang oeffnen. VIRTUAL_MIDI erzeugt einen eigenen virtuellen
    Port (macOS/Linux), sonst wird der vorhandene Port geoeffnet."""
    if not midi_name:
        return None
    if midi_name == VIRTUAL_MIDI:
        return mido.open_output(VIRTUAL_MIDI_NAME, virtual=True)
    return mido.open_output(midi_name)


def midi_output_desc(midi_name):
    """Anzeigename eines MIDI-Ausgangs (loest das VIRTUAL_MIDI-Sentinel auf)."""
    if not midi_name:
        return "Kein MIDI"
    if midi_name == VIRTUAL_MIDI:
        return f"virtueller Port '{VIRTUAL_MIDI_NAME}'"
    return midi_name


def pick_input_samplerate(device_index):
    try:
        sd.check_input_settings(device=device_index, samplerate=INPUT_SR,
                                channels=1, dtype='float32')
        return INPUT_SR
    except Exception:
        return int(sd.query_devices(device_index)['default_samplerate'])


# ===========================================================================
# Mithören (analysiertes Signal auf einen Ausgang legen)
# ===========================================================================
def choose_monitor_output(exclude_hint=""):
    """Fragt einen Ausgang zum Mithören ab. Rueckgabe: sounddevice-Index oder None.

    Listet ALLE Ausgaenge ueber alle Host-APIs (so erscheinen auch Kopfhoerer
    o.ae. mit vollem Namen) und erlaubt einen Testton zum Identifizieren.
    """
    if sd is None:
        print("\n(Mithören nicht moeglich: 'sounddevice' ist nicht installiert.)")
        return None
    outputs = _list_io_devices('out')
    if not outputs:
        print("  Kein Ausgabegeraet gefunden -- uebersprungen.")
        return None
    print("\nMithören -- analysiertes Signal zusaetzlich auf einen Ausgang legen?")
    for n, (i, label) in enumerate(outputs):
        print(f"  [{n}] {label}")
    print("  [tN] Testton auf Geraet N abspielen (zum Identifizieren, z. B. t3)")
    print("  [x]  Kein Mithören (Standard)")
    if exclude_hint:
        print(f"  ACHTUNG: NICHT das gerade mitgeschnittene Geraet waehlen")
        print(f"           ('{exclude_hint}') -> sonst Rueckkopplung/Echo.")
    while True:
        raw = input("Mithör-Ausgang (Nummer / tN / x): ").strip().lower()
        if raw in ("", "x"):
            return None
        if raw.startswith("t"):
            try:
                idx = outputs[int(raw[1:])][0]
                print(f"    Testton auf [{int(raw[1:])}] ...")
                play_test_tone(idx)
            except (ValueError, IndexError):
                print("Ungueltige Testton-Nummer.")
            continue
        try:
            return outputs[int(raw)][0]
        except (ValueError, IndexError):
            print("Ungueltige Eingabe, bitte erneut.")


def feed_monitor(monitor_q, mono):
    """Block in die Monitor-Queue legen; bei Stau aeltesten Block verwerfen
    (begrenzt die Mithör-Latenz)."""
    if monitor_q is None:
        return
    try:
        if monitor_q.qsize() >= MONITOR_QUEUE_MAX:
            try:
                monitor_q.get_nowait()
            except queue.Empty:
                pass
        monitor_q.put_nowait(mono)
    except Exception:
        pass


def update_level(shared, block):
    """Eingangspegel (RMS, EMA) direkt im Capture (Producer) aktualisieren.
    So bleibt die Pegelanzeige live, auch wenn die Analyse gerade rechnet."""
    if block is None or len(block) == 0:
        return
    rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
    with shared.lock:
        shared.level = 0.7 * shared.level + 0.3 * rms
        shared.level_time = time.perf_counter()
        if shared.rec_active:          # Aufnahme: analysierten Block mitschneiden
            shared.rec_blocks.append(np.asarray(block, dtype=np.float32).copy())


def monitor_worker(out_stream, monitor_q, channels, stop_event):
    try:
        out_stream.start()
    except Exception as e:
        print(f"\n[Mithören konnte nicht gestartet werden: {e}]")
        return
    stereo = (channels >= 2)
    while not stop_event.is_set():
        try:
            block = monitor_q.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            out_stream.write(np.column_stack([block, block]) if stereo else block)
        except Exception:
            pass
    try:
        out_stream.stop()
        out_stream.close()
    except Exception:
        pass


# ===========================================================================
# Loopback-Aufnahme-Thread (soundcard)
# ===========================================================================
def loopback_capture_worker(mic, sr, audio_q, monitor_q, shared, stop_event):
    if sys.platform == 'win32':
        import ctypes
        try:
            ctypes.windll.ole32.CoInitialize(None)
        except Exception:
            pass

    try:
        with mic.recorder(samplerate=sr, blocksize=LOOPBACK_CHUNK) as rec:
            while not stop_event.is_set():
                data = rec.record(numframes=LOOPBACK_CHUNK)  # (frames, channels)
                if data.ndim > 1:
                    mono = data.mean(axis=1)
                else:
                    mono = data
                mono = mono.astype(np.float32).copy()
                feed_analysis(audio_q, mono)
                feed_monitor(monitor_q, mono)
                update_level(shared, mono)
    except Exception as e:
        msg = f"[Loopback-Aufnahme gestoppt: {e}]"
        print("\n" + msg)
        log_message(msg)
        stop_event.set()
    finally:
        if sys.platform == 'win32':
            import ctypes
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass


# ===========================================================================
# Quelle/Capture/Monitor: Lebenszyklus (auch zur Laufzeit umschaltbar)
# ===========================================================================
def choose_capture_source():
    """Fragt Modus + Geraet ab. Rueckgabe: (mode, source, capture_sr, beschreibung).
    source ist im Eingangs-Modus der sounddevice-Index, im Loopback der Mic."""
    mode = choose_capture_mode()
    if mode == "1":
        idx = choose_audio_input()
        sr = pick_input_samplerate(idx)
        name = sd.query_devices(idx)['name']
        return mode, idx, float(sr), f"Eingang '{name}' @ {int(sr)} Hz"
    else:
        mic = choose_loopback_speaker()
        return mode, mic, float(LOOPBACK_SR), f"Loopback '{mic.name}' @ {LOOPBACK_SR} Hz"


def drain_queue(q):
    if q is None:
        return
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


def start_capture(mode, source, capture_sr, audio_q, monitor_q, shared,
                  blocksize=AUDIO_BLOCKSIZE):
    """Startet die Aufnahme. Rueckgabe: (stream, thread, cap_stop).

    blocksize: kleinere Bloecke (z. B. NOTE_BLOCKSIZE) senken die Latenz im
    Noten-Modus; im Clock-Modus bleibt es bei AUDIO_BLOCKSIZE."""
    cap_stop = threading.Event()
    if mode == "1":
        def audio_callback(indata, frames, time_info, status):
            mono = indata[:, 0].copy()
            feed_analysis(audio_q, mono)
            feed_monitor(monitor_q, mono)
            update_level(shared, mono)

        stream = sd.InputStream(
            device=source, channels=1, samplerate=int(capture_sr),
            dtype='float32', blocksize=int(blocksize), callback=audio_callback)
        stream.start()
        return stream, None, cap_stop
    else:
        thread = threading.Thread(
            target=loopback_capture_worker,
            args=(source, capture_sr, audio_q, monitor_q, shared, cap_stop),
            daemon=True)
        thread.start()
        return None, thread, cap_stop


def stop_capture(stream, thread, cap_stop):
    if cap_stop is not None:
        cap_stop.set()
    if stream is not None:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
    if thread is not None:
        thread.join(timeout=1.5)


def start_monitor(device_index, capture_sr, monitor_q):
    """Startet den Mithör-Ausgang. Rueckgabe: (out, thread, mon_stop, name)
    oder (None, None, None, 'aus')."""
    mon_stop = threading.Event()
    channels = 1
    try:
        try:
            out = sd.OutputStream(device=device_index, channels=1,
                                  samplerate=int(capture_sr), dtype='float32')
        except Exception:
            out = sd.OutputStream(device=device_index, channels=2,
                                  samplerate=int(capture_sr), dtype='float32')
            channels = 2
    except Exception as e:
        print(f"\n[Mithören deaktiviert: {e}]")
        print(f"  (Der Ausgang muss {int(capture_sr)} Hz unterstuetzen.)")
        return None, None, None, "aus"
    thread = threading.Thread(
        target=monitor_worker, args=(out, monitor_q, channels, mon_stop),
        daemon=True)
    thread.start()
    try:
        name = sd.query_devices(device_index)['name']
    except Exception:
        name = f"Geraet #{device_index}"
    return out, thread, mon_stop, name


def stop_monitor(thread, mon_stop):
    if mon_stop is not None:
        mon_stop.set()
    if thread is not None:
        thread.join(timeout=1.5)


# ===========================================================================
# Tastenabfrage waehrend des Laufs (plattformuebergreifend)
# ===========================================================================
class KeyPoller:
    """Nicht blockierende Einzeltasten-Abfrage fuer die Hotkeys im Lauf.

    Windows: msvcrt, exakt wie bisher (kein Terminal-Umbau noetig).
    macOS/Linux: stdin wird waehrend der Statusschleife in den cbreak-Modus
    geschaltet (Taste sofort lesbar, ohne Enter und ohne Echo). Fuer die
    interaktiven input()-Dialoge stellt pause() den Normalmodus wieder her,
    resume() schaltet danach zurueck. Strg+C bleibt in beiden Modi wirksam
    (cbreak laesst ISIG an)."""

    def __init__(self):
        self._posix = False
        self._saved = None          # gesicherte Terminal-Attribute (POSIX)
        if msvcrt is None:
            try:
                import termios, tty, select  # noqa: F401 -- nur Verfuegbarkeit pruefen
                self._posix = sys.stdin.isatty()
            except Exception:
                self._posix = False

    @property
    def available(self):
        return msvcrt is not None or self._posix

    def resume(self):
        """cbreak-Modus aktivieren (POSIX; unter Windows ein No-Op)."""
        if self._posix and self._saved is None:
            import termios, tty
            try:
                fd = sys.stdin.fileno()
                self._saved = termios.tcgetattr(fd)
                tty.setcbreak(fd)
            except Exception:
                self._saved = None
                self._posix = False

    def pause(self):
        """Terminal fuer input()-Dialoge zuruecksetzen (POSIX; sonst No-Op)."""
        if self._posix and self._saved is not None:
            import termios
            try:
                termios.tcsetattr(sys.stdin.fileno(),
                                  termios.TCSADRAIN, self._saved)
            except Exception:
                pass
            self._saved = None

    def poll(self):
        """Gedrueckte Taste (kleingeschrieben) oder None; blockiert nie."""
        if msvcrt is not None:
            if msvcrt.kbhit():
                return msvcrt.getwch().lower()
            return None
        if self._posix and self._saved is not None:
            import select
            try:
                if select.select([sys.stdin], [], [], 0)[0]:
                    ch = sys.stdin.read(1)
                    return ch.lower() if ch else None
            except Exception:
                pass
        return None


def choose_run_mode(midi_name):
    """Betriebsart: Tempo/Clock oder Noten-Modus (mono/poly).
    Rueckgabe 'clock' | 'mono' | 'poly'."""
    print("\nBetriebsart waehlen:")
    print("  [1] Tempo & MIDI-Clock (Standard)")
    print("  [2] Noten -> MIDI, monophon (eine Note; geringe Latenz)")
    print("  [3] Noten -> MIDI, polyphon (mehrere Noten; etwas hoehere Latenz)")
    while True:
        sel = input("Modus (1/2/3): ").strip()
        if sel in ("1", "2", "3"):
            break
        print("Bitte 1, 2 oder 3 eingeben.")
    mode = {"1": "clock", "2": "mono", "3": "poly"}[sel]
    if mode != "clock" and not midi_name:
        print("Hinweis: Im Noten-Modus ohne MIDI-Ausgang werden keine Noten "
              "gesendet (nur Anzeige).")
    return mode


# ===========================================================================
# Datei-Modus (Konsole): nicht-interaktiver Sonderweg
# ===========================================================================
def _arg_value(flag):
    """Liest --flag WERT oder --flag=WERT aus sys.argv; None, wenn nicht da."""
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(flag + "="):
            return a[len(flag) + 1:]
    return None


def run_file_mode(path):
    """Spielt eine Audiodatei ab und gibt eine driftfreie MIDI-Clock zur
    Wiedergabe aus (Konsolen-Variante des Datei-Modus). Vorab wird die Datei
    einmal zu einer Beat-Map analysiert."""
    try:
        mido.set_backend('mido.backends.rtmidi')
    except Exception:
        pass
    if not os.path.exists(path):
        sys.exit(f"Datei nicht gefunden: {path}")
    midi_name = choose_midi_output()
    try:
        midi_out = open_midi_output(midi_name) if midi_name else None
    except Exception as e:
        sys.exit(f"MIDI-Ausgang fehlgeschlagen: {e}")

    print("\nLade & analysiere Datei (einmalig, kann kurz dauern) ...")
    try:                                       # librosa/numba aufwaermen
        _w = np.zeros(int(ANALYSIS_SR * WINDOW_SECONDS), dtype=np.float32)
        _w[::ANALYSIS_SR // 4] = 0.5
        estimate_tempo(_w, ANALYSIS_SR)
    except Exception:
        pass
    try:
        y_an, audio, sr_play = load_audio_file(path)
    except Exception as e:
        sys.exit(f"Datei konnte nicht geladen werden: {e}")
    info = analyze_file_beatmap(y_an, ANALYSIS_SR, MIN_BPM, MAX_BPM)
    if info is None:
        sys.exit("Kein Tempo erkannt -- Datei zu kurz oder ohne klaren Beat?")
    key = "—"
    try:
        key, _m = estimate_key(y_an, ANALYSIS_SR, with_margin=True)
    except Exception:
        pass
    tag = "konstant -> driftfrei" if info["constant"] else "variabel"
    dur = info["duration"]
    print(f"Tempo {info['bpm']:.1f} BPM ({tag}), Tonart {key}, "
          f"Dauer {int(dur) // 60}:{int(dur) % 60:02d} min")
    print(f"MIDI-Ausgang: {midi_output_desc(midi_name)}\n"
          f"Wiedergabe laeuft -- Beenden mit Strg+C.\n")

    shared = Shared()
    player = FilePlayer(audio, sr_play)
    stop_event = threading.Event()
    try:
        player.start()
    except Exception as e:
        sys.exit(f"Wiedergabe fehlgeschlagen: {e}")
    clk = threading.Thread(target=file_clock_worker,
                           args=(shared, player, info["ticks"], midi_out,
                                 stop_event), daemon=True)
    clk.start()
    try:
        while not player.is_done():
            pos = max(0.0, min(dur, player.play_pos()))
            bpm = file_bpm_at(info["beats"], pos, info["bpm"])
            print(f"\r{int(pos) // 60}:{int(pos) % 60:02d}/"
                  f"{int(dur) // 60}:{int(dur) % 60:02d}  "
                  f"BPM {bpm:6.1f}  Tonart {key:9s}", end="", flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        clk.join(timeout=1.0)
        player.stop()
        if midi_out is not None:
            try:
                midi_out.close()
            except Exception:
                pass
    print("\nFertig.")


def run_dj_mode(path_a, path_b):
    """Konsolen-DJ: zwei Dateien laden/analysieren, in einem Stream mischen;
    [a]/[b] blenden zum jeweiligen Deck, die MIDI-Clock folgt automatisch."""
    try:
        mido.set_backend('mido.backends.rtmidi')
    except Exception:
        pass
    for p in (path_a, path_b):
        if not os.path.exists(p):
            sys.exit(f"Datei nicht gefunden: {p}")
    midi_name = choose_midi_output()
    try:
        midi_out = open_midi_output(midi_name) if midi_name else None
    except Exception as e:
        sys.exit(f"MIDI-Ausgang fehlgeschlagen: {e}")
    print("\nLade & analysiere beide Decks (einmalig, kann kurz dauern) ...")
    try:
        _w = np.zeros(int(ANALYSIS_SR * WINDOW_SECONDS), dtype=np.float32)
        _w[::ANALYSIS_SR // 4] = 0.5
        estimate_tempo(_w, ANALYSIS_SR)
    except Exception:
        pass
    eng = DJEngine(channels=2)
    for idx, p in enumerate((path_a, path_b)):
        try:
            y_an, audio, sr_play = load_audio_file(p)
            info = analyze_file_beatmap(y_an, ANALYSIS_SR, MIN_BPM, MAX_BPM)
        except Exception as e:
            sys.exit(f"Deck {'AB'[idx]} fehlgeschlagen: {e}")
        key = ""
        try:
            key = estimate_key(y_an, ANALYSIS_SR)
        except Exception:
            pass
        eng.load(idx, audio, sr_play, info, key, os.path.basename(p))
        print(f"  Deck {'AB'[idx]}: {os.path.basename(p)} -> "
              f"{int(round(info['bpm'])) if info else '-'} BPM"
              f"{' ' + key if key else ''}"
              f"{'' if info else '  (kein klares Tempo)'}")
    try:
        eng.start_stream()
    except Exception as e:
        sys.exit(f"Audioausgabe fehlgeschlagen: {e}")
    stop_event = threading.Event()
    clk = threading.Thread(target=dj_clock_worker,
                           args=(eng, midi_out, stop_event), daemon=True)
    clk.start()
    eng.play(0)                                # Deck A startet
    keys = KeyPoller()
    print(f"\nMIDI-Ausgang: {midi_output_desc(midi_name)}")
    print("[a] zu Deck A faden   [b] zu Deck B faden   [q] Beenden\n"
          if keys.available else "(Beenden mit Strg+C; ohne Tastatursteuerung)")
    keys.resume()
    try:
        while True:
            ch = keys.poll()
            if ch == 'q':
                break
            elif ch == 'a':
                eng.fade_to(0)
            elif ch == 'b':
                eng.fade_to(1)
            pa, pb = eng.play_pos(0), eng.play_pos(1)
            dom = 'B' if eng.dominant() else 'A'
            print(f"\rA {int(pa) // 60}:{int(pa) % 60:02d}  |  "
                  f"B {int(pb) // 60}:{int(pb) % 60:02d}  |  "
                  f"Clock folgt: {dom}   ", end="", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        keys.pause()
        stop_event.set()
        clk.join(timeout=1.0)
        eng.teardown()
        if midi_out is not None:
            try:
                midi_out.close()
            except Exception:
                pass
    print("\nFertig.")


def run_stems_export(path, out_dir=None):
    """Konsole: eine Datei lokal per Demucs in Stems trennen und als WAVs
    speichern. Offline, kann je nach CPU einige Minuten dauern."""
    if not os.path.exists(path):
        sys.exit(f"Datei nicht gefunden: {path}")
    if not demucs_available():
        sys.exit("Demucs ist nicht installiert. Installiere mit: pip install demucs")
    out_dir = out_dir or os.path.dirname(os.path.abspath(path)) or os.getcwd()
    if not os.path.isdir(out_dir):
        sys.exit(f"Zielordner nicht gefunden: {out_dir}")
    print(f"\nTrenne Stems (Demucs, lokal & offline) aus:\n  {path}")
    print("Das kann je nach CPU einige Minuten dauern (GPU ist deutlich schneller) ...")
    try:
        written = separate_stems_to_files(path, out_dir, model="htdemucs",
                                          log=lambda m: print("  " + m))
    except Exception as e:
        sys.exit(f"Stem-Trennung fehlgeschlagen: {e}")
    print("Geschrieben:")
    for p in written:
        print("  " + p)
    print("Fertig.")


def run_song_sheet(path, out_dir=None, language=None, whisper_size="medium"):
    """Konsole: Gesang heraustrennen + transkribieren, Akkorde bestimmen und ein
    Chord-Sheet schreiben (Text + ChordPro). Offline, kann einige Minuten dauern.
    language: 'de'/'en'/... erzwingt die Sprache (bei Gesang empfohlen)."""
    if not os.path.exists(path):
        sys.exit(f"Datei nicht gefunden: {path}")
    if not demucs_available():
        sys.exit("Song-Sheet braucht 'demucs'. Installiere mit: pip install demucs")
    if not whisper_available():
        sys.exit("Song-Sheet braucht 'faster-whisper'. "
                 "Installiere mit: pip install faster-whisper")
    out_dir = out_dir or os.path.dirname(os.path.abspath(path)) or os.getcwd()
    if not os.path.isdir(out_dir):
        sys.exit(f"Zielordner nicht gefunden: {out_dir}")
    print(f"\nErzeuge Song-Sheet (lokal & offline) aus:\n  {path}")
    print(f"  Sprache: {language or 'automatisch'}  ·  Modell: {whisper_size}")
    try:
        res = song_sheet(path, model="htdemucs", whisper_size=whisper_size,
                         language=language, log=lambda m: print("  " + m))
    except Exception as e:
        sys.exit(f"Song-Sheet fehlgeschlagen: {e}")
    base = sanitize_filename(res.get("title") or
                             os.path.splitext(os.path.basename(path))[0])
    txt_p = os.path.join(out_dir, base + ".txt")
    cp_p = os.path.join(out_dir, base + ".chordpro")
    with open(txt_p, "w", encoding="utf-8") as fh:
        fh.write(res["text"])
    with open(cp_p, "w", encoding="utf-8") as fh:
        fh.write(res["chordpro"])
    print("\n" + res["text"])
    print("Geschrieben:")
    print("  " + txt_p)
    print("  " + cp_p)
    print("Fertig.")


def _console_stop_and_save(shared):
    """Konsole: laufende Aufnahme stoppen, in Stuecke zerlegen und als WAV
    speichern (gemeinsamer Zielordner, Namensvorschlag BPM+Tonart)."""
    with shared.lock:
        shared.rec_active = False
        blocks = shared.rec_blocks
        shared.rec_blocks = []
        sr = int(shared.capture_sr)
    if not blocks:
        print("\n[Aufnahme leer.]")
        return
    rec = np.concatenate(blocks).astype(np.float32)
    if len(rec) < sr:
        print("\n[Aufnahme zu kurz zum Speichern.]")
        return
    print(f"\nAufnahme {len(rec) / sr:.1f}s -- analysiere Stuecke ...")
    try:
        segs = segment_recording(rec, sr, MIN_BPM, MAX_BPM)
    except Exception as e:
        print(f"[Segmentierung fehlgeschlagen: {e}] -- speichere als ein Stueck.")
        segs = [{"start": 0, "end": len(rec), "bpm": 0.0, "key": "",
                 "confident": True, "name": "Aufnahme"}]
    print(f"{len(segs)} Stueck(e):")
    for i, s in enumerate(segs):
        a, b = int(s['start'] / sr), int(s['end'] / sr)
        print(f"  [{i + 1}] {a // 60}:{a % 60:02d}-{b // 60}:{b % 60:02d}  "
              f"{int(round(s['bpm'])) if s['bpm'] else '-'} BPM  "
              f"{s['key'] or '?'}  -> {s['name']}.wav")
    if sf is None:
        print("Speichern nicht moeglich: 'soundfile' fehlt (pip install soundfile).")
        return
    target = input("Zielordner (leer = aktueller, x = abbrechen): ").strip()
    if target.lower() == 'x':
        print("Abgebrochen.")
        return
    target = target or os.getcwd()
    if not os.path.isdir(target):
        print(f"Ordner nicht gefunden: {target}")
        return
    ok = 0
    for s in segs:
        try:
            save_wav_slice(rec, sr, s['start'], s['end'],
                           os.path.join(target, sanitize_filename(s['name']) + '.wav'))
            ok += 1
        except Exception as e:
            print(f"  Fehler bei {s['name']}: {e}")
    print(f"{ok} von {len(segs)} Stueck(en) in '{target}' gespeichert.\n")


# ===========================================================================
# Hauptprogramm
# ===========================================================================
def main():
    # Datei-Modus: Datei -> MIDI-Clock (driftfrei), nicht-interaktiv
    file_path = _arg_value('--file')
    if file_path:
        run_file_mode(file_path)
        return

    # DJ-Modus: zwei Dateien nebeneinander, Crossfade, Clock folgt
    if '--dj' in sys.argv:
        i = sys.argv.index('--dj')
        if i + 2 < len(sys.argv):
            run_dj_mode(sys.argv[i + 1], sys.argv[i + 2])
        else:
            sys.exit("Verwendung: --dj DATEI_A DATEI_B")
        return

    # Stems exportieren: KI-Trennung (Demucs) -> einzelne WAVs
    stems_path = _arg_value('--stems')
    if stems_path:
        run_stems_export(stems_path, _arg_value('--out'))
        return

    # Song-Sheet: Gesangstext (Whisper) + Akkorde -> Text + ChordPro
    #   --lang de|en (Sprache erzwingen, empfohlen)  --whisper small|medium|large-v3
    sheet_path = _arg_value('--sheet')
    if sheet_path:
        run_song_sheet(sheet_path, _arg_value('--out'),
                       language=_arg_value('--lang'),
                       whisper_size=_arg_value('--whisper') or "medium")
        return

    winmm = None
    if sys.platform == 'win32':
        try:
            import ctypes
            winmm = ctypes.windll.winmm
            winmm.timeBeginPeriod(1)
        except Exception:
            winmm = None

    try:
        mido.set_backend('mido.backends.rtmidi')
    except Exception:
        pass

    shared = Shared()
    audio_q = queue.Queue()
    monitor_q = queue.Queue()          # immer vorhanden; Capture speist sie stets
    stop_event = threading.Event()

    # ---- Quelle + MIDI + Betriebsart + Mithören waehlen ----
    mode, source, capture_sr, src_desc = choose_capture_source()
    midi_name = choose_midi_output()
    run_mode = choose_run_mode(midi_name)
    note_mode = run_mode != "clock"
    poly = run_mode == "poly"
    cap_bs = NOTE_BLOCKSIZE if note_mode else AUDIO_BLOCKSIZE
    monitor_exclude = source.name if mode == "2" else ""
    monitor_index = choose_monitor_output(monitor_exclude)

    shared.capture_sr = capture_sr
    try:
        midi_out = open_midi_output(midi_name)
    except Exception as e:
        sys.exit(f"MIDI-Ausgang fehlgeschlagen: {e}")

    # librosa/numba einmalig "aufwaermen" (sonst dauert der erste echte Analyse-
    # Aufruf mehrere Sekunden). Im Noten-Modus unnoetig -- dort laeuft keine
    # Tempo-/Tonart-Analyse.
    if not note_mode:
        print("\nInitialisiere Analyse (einmalig, kann kurz dauern) ...")
        try:
            _warm = np.zeros(int(ANALYSIS_SR * WINDOW_SECONDS), dtype=np.float32)
            _warm[::ANALYSIS_SR // 4] = 0.5     # ein paar Onsets
            estimate_tempo(_warm, ANALYSIS_SR)
            chroma_pcp(_warm, ANALYSIS_SR)
        except Exception:
            pass

    stream = loopback_thread = cap_stop = None
    monitor_out = monitor_thread = mon_stop = None
    monitor_desc = "aus"

    try:
        # ---- Mithören + Capture + Worker starten ----
        if monitor_index is not None:
            monitor_out, monitor_thread, mon_stop, monitor_desc = start_monitor(
                monitor_index, capture_sr, monitor_q)
        try:
            stream, loopback_thread, cap_stop = start_capture(
                mode, source, capture_sr, audio_q, monitor_q, shared,
                blocksize=cap_bs)
        except Exception as e:
            sys.exit(f"Konnte die Quelle nicht oeffnen: {e}")

        # Noten-Modus: nur der schlanke Noten-Worker, KEINE Tempo-/Tonart-
        # Analyse und KEINE Clock (minimale Latenz). Sonst der Normalbetrieb.
        analysis_thread = clock_thread = note_thread = None
        if note_mode:
            note_thread = threading.Thread(
                target=note_worker,
                args=(shared, audio_q, midi_out, stop_event, run_mode), daemon=True)
            note_thread.start()
        else:
            analysis_thread = threading.Thread(
                target=analysis_worker_safe, args=(shared, audio_q, stop_event),
                daemon=True)
            clock_thread = threading.Thread(
                target=clock_worker, args=(shared, midi_out, stop_event), daemon=True)
            analysis_thread.start()
            clock_thread.start()

        keys = KeyPoller()
        hotkeys = ("[i] Eingang wechseln   [o] Mithör-Ausgang   "
                   "[s] Signal-Scan   [r] Aufnahme   [?] Hilfe   [q] Beenden"
                   if keys.available else "(Beenden mit Strg+C)")
        mode_desc = {"clock": "Tempo & MIDI-Clock",
                     "mono": "Noten -> MIDI (monophon)",
                     "poly": "Noten -> MIDI (polyphon)"}[run_mode]
        print(f"\nQuelle: {src_desc}")
        print(f"MIDI-Ausgang: {midi_output_desc(midi_name)}")
        print(f"Betriebsart: {mode_desc}")
        print(f"Mithören: {monitor_desc}")
        print(f"Tasten: {hotkeys}\n")
        keys.resume()

        while not stop_event.is_set():
            # ---------- Statuszeile ----------
            with shared.lock:
                bpm = shared.target_bpm
                raw = shared.raw_bpm
                key = shared.key
                key_conf = shared.key_confident
                level = shared.level
                level_time = shared.level_time
                have = shared.have_estimate
                note_disp = shared.note_display
            # Pegel abklingen lassen, wenn keine frischen Bloecke mehr kommen
            age = time.perf_counter() - level_time
            if age > 0.3:
                level *= math.exp(-(age - 0.3) / 0.4)
            db, bar = level_bar(level)
            if note_mode:
                tag = "poly" if poly else "mono"
                print(f"\rNoten ({tag}): {note_disp:24s} | "
                      f"Pegel: {db:5.0f}dB [{bar}]",
                      end="", flush=True)
            else:
                if db <= -55.0:
                    status = "KEIN SIGNAL"
                elif not have:
                    status = "analysiere ..."
                else:
                    status = "laeuft"
                key_txt = key + ("" if key_conf or key == "—" else "?")
                print(f"\rBPM: {bpm:6.1f} | roh: {raw:6.1f} | Tonart: {key_txt:9s} | "
                      f"Pegel: {db:5.0f}dB [{bar}] | {status:<14}",
                      end="", flush=True)

            # ---------- Tastatur ----------
            ch = keys.poll()
            if ch is not None:
                if ch == 'q':
                    break

                elif ch == '?':
                    print(f"\nTasten: {hotkeys}\n")

                elif ch == 's':
                    print()
                    scan_input_levels()
                    print()

                elif ch == 'r':
                    with shared.lock:
                        active = shared.rec_active
                    if not active:
                        with shared.lock:
                            shared.rec_blocks = []
                            shared.rec_active = True
                        print("\n[Aufnahme laeuft -- 'r' stoppt und speichert]\n")
                    else:
                        keys.pause()
                        _console_stop_and_save(shared)
                        keys.resume()

                elif ch == 'i':
                    keys.pause()    # input()-Dialoge brauchen den Normalmodus
                    print("\n--- Eingangsquelle wechseln ---")
                    try:
                        new = choose_capture_source()
                    except Exception as e:
                        print(f"Abgebrochen: {e}")
                        new = None
                    if new is not None:
                        stop_capture(stream, loopback_thread, cap_stop)
                        drain_queue(audio_q)
                        drain_queue(monitor_q)
                        mode, source, capture_sr, src_desc = new
                        with shared.lock:
                            shared.capture_sr = capture_sr
                            shared.have_estimate = False
                            shared.raw_bpm = 0.0
                        # Mithör-Ausgang an evtl. neue Rate anpassen
                        if monitor_thread is not None:
                            stop_monitor(monitor_thread, mon_stop)
                            monitor_out, monitor_thread, mon_stop, monitor_desc = \
                                start_monitor(monitor_index, capture_sr, monitor_q)
                        try:
                            stream, loopback_thread, cap_stop = start_capture(
                                mode, source, capture_sr, audio_q, monitor_q,
                                shared, blocksize=cap_bs)
                        except Exception as e:
                            print(f"Konnte neue Quelle nicht oeffnen: {e}")
                            stream = loopback_thread = None
                            cap_stop = threading.Event()
                        print(f"Neue Quelle: {src_desc}\n")
                    keys.resume()

                elif ch == 'o':
                    keys.pause()    # input()-Dialog braucht den Normalmodus
                    print("\n--- Mithör-Ausgang wechseln ---")
                    exclude = source.name if mode == "2" else ""
                    new_idx = choose_monitor_output(exclude)
                    stop_monitor(monitor_thread, mon_stop)
                    monitor_out = monitor_thread = mon_stop = None
                    drain_queue(monitor_q)
                    if new_idx is not None:
                        monitor_index = new_idx
                        monitor_out, monitor_thread, mon_stop, monitor_desc = \
                            start_monitor(monitor_index, capture_sr, monitor_q)
                    else:
                        monitor_index = None
                        monitor_desc = "aus"
                    print(f"Mithören: {monitor_desc}\n")
                    keys.resume()

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n\nBeende ...")
    finally:
        stop_event.set()
        try:
            keys.pause()            # POSIX: Terminal-Modus wiederherstellen
        except NameError:
            pass                    # Abbruch noch vor der Statusschleife
        stop_capture(stream, loopback_thread, cap_stop)
        stop_monitor(monitor_thread, mon_stop)
        time.sleep(0.1)
        try:
            if midi_out is not None:
                midi_out.close()
        except Exception:
            pass
        if winmm is not None:
            try:
                winmm.timeEndPeriod(1)
            except Exception:
                pass
        print("Gestoppt.")


if __name__ == "__main__":
    main()

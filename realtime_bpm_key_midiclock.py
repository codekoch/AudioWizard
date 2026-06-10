#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realtime_bpm_key_midiclock_loopback.py
======================================

Echtzeit-Analyse von Audio (BPM + Tonart) mit stabiler MIDI-Clock-Ausgabe.

NEU gegenueber der Grundversion:
  Du kannst als Quelle entweder
    (1) einen normalen Audio-Eingang / ein Mikrofon (ueber sounddevice)
        ODER
    (2) die Lautsprecher-/Kopfhoerer-AUSGABE mithoeren (Loopback, ueber
        soundcard / WASAPI) -- z. B. um zu analysieren, was Spotify gerade
        ueber deinen Kopfhoerer-Ausgang abspielt.
  waehlen.

Wichtig zum Loopback: Es wird ALLES erfasst, was an den gewaehlten
Ausgang geht (also auch Windows-Systemklaenge, Benachrichtigungen usw.),
nicht nur Spotify allein.

Installation:
    pip install sounddevice mido python-rtmidi librosa numpy soundfile soundcard

('soundcard' wird nur fuer den Loopback-Modus gebraucht.)

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


# ===========================================================================
# Konfiguration
# ===========================================================================
WINDOW_SECONDS        = 8.0     # Laenge des Analysefensters
ANALYSIS_INTERVAL     = 1.0     # wie oft (Sek.) neu analysiert wird
ANALYSIS_SR           = 22050   # Analyse-Abtastrate (Fenster wird ggf. heruntergerechnet)
ONSET_HOP             = 256     # Hop der Onset-Huellkurve (kleiner = feineres Tempo-Raster)
PPQN                  = 24      # MIDI-Clock: 24 Pulse pro Viertelnote

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
KEY_EMA_SEC           = 15.0    # Zeitkonstante der schnellen Chroma-Mittelung;
                                #   die Tonart-Entscheidung nutzt 50 % davon und
                                #   50 % Gesamtmittel seit Songbeginn -> reagiert
                                #   anfangs schnell, wird mit der Zeit stabiler
BASS_TONIC_WEIGHT     = 0.30    # Bonus fuer Tonarten, deren Grundton den Bass
                                #   dominiert (unterscheidet Dur von der
                                #   Mollparallele -- gleiches Tonmaterial!)
KEY_SWITCH_CONFIRM    = 2       # Tonartwechsel erst nach N uebereinstimmenden
                                #   Folge-Schaetzungen anzeigen (gegen Flackern)
KEY_CONFIDENT_MARGIN  = 0.04    # Mindestvorsprung des besten Tonart-Kandidaten
                                #   vor dem zweitbesten, damit die Tonart als
                                #   "sicher" gilt (Anzeige sonst gedimmt)
ANALYSIS_QUEUE_MAX    = 256     # max. gepufferte Bloecke fuer die Analyse --
                                #   verhindert unbegrenztes Speicherwachstum,
                                #   falls die Analyse haengt (aeltester fliegt)
RESAMPLE_CTX          = 2048    # Roh-Samples Kontext fuers blockweise Resampling
                                #   (vermeidet Filterartefakte an den Nahtstellen)
BEAT_VALID_SEC        = 8.0     # so lange gilt ein Beat-Anker aus der Analyse
BEAT_NUDGE_MAX        = 0.0015  # max. Phasenkorrektur der Clock pro Tick (Sek.)
BEAT_NUDGE_GAIN       = 0.1     # Anteil des Phasenfehlers, der pro Tick
                                #   korrigiert wird (sanfte Regelschleife)

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "audio2midi.log")


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
    """Block in die Analyse-Queue legen; bei Stau aeltesten Block verwerfen."""
    try:
        if audio_q.qsize() >= ANALYSIS_QUEUE_MAX:
            try:
                audio_q.get_nowait()
            except queue.Empty:
                pass
        audio_q.put_nowait(block)
    except Exception:
        pass
SILENCE_DB            = -50.0   # Pegel darunter gilt als Stille
SILENCE_RESET_SEC     = 2.0     # so lange Stille (Pause/Songwechsel) -> Analyse zuruecksetzen
CLOCK_SLEW_BPM_PER_S  = 4.0     # max. Tempoaenderung der Clock pro Sekunde
INITIAL_BPM           = 120.0

AUDIO_BLOCKSIZE       = 2048    # Blockgroesse fuer den Eingangs-Modus

MONITOR_QUEUE_MAX     = 8       # max. gepufferte Bloecke beim Mithören (begrenzt die Latenz)


# Tonprofile (Index 0 = Grundton). Sha'ath-Profile (wie in "KeyFinder"):
# unterscheiden Dur und seine Moll-Parallele zuverlaessiger als Krumhansl-Kessler
# -- im Test gegen echte Stuecke deutlich treffsicherer.
KS_MAJOR = np.array([6.6, 2.0, 3.5, 2.3, 4.6, 4.0,
                     2.5, 5.2, 2.4, 3.7, 2.3, 3.4])
KS_MINOR = np.array([6.5, 2.7, 3.5, 5.4, 2.6, 3.5,
                     2.5, 5.2, 4.0, 2.7, 4.3, 3.2])
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F',
              'F#', 'G', 'G#', 'A', 'A#', 'B']


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
        self.level = 0.0          # aktueller Eingangspegel (RMS, linear)
        self.level_time = 0.0     # perf_counter des letzten Pegel-Updates
        self.capture_sr = float(ANALYSIS_SR)  # aktuelle Aufnahmerate (live aenderbar)
        self.have_estimate = False
        self.hold = False         # Analyse eingefroren (lange Breaks):
                                  # Ergebnisse bleiben stehen, kein
                                  # Stille-Reset, Clock laeuft konstant
        self.beat_sync = False    # Clock auf den Beat einrasten (GUI-Option)
        self.beat_anchor = 0.0    # perf_counter-Zeit eines erkannten Beats
        self.beat_period = 0.0    # Beat-Abstand in Sekunden
        self.beat_valid_time = 0.0  # wann der Anker zuletzt erneuert wurde


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


def fold_bpm(bpm):
    if bpm <= 0:
        return bpm
    while bpm < MIN_BPM:
        bpm *= 2.0
    while bpm > MAX_BPM:
        bpm /= 2.0
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


def chroma_pcp(y, sr, y_harm=None):
    """Chroma-Gesamtprofil + Bass-Profil: (pcp, bass) mit je 12 Werten
    (auf Summe 1 normiert) oder None.

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
        chroma = librosa.feature.chroma_cqt(y=y_harm, sr=sr)
        pcp = chroma.mean(axis=1)
        s = pcp.sum()
        if s <= 0:
            return None
        pcp = pcp / s             # Normierung -> laute Stellen dominieren nicht
        bass = np.zeros(12)
        if BASS_TONIC_WEIGHT > 0:   # = 0 spart das zweite CQT (z. B. Pi 4)
            try:
                bchroma = librosa.feature.chroma_cqt(
                    y=y_harm, sr=sr, fmin=librosa.note_to_hz('C1'), n_octaves=3)
                bass = bchroma.mean(axis=1)
                bs = bass.sum()
                bass = bass / bs if bs > 0 else np.zeros(12)
            except Exception:
                pass
        return pcp, bass
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

    with_margin=True liefert (name, vorsprung) -- der Vorsprung des besten
    vor dem zweitbesten Kandidaten ist ein brauchbares Konfidenzmass."""
    if pcp is None or not np.any(pcp):
        return ("—", 0.0) if with_margin else "—"
    best_score = -2.0
    second = -2.0
    best_name = "—"
    use_bass = bass is not None and np.any(bass)
    for i in range(12):
        bonus = 0.0
        if use_bass:
            bonus = BASS_TONIC_WEIGHT * (bass[i] + 0.5 * bass[(i + 7) % 12])
        maj = np.corrcoef(pcp, np.roll(KS_MAJOR, i))[0, 1] + bonus
        mino = np.corrcoef(pcp, np.roll(KS_MINOR, i))[0, 1] + bonus
        for score, name in ((maj, f"{NOTE_NAMES[i]} Dur"),
                            (mino, f"{NOTE_NAMES[i]} Moll")):
            if score > best_score:
                if best_name != name:
                    second = best_score
                best_score, best_name = score, name
            elif score > second:
                second = score
    if with_margin:
        return best_name, best_score - second
    return best_name


def estimate_beat_phase(y, sr, bpm):
    """Zeitpunkt des letzten Beats im Fenster, als Sekunden VOR Fensterende.
    None, wenn keine brauchbare Phase bestimmbar ist.

    Die Onset-Huellkurve wird auf die Beat-Periode gefaltet (Histogramm der
    Phasenlage, spaete Frames staerker gewichtet, damit die Phase zum
    aktuellen Fensterende passt); der staerkste Phasen-Bin ist der Beat.
    Wie estimate_tempo arbeitet die Funktion am besten auf dem perkussiven
    Anteil des Signals."""
    try:
        if bpm <= 0:
            return None
        oe = librosa.onset.onset_strength(y=y, sr=sr, hop_length=ONSET_HOP)
        if not np.any(oe):
            return None
        fr = sr / ONSET_HOP
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
    silence_rms = 10.0 ** (SILENCE_DB / 20.0)
    silent_since = None
    err_shown = False           # Analyse-Fehler nur einmal melden

    while not stop_event.is_set():
        try:
            block = audio_q.get(timeout=0.1)
            blocks = [block]
            # Alle weiteren wartenden Bloecke mitnehmen, damit die Analyse nicht
            # hinterherhinkt, falls ein Durchlauf laenger gedauert hat.
            try:
                while True:
                    blocks.append(audio_q.get_nowait())
            except queue.Empty:
                pass
        except queue.Empty:
            blocks = []

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
                buf = np.zeros(0, dtype=np.float32)
                res_tail = np.zeros(0, dtype=np.float32)
                ema_pcp = ema_bass = None
                cum_pcp = np.zeros(12)
                cum_bass = np.zeros(12)
                cum_n = 0
                bpm_hist.clear()
                key_disp, key_pend, key_pend_n = "—", None, 0
                with shared.lock:
                    shared.have_estimate = False
                    shared.raw_bpm = 0.0
                    shared.key = "—"
                    shared.key_confident = False
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
            buf = np.zeros(0, dtype=np.float32)
            res_tail = np.zeros(0, dtype=np.float32)
            ema_pcp = ema_bass = None
            cum_pcp = np.zeros(12)
            cum_bass = np.zeros(12)
            cum_n = 0
            bpm_hist.clear()
            key_disp, key_pend, key_pend_n = "—", None, 0

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
        buf_end_wall = time.perf_counter()

        now = time.perf_counter()
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
            bpm = fold_bpm(estimate_tempo(y_perc, sr, prev))
            if bpm <= 0:
                # kaum Perkussives (z. B. Ballade) -> Voll-Mix versuchen
                bpm = fold_bpm(estimate_tempo(y, sr, prev))
            chroma_res = chroma_pcp(y, sr, y_harm=y_harm)
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
            p, b = chroma_res
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
            cand, cand_margin = classify_key(prof, bprof, with_margin=True)
        else:
            cand, cand_margin = "—", 0.0

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
                     and cand_margin >= KEY_CONFIDENT_MARGIN and cum_n >= 5)

        # Tempo: Median der letzten Schaetzungen -> robust gegen Ausreisser.
        if bpm > 0:
            bpm_hist.append(bpm)
            # Echten Tempowechsel erkennen: stimmen die letzten 5 Schaetzungen
            # eng untereinander ueberein (< 3 % Streuung), weichen aber deutlich
            # vom bisherigen Median ab, alte Schaetzungen verwerfen -- so
            # springt die Anzeige in ~5 s auf das neue Tempo statt in ~16 s.
            # Liegt der Sprung aber auf einem typischen Alias-Verhaeltnis
            # (4/3, 3/2 bzw. deren Kehrwerte), ist es fast sicher ein
            # Schaetzfehler-Lauf und kein echter Wechsel -> nicht verwerfen,
            # der Median uebersteht solche Laeufe.
            if len(bpm_hist) >= 10:
                recent = list(bpm_hist)[-5:]
                rmed = float(np.median(recent))
                omed = float(np.median(bpm_hist))
                ratio = rmed / omed
                alias = any(abs(ratio / h - 1.0) < 0.04
                            for h in (4 / 3, 3 / 2, 2 / 3, 3 / 4))
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
            offs = estimate_beat_phase(y_perc, sr, target)
            if offs is not None:
                beat_update = (buf_end_wall - offs, 60.0 / target)

        with shared.lock:
            if target > 0:
                shared.target_bpm = target
                shared.have_estimate = True
            if bpm > 0:
                shared.raw_bpm = bpm
            shared.key = key
            shared.key_confident = confident
            if beat_update is not None:
                shared.beat_anchor = beat_update[0]
                shared.beat_period = beat_update[1]
                shared.beat_valid_time = time.perf_counter()


def analysis_worker_safe(shared, audio_q, stop_event):
    """analysis_worker mit Absturzschutz: ein unerwarteter Fehler wird
    geloggt und der Worker neu gestartet, statt die Analyse dauerhaft zu
    verlieren (die Anzeige wuerde sonst stumm einfrieren)."""
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
# Praezises Warten + MIDI-Clock
# ===========================================================================
def precise_sleep_until(target_perf, stop_event):
    while True:
        if stop_event.is_set():
            return
        remaining = target_perf - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.0015:
            time.sleep(remaining - 0.001)


def clock_worker(shared, midi_out, stop_event):
    """MIDI-Clock-Thread. Die Clock laeuft NUR, wenn eine echte Tempo-
    Schaetzung vorliegt: vorher waere es ein fiktives Tempo (INITIAL_BPM).
    Bei Stille/Reset stoppt sie (MIDI 'stop') und startet beim naechsten
    Stueck neu ('start') -- im Beat-Sync-Modus exakt auf dem naechsten
    erkannten Beat."""
    clock_msg = mido.Message('clock')
    running = False
    cur_bpm = INITIAL_BPM
    next_tick = time.perf_counter()
    last_loop = next_tick
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
            tick_in_beat = 1
            continue

        now = time.perf_counter()
        dt = now - last_loop
        last_loop = now

        max_step = CLOCK_SLEW_BPM_PER_S * dt
        diff = target - cur_bpm
        if abs(diff) <= max_step:
            cur_bpm = target
        else:
            cur_bpm += math.copysign(max_step, diff)
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
            next_tick = now + interval

        precise_sleep_until(next_tick, stop_event)
        try:
            if midi_out is not None:
                midi_out.send(clock_msg)
        except Exception:
            break
        tick_in_beat = (tick_in_beat + 1) % PPQN

    try:
        if midi_out is not None and running:
            midi_out.send(mido.Message('stop'))
    except Exception:
        pass


# ===========================================================================
# Quellen-Auswahl
# ===========================================================================
def choose_capture_mode():
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
    if not names:
        print("  Kein MIDI-Ausgang gefunden (wird uebersprungen).")
        return None
        
    for n, name in enumerate(names):
        print(f"  [{n}] {name}")
    print("  [x] Ueberspringen (kein MIDI)")
    
    while True:
        try:
            raw = input("MIDI-Ausgang waehlen (Nummer oder 'x'): ").strip().lower()
            if raw == 'x':
                return None
            sel = int(raw)
            return names[sel]
        except (ValueError, IndexError):
            print("Ungueltige Eingabe, bitte erneut.")


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


def start_capture(mode, source, capture_sr, audio_q, monitor_q, shared):
    """Startet die Aufnahme. Rueckgabe: (stream, thread, cap_stop)."""
    cap_stop = threading.Event()
    if mode == "1":
        def audio_callback(indata, frames, time_info, status):
            mono = indata[:, 0].copy()
            feed_analysis(audio_q, mono)
            feed_monitor(monitor_q, mono)
            update_level(shared, mono)

        stream = sd.InputStream(
            device=source, channels=1, samplerate=int(capture_sr),
            dtype='float32', blocksize=AUDIO_BLOCKSIZE, callback=audio_callback)
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
# Hauptprogramm
# ===========================================================================
def main():
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

    # ---- Quelle + MIDI + Mithören waehlen ----
    mode, source, capture_sr, src_desc = choose_capture_source()
    midi_name = choose_midi_output()
    monitor_exclude = source.name if mode == "2" else ""
    monitor_index = choose_monitor_output(monitor_exclude)

    shared.capture_sr = capture_sr
    midi_out = mido.open_output(midi_name) if midi_name else None

    # librosa/numba einmalig "aufwaermen" (sonst dauert der erste echte Analyse-
    # Aufruf mehrere Sekunden, was Pegel/Analyse anfangs blockiert wirken laesst).
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
                mode, source, capture_sr, audio_q, monitor_q, shared)
        except Exception as e:
            sys.exit(f"Konnte die Quelle nicht oeffnen: {e}")

        analysis_thread = threading.Thread(
            target=analysis_worker_safe, args=(shared, audio_q, stop_event),
            daemon=True)
        clock_thread = threading.Thread(
            target=clock_worker, args=(shared, midi_out, stop_event), daemon=True)
        analysis_thread.start()
        clock_thread.start()

        hotkeys = ("[i] Eingang wechseln   [o] Mithör-Ausgang   "
                   "[s] Signal-Scan   [?] Hilfe   [q] Beenden"
                   if msvcrt is not None else "(Beenden mit Strg+C)")
        print(f"\nQuelle: {src_desc}")
        print(f"MIDI-Ausgang: {midi_name if midi_name else 'Kein MIDI'}")
        print(f"Mithören: {monitor_desc}")
        print(f"Tasten: {hotkeys}\n")

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
            # Pegel abklingen lassen, wenn keine frischen Bloecke mehr kommen
            age = time.perf_counter() - level_time
            if age > 0.3:
                level *= math.exp(-(age - 0.3) / 0.4)
            db, bar = level_bar(level)
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

            # ---------- Tastatur (nur Windows) ----------
            if msvcrt is not None and msvcrt.kbhit():
                ch = msvcrt.getwch().lower()
                if ch == 'q':
                    break

                elif ch == '?':
                    print(f"\nTasten: {hotkeys}\n")

                elif ch == 's':
                    print()
                    scan_input_levels()
                    print()

                elif ch == 'i':
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
                                mode, source, capture_sr, audio_q, monitor_q, shared)
                        except Exception as e:
                            print(f"Konnte neue Quelle nicht oeffnen: {e}")
                            stream = loopback_thread = None
                            cap_stop = threading.Event()
                        print(f"Neue Quelle: {src_desc}\n")

                elif ch == 'o':
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

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n\nBeende ...")
    finally:
        stop_event.set()
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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_chords.py -- Messwerkzeug fuer die Akkorderkennung (Proxy-Metriken).

Zu den Testdateien gibt es keine Akkord-Referenz (anders als BPM/Tonart im
Dateinamen), darum zwei Naeherungen:

  - Diatonik-Quote: Anteil der Analyse-Schritte, deren erkannter Akkord
    vollstaendig im Tonmaterial der bekannten Tonart liegt (Moll inkl.
    erhoehtem Leitton). Echte Songs enthalten auch leiterfremde Akkorde --
    100 % sind nicht das Ziel, aber MEHR bei gleichem Material ist besser.
  - Wechsel/min: wie oft der angezeigte Akkord wechselt. Sehr hohe Werte
    deuten auf Flackern zwischen verwandten Deutungen hin.

ACHTUNG: Sobald die Akkorderkennung einen Tonart-Prior nutzt, ist die
Diatonik-Quote nach oben verzerrt (der Prior belohnt genau das Gemessene) --
dann zaehlen Wechselrate und Sichtpruefung der Timeline mehr.

Wie eval_detection.py bildet dieses Werkzeug den Akkord-Pfad aus
analysis_worker() nach -- wer dort etwas aendert, muss es hier nachziehen.

Aufruf (Testdateien <BPM>BPM_<Tonart>.mp3 im Skriptordner):
    python eval_chords.py             # 75 s je Datei
    python eval_chords.py 120         # 120 s je Datei
    python eval_chords.py 75 -t       # zusaetzlich Akkord-Timeline zeigen
"""

import os
import re
import sys
import time
from collections import deque

import numpy as np
import librosa

import realtime_bpm_key_midiclock as core

HERE = os.path.dirname(os.path.abspath(__file__))
PATTERN = re.compile(r"^(\d+(?:\.\d+)?)BPM_(.+)\.(mp3|wav|flac|ogg)$", re.I)
args = [a for a in sys.argv[1:] if a != "-t"]
SHOW_TIMELINE = "-t" in sys.argv[1:]
DUR = float(args[0]) if args else 75.0

files = []
for fn in sorted(os.listdir(HERE)):
    m = PATTERN.match(fn)
    if m:
        files.append((fn, float(m.group(1)), m.group(2).replace("_", " ")))
if not files:
    sys.exit("Keine Testdateien gefunden (Muster: <BPM>BPM_<Tonart>.mp3).")

SR = core.ANALYSIS_SR
WIN = core.WINDOW_SECONDS
STEP = core.ANALYSIS_INTERVAL
N_TYPES = len(core.CHORD_TYPES)

MAJ_SCALE = {0, 2, 4, 5, 7, 9, 11}
MIN_SCALE = {0, 2, 3, 5, 7, 8, 10, 11}   # natuerlich + erhoehter Leitton


def key_scale(key_name):
    """'C Dur' / 'D Moll' -> Menge der leitereigenen Tonklassen."""
    note, mode = key_name.rsplit(" ", 1)
    root = core.NOTE_NAMES.index(note)
    base = MAJ_SCALE if mode.lower() == "dur" else MIN_SCALE
    return {(root + i) % 12 for i in base}


def chord_pcs(chord_name):
    """Tonklassen eines Akkordnamens aus den Schablonen (None bei '—')."""
    k = core._CHORD_IDX.get(chord_name)
    if k is None:
        return None
    root, t = divmod(k, N_TYPES)
    return {(root + iv) % 12 for iv in core.CHORD_TYPES[t][1]}


print(f"Akkord-Fenster: {'beat-synchron' if core.CHORD_TAIL_BEAT else 'fest'}"
      f" (fest = {core.CHORD_TAIL_SEC} s), "
      f"Pfad: {'SCHNELL (' + str(core.CHORD_FAST_INTERVAL) + ' s-Takt)' if core.CHORD_FAST else 'normal (1 s-Takt)'}, "
      f"Messdauer {DUR:.0f} s je Datei")
for path, true_bpm, true_key in files:
    y, _ = librosa.load(os.path.join(HERE, path), sr=SR, mono=True,
                        duration=DUR)
    scale = key_scale(true_key)
    bpm_hist = deque(maxlen=core.BPM_MEDIAN_LEN)
    chord_disp = "—"
    tracker = core.ChordTracker()
    tuner = core.TuningEstimator()
    # Tonart-Verfolgung wie im Worker (EMA + Gesamtmittel + Hysterese):
    # der Tonart-Prior des Trackers bekommt die ERKANNTE Tonart -- die
    # anfangs auch falsch sein kann --, nicht die wahre, sonst misst der
    # Proxy am echten Verhalten vorbei.
    key_a = min(1.0, STEP / core.KEY_EMA_SEC)
    ema_p = ema_b = None
    cum_p = np.zeros(12)
    cum_b = np.zeros(12)
    cum_n = 0
    key_disp, key_pend, key_pend_n = "—", None, 0
    timeline = []               # (t, angezeigter Akkord)
    n_steps = n_diatonic = n_changes = 0
    ft_next = WIN / 2           # naechster Schritt des schnellen Pfads
    ccomp, cn = 0.0, 0          # Zeitmessung des schnellen Akkord-Pfads
    t = WIN / 2
    while t <= len(y) / SR:
        seg = y[max(0, int((t - WIN) * SR)):int(t * SR)]
        y_h, y_p = core.split_harmonic_percussive(seg)
        env_fr = SR / core.ONSET_HOP
        try:
            perc_env = librosa.onset.onset_strength(
                y=y_p, sr=SR, hop_length=core.ONSET_HOP)
        except Exception:
            perc_env = None
        prev = float(np.median(bpm_hist)) if bpm_hist else 0.0
        bpm = 0.0
        if perc_env is not None:
            bpm = core.fold_bpm(
                core._tempo_from_onset_env(perc_env, env_fr, prev), prev)
        if bpm <= 0:
            bpm = core.fold_bpm(core.estimate_tempo(seg, SR, prev), prev)
        if bpm > 0:
            bpm_hist.append(bpm)
        tuning = tuner.update(y_h, SR)
        # Im schnellen Modus laesst die grosse Analyse ihr Akkordfenster
        # weg (wie im Worker); der Akkord kommt aus den Fast-Schritten.
        tail = 0.0 if core.CHORD_FAST else core.chord_tail_sec(
            perc_env, env_fr, prev if prev > 0 else bpm)
        res = core.chroma_pcp(seg, SR, y_harm=y_h, tail_sec=tail,
                              tuning=tuning)
        if res is not None:
            p, b = res[0], res[1]
            ema_p = p if ema_p is None else (1 - key_a) * ema_p + key_a * p
            ema_b = b if ema_b is None else (1 - key_a) * ema_b + key_a * b
            cum_p = cum_p + p
            cum_b = cum_b + b
            cum_n += 1
        if cum_n:
            cand_key, _m, _2 = core.classify_key(
                0.5 * ema_p + 0.5 * cum_p / cum_n,
                0.5 * ema_b + 0.5 * cum_b / cum_n, with_margin=True)
        else:
            cand_key = "—"
        if cand_key != key_disp:
            if key_disp == "—" or cand_key == "—":
                key_disp, key_pend, key_pend_n = cand_key, None, 0
            elif cand_key == key_pend:
                key_pend_n += 1
                if key_pend_n >= core.KEY_SWITCH_CONFIRM:
                    key_disp, key_pend, key_pend_n = cand_key, None, 0
            else:
                key_pend, key_pend_n = cand_key, 1
        else:
            key_pend, key_pend_n = None, 0
        if core.CHORD_FAST:
            # Schnellen Pfad nachbilden: leichte Akkord-Analysen im
            # CHORD_FAST_INTERVAL-Takt auf den juengsten Sekunden.
            while ft_next <= t:
                a = max(0, int((ft_next - core.CHORD_FAST_WIN) * SR))
                seg_f = y[a:int(ft_next * SR)]
                c0 = time.perf_counter()
                fres = core.chroma_pcp_fast(seg_f, SR, tuning=tuning)
                ccomp += time.perf_counter() - c0
                cn += 1
                if fres is not None:
                    alt = (fres[2], fres[3]) if fres[2] is not None else None
                    cand = tracker.update(fres[0], fres[1], key=key_disp,
                                          dt=core.CHORD_FAST_INTERVAL,
                                          alt=alt)
                    if cand != "—" and cand != chord_disp:
                        chord_disp = cand
                        n_changes += 1
                    if chord_disp != "—":
                        n_steps += 1
                        pcs = chord_pcs(chord_disp)
                        if pcs is not None and pcs <= scale:
                            n_diatonic += 1
                        timeline.append((ft_next, chord_disp))
                ft_next += core.CHORD_FAST_INTERVAL
        elif res is not None and len(res) > 2:
            cand = tracker.update(res[2], res[3], key=key_disp)
            if cand != "—" and cand != chord_disp:
                chord_disp = cand
                n_changes += 1
            if chord_disp != "—":
                n_steps += 1
                pcs = chord_pcs(chord_disp)
                if pcs is not None and pcs <= scale:
                    n_diatonic += 1
                timeline.append((t, chord_disp))
        t += STEP

    minutes = max(1e-9, (len(y) / SR - WIN / 2) / 60.0)
    dia = 100.0 * n_diatonic / n_steps if n_steps else 0.0
    extra = f" | {ccomp / cn * 1000:4.0f} ms/Akkord-Analyse" if cn else ""
    print(f"  {path:24s} ({true_key:8s}) "
          f"diatonisch {dia:5.1f} % ({n_diatonic}/{n_steps})"
          f" | Wechsel/min {n_changes / minutes:5.1f}{extra}")
    if SHOW_TIMELINE:
        out, last = [], None
        for tt, ch in timeline:
            if ch != last:
                out.append(f"{tt:5.1f}s {ch}")
                last = ch
        print("      " + "  ".join(out))

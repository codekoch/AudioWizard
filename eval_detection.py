#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_detection.py -- Messwerkzeug fuer die Erkennungsqualitaet.

Simuliert den Analyse-Lauf des Workers ueber Testdateien und misst, wann
BPM (Toleranz 2 %) und Tonart dauerhaft korrekt angezeigt wuerden. Damit
laesst sich jede Aenderung an den Stellschrauben in
realtime_bpm_key_midiclock.py nachmessen statt nach Gefuehl zu drehen.

Testdateien muessen nach dem Muster  <BPM>BPM_<Tonart>.mp3  benannt sein,
z. B.  106BPM_C_Dur.mp3  oder  72BPM_D_Moll.mp3  (auch .wav/.flac/.ogg).

Aufruf:
    python eval_detection.py              # alle Testdateien im Skriptordner
    python eval_detection.py 120          # nur die ersten 120 s je Datei

HINWEIS: Die Anzeige-Logik (EMA + Gesamtmittel, Hysterese, Median, Flush)
ist hier bewusst aus analysis_worker() nachgebildet -- wer dort etwas
aendert, muss es hier nachziehen, sonst misst das Werkzeug am Code vorbei.
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
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 75.0

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

w = np.zeros(int(SR * WIN), dtype=np.float32)
w[::SR // 4] = 0.5
core.estimate_tempo(w, SR)
core.chroma_pcp(w, SR)

print(f"BPM-Bereich {core.MIN_BPM:.0f}-{core.MAX_BPM:.0f}, "
      f"Fenster {WIN:.0f} s, Messdauer {DUR:.0f} s je Datei")
for path, true_bpm, true_key in files:
    y, _ = librosa.load(os.path.join(HERE, path), sr=SR, mono=True,
                        duration=DUR)
    bpm_hist = deque(maxlen=core.BPM_MEDIAN_LEN)
    key_a = min(1.0, STEP / core.KEY_EMA_SEC)
    ema_p = ema_b = None
    cum_p = np.zeros(12)
    cum_b = np.zeros(12)
    cum_n = 0
    key_disp, key_pend, key_pend_n = "—", None, 0
    timeline = []
    comp = 0.0
    margin = 0.0
    raw_out = 0
    raw_n = 0
    t = WIN / 2
    while t <= len(y) / SR:
        seg = y[max(0, int((t - WIN) * SR)):int(t * SR)]
        c0 = time.perf_counter()
        y_h, y_p = core.split_harmonic_percussive(seg)
        prev = float(np.median(bpm_hist)) if bpm_hist else 0.0
        bpm = core.fold_bpm(core.estimate_tempo(y_p, SR, prev))
        if bpm <= 0:
            bpm = core.fold_bpm(core.estimate_tempo(seg, SR, prev))
        res = core.chroma_pcp(seg, SR, y_harm=y_h)
        comp += time.perf_counter() - c0
        if bpm > 0:
            raw_n += 1
            if abs(bpm - true_bpm) / true_bpm > 0.03:
                raw_out += 1
        if res is not None:
            p, b = res
            ema_p = p if ema_p is None else (1 - key_a) * ema_p + key_a * p
            ema_b = b if ema_b is None else (1 - key_a) * ema_b + key_a * b
            cum_p = cum_p + p
            cum_b = cum_b + b
            cum_n += 1
        if cum_n:
            cand, margin = core.classify_key(
                0.5 * ema_p + 0.5 * cum_p / cum_n,
                0.5 * ema_b + 0.5 * cum_b / cum_n, with_margin=True)
        else:
            cand, margin = "—", 0.0
        if cand != key_disp:
            if key_disp == "—" or cand == "—":
                key_disp, key_pend, key_pend_n = cand, None, 0
            elif cand == key_pend:
                key_pend_n += 1
                if key_pend_n >= core.KEY_SWITCH_CONFIRM:
                    key_disp, key_pend, key_pend_n = cand, None, 0
            else:
                key_pend, key_pend_n = cand, 1
        else:
            key_pend, key_pend_n = None, 0
        if bpm > 0:
            bpm_hist.append(bpm)
            if len(bpm_hist) >= 10:
                recent = list(bpm_hist)[-5:]
                rmed = float(np.median(recent))
                omed = float(np.median(bpm_hist))
                ratio = rmed / omed
                alias = any(abs(ratio / h - 1.0) < 0.04
                            for h in (4 / 3, 3 / 2, 2 / 3, 3 / 4))
                if (max(recent) / min(recent) - 1.0) < 0.03 and \
                        abs(rmed - omed) / omed > core.TEMPO_FLUSH_DEV and \
                        not alias:
                    while len(bpm_hist) > 5:
                        bpm_hist.popleft()
        tgt = float(np.median(bpm_hist)) if bpm_hist else 0.0
        timeline.append((t, tgt, key_disp))
        t += STEP

    def lock_time(good):
        lock = None
        for tt, b, k in timeline:
            if good(b, k):
                if lock is None:
                    lock = tt
            else:
                lock = None
        return lock

    bl = lock_time(lambda b, k: b > 0 and abs(b - true_bpm) / true_bpm <= 0.02)
    kl = lock_time(lambda b, k: k == true_key)
    tt, b_end, k_end = timeline[-1]
    bl_s = f"{bl:5.1f} s" if bl else "  nie  "
    kl_s = f"{kl:5.1f} s" if kl else "  nie  "
    print(f"  {path:24s} BPM-Lock: {bl_s}"
          f" | Tonart-Lock: {kl_s}"
          f" | Ende: {b_end:6.1f} BPM, {k_end:8s} (Vorsprung {margin:.3f})"
          f" | Roh-Ausreisser {raw_out}/{raw_n}"
          f" | {comp / len(timeline) * 1000:4.0f} ms/Analyse")

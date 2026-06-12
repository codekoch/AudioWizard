# -*- coding: utf-8 -*-
"""End-to-End-Test des Beat-Sync: synthetischer 120-BPM-Klicktrack laeuft in
Echtzeit durch feed_analysis -> analysis_worker -> clock_worker (FakeMidi).
Gemessen wird die Phasenlage der Beat-Ticks (jeder 24. Tick) relativ zu den
bekannten Klick-Zeiten: der MITTELWERT (konstante Latenz) ist egal, die
STREUUNG ist das, was man als "Schwanken" hoert."""
import threading
import time

import numpy as np

import realtime_bpm_key_midiclock as core

SR = 48000              # wie Loopback -> Resampling-Pfad wird mitgetestet
CHUNK = 4096
BPM = 120.0
PERIOD = 60.0 / BPM
DUR = 26.0


class FakeMidi:
    def __init__(self):
        self.ticks = []
        self.starts = []

    def send(self, msg):
        if msg.type == 'clock':
            self.ticks.append(time.perf_counter())
        elif msg.type == 'start':
            self.starts.append(time.perf_counter())


def make_block(start_sample):
    """CHUNK Samples ab start_sample: Klick (8 ms Rauschburst) auf jedem
    Beat plus leiser Dauerton (haelt den Pegel ueber der Stille-Schwelle)."""
    n = np.arange(start_sample, start_sample + CHUNK)
    y = 0.05 * np.sin(2 * np.pi * 220.0 * n / SR).astype(np.float64)
    click_len = int(0.008 * SR)
    period_smp = int(PERIOD * SR)
    phase = n % period_smp
    mask = phase < click_len
    if np.any(mask):
        rng = np.random.default_rng(int(start_sample))
        y[mask] += 0.8 * rng.standard_normal(int(mask.sum()))
    return y.astype(np.float32)


print("Waerme librosa/numba vor ...")
_w = np.zeros(int(core.ANALYSIS_SR * core.WINDOW_SECONDS), dtype=np.float32)
_w[::core.ANALYSIS_SR // 4] = 0.5
core.estimate_tempo(_w, core.ANALYSIS_SR)
core.split_harmonic_percussive(_w)
core.chroma_pcp(_w, core.ANALYSIS_SR)

shared = core.Shared()
shared.capture_sr = float(SR)
shared.beat_sync = True
audio_q = core.queue.Queue()
stop = threading.Event()
midi = FakeMidi()

threading.Thread(target=core.analysis_worker_safe,
                 args=(shared, audio_q, stop), daemon=True).start()
threading.Thread(target=core.clock_worker,
                 args=(shared, midi, stop), daemon=True).start()

print(f"Spiele {DUR:.0f} s Klicktrack @ {BPM:.0f} BPM in Echtzeit ein ...")
t0 = time.perf_counter()
pos = 0
feed_log = []                   # (wall_time_des_blockendes, end_sample)
while pos < DUR * SR:
    block = make_block(pos)
    pos += CHUNK
    target_t = t0 + pos / SR
    while time.perf_counter() < target_t:
        time.sleep(0.001)
    core.feed_analysis(audio_q, block)
    core.update_level(shared, block)
    feed_log.append((time.perf_counter(), pos))
stop.set()
time.sleep(0.3)

assert midi.starts, "Clock ist nie gestartet"
print(f"Clock-Starts: {len(midi.starts)}, Ticks: {len(midi.ticks)}")

# Wanduhr-Zeiten der Klicks rekonstruieren (Sample -> Feed-Zeitstempel)
feed_t = np.array([f[0] for f in feed_log])
feed_end = np.array([f[1] for f in feed_log])
period_smp = int(PERIOD * SR)
click_samples = np.arange(0, int(DUR * SR), period_smp)
idx = np.searchsorted(feed_end, click_samples, side='left')
idx = np.clip(idx, 0, len(feed_end) - 1)
click_t = feed_t[idx] - (feed_end[idx] - click_samples) / SR

# Beat-Ticks: jeder 24. Tick nach dem letzten 'start'
ticks = np.array(midi.ticks)
ticks = ticks[ticks > midi.starts[-1] - 1e-6]
beats = ticks[0::core.PPQN]
# Auswertung erst nach Einschwingen (Anker-EMA + Nudge): letzte ~12 s
beats = beats[beats > t0 + DUR - 12.0]
assert len(beats) >= 16, f"zu wenige Beats erfasst: {len(beats)}"

# Phasenfehler jedes Beat-Ticks zum naechstgelegenen Klick, auf
# +-PERIOD/2 gefaltet
base = click_t[0]
err = (beats - base + PERIOD / 2) % PERIOD - PERIOD / 2
print(f"\nBeat-Tick vs. Klick (letzte 12 s, n={len(beats)}):")
print(f"  konstanter Versatz (egal):   {err.mean()*1000:+7.1f} ms")
print(f"  Schwankung (std):            {err.std()*1000:7.1f} ms")
print(f"  Spitze-Spitze:               {(err.max()-err.min())*1000:7.1f} ms")
bb = np.diff(beats)
print(f"  Beat-zu-Beat-Periode: mean={bb.mean()*1000:.2f} ms "
      f"std={bb.std()*1000:.2f} ms (Soll {PERIOD*1000:.2f} ms)")

assert err.std() < 0.012, f"Phase schwankt zu stark: std={err.std()*1000:.1f} ms"
assert abs(bb.mean() - PERIOD) < PERIOD * 0.004, "Tempo daneben"
print("\nOK: Clock haelt Tempo und Beat-Phase stabil.")

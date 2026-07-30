#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bpm_key_display.py
==================

Fullscreen-Anzeige (BPM + Tonart) fuer ein 7-Zoll-Display (800x600),
gedacht fuer den Raspberry Pi -- laeuft zum Testen aber genauso unter
Windows und macOS in einem normalen Fenster.

Der Analyse- und MIDI-Clock-Kern wird aus realtime_bpm_key_midiclock.py
importiert (gleiche Logik, eine Codebasis). Dieses Skript ersetzt nur die
Konsolen-Bedienung durch eine Touch-taugliche Oberflaeche:

  * Erststart: Auswahlbildschirm fuer Audio-Eingang und MIDI-Ausgang.
    Die Wahl wird in display_config.json gespeichert; danach startet das
    Programm direkt in die Anzeige (Kiosk-Betrieb).
  * Unter Windows stehen zusaetzlich "Loopback:"-Eintraege in der Liste
    (Ausgabe mithoeren, z. B. Spotify; braucht das Paket 'soundcard').
    Auf dem Pi uebernehmen das die PipeWire/Pulse-"Monitor"-Eingaenge,
    die als normale Eingaenge erscheinen; unter macOS ein virtuelles
    Ausgabegeraet wie BlackHole (erscheint ebenfalls als Eingang).
  * macOS/Linux: In der MIDI-Liste laesst sich zusaetzlich ein eigener
    virtueller Port erzeugen (CoreMIDI/ALSA) -- kein IAC/loopMIDI noetig.
  * Hauptbildschirm: BPM gross, Tonart darunter, Pegelbalken, Status.

Start:
    python bpm_key_display.py                # Pi: Vollbild, Windows: Fenster
    python bpm_key_display.py --fullscreen   # Vollbild erzwingen
    python bpm_key_display.py --windowed     # Fenster erzwingen
    python bpm_key_display.py --setup        # Auswahlbildschirm erzwingen

Tasten:  F11 = Vollbild umschalten,  Esc = Beenden.
"""

import json
import math
import os
import queue
import re
import sys
import threading
import time
import traceback

import numpy as np

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    sys.exit("Tkinter fehlt. Raspberry Pi OS: sudo apt install python3-tk")

try:
    import sounddevice as sd
except ImportError:
    sys.exit("Fehlt: 'sounddevice'. Installiere mit: pip install sounddevice")

import mido

import realtime_bpm_key_midiclock as core
import deluge_export as deluge

# Windows: Die Wiedergabe (z. B. Spotify) laesst sich per Loopback mithoeren.
# Auf dem Raspberry Pi ist das ueberfluessig -- dort erscheinen die
# PipeWire/Pulse-"Monitor"-Quellen als normale Eingaenge in der Geraeteliste.
# Unter macOS uebernimmt das ein virtuelles Ausgabegeraet wie BlackHole,
# das ebenfalls als normaler Eingang erscheint.
sc = None
if sys.platform == 'win32':
    try:
        import warnings
        import soundcard as sc
        # soundcard schaltet beim Import seine Warnungen auf 'always' und
        # ueberschreibt damit den Filter des Kernmoduls -> erneut daempfen.
        warnings.filterwarnings("ignore",
                                message="data discontinuity in recording")
    except Exception:
        sc = None


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "display_config.json")

# Betriebsart: Tempo/Clock oder Noten-Modus (Pitch -> MIDI)
MODE_LABELS = {"clock": "Tempo & MIDI-Clock",
               "mono": "Noten → MIDI (monophon)",
               "poly": "Noten → MIDI (polyphon)",
               "chord": "Noten → MIDI (Akkorde)"}
MODE_FROM_LABEL = {v: k for k, v in MODE_LABELS.items()}

# Farbschema (dunkles Kiosk-Display)
COL_BG      = "#16161a"   # Hintergrund
COL_FG      = "#F1EFE8"   # Hauptschrift (BPM)
COL_MUTED   = "#888780"   # Beschriftungen / Nebentext
COL_ACCENT  = "#9FE1CB"   # Tonart
COL_OK      = "#5DCAA5"   # Status "laeuft" / Pegelbalken
COL_WARN    = "#EF9F27"   # Status "kein Signal"
COL_BAR_BG  = "#2c2c2a"   # Pegelbalken-Hintergrund
COL_SURFACE = "#222226"   # Listen/Buttons im Setup
COL_SURF_HI = "#33333a"   # Hover/Active


def _parse_time_str(s):
    """Zeitangabe aus einem Eingabefeld lesen -- absichtlich TOLERANT:
    '83', '83,4', '83.456', '1:23.456', '1:02:03.5' (h:mm:ss) sind alle gueltig,
    Leerzeichen und ein angehaengtes 's' stoeren nicht. Rueckgabe Sekunden als
    float (volle Genauigkeit) oder None, wenn nichts Sinnvolles drinsteht."""
    s = str(s).strip().replace(",", ".").rstrip("sS ").strip()
    if not s:
        return None
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3:
        return None
    t = 0.0
    for p in parts:                       # 83 | mm:ss | h:mm:ss
        t = t * 60.0 + p
    return t if t >= 0 else None


def _fmt_time(t):
    """Sekunden als m:ss.mmm (millisekundengenau, so wie eingegeben werden darf)."""
    t = max(0.0, float(t))
    m, s = divmod(t, 60.0)
    return f"{int(m)}:{s:06.3f}"


def parallel_key(key):
    """Paralleltonart zu 'C Dur' / 'A Moll' usw.; '' wenn nicht bestimmbar."""
    parts = key.split()
    if len(parts) != 2 or parts[0] not in core.NOTE_NAMES:
        return ""
    i = core.NOTE_NAMES.index(parts[0])
    if parts[1] == "Dur":
        return f"{core.NOTE_NAMES[(i + 9) % 12]} Moll"
    if parts[1] == "Moll":
        return f"{core.NOTE_NAMES[(i + 3) % 12]} Dur"
    return ""


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Konfiguration konnte nicht gespeichert werden: {e}]")


class DisplayApp:
    def __init__(self, root, fullscreen, force_setup=False):
        self.root = root
        root.title("AudioWizard")
        root.configure(bg=COL_BG)
        # Hauptfenster mittig auf dem Bildschirm platzieren (im Fenstermodus;
        # bei Vollbild ueberschreibt set_fullscreen das ohnehin).
        win_w, win_h = 800, 600
        cx = max(0, (root.winfo_screenwidth() - win_w) // 2)
        cy = max(0, (root.winfo_screenheight() - win_h) // 2)
        root.geometry(f"{win_w}x{win_h}+{cx}+{cy}")
        root.minsize(480, 360)
        root.protocol("WM_DELETE_WINDOW", self.quit_app)
        root.bind("<F11>", lambda e: self.set_fullscreen(not self.fullscreen))
        root.bind("<Escape>", lambda e: self.quit_app())
        root.bind("<space>", lambda e: self.toggle_hold())
        root.bind("<Configure>", self._on_resize)

        # ---- Laufzeit-Zustand (Analyse-Kern) ----
        self.shared = core.Shared()
        self.audio_q = core.queue.Queue()
        self.app_stop = threading.Event()     # beendet den Analyse-Thread
        self.analysis_thread = None
        self.stream = None                    # sounddevice-InputStream
        self.cap_thread = None                # Loopback-Aufnahme-Thread
        self.cap_stop = None
        self.clock_stop = None
        self.clock_thread = None
        self.note_stop = None                 # Noten-Modus: Worker-Stop-Event
        self.note_thread = None
        self.midi_out = None
        self.midi_name = None
        self.warmed = False
        self.status_override = None           # z. B. "Initialisiere ..."
        self._begin_args = None               # vom Warmup-Thread gesetzt;
                                              # _tick() startet dann die Session
                                              # (Tk darf nur im Main-Thread laufen)
        self._session_gen = 0                 # Generationszaehler: entwertet
                                              # einen noch laufenden Warmup,
                                              # wenn die Session inzwischen
                                              # gestoppt wurde (sonst koennten
                                              # zwei Sessions parallel starten
                                              # -> doppelte Clock/Aufnahme)
        self._last_height = 0
        self._last_width = 0
        self._bpm_big = True                  # BPM-Label gerade gross/aktiv?
        self.hold = False                     # Analyse eingefroren?
        # ---- Datei-Modus (Datei -> MIDI-Clock, driftfrei) ----
        self.file_mode = False                # Datei-Wiedergabe statt Live-Analyse?
        self.file_player = None               # core.FilePlayer
        self.file_clock_stop = None
        self.file_clock_thread = None
        self.file_midi = None                 # eigener MIDI-Ausgang im Datei-Modus
        self.file_audio = None                # dekodierter Puffer (fuer Start/Stop)
        self.file_sr = 0                      # dessen Samplerate
        self._file_playing = False            # laeuft die Datei-Wiedergabe gerade?
        self.file_info = None                 # Beat-Map-dict (beats/ticks/bpm/...)
        self.file_name = ""
        self.file_key = "—"
        self.file_key_conf = False
        self._file_begin_args = None          # vom Analyse-Thread gesetzt;
                                              #   _tick() startet die Wiedergabe
        # ---- Aufnahme (Mitschnitt der Live-Analyse + Speichern) ----
        self.rec_start_perf = 0.0
        self._rec_audio = None                # fertiger Mitschnitt (Mono)
        self._rec_sr = 0
        self._rec_segs = None                 # vom Segmentier-Thread gesetzt
        self._rec_name_vars = []
        self._rec_save_win = None
        # ---- DJ-Modus (zwei Decks, Crossfade, Clock folgt) ----
        self.dj_engine = None
        self.dj_clock_stop = None
        self.dj_clock_thread = None
        self.dj_midi = None
        self.dj_win = None
        self.dj_w = [{}, {}]                  # Widget-Referenzen je Deck
        self._dj_load_res = None              # (idx, audio, sr, info, key, name)
        self._dj_stems_res = None             # (idx, stems, sr, err) vom Trenn-Thread
        self._stem_players = []               # offene StemPlayer
        self._midi_players = []               # (MultiStemMidiPlayer, port) MIDI-Datei
        # GEMEINSAMER MIDI-Ausgang: WinMM-Ports (z. B. "GS Wavetable Synth") sind
        # single-client -> nur EINMAL oeffnen und per Referenzzaehler teilen.
        self._midi_shared = {"name": None, "port": None, "refs": 0}
        self._midi_lock = threading.Lock()
        self._material_res = None             # (out|None, err) vom Verarbeitungs-Thread
        self._material_clock = None           # Datei-Pfad: Clock NACH Verarbeitung
        self._material_queue = []             # weitere Stuecke nacheinander (Aufnahme)
        self._load_options()                  # Optionen + BPM-Bereich anwenden

        # ---- Schriften (Groesse wird bei Resize angepasst) ----
        self.f_bpm     = tkfont.Font(family="Helvetica", size=-160)
        self.f_key     = tkfont.Font(family="Helvetica", size=-60)
        self.f_key_par = tkfont.Font(family="Helvetica", size=-26)
        self.f_cap   = tkfont.Font(family="Helvetica", size=-16)
        self.f_small = tkfont.Font(family="Helvetica", size=-14)
        self.f_h1    = tkfont.Font(family="Helvetica", size=-26)
        self.f_list  = tkfont.Font(family="Helvetica", size=-17)
        self.f_btn   = tkfont.Font(family="Helvetica", size=-16)
        self.f_tiny  = tkfont.Font(family="Helvetica", size=-11)

        self._build_main_frame()
        self._build_setup_frame()

        self.fullscreen = False
        if fullscreen:
            self.set_fullscreen(True)

        self._tick()

        # ---- Autostart, falls gespeicherte Geraete vorhanden sind ----
        cfg = load_config()
        auto = None
        if not force_setup and cfg.get("input_name"):
            src = self._find_saved_source(cfg)
            midi = cfg.get("midi_output") or None
            if (midi and midi != core.VIRTUAL_MIDI
                    and midi not in mido.get_output_names()):
                midi = "?"                    # gespeicherter Port fehlt
            if src is not None and midi != "?":
                auto = (src, midi)
        if auto is not None:
            self.start_session(*auto)
        else:
            self.show_setup()

    def _load_options(self):
        """Anzeige-Optionen und BPM-Suchbereich aus der Konfiguration lesen
        und den Suchbereich direkt im Analyse-Kern setzen."""
        cfg = load_config()
        self.opt_bpm_decimal = bool(cfg.get("bpm_dezimal", False))
        self.opt_beat_sync = bool(cfg.get("beat_sync", False))
        mode = cfg.get("note_mode", "clock")
        self.opt_note_mode = mode if mode in MODE_LABELS else "clock"
        self.opt_chords = bool(cfg.get("akkorde", False))
        self.opt_chord_log = bool(cfg.get("akkorde_datei", False))
        self.opt_chord_fast = bool(cfg.get("akkorde_schnell", False))
        # Akkorde berechnen, sobald Anzeige ODER Protokoll sie braucht;
        # geschrieben wird nur, wenn das Protokoll gewaehlt ist.
        core.CHORD_ENABLED = self.opt_chords or self.opt_chord_log
        core.CHORD_LOG_PATH = (core.CHORD_LOG_FILE
                               if self.opt_chord_log else None)
        core.CHORD_FAST = self.opt_chord_fast
        try:
            mn = float(cfg.get("min_bpm", 70))
            mx = float(cfg.get("max_bpm", 140))
        except (TypeError, ValueError):
            mn, mx = 70.0, 140.0
        if not (30.0 <= mn < mx <= 300.0):
            mn, mx = 70.0, 140.0
        self.opt_min_bpm, self.opt_max_bpm = mn, mx
        core.MIN_BPM = mn
        core.MAX_BPM = mx
        # Tempo-Prior in die Mitte des Bereichs legen (geometrisch)
        core.TEMPO_CENTER_BPM = math.sqrt(mn * mx)

    def _find_input_by_name(self, name):
        """sounddevice-Index zum gespeicherten Geraetenamen; None wenn weg."""
        try:
            for idx, _label in core._list_io_devices('in'):
                if sd.query_devices(idx)['name'] == name:
                    return idx
        except Exception:
            pass
        return None

    def _find_saved_source(self, cfg):
        """Gespeicherte Quelle aufloesen: ('input', sd-Index) oder
        ('loopback', Lautsprechername); None, wenn nicht mehr vorhanden."""
        name = cfg.get("input_name")
        if cfg.get("input_type", "input") == "loopback":
            if sc is None:
                return None
            try:
                for sp in sc.all_speakers():
                    if sp.name == name:
                        return ("loopback", name)
            except Exception:
                pass
            return None
        idx = self._find_input_by_name(name)
        return None if idx is None else ("input", idx)

    # ------------------------------------------------------------------
    # Oberflaeche: Hauptbildschirm
    # ------------------------------------------------------------------
    def _build_main_frame(self):
        f = tk.Frame(self.root, bg=COL_BG)
        self.main_frame = f
        f.columnconfigure(0, weight=1)
        for r in (1, 4, 7):                   # Abstandshalter-Zeilen
            f.rowconfigure(r, weight=1)

        top = tk.Frame(f, bg=COL_BG)
        top.grid(row=0, column=0, sticky="ew", padx=24, pady=(16, 0))
        self.src_label = tk.Label(top, text="", font=self.f_small,
                                  bg=COL_BG, fg=COL_MUTED, anchor="w")
        self.src_label.pack(side="left")
        self.status_label = tk.Label(top, text="", font=self.f_small,
                                     bg=COL_BG, fg=COL_MUTED, anchor="e")
        self.status_label.pack(side="right")

        self.bpm_label = tk.Label(f, text="—", font=self.f_bpm,
                                  bg=COL_BG, fg=COL_FG)
        self.bpm_label.grid(row=2, column=0)
        self.bpm_cap_label = tk.Label(f, text="BPM", font=self.f_cap,
                                      bg=COL_BG, fg=COL_MUTED)
        self.bpm_cap_label.grid(row=3, column=0)

        # Tonart und (optional) Akkord nebeneinander, je mit eigener
        # Beschriftung; der Akkord-Block wird in show_main() nur gepackt,
        # wenn die Option aktiv ist.
        keyarea = tk.Frame(f, bg=COL_BG)
        keyarea.grid(row=5, column=0)
        keyblock = tk.Frame(keyarea, bg=COL_BG)
        keyblock.pack(side="left")
        keyrow = tk.Frame(keyblock, bg=COL_BG)
        keyrow.pack()
        self.key_label = tk.Label(keyrow, text="—", font=self.f_key,
                                  bg=COL_BG, fg=COL_ACCENT)
        self.key_label.pack(side="left", anchor="s")
        self.key_par_label = tk.Label(keyrow, text="", font=self.f_key_par,
                                      bg=COL_BG, fg=COL_MUTED)
        self.key_par_label.pack(side="left", anchor="s", pady=(0, 8))
        tk.Label(keyblock, text="TONART", font=self.f_cap,
                 bg=COL_BG, fg=COL_MUTED).pack()
        self.chord_block = tk.Frame(keyarea, bg=COL_BG)
        self.chord_label = tk.Label(self.chord_block, text="—",
                                    font=self.f_key, bg=COL_BG, fg=COL_MUTED)
        self.chord_label.pack()
        tk.Label(self.chord_block, text="AKKORD", font=self.f_cap,
                 bg=COL_BG, fg=COL_MUTED).pack()

        lvl = tk.Frame(f, bg=COL_BG)
        lvl.grid(row=8, column=0, sticky="ew", padx=24, pady=(0, 4))
        self.level_cap_label = tk.Label(lvl, text="PEGEL", font=self.f_small,
                                        bg=COL_BG, fg=COL_MUTED)
        self.level_cap_label.pack(side="left")
        self.db_label = tk.Label(lvl, text="-60 dB", font=self.f_small,
                                 bg=COL_BG, fg=COL_MUTED, width=7, anchor="e")
        self.db_label.pack(side="right")
        self.level_canvas = tk.Canvas(lvl, height=12, bg=COL_BAR_BG,
                                      highlightthickness=0, bd=0)
        self.level_canvas.pack(side="left", fill="x", expand=True, padx=12)
        self.level_rect = self.level_canvas.create_rectangle(
            0, 0, 0, 14, fill=COL_OK, width=0)

        # Zwei Reihen, damit die Knoepfe auch auf dem 7-Zoll-Display (800 px)
        # nicht aus dem Bild laufen: oben Live-Analyse + Navigation, unten die
        # Quellen/Modi (Datei/Aufnahme/DJ).
        btns = tk.Frame(f, bg=COL_BG)
        btns.grid(row=9, column=0, sticky="ew", padx=24, pady=(0, 12))
        row1 = tk.Frame(btns, bg=COL_BG)
        row1.pack(fill="x")
        row2 = tk.Frame(btns, bg=COL_BG)
        row2.pack(fill="x", pady=(8, 0))

        def _ctl(parent, text, cmd):
            return tk.Button(parent, text=text, command=cmd, font=self.f_small,
                             bg=COL_SURFACE, fg=COL_FG,
                             activebackground=COL_SURF_HI,
                             activeforeground=COL_FG, bd=0, padx=16, pady=6,
                             highlightthickness=0, takefocus=0, cursor="hand2")

        self.hold_btn = _ctl(row1, "Analyse anhalten", self.toggle_hold)
        self.hold_btn.pack(side="left")
        self.reset_btn = _ctl(row1, "Analyse neu starten", self.reset_analysis)
        self.reset_btn.pack(side="left", padx=(8, 0))
        self._small_button(row1, "Beenden", self.quit_app).pack(side="right")
        self._small_button(row1, "Einstellungen",
                           self.on_settings).pack(side="right", padx=(0, 8))

        self.file_btn = _ctl(row2, "Datei (Audio/MIDI) …", self.on_load_file)
        self.file_btn.pack(side="left")
        self.rec_btn = _ctl(row2, "● Aufnahme", self.toggle_record)
        self.rec_btn.pack(side="left", padx=(8, 0))
        self.dj_btn = _ctl(row2, "DJ", self.open_dj)
        self.dj_btn.pack(side="left", padx=(8, 0))
        # Transport fuer den Datei-Modus (Start/Stopp der Wiedergabe + Clock);
        # nur im Datei-Modus sichtbar (sonst laeuft die Datei nicht von allein los).
        self.file_play_btn = _ctl(row2, "▶ Start", self._file_toggle)

    def _small_button(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, font=self.f_small,
                         bg=COL_BG, fg=COL_MUTED, activebackground=COL_SURFACE,
                         activeforeground=COL_FG, bd=0, padx=10, pady=4,
                         highlightthickness=0, cursor="hand2")

    # ------------------------------------------------------------------
    # Oberflaeche: Auswahlbildschirm
    # ------------------------------------------------------------------
    def _build_setup_frame(self):
        f = tk.Frame(self.root, bg=COL_BG)
        self.setup_frame = f

        tk.Label(f, text="Einstellungen", font=self.f_h1,
                 bg=COL_BG, fg=COL_FG).pack(pady=(20, 2))
        tk.Label(f, text="Quelle + MIDI-Ausgang wählen, dann „Start“ – oder direkt "
                 "eine Datei (Audio/MIDI) laden.", font=self.f_small, bg=COL_BG,
                 fg=COL_MUTED).pack(pady=(0, 12))

        body = tk.Frame(f, bg=COL_BG)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(1, weight=1)

        tk.Label(body, text="Audio-Eingang", font=self.f_cap, bg=COL_BG,
                 fg=COL_MUTED, anchor="w").grid(row=0, column=0,
                                                sticky="w", pady=(0, 6))
        tk.Label(body, text="MIDI-Ausgang", font=self.f_cap, bg=COL_BG,
                 fg=COL_MUTED, anchor="w").grid(row=0, column=1,
                                                sticky="w", padx=(16, 0),
                                                pady=(0, 6))
        # height=4: kleine MINDESThoehe -- die Listen wachsen per grid-weight
        # mit dem Fenster, druecken aber an kleinen Fenstern nicht mehr die
        # Optionen und Buttons aus dem Bild (Listbox-Standard waere 10 Zeilen).
        kw = dict(font=self.f_list, bg=COL_SURFACE, fg=COL_FG, height=4,
                  selectbackground="#1D9E75", selectforeground="#04342C",
                  highlightthickness=0, bd=0, activestyle="none",
                  exportselection=False)
        self.lb_in = tk.Listbox(body, **kw)
        self.lb_in.grid(row=1, column=0, sticky="nsew")
        self.lb_midi = tk.Listbox(body, **kw)
        self.lb_midi.grid(row=1, column=1, sticky="nsew", padx=(16, 0))
        # Direkt unter der Liste: gewaehlten MIDI-Ausgang testen (hoerbare Sequenz).
        self._small_button(body, "▶ MIDI-Ausgang testen",
                           self._test_midi_output).grid(
            row=2, column=1, sticky="w", padx=(16, 0), pady=(4, 0))
        if sys.platform == 'darwin':
            # macOS hat kein Loopback -- der uebliche Weg ist BlackHole.
            tk.Label(body, text="Wiedergabe mithoeren: BlackHole installieren"
                                " -- erscheint dann als Audio-Eingang.",
                     font=self.f_tiny, bg=COL_BG, fg=COL_MUTED,
                     anchor="w").grid(row=2, column=0, sticky="w",
                                      pady=(4, 0))

        # Optionen als Flow-Layout: _flow_options() bricht die Widgets je
        # nach Fensterbreite in so viele Zeilen um wie noetig. Mit den
        # frueheren zwei festen Zeilen liefen an schmalen Fenstern die
        # hinteren Checkboxen rechts aus dem Bild.
        cont = tk.Frame(f, bg=COL_BG)
        self.opts_container = cont
        self._opt_rows = []
        self._flow_pending = False
        self.var_dec = tk.BooleanVar()
        self.var_beat = tk.BooleanVar()
        self.var_chord = tk.BooleanVar()
        self.var_chordlog = tk.BooleanVar()
        self.var_chordfast = tk.BooleanVar()
        ck = dict(bg=COL_BG, fg=COL_FG, selectcolor=COL_SURFACE,
                  activebackground=COL_BG, activeforeground=COL_FG,
                  highlightthickness=0, font=self.f_small, cursor="hand2")
        rng = tk.Frame(cont, bg=COL_BG)
        tk.Label(rng, text="BPM-Bereich", font=self.f_small, bg=COL_BG,
                 fg=COL_MUTED).pack(side="left", padx=(0, 6))
        ent = dict(font=self.f_small, bg=COL_SURFACE, fg=COL_FG, width=4,
                   bd=0, insertbackground=COL_FG, justify="center")
        self.ent_min = tk.Entry(rng, **ent)
        self.ent_min.pack(side="left", ipady=3)
        tk.Label(rng, text="–", font=self.f_small, bg=COL_BG,
                 fg=COL_MUTED).pack(side="left", padx=4)
        self.ent_max = tk.Entry(rng, **ent)
        self.ent_max.pack(side="left", ipady=3)
        # Betriebsart: Tempo/Clock oder Noten-Modus (Pitch -> MIDI)
        self.var_mode = tk.StringVar(value=MODE_LABELS["clock"])
        modef = tk.Frame(cont, bg=COL_BG)
        tk.Label(modef, text="Modus", font=self.f_small, bg=COL_BG,
                 fg=COL_MUTED).pack(side="left", padx=(0, 6))
        om = tk.OptionMenu(modef, self.var_mode, *MODE_LABELS.values())
        om.config(font=self.f_small, bg=COL_SURFACE, fg=COL_FG, bd=0,
                  highlightthickness=0, activebackground=COL_SURF_HI,
                  activeforeground=COL_FG, cursor="hand2")
        om["menu"].config(bg=COL_SURFACE, fg=COL_FG)
        om.pack(side="left")
        self.opt_widgets = [
            modef,
            tk.Checkbutton(cont, text="BPM mit Nachkommastelle",
                           variable=self.var_dec, **ck),
            tk.Checkbutton(cont, text="Beat-synchrone Clock (experimentell)",
                           variable=self.var_beat, **ck),
            tk.Checkbutton(cont, text="Akkorde anzeigen",
                           variable=self.var_chord, **ck),
            tk.Checkbutton(cont,
                           text="Akkorde in Textdatei schreiben (akkorde.txt)",
                           variable=self.var_chordlog, **ck),
            tk.Checkbutton(cont,
                           text="Akkorde schneller berechnen (mehr CPU-Last)",
                           variable=self.var_chordfast, **ck),
            rng,
        ]
        self._flow_options()

        self.err_label = tk.Label(f, text="", font=self.f_small,
                                  bg=COL_BG, fg=COL_WARN)

        bottom = tk.Frame(f, bg=COL_BG)
        left = tk.Frame(bottom, bg=COL_BG)
        left.pack(side="left")
        tk.Label(left, text="F11: Vollbild   Esc: Beenden",
                 font=self.f_small, bg=COL_BG,
                 fg=COL_MUTED).pack(anchor="w")
        tk.Label(left, text="codekoch / claude", font=self.f_tiny,
                 bg=COL_BG, fg="#55544E").pack(anchor="w", pady=(2, 0))
        self._small_button(bottom, "Noten-Kalibrierung …",
                           self.open_note_calib).pack(side="left", padx=(16, 0))
        # Stapel: viele Dateien -> Play-Along-Mixe (braucht keine Live-Quelle)
        self._small_button(bottom, "Stapel: Play-Along …",
                           self.on_batch_playalong).pack(side="left", padx=(8, 0))
        # Fertige Spuren (z. B. exportierte Stems) direkt in den Part-Editor
        self._small_button(bottom, "Spuren → Part-Editor …",
                           self.on_open_tracks).pack(side="left", padx=(8, 0))
        tk.Button(bottom, text="Start", command=self.on_setup_start,
                  font=self.f_btn, bg="#1D9E75", fg="#04342C",
                  activebackground=COL_OK, activeforeground="#04342C",
                  bd=0, padx=28, pady=8, highlightthickness=0,
                  cursor="hand2").pack(side="right")
        tk.Button(bottom, text="Aktualisieren", command=self._populate_setup,
                  font=self.f_btn, bg=COL_SURFACE, fg=COL_FG,
                  activebackground=COL_SURF_HI, activeforeground=COL_FG,
                  bd=0, padx=16, pady=8, highlightthickness=0,
                  cursor="hand2").pack(side="right", padx=(0, 10))
        # Datei -> MIDI-Clock (driftfrei): braucht keine Live-Quelle, daher
        # auch direkt aus dem Setup erreichbar.
        tk.Button(bottom, text="Datei (Audio/MIDI) …", command=self.on_load_file,
                  font=self.f_btn, bg=COL_SURFACE, fg=COL_FG,
                  activebackground=COL_SURF_HI, activeforeground=COL_FG,
                  bd=0, padx=16, pady=8, highlightthickness=0,
                  cursor="hand2").pack(side="right", padx=(0, 10))

        # Pack-Reihenfolge = Prioritaet bei knappem Platz: Bedienleiste,
        # Fehlerzeile und Optionen werden zuerst (von unten) gesetzt, die
        # Geraetelisten bekommen den Rest und schrumpfen als erstes --
        # so bleiben Buttons und Checkboxen auch an kleinen Fenstern sichtbar.
        bottom.pack(side="bottom", fill="x", padx=24, pady=(6, 16))
        self.err_label.pack(side="bottom", fill="x", padx=24, pady=(8, 0))
        cont.pack(side="bottom", fill="x", padx=24, pady=(12, 0))
        body.pack(fill="both", expand=True, padx=24)

    def _flow_options(self, width=None):
        """Options-Widgets zeilenweise anordnen (Flow-Layout): in jede Zeile
        kommen so viele, wie die Fensterbreite hergibt, der Rest bricht um.
        Wird bei jeder Groessenaenderung neu berechnet (_on_resize)."""
        if width is None:
            width = self.root.winfo_width()
        if width <= 1:
            width = 800                 # vor dem ersten Mapping: Startgroesse
        avail = max(200, width - 48)    # Aussenabstand des Containers (2x24)
        for wdg in self.opt_widgets:
            wdg.pack_forget()
        for row in self._opt_rows:
            row.destroy()
        self._opt_rows = []
        row, x = None, 0
        for wdg in self.opt_widgets:
            need = wdg.winfo_reqwidth()
            if row is None or (x > 0 and x + 16 + need > avail):
                row = tk.Frame(self.opts_container, bg=COL_BG)
                row.pack(fill="x", pady=(0, 2))
                # Die Widgets sind Geschwister der Zeilen-Frames (pack mit
                # in_=...) -- die spaeter erzeugte Zeile laege sonst in der
                # Stapelreihenfolge UEBER ihnen und wuerde sie verdecken.
                row.lower()
                self._opt_rows.append(row)
                x = 0
            pad = 0 if x == 0 else 16
            wdg.pack(in_=row, side="left", padx=(pad, 0))
            x += pad + need

    def _reflow(self):
        self._flow_pending = False
        self._flow_options()

    def _populate_setup(self):
        cfg = load_config()
        cfg_type = cfg.get("input_type", "input")
        # Quellenliste: echte Eingaenge + (nur Windows) Loopback der Ausgaenge.
        # Eintrag: (typ, kennung, speichername, anzeigetext)
        self.sources = []
        for idx, label in core._list_io_devices('in'):
            try:
                name = sd.query_devices(idx)['name']
            except Exception:
                name = ""
            self.sources.append(("input", idx, name, f"  {label}"))
        if sc is not None:
            default_name = ""
            try:
                default_name = sc.default_speaker().name
            except Exception:
                pass
            try:
                for sp in sc.all_speakers():
                    tag = "  <- Standard" if sp.name == default_name else ""
                    self.sources.append(
                        ("loopback", sp.name, sp.name,
                         f"  Loopback: {sp.name}{tag}"))
            except Exception:
                pass
        self.lb_in.delete(0, "end")
        sel_in = 0
        for n, (kind, _ident, name, text) in enumerate(self.sources):
            self.lb_in.insert("end", text)
            if kind == cfg_type and name == cfg.get("input_name"):
                sel_in = n
        if self.sources:
            self.lb_in.selection_set(sel_in)
            self.lb_in.see(sel_in)

        self.midi_names = mido.get_output_names()
        if sys.platform != 'win32':
            # CoreMIDI (macOS) / ALSA (Linux) koennen eigene virtuelle Ports
            # erzeugen -- so braucht es kein IAC-/loopMIDI-Gegenstueck.
            self.midi_names = self.midi_names + [core.VIRTUAL_MIDI]
        self.lb_midi.delete(0, "end")
        self.lb_midi.insert("end", "  Kein MIDI (nur Anzeige)")
        sel_midi = 0
        for n, name in enumerate(self.midi_names):
            label = (f"  Virtueller Port '{core.VIRTUAL_MIDI_NAME}' erzeugen"
                     if name == core.VIRTUAL_MIDI else f"  {name}")
            self.lb_midi.insert("end", label)
            if name == cfg.get("midi_output"):
                sel_midi = n + 1
        self.lb_midi.selection_set(sel_midi)
        self.lb_midi.see(sel_midi)

        self.var_dec.set(self.opt_bpm_decimal)
        self.var_beat.set(self.opt_beat_sync)
        self.var_chord.set(self.opt_chords)
        self.var_chordlog.set(self.opt_chord_log)
        self.var_chordfast.set(self.opt_chord_fast)
        self.var_mode.set(MODE_LABELS.get(self.opt_note_mode, MODE_LABELS["clock"]))
        self.ent_min.delete(0, "end")
        self.ent_min.insert(0, f"{self.opt_min_bpm:.0f}")
        self.ent_max.delete(0, "end")
        self.ent_max.insert(0, f"{self.opt_max_bpm:.0f}")

    def _selected_midi_name(self):
        """Aktuell in der Liste gewaehlter MIDI-Ausgang (None = 'Kein MIDI')."""
        try:
            sel = self.lb_midi.curselection()
            idx = sel[0] if sel else 0
        except Exception:
            idx = 0
        names = getattr(self, "midi_names", [])
        if idx <= 0 or not (0 <= idx - 1 < len(names)):
            return None
        return names[idx - 1]

    def _acquire_midi_out(self, name):
        """Gemeinsamen MIDI-Ausgang holen: EINMAL geoeffnet, von allen Fenstern
        genutzt (WinMM-Ports wie der GS Wavetable Synth sind single-client). Erhoeht
        den Referenzzaehler; mit _release_midi_out(port) wieder freigeben. Wirft,
        wenn kein Ausgang gewaehlt ist oder das Oeffnen scheitert."""
        if not name:
            raise RuntimeError("kein MIDI-Ausgang eingestellt")
        with self._midi_lock:
            sh = self._midi_shared
            if sh["port"] is not None and sh["name"] == name:
                sh["refs"] += 1
                return sh["port"]
            if sh["port"] is None:
                port = core.open_midi_output(name)
                if port is None:
                    raise RuntimeError("kein MIDI-Ausgang eingestellt")
                sh["name"], sh["port"], sh["refs"] = name, port, 1
                return port
        # Ein ANDERER Port ist bereits geteilt offen -> separaten (eigenen) oeffnen
        port = core.open_midi_output(name)
        if port is None:
            raise RuntimeError("kein MIDI-Ausgang eingestellt")
        return port

    def _release_midi_out(self, port):
        """Eine Referenz auf den (gemeinsamen) MIDI-Ausgang freigeben; beim letzten
        Nutzer wird der Port geschlossen. Separat geoeffnete Ports werden direkt
        geschlossen."""
        if port is None:
            return
        with self._midi_lock:
            sh = self._midi_shared
            if port is sh["port"]:
                sh["refs"] -= 1
                if sh["refs"] <= 0:
                    try:
                        sh["port"].close()
                    except Exception:
                        pass
                    sh["name"], sh["port"], sh["refs"] = None, None, 0
                return
        try:                                   # nicht der geteilte Port -> direkt zu
            port.close()
        except Exception:
            pass

    def _test_midi_output(self):
        """Sendet eine Testsequenz (Start + 1 Takt Clock + Dreiklang + Stop) an den
        aktuell gewaehlten MIDI-Ausgang und meldet das Ergebnis -- so laesst sich
        pruefen, ob der Ausgang den angeschlossenen Klangerzeuger erreicht."""
        name = self._selected_midi_name()
        if not name:
            self.err_label.config(text="Kein MIDI-Ausgang gewählt (Liste links).",
                                  fg=COL_WARN)
            return
        self.err_label.config(
            text=f"MIDI-Test läuft … ({core.midi_output_desc(name)})", fg=COL_MUTED)

        def _work():
            port = None
            try:
                port = self._acquire_midi_out(name)   # geteilter Port (kein Konflikt)
                n = core.midi_test(name, port=port)
            except Exception as e:
                self.root.after(0, lambda e=e: self.err_label.config(
                    text=f"MIDI-Test fehlgeschlagen: {e}", fg=COL_WARN))
                return
            finally:
                self._release_midi_out(port)
            self.root.after(0, lambda: self.err_label.config(
                text=f"✓ {n} MIDI-Nachrichten an {core.midi_output_desc(name)} "
                     "gesendet – Dreiklang C-E-G-C am Klangerzeuger hörbar?",
                fg=COL_OK))
        threading.Thread(target=_work, daemon=True).start()

    # ------------------------------------------------------------------
    # Bildschirm-Wechsel
    # ------------------------------------------------------------------
    def show_setup(self, error=""):
        self.main_frame.pack_forget()
        self._populate_setup()
        self.err_label.config(text=error)
        self.setup_frame.pack(fill="both", expand=True)

    def show_main(self):
        self.setup_frame.pack_forget()
        if self.opt_chords:
            self.chord_block.pack(side="left", padx=(48, 0))
        else:
            self.chord_block.pack_forget()
        self.main_frame.pack(fill="both", expand=True)

    def on_settings(self):
        self.stop_session()
        self.show_setup()

    def _set_hold(self, on):
        """Analyse einfrieren/fortsetzen (Button-Optik inklusive)."""
        with self.shared.lock:
            self.shared.hold = on
        self.hold = on
        if on:
            self.hold_btn.config(text="Analyse fortsetzen", bg=COL_WARN,
                                 fg="#412402", activebackground="#FAC775",
                                 activeforeground="#412402")
        else:
            self.hold_btn.config(text="Analyse anhalten", bg=COL_SURFACE,
                                 fg=COL_FG, activebackground=COL_SURF_HI,
                                 activeforeground=COL_FG)

    def toggle_hold(self):
        """Fuer Stuecke mit langen Breaks: Ergebnisse einfrieren, die
        MIDI-Clock laeuft konstant weiter, Stille loest keinen Reset aus."""
        if self.stream is None and self.cap_thread is None:
            return                  # keine laufende Sitzung
        if not self.hold:
            with self.shared.lock:
                have = self.shared.have_estimate
            if not have:
                return              # noch nichts zu halten
        self._set_hold(not self.hold)

    def note_calib(self):
        """Tracking-Parameter fuer den Noten-/Akkord-Worker aus der Konfiguration
        (mit sinnvollen Vorgaben). dB-Schwellen werden in RMS umgerechnet."""
        cfg = load_config()

        def db2rms(db):
            try:
                return 10.0 ** (float(db) / 20.0)
            except (TypeError, ValueError):
                return None
        c = {}
        sil = db2rms(cfg.get("note_silence_db", -48))
        sus = db2rms(cfg.get("note_sustain_db", -56))
        if sil:
            c["silence_rms"] = sil
        if sus:
            c["sustain_rms"] = sus
        for key, attr in (("note_off_frames", "off_frames"),
                          ("note_change_frames", "change_frames"),
                          ("note_max_poly", "max_poly")):
            v = cfg.get(key)
            if isinstance(v, (int, float)):
                c[attr] = int(v)
        y = cfg.get("yin_threshold")
        if isinstance(y, (int, float)):
            c["yin_threshold"] = float(y)
        return c

    def reset_analysis(self):
        """Analyse von vorn beginnen, z. B. wenn ein Songwechsel ohne
        Pause die Historie mit dem alten Stueck gefuellt hat: der Worker
        verwirft Puffer und Historie, Anzeige und MIDI-Clock stoppen und
        kommen mit der naechsten echten Tempo-Schaetzung (~4 s) zurueck."""
        if self.stream is None and self.cap_thread is None:
            return                  # keine laufende Sitzung
        if self.hold:
            self._set_hold(False)   # eingefrorene Analyse erst fortsetzen
        with self.shared.lock:
            self.shared.reset_request = True

    # ------------------------------------------------------------------
    # Datei-Modus: Datei -> MIDI-Clock (driftfrei)
    # ------------------------------------------------------------------
    @staticmethod
    def _fmt_pos(s):
        s = max(0, int(s))
        return f"{s // 60}:{s % 60:02d}"

    def on_load_file(self):
        """Audio- ODER MIDI-Datei waehlen. Bei Audio: fragen, was passieren soll
        (MIDI-Clock / Stems exportieren/abspielen / Stems->MIDI / Song-Sheet,
        beliebig kombinierbar). Bei einer .mid-Datei: direkt instrumentenweise
        ueber den MIDI-Ausgang abspielen (pro Spur an/aus + Kanal)."""
        path = filedialog.askopenfilename(
            title="Audio- oder MIDI-Datei waehlen",
            filetypes=[("Audio & MIDI", "*.wav *.flac *.mp3 *.ogg *.m4a *.aif "
                        "*.aiff *.mid *.midi"),
                       ("MIDI-Datei", "*.mid *.midi"),
                       ("Audio", "*.wav *.flac *.mp3 *.ogg *.m4a *.aif *.aiff"),
                       ("Alle Dateien", "*.*")])
        if not path:
            return
        if path.lower().endswith((".mid", ".midi")):
            self._open_midi_file_player(path)
            return
        actions = self._ask_actions(os.path.basename(path), allow_clock=True)
        if not actions:
            return
        title = os.path.splitext(os.path.basename(path))[0]
        self._run_material(path, actions, title)

    def _begin_file_clock(self, path):
        """Datei vorab zu einer Beat-Map analysieren und mit driftfreier
        MIDI-Clock abspielen (mirror der WebApp). Beendet eine laufende Sitzung."""
        self.stop_session()                   # Live-Sitzung beenden (zaehlt gen hoch)
        self.show_main()
        self.file_mode = True
        self.file_name = os.path.basename(path)
        self.file_info = None
        self.file_player = None
        self.status_override = "ANALYSIERE DATEI …"
        nm = self.file_name if len(self.file_name) <= 40 else self.file_name[:39] + "…"
        self.src_label.config(text=f"DATEI: {nm}")
        gen = self._session_gen
        threading.Thread(target=self._analyze_file, args=(path, gen),
                         daemon=True).start()

    def _analyze_file(self, path, gen):
        """Im Hintergrund: Datei laden, Beat-Map + Tonart schaetzen. Ergebnis
        wird ueber _file_begin_args an den Main-Thread uebergeben (Tk-only)."""
        if not self.warmed:
            try:
                w = np.zeros(int(core.ANALYSIS_SR * core.WINDOW_SECONDS),
                             dtype=np.float32)
                w[::core.ANALYSIS_SR // 4] = 0.5
                core.estimate_tempo(w, core.ANALYSIS_SR)
                core.chroma_pcp(w, core.ANALYSIS_SR)
            except Exception:
                pass
            self.warmed = True
        try:
            y_an, audio, sr_play = core.load_audio_file(path)
        except Exception as e:
            self._file_begin_args = ("error", gen, f"Datei konnte nicht geladen werden: {e}")
            return
        try:
            info = core.analyze_file_beatmap(y_an, core.ANALYSIS_SR,
                                             core.MIN_BPM, core.MAX_BPM)
        except Exception as e:
            self._file_begin_args = ("error", gen, f"Analyse fehlgeschlagen: {e}")
            return
        if info is None:
            self._file_begin_args = ("error", gen, "Kein Tempo erkannt.")
            return
        key, key_conf = "—", False
        try:
            name, margin = core.estimate_key(y_an, core.ANALYSIS_SR, with_margin=True)
            key, key_conf = name, margin >= core.KEY_CONFIDENT_MARGIN
        except Exception:
            pass
        self._file_begin_args = ("ok", gen, (audio, sr_play, info, key, key_conf))

    def _file_begin(self, audio, sr_play, info, key, key_conf):
        """Main-Thread: Datei ist analysiert -> Wiedergabe VORBEREITEN, aber NICHT
        automatisch starten. Der Transport-Button (▶ Start / ■ Stopp) steuert sie."""
        if self.app_stop.is_set() or not self.file_mode:
            return
        self.file_audio = audio
        self.file_sr = sr_play
        self.file_info = info
        self.file_key = key
        self.file_key_conf = key_conf
        self._file_playing = False
        # Hold/Reset/Aufnahme gelten nur im Live-Betrieb
        self.hold_btn.config(state="disabled")
        self.reset_btn.config(state="disabled")
        self.rec_btn.config(state="disabled")
        self.db_label.config(width=13)
        self.status_override = None
        self.file_play_btn.config(text="▶ Start", state="normal")
        if not self.file_play_btn.winfo_ismapped():
            self.file_play_btn.pack(side="left", padx=(8, 0))

    def _file_start_playback(self):
        """Datei-Wiedergabe + driftfreie MIDI-Clock starten (ab Position 0)."""
        if self.file_audio is None or self._file_playing:
            return
        self.file_midi = None
        cfg = load_config()
        midi_name = cfg.get("midi_output") or None
        if midi_name and (midi_name == core.VIRTUAL_MIDI
                          or midi_name in mido.get_output_names()):
            try:
                self.file_midi = self._acquire_midi_out(midi_name)
            except Exception:
                self.file_midi = None
        try:
            self.file_player = core.FilePlayer(self.file_audio, self.file_sr)
            self.file_player.start()
        except Exception as e:
            if self.file_midi is not None:
                try:
                    self._release_midi_out(self.file_midi)
                except Exception:
                    pass
                self.file_midi = None
            self.file_player = None
            self.status_label.config(text=f"Wiedergabe fehlgeschlagen: {e}",
                                     fg=COL_WARN)
            return
        self.file_clock_stop = threading.Event()
        self.file_clock_thread = threading.Thread(
            target=core.file_clock_worker,
            args=(self.shared, self.file_player, self.file_info["ticks"],
                  self.file_midi, self.file_clock_stop), daemon=True)
        self.file_clock_thread.start()
        self._file_playing = True
        self.file_play_btn.config(text="■ Stopp")

    def _file_stop_playback(self):
        """Wiedergabe + Clock anhalten (MIDI-Stop), aber IM Datei-Modus bleiben --
        erneutes Start spielt von vorne."""
        if self.file_clock_stop is not None:
            self.file_clock_stop.set()
        if self.file_clock_thread is not None:
            self.file_clock_thread.join(timeout=1.5)
        self.file_clock_thread = self.file_clock_stop = None
        if self.file_player is not None:
            try:
                self.file_player.stop()
            except Exception:
                pass
            self.file_player = None
        if self.file_midi is not None:
            self._release_midi_out(self.file_midi)
            self.file_midi = None
        self._file_playing = False
        try:
            if self.file_mode:
                self.file_play_btn.config(text="▶ Start")
        except Exception:
            pass

    def _file_toggle(self):
        if not self.file_mode:
            return
        if self._file_playing:
            self._file_stop_playback()
        else:
            self._file_start_playback()

    def stop_file(self):
        """Datei-Modus KOMPLETT verlassen (Wiedergabe beenden + aufraeumen)."""
        self._file_stop_playback()
        self.file_mode = False
        self.file_info = None
        self.file_audio = None
        self._file_begin_args = None
        try:
            self.file_play_btn.pack_forget()
            self.hold_btn.config(state="normal")
            self.reset_btn.config(state="normal")
            self.rec_btn.config(state="normal")
            self.level_cap_label.config(text="PEGEL")
            self.db_label.config(width=7, text="-60 dB")
        except Exception:
            pass

    def _tick_file(self):
        """Anzeige im Datei-Modus: BPM aus dem Beat-Raster an der aktuellen
        Wiedergabeposition, Tonart aus der Vorab-Schaetzung, Fortschrittsbalken.
        Ohne laufende Wiedergabe (vor ▶ Start / nach ■ Stopp / am Ende) wird die
        Position 0 angezeigt und auf den Start gewartet."""
        info = self.file_info
        if info is None:
            return
        player = self.file_player
        if player is not None and player.is_done():
            self._file_stop_playback()           # am Ende: zurueck auf "▶ Start"
            player = None
        dur = info.get("duration", 0.0) or 0.0
        pos = max(0.0, player.play_pos()) if player is not None else 0.0
        if dur > 0:
            pos = min(pos, dur)
        bpm = core.file_bpm_at(info["beats"], pos, info.get("bpm", 0.0))
        self.bpm_cap_label.config(text="BPM")
        if not self._bpm_big:
            self.bpm_label.config(font=self.f_bpm, fg=COL_FG)
            self._bpm_big = True
        self.bpm_label.config(
            text=f"{bpm:.1f}" if self.opt_bpm_decimal else f"{bpm:.0f}",
            fg=COL_FG if self._file_playing else COL_MUTED)
        self.key_label.config(text=self.file_key,
                              fg=COL_ACCENT if self.file_key_conf else COL_MUTED)
        par = parallel_key(self.file_key)
        self.key_par_label.config(text=f"   {par}" if par else "")
        if self.opt_chords:
            self.chord_label.config(text="")
        self.level_cap_label.config(text="POSITION")
        frac = max(0.0, min(1.0, pos / dur if dur > 0 else 0.0))
        w = self.level_canvas.winfo_width()
        self.level_canvas.coords(self.level_rect, 0, 0, int(w * frac), 14)
        self.db_label.config(text=f"{self._fmt_pos(pos)} / {self._fmt_pos(dur)}")
        tag = "DRIFTFREI" if info.get("constant") else "VARIABEL"
        if not self._file_playing:
            self.status_label.config(text="● DATEI · BEREIT – ▶ Start drücken",
                                     fg=COL_MUTED)
        elif self.file_midi is not None:
            self.status_label.config(text=f"● DATEI · {tag}", fg=COL_OK)
        else:
            self.status_label.config(text=f"DATEI · {tag} · OHNE MIDI", fg=COL_MUTED)

    # ------------------------------------------------------------------
    # Aufnahme: Mitschnitt der Live-Analyse + Speichern (mehrere Stuecke)
    # ------------------------------------------------------------------
    def _rec_btn_idle(self):
        self.rec_btn.config(text="● Aufnahme", bg=COL_SURFACE, fg=COL_FG,
                            activebackground=COL_SURF_HI, activeforeground=COL_FG)

    def toggle_record(self):
        """Mitschnitt des gerade analysierten Live-Signals starten/stoppen.
        Nur im Live-Betrieb (nicht im Datei-Modus)."""
        if self.file_mode:
            return
        if self.stream is None and self.cap_thread is None:
            return                            # keine laufende Live-Sitzung
        with self.shared.lock:
            active = self.shared.rec_active
        if not active:
            with self.shared.lock:
                self.shared.rec_blocks = []
                self.shared.rec_active = True
            self.rec_start_perf = core.time.perf_counter()
            self.rec_btn.config(text="■ Aufnahme 0:00", bg=COL_WARN,
                                fg="#412402", activebackground="#FAC775",
                                activeforeground="#412402")
        else:
            self._finish_record()

    def _finish_record(self):
        with self.shared.lock:
            self.shared.rec_active = False
            blocks = self.shared.rec_blocks
            self.shared.rec_blocks = []
            sr = int(self.shared.capture_sr)
        self._rec_btn_idle()
        if not blocks:
            return
        try:
            rec = np.concatenate(blocks).astype(np.float32)
        except Exception:
            return
        if len(rec) < sr:                     # < 1 s -> nichts Sinnvolles
            self.status_override = None
            return
        self._open_rec_save(rec, sr)

    def _open_rec_save(self, rec, sr):
        """Speichern-/Pruef-Fenster: Stuecke erkennen, Namen anpassen, als WAV
        ablegen (einzeln oder alle in einen Ordner; Ordner wird gemerkt)."""
        self._rec_audio = rec
        self._rec_sr = sr
        self._rec_segs = None
        self._rec_name_vars = []
        win = tk.Toplevel(self.root)
        win.title("Aufnahme speichern")
        win.configure(bg=COL_BG)
        win.geometry("680x440")
        win.transient(self.root)
        self._rec_save_win = win
        dur = len(rec) / sr
        tk.Label(win, text="Aufnahme speichern", font=self.f_h1,
                 bg=COL_BG, fg=COL_FG).pack(pady=(14, 4))
        self._rec_info = tk.Label(
            win, text=f"Länge {self._fmt_pos(dur)} · Stück-Grenzen werden gesucht …",
            font=self.f_small, bg=COL_BG, fg=COL_MUTED)
        self._rec_info.pack(pady=(0, 8))
        self._rec_listf = tk.Frame(win, bg=COL_BG)
        self._rec_listf.pack(fill="both", expand=True, padx=16)
        bf = tk.Frame(win, bg=COL_BG)
        bf.pack(fill="x", padx=16, pady=12)
        self._rec_all_btn = tk.Button(
            bf, text="Alle speichern …", command=self._save_all_rec,
            font=self.f_small, bg="#1D9E75", fg="#04342C",
            activebackground=COL_OK, activeforeground="#04342C", bd=0,
            padx=18, pady=6, highlightthickness=0, cursor="hand2",
            state="disabled")
        self._rec_all_btn.pack(side="left")
        self._small_button(bf, "Weiter (Stems / Song-Sheet) …",
                           self._rec_actions).pack(side="left", padx=(10, 0))
        self._small_button(bf, "Schließen", win.destroy).pack(side="right")
        threading.Thread(target=self._segment_rec_thread, daemon=True).start()
        win.after(250, self._poll_rec_segs)

    # ------------------------------------------------------------------
    # Fortschritts-/Log-Fenster fuer die Stem-Trennung
    # ------------------------------------------------------------------
    def _stem_log_open(self, title="Stem-Trennung"):
        """Oeffnet ein eigenes Fenster, das Fortschritt und (volle) Fehler der
        Stem-Trennung anzeigt. Worker-Threads schicken Zeilen ueber eine
        thread-sichere Queue; geleert wird sie im Tk-Thread per after()-Schleife.
        Rueckgabe: Handle-dict {win, txt, q} fuer _stem_log()."""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=COL_BG)
        win.transient(self.root)
        win.geometry("660x430")
        tk.Label(win, text=title, font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text="Fortschritt & Meldungen – die KI laeuft lokal "
                 "(kann einige Minuten dauern).", font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED).pack(pady=(0, 6))
        stat = tk.Label(win, text="Starte …", font=self.f_small, bg=COL_BG,
                        fg=COL_FG)
        stat.pack(pady=(0, 2))
        bar = ttk.Progressbar(win, length=620, mode="determinate", maximum=100)
        bar.pack(padx=16, pady=(0, 8))
        frame = tk.Frame(win, bg=COL_BG)
        frame.pack(fill="both", expand=True, padx=14, pady=4)
        sb = tk.Scrollbar(frame)
        sb.pack(side="right", fill="y")
        txt = tk.Text(frame, wrap="word", bg=COL_SURFACE, fg=COL_FG,
                      insertbackground=COL_FG, bd=0, highlightthickness=0,
                      font=("Courier", 10), yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.config(command=txt.yview)
        txt.config(state="disabled")
        self._small_button(win, "Schließen", win.destroy).pack(pady=8)
        log = {"win": win, "txt": txt, "q": queue.Queue(),
               "bar": bar, "stat": stat, "t_start": time.time(), "prog": None,
               "t_done": None}              # Endzeit, einmal gesetzt -> Dauer steht still

        def _fmt(sec):
            sec = max(0, int(sec))
            return f"{sec // 60}:{sec % 60:02d}"

        def _poll():
            if not win.winfo_exists():
                return
            got = False
            try:
                while True:
                    line = log["q"].get_nowait()
                    if not got:
                        txt.config(state="normal")
                        got = True
                    txt.insert("end", line + "\n")
            except queue.Empty:
                pass
            if got:
                txt.see("end")
                txt.config(state="disabled")
            # Fortschrittsbalken: rastet pro fertiger Phase ein, kriecht innerhalb
            # einer Phase sanft weiter (kein erfundener Prozentwert).
            prog = log.get("prog")
            if prog:
                k, tot, name = prog["step"], prog["total"], prog["name"]
                if k >= tot:
                    if log.get("t_done") is None:      # Endzeit EINMALIG einfrieren
                        log["t_done"] = time.time()    # sonst laeuft "Gesamtdauer" weiter
                    bar["value"] = 100
                    stat.config(text="Fertig · Gesamtdauer "
                                     f"{_fmt(log['t_done'] - log['t_start'])}")
                else:
                    el = time.time() - log["t_start"]
                    tau = 25.0 if name in ("Stems trennen", "Song-Sheet") else 12.0
                    fp = 1.0 - math.exp(-max(0.0, time.time() - prog["tp"]) / tau)
                    bar["value"] = (k + min(0.92, fp)) / tot * 100.0
                    stat.config(text=f"Schritt {k + 1}/{tot}: {name} · "
                                     f"laeuft seit {_fmt(el)}")
            win.after(150, _poll)

        _poll()
        return log

    def _stem_log(self, log, line):
        """Thread-sicher eine Zeile an das Log-Fenster schicken."""
        if not log:
            return
        try:
            log["q"].put(str(line))
        except Exception:
            pass

    def _stem_progress(self, log, step, total, name):
        """Thread-sicher den aktuellen Verarbeitungsschritt melden (treibt den
        Fortschrittsbalken). step = Anzahl bereits FERTIGER Phasen, total = gesamt;
        step >= total bedeutet fertig."""
        if not log:
            return
        log["prog"] = {"step": int(step), "total": max(1, int(total)),
                       "name": str(name), "tp": time.time()}

    def _stem_log_error(self, log):
        """Vollen Traceback ins Log-Fenster schreiben (im Fehlerfall)."""
        self._stem_log(log, "")
        self._stem_log(log, "── FEHLER ──")
        self._stem_log(log, traceback.format_exc().rstrip())

    def _rec_actions(self):
        """Aufnahme weiterverarbeiten: fragt EINMAL, was passieren soll (Stems
        exportieren/abspielen, Song-Sheet, Deluge) – ohne die Aufnahme erst
        speichern zu muessen. Die gewaehlten Aktionen laufen anschliessend
        NACHEINANDER ueber JEDES erkannte Stueck (eigene Stems/Sheet/Deluge je
        Stueck, mit dem Stueck-Namen als Datei-Basis). Pro Stueck laeuft die
        Stem-Trennung einmal fuer alle Aktionen."""
        if self._rec_audio is None:
            return
        actions = self._ask_actions("Aufnahme", allow_clock=False)
        if not actions:
            return
        rec, sr = self._rec_audio, self._rec_sr
        segs = self._rec_segs
        if not segs:                              # noch nicht erkannt -> ganze Aufnahme
            segs = [{"start": 0, "end": len(rec), "name": "Aufnahme"}]
        jobs = []
        for i, seg in enumerate(segs):
            s, e = int(seg["start"]), int(seg["end"])
            # vom User angepasster Name (aus der Liste), sonst Vorschlag
            if i < len(self._rec_name_vars) and self._rec_name_vars[i].get().strip():
                nm = core.sanitize_filename(self._rec_name_vars[i].get())
            else:
                nm = core.sanitize_filename(seg.get("name") or f"Aufnahme_{i + 1}")
            jobs.append((("array", rec[s:e], sr),
                         self._actions_for_piece(actions, nm, len(segs)), nm))
        # Speichern-Fenster schliessen, damit es nicht ueber den Ergebnissen haengt
        if self._rec_save_win is not None and self._rec_save_win.winfo_exists():
            self._rec_save_win.destroy()
        self._run_material_queue(jobs)

    def _actions_for_piece(self, actions, name, count):
        """Aktionen fuer EIN Stueck. Export-WAVs werden ohnehin mit dem Stueck-Namen
        (= title) praefigiert; der Deluge-Speicherpfad wird je Stueck erst im Tuning-
        Dialog gefragt. Daher sind aktuell keine Pfad-Anpassungen noetig – Platzhalter
        fuer kuenftige stueck-spezifische Optionen."""
        return actions

    def _run_material_queue(self, jobs):
        """Verarbeitet mehrere Stuecke NACHEINANDER (ein Job = (source, actions,
        title)). Sequenziell, weil es nur einen Ergebnis-Slot (_material_res) gibt
        und sich Ergebnis-Fenster (Stem-Player, Sheet) sonst ueberlagern wuerden:
        der naechste Job startet erst, wenn _tick den vorigen konsumiert hat."""
        self._material_queue = list(jobs)
        self._material_start_next()

    def _material_start_next(self):
        """Naechsten wartenden Job starten (No-op bei leerer Schlange). Jobs ohne
        echte Verarbeitung (kein Stem-Bedarf) werden uebersprungen, damit die
        Schlange nicht haengen bleibt."""
        while self._material_queue:
            source, actions, title = self._material_queue.pop(0)
            if self._run_material(source, actions, title):
                return                            # Worker laeuft -> _tick macht weiter

    def _open_stem_player(self, stems_dict, sr, midi_notes=None, bpm=0.0,
                          clock_default=False):
        """Fenster mit Pegel-Fadern je Stem + Play/Pause; spielt die getrennten
        Spuren einzeln oder parallel (eigener StemPlayer). Sind midi_notes
        uebergeben (dict {stem: notes} aus Basic Pitch), gibt es einen MIDI-Bereich:
        je Spur An/Aus + frei waehlbarer MIDI-Kanal, ein Master-Schalter, ein
        Mindestnoten-Regler und „MIDI speichern…" (mehrspurige Datei). Die Noten
        laufen synchron zur Wiedergabe ueber den eingestellten MIDI-Ausgang."""
        names = ([n for n in core.STEM_NAMES if n in stems_dict]
                 + [n for n in stems_dict if n not in core.STEM_NAMES])
        stem_list = [stems_dict[n] for n in names]
        try:
            player = core.StemPlayer(stem_list, sr, names=names)
            player.start_stream()
        except Exception as e:
            messagebox.showerror("Stems abspielen", f"Wiedergabe fehlgeschlagen:\n{e}")
            return
        self._stem_players.append(player)
        win = tk.Toplevel(self.root)
        win.title("Stems abspielen")
        win.configure(bg=COL_BG)
        win.transient(self.root)
        tk.Label(win, text="Stems abspielen", font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text="Pegel je Spur (live) · Doppelklick = 1.0",
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED).pack(pady=(0, 8))
        body = tk.Frame(win, bg=COL_BG)
        body.pack(padx=20, pady=6)
        for k, nm in enumerate(names):
            col = tk.Frame(body, bg=COL_BG)
            col.pack(side="left", padx=12)
            vl = tk.Label(col, text="1.0", font=self.f_tiny, bg=COL_BG, fg=COL_FG)
            vl.pack()
            var = tk.DoubleVar(value=1.0)
            sc = tk.Scale(col, from_=1.5, to=0.0, resolution=0.01, orient="vertical",
                          variable=var, showvalue=False, length=150,
                          command=lambda val, kk=k, lab=vl: (
                              player.set_gain(kk, float(val)),
                              lab.config(text=f"{float(val):.1f}")),
                          bg=COL_BG, fg=COL_FG, troughcolor=COL_SURFACE,
                          highlightthickness=0, bd=0, sliderrelief="flat",
                          activebackground=COL_OK, width=16)
            sc.pack()
            sc.bind("<Double-Button-1>",
                    lambda e, v=var, kk=k, lab=vl: (v.set(1.0),
                                                    player.set_gain(kk, 1.0),
                                                    lab.config(text="1.0"), "break")[-1])
            tk.Label(col, text=core.STEM_LABELS.get(nm, nm), font=self.f_small,
                     bg=COL_BG, fg=COL_ACCENT).pack()
        ctl = tk.Frame(win, bg=COL_BG)
        ctl.pack(pady=10)
        playbtn = tk.Button(ctl, text="▶", font=self.f_btn, bg=COL_SURFACE,
                            fg=COL_FG, activebackground=COL_SURF_HI,
                            activeforeground=COL_FG, bd=0, padx=20, pady=6,
                            highlightthickness=0, cursor="hand2")
        playbtn.pack(side="left", padx=(0, 12))

        def _restart():
            player.seek(0.0)
            player.play()
            playbtn.config(text="⏸")
        self._small_button(ctl, "⏮ Anfang", _restart).pack(side="left", padx=(0, 12))
        poslbl = tk.Label(ctl, text="0:00 / 0:00", font=self.f_small, bg=COL_BG,
                          fg=COL_MUTED)
        poslbl.pack(side="left")
        playbtn.config(command=lambda: playbtn.config(
            text="⏸" if player.toggle() else "▶"))

        # --- Stems speichern (einzeln/alle, optional auf Takt geschnitten) ---
        savef = tk.Frame(win, bg=COL_BG)
        savef.pack(pady=(0, 6))
        self._small_button(
            savef, "💾 Stems speichern…",
            lambda: self._save_stems_dialog(win, stems_dict, sr, bpm)).pack(
                side="left", padx=(0, 8))
        self._small_button(
            savef, "🎤 Play-Along-Mix… (Spuren ausblenden)",
            lambda: self._mixout_dialog(win, stems_dict, sr)).pack(side="left")

        # --- Stems → MIDI (Basic Pitch): mehrere Spuren synchron senden ---
        midi_player = {"obj": None, "port": None}
        if midi_notes:
            cfg = load_config()
            try:
                port = self._acquire_midi_out(cfg.get("midi_output") or None)
                mp = core.MultiStemMidiPlayer(
                    port, position_fn=lambda: player.position()[0],
                    is_playing_fn=player.is_playing)
                midi_player["obj"] = mp
                midi_player["port"] = port
                order = [n for n in core.STEM_MIDI_NAMES if n in midi_notes]
                has_drums = "drums" in midi_notes
                def_ch = {"bass": 1, "other": 2, "vocals": 3,
                          "drums": core.DRUM_DEFAULT_CHANNEL}
                for i, nm in enumerate(order):
                    ch = int(cfg.get("midi_ch_" + nm, def_ch.get(nm, i + 1)))
                    mp.set_track(nm, midi_notes[nm], channel=ch - 1,
                                 enabled=(nm == "bass"))   # Bass standardmaessig an
                if has_drums:
                    dch = int(cfg.get("midi_ch_drums", core.DRUM_DEFAULT_CHANNEL))
                    mp.set_track("drums", midi_notes["drums"], channel=dch - 1,
                                 enabled=True)             # Schlagzeug standardmaessig an
                mp.start()
                mp.set_clock(False, bpm)       # Tempo merken, Clock zunaechst aus

                midf = tk.LabelFrame(win, text="Stems → MIDI (Basic Pitch)",
                                     font=self.f_tiny, bg=COL_BG, fg=COL_ACCENT,
                                     bd=1, highlightthickness=0, labelanchor="nw")
                midf.pack(padx=20, pady=(2, 8), fill="x")
                masterbtn = tk.Button(midf, text="♪ MIDI-Ausgabe: an",
                                      font=self.f_small, bg=COL_SURFACE, fg=COL_OK,
                                      activebackground=COL_SURF_HI,
                                      activeforeground=COL_FG, bd=0, padx=12, pady=4,
                                      highlightthickness=0, cursor="hand2")

                def _toggle_master():
                    if mp.is_active():
                        mp.stop()
                        masterbtn.config(text="♪ MIDI-Ausgabe: aus", fg=COL_MUTED)
                    else:
                        mp.start()
                        masterbtn.config(text="♪ MIDI-Ausgabe: an", fg=COL_OK)
                masterbtn.config(command=_toggle_master)
                masterbtn.grid(row=0, column=0, sticky="w", padx=6, pady=(4, 4))
                sentlbl = tk.Label(midf, text="gesendet: 0", font=self.f_tiny,
                                   bg=COL_BG, fg=COL_MUTED)
                sentlbl.grid(row=0, column=1, columnspan=2, sticky="e", padx=6)
                midi_player["sentlbl"] = sentlbl

                # MIDI-Clock mitsenden (24 PPQN, Start bei ▶) -- so kann ein
                # externer Recorder die Noten taktgenau mitschneiden.
                # Clock direkt an, wenn sie als Aktion gewaehlt war (sonst haette die
                # separate Datei-Clock denselben Port ein zweites Mal geoeffnet).
                want_clock = bool(clock_default and bpm > 0)
                clkvar = tk.BooleanVar(value=want_clock)
                if want_clock:
                    mp.set_clock(True, bpm)
                win._a2m_clkvar = clkvar
                clk_txt = ("MIDI-Clock mitsenden (Start bei ▶)" if bpm > 0
                           else "MIDI-Clock – Tempo unbekannt")
                clk_cb = tk.Checkbutton(
                    midf, text=clk_txt, variable=clkvar,
                    command=lambda: mp.set_clock(bool(clkvar.get()), bpm),
                    font=self.f_small, bg=COL_BG,
                    fg=COL_FG if bpm > 0 else COL_MUTED, selectcolor=COL_SURFACE,
                    activebackground=COL_BG, activeforeground=COL_FG, bd=0,
                    highlightthickness=0, anchor="w")
                if bpm <= 0:
                    clk_cb.config(state="disabled")
                clk_cb.grid(row=1, column=0, columnspan=3, sticky="w", padx=6,
                            pady=(0, 2))

                def _mk_enable(name, var):
                    return lambda: mp.set_enabled(name, bool(var.get()))

                def _mk_channel(name, var):
                    def _f(_v=None):
                        mp.set_channel(name, int(var.get()) - 1)
                        save_config({**load_config(), "midi_ch_" + name: int(var.get())})
                    return _f

                midi_vars = {}
                rows_spec = list(order) + (["drums"] if has_drums else [])
                for r, nm in enumerate(rows_spec, start=2):
                    onv = tk.BooleanVar(value=(nm in ("bass", "drums")))
                    chv = tk.IntVar(value=int(cfg.get("midi_ch_" + nm,
                                                      def_ch.get(nm, r))))
                    midi_vars[nm] = (onv, chv)
                    tk.Checkbutton(midf, text=core.STEM_LABELS.get(nm, nm),
                                   variable=onv, command=_mk_enable(nm, onv),
                                   font=self.f_small, bg=COL_BG, fg=COL_FG,
                                   selectcolor=COL_SURFACE, activebackground=COL_BG,
                                   activeforeground=COL_FG, bd=0, highlightthickness=0,
                                   anchor="w", width=8).grid(row=r, column=0,
                                                             sticky="w", padx=6)
                    tk.Label(midf, text="Kanal", font=self.f_tiny, bg=COL_BG,
                             fg=COL_MUTED).grid(row=r, column=1, padx=(8, 2))
                    om = tk.OptionMenu(midf, chv, *range(1, 17),
                                       command=_mk_channel(nm, chv))
                    om.config(bg=COL_SURFACE, fg=COL_FG, activebackground=COL_SURF_HI,
                              activeforeground=COL_FG, bd=0, highlightthickness=0,
                              font=self.f_tiny, width=2, cursor="hand2")
                    om["menu"].config(bg=COL_SURFACE, fg=COL_FG)
                    om.grid(row=r, column=2, sticky="w")
                    if nm == "drums":
                        # eigenes Fenster: Note je Schlagzeug-Komponente + Empfindlichkeit
                        self._small_button(
                            midf, "Schlagzeug…",
                            lambda mpr=mp: self._open_drum_window(
                                win, mpr, stems_dict, sr)).grid(
                                    row=r, column=3, sticky="w", padx=(8, 0))
                win._a2m_midi_vars = midi_vars   # Tk-Variablen vor GC schuetzen

                # duenne Trennlinie zwischen Spuren und Dichte/Export
                sep = tk.Frame(midf, bg=COL_SURFACE, height=1)
                sep.grid(row=len(rows_spec) + 2, column=0, columnspan=4, sticky="we",
                         padx=6, pady=(6, 4))
                crow = len(rows_spec) + 3
                minms = tk.IntVar(value=int(cfg.get("bass_min_ms", 130)))
                win._a2m_minms = minms
                mslbl = tk.Label(midf, text=f"Dichte: Mindestnote {minms.get()} ms",
                                 font=self.f_tiny, bg=COL_BG, fg=COL_FG)
                tk.Scale(midf, from_=60, to=500, resolution=10, orient="horizontal",
                         variable=minms, showvalue=False, length=140,
                         command=lambda v: mslbl.config(
                             text=f"Dichte: Mindestnote {int(float(v))} ms"),
                         bg=COL_BG, fg=COL_FG, troughcolor=COL_SURFACE,
                         highlightthickness=0, bd=0, sliderrelief="flat",
                         activebackground=COL_OK, width=12).grid(
                             row=crow, column=0, columnspan=2, sticky="w",
                             padx=6, pady=(2, 2))
                mslbl.grid(row=crow, column=2, sticky="w")
                mstat = tk.Label(midf, text="", font=self.f_tiny, bg=COL_BG,
                                 fg=COL_MUTED)
                mstat.grid(row=crow + 1, column=0, columnspan=3, sticky="w", padx=6)

                def _recompute():
                    val = int(minms.get())
                    mstat.config(text="berechne … (alle Spuren)")
                    save_config({**load_config(), "bass_min_ms": val})

                    def _work():
                        try:
                            new = core.stems_to_midi_notes(
                                stems_dict, sr, names=tuple(order),
                                min_note_ms=float(val))
                        except Exception as ex:
                            self.root.after(0, lambda e=ex: mstat.config(
                                text=f"Fehler: {e}"))
                            return

                        def _apply():
                            for nm2, nts in new.items():
                                mp.set_notes(nm2, nts)
                            if mstat.winfo_exists():
                                tot = sum(len(v) for v in new.values())
                                mstat.config(text=f"{tot} Noten neu berechnet")
                        self.root.after(0, _apply)
                    threading.Thread(target=_work, daemon=True).start()

                def _save_midi():
                    tracks = mp.enabled_tracks()
                    if not tracks:
                        mstat.config(text="Keine Spur aktiv – nichts zu speichern.")
                        return
                    cfg2 = load_config()
                    p = filedialog.asksaveasfilename(
                        title="MIDI-Datei speichern", defaultextension=".mid",
                        initialfile="stems.mid",
                        initialdir=cfg2.get("last_save_dir") or "",
                        filetypes=[("MIDI-Datei", "*.mid"), ("Alle", "*.*")])
                    if not p:
                        return
                    try:
                        core.write_stems_midi_file(tracks, p, bpm=bpm or 120.0)
                        save_config({**cfg2, "last_save_dir": os.path.dirname(p)})
                        mstat.config(text=f"Gespeichert: {os.path.basename(p)} "
                                          f"({len(tracks)} Spuren)")
                    except Exception as ex:
                        mstat.config(text=f"Speichern fehlgeschlagen: {ex}")

                brow = crow + 2
                self._small_button(midf, "Anwenden", _recompute).grid(
                    row=brow, column=0, sticky="w", padx=6, pady=(2, 6))
                self._small_button(midf, "MIDI speichern…", _save_midi).grid(
                    row=brow, column=1, columnspan=2, sticky="w", pady=(2, 6))
                self._small_button(
                    midf, "Deluge-Song…",
                    lambda: self._save_deluge(mp, bpm, stems_dict, sr)).grid(
                        row=brow + 1, column=0, columnspan=3, sticky="w",
                        padx=6, pady=(0, 6))
            except Exception as e:
                midi_player["obj"] = None
                msg = str(e)
                if "openPort" in msg or "creating Windows MM" in msg:
                    msg = ("MIDI-Port belegt – der GS Wavetable Synth erlaubt nur "
                           "EINEN Zugriff gleichzeitig. Bitte ein noch offenes "
                           "MIDI-/Stems-Fenster oder eine zweite Programm-Instanz "
                           "schließen – oder einen loopMIDI-Port wählen (mehrfach "
                           "nutzbar). Audio läuft trotzdem.")
                tk.Label(win, text=f"⚠ MIDI aus: {msg}", font=self.f_tiny,
                         bg=COL_BG, fg=COL_WARN, wraplength=560,
                         justify="left").pack(pady=(0, 6))

        def _upd():
            if not win.winfo_exists():
                return
            pos, total = player.position()
            poslbl.config(text=f"{self._fmt_pos(pos)} / {self._fmt_pos(total)}")
            playbtn.config(text="⏸" if player.is_playing() else "▶")
            sl = midi_player.get("sentlbl")
            if sl is not None and midi_player["obj"] is not None:
                act = "an" if midi_player["obj"].is_active() else "aus"
                sl.config(text=f"gesendet: {midi_player['obj'].sent} ({act})")
            win.after(200, _upd)

        def _close():
            if midi_player["obj"] is not None:
                try:
                    midi_player["obj"].stop()
                except Exception:
                    pass
            if midi_player["port"] is not None:
                self._release_midi_out(midi_player["port"])
            try:
                player.stop()
            except Exception:
                pass
            if player in self._stem_players:
                self._stem_players.remove(player)
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _close)
        self._small_button(win, "Schließen", _close).pack(pady=8)
        _upd()
        return player

    def _save_deluge(self, mp, bpm, stems_dict, sr):
        """Aus dem Stem-Player: aktive MIDI-Spuren einsammeln und den Probehoer-/
        Tuning-Dialog oeffnen."""
        tracks = mp.enabled_tracks()             # [(name, notes, channel)]
        if not tracks:
            messagebox.showinfo("Deluge-Song",
                                "Keine aktive Spur – bitte Häkchen setzen.")
            return
        midi = {n: list(notes) for n, notes, _c in tracks}
        self._deluge_tune_dialog(stems_dict, sr, midi, bpm, list(midi.keys()))

    def _deluge_tune_dialog(self, stems_dict, sr, midi, bpm, instruments, lead=2,
                            title="AudioWizard"):
        """Deluge-BUNDLE-Tuning: richtet Stems UND MIDI mit DEMSELBEN Downbeat aufs
        Takt-Raster aus. Der Downbeat wird automatisch (energie-/kick-basiert) erkannt,
        ist per ◀/▶ um ganze Beats verschiebbar und per „Probehören (mit Click)" vorab
        mit Metronom anhoerbar (kurzer Original-Ausschnitt). Warp wird gecacht (Bundle
        und Parts teilen es). „Bundle speichern" schreibt EIN Stueck (Stem-WAVs + XML);
        „Parts speichern" schneidet je erkanntem Abschnitt x Stem einen bar-langen Clip
        zum freien Arrangieren. midi: {stem: [(s,e,pitch,vel)]} in ORIGINAL-Zeit (wird
        beim Warp mitgezogen). Aus Stem-Player UND „Was soll passieren?"-Ablauf."""
        win = tk.Toplevel(self.root)
        win.title("Deluge-Song (Bundle)")
        win.configure(bg=COL_BG)
        win.transient(self.root)
        tk.Label(win, text="Deluge-Song (Stems + MIDI)", font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        active = ", ".join(core.STEM_LABELS.get(n, n) for n in instruments)
        tk.Label(win, text=f"Spuren: {active}", font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED).pack(pady=(0, 8))
        dbv = {"shift": 0}            # Downbeat-Versatz in ganzen Beats (vom Auto-Downbeat)
        warp = {"ws": None, "info": None, "mode": None, "key": None}  # gecachtes Warp
        pv = {"player": None}                     # laufende Vorschau-Wiedergabe
        # Whisper-Gesangstext (Original-Zeit) + Online-Identifikation; 'online' merkt
        # sich, mit welchem Haekchen-Stand der Cache gefuellt wurde
        vcache = {"lines": None, "done": False, "ident": None, "online": None}

        def _db_orig():
            """Im Probehoeren GEWAEHLTER Downbeat in ORIGINAL-Zeit (Auto + ◀/▶-Versatz)."""
            t0o, bpmo = _auto_db()
            bpmo = bpmo if bpmo > 0 else (bpm or 120.0)
            return max(0.0, t0o + dbv["shift"] * (60.0 / bpmo))

        def _ensure_warp(g):
            """Warp fuer Modus g (gecacht; Vorschau und Export teilen es). Die Bar-Phase
            wird an den GEWAEHLTEN Downbeat gekoppelt, damit Parts exakt auf der 1
            starten – Cache daher nach (Modus, Downbeat)."""
            dbo = _db_orig()
            key = (g, round(dbo, 3))
            if warp["key"] == key and warp["ws"] is not None:
                return warp["ws"], warp["info"]
            if g == "off":
                ws, info = stems_dict, None
            else:
                ws, info = core.warp_stems_to_grid(stems_dict, sr, per=g, db_orig=dbo)
            warp.update(ws=ws, info=info, mode=g, key=key)
            return ws, info

        gridc = {"val": None, "done": False}      # gecachte Beat-Karte (beats/bpm/phase)

        def _grid_cache():
            """Echte Beat-Karte (Beats + Tempo + Kick-Phase) – einmalig gecacht. Basis
            fuer Auto-Downbeat UND den driftfreien Vorschau-Klick (auf echten Beats)."""
            if not gridc["done"]:
                try:
                    gridc["val"] = core.beat_grid(stems_dict, sr)
                except Exception:
                    gridc["val"] = None
                gridc["done"] = True
            return gridc["val"]

        def _auto_db():
            """Auto-Downbeat (Original-Zeit in s) + Tempo – Kick-basiert, OHNE Warp."""
            g = _grid_cache()
            if not g:
                return (0.0, bpm or 120.0)
            beats = g["beats"]
            return (float(beats[min(g["phase"], len(beats) - 1)]), g["bpm"])

        def _downbeat(info):
            """Downbeat-Zeit. Basis = GENAU der im Probehören eingestellte Punkt
            (Auto-Downbeat + dbv['shift'] in ORIGINAL-Zeit). Bei info!=None wird dieser
            Punkt in die GEWARPTE Zeit gezogen und aufs Raster gesnappt – so landet im
            Export exakt der gehörte Downbeat (nicht der separate Warp-Downbeat
            info['t_db'], der eine andere Zählzeit treffen könnte)."""
            t0o, bpmo = _auto_db()
            spb = 60.0 / (bpmo if bpmo > 0 else (bpm or 120.0))
            t_orig = max(0.0, t0o + dbv["shift"] * spb)   # im Probehören gehört
            if info is None:
                return t_orig
            wt = float(np.interp(t_orig, info["map_src"], info["map_tgt"]))
            spbw = 60.0 / float(info["bpm"])
            ref = float(info.get("t_db", wt))
            return ref + round((wt - ref) / spbw) * spbw   # auf Raster snappen

        def _stop_preview(*_a):
            if pv["player"] is not None:
                try:
                    pv["player"].stop()
                except Exception:
                    pass
                if pv["player"] in self._stem_players:
                    try:
                        self._stem_players.remove(pv["player"])
                    except ValueError:
                        pass
                pv["player"] = None

        def _preview():
            """LEICHTE Vorschau: KEIN Phase-Vocoder-Warp (der friert schwache Rechner
            ein!), sondern ein kurzer Ausschnitt (~24 s) der ORIGINAL-Stems + Klick auf
            den ECHTEN erkannten Beats (driftet NICHT gegen die Musik) – nur zum Hoeren,
            ob der laute Akzent auf der musikalischen 1 sitzt. ◀/▶ verschiebt die
            Akzent-Phase. Das Aufs-Raster-Ziehen passiert erst beim Speichern."""
            status.config(text="bereite Vorschau-Ausschnitt vor …")

            def _bg():
                try:
                    g = _grid_cache()
                    bpmo = (g["bpm"] if g else (bpm or 120.0)) or (bpm or 120.0)
                    spb = 60.0 / bpmo
                    win_s = 24.0
                    if g:
                        beats = g["beats"]
                        anchor = g["phase"] + int(dbv["shift"])   # gewaehlte "1"-Phase
                        cand = [k for k in range(len(beats)) if (k - anchor) % 4 == 0]
                        a0 = cand[0] if cand else max(0, anchor)
                        t0 = float(beats[min(a0, len(beats) - 1)])
                    else:                                     # Fallback: konstantes Raster
                        t0o, _ = _auto_db()
                        t0 = max(0.0, t0o + dbv["shift"] * spb)
                    start = max(0.0, t0 - 1.5 * 4 * spb)      # ~1,5 Takte Vorlauf
                    names = [n for n in stems_dict if stems_dict[n] is not None]
                    i0, i1 = int(start * sr), int((start + win_s) * sr)
                    L = max(1, i1 - i0)
                    mix = np.zeros(L, dtype=np.float32)
                    for n in names:
                        a = np.asarray(stems_dict[n], dtype=np.float32)
                        if a.ndim == 2:
                            a = a.mean(axis=1)
                        seg = a[i0:i1]
                        mix[:len(seg)] += seg
                    peak = float(np.percentile(np.abs(mix), 99.5)) or 1.0
                    mix *= min(1.0, 0.7 / peak)
                    if g:                                     # Klick auf ECHTEN Beats
                        beats = g["beats"]
                        sel = [(float(beats[k]) - start, (k - anchor) % 4 == 0)
                               for k in range(len(beats))
                               if start <= beats[k] < start + win_s]
                        click = core.click_from_beats([t for t, _ in sel],
                                                      [acc for _, acc in sel], win_s, sr)
                    else:
                        click = core.metronome_click(t0 - start, spb, win_s, sr)
                    m = min(len(mix), len(click))
                    mix[:m] += click[:m]

                    def _start():
                        _stop_preview()
                        try:
                            pl = core.StemPlayer([mix], sr, names=["Vorschau"])
                            pl.start_stream()
                            pl.play()
                            pv["player"] = pl
                            self._stem_players.append(pl)
                            status.config(text="Vorschau-Ausschnitt läuft – ◀/▶ schiebt "
                                          "den Downbeat; sitzt der LAUTE Click auf der 1?")
                        except Exception as ex:
                            status.config(text=f"Vorschau-Wiedergabe: {ex}")
                    self.root.after(0, _start)
                except Exception as ex:
                    self.root.after(0, lambda e=ex:
                                    status.config(text=f"Vorschau-Fehler: {e}"))
            threading.Thread(target=_bg, daemon=True).start()

        dbr = tk.Frame(win, bg=COL_BG)
        dbr.pack(padx=20, pady=2, anchor="w")
        tk.Label(dbr, text="Downbeat-Versatz:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_ACCENT).pack(side="left", padx=(0, 6))
        dblbl = tk.Label(dbr, text=f"{dbv['shift']:+d} Beats", font=self.f_small,
                         bg=COL_BG, fg=COL_FG, width=9)

        def _nudge(d):
            dbv["shift"] += d
            dblbl.config(text=f"{dbv['shift']:+d} Beats")
            if pv["player"] is not None:          # laeuft Vorschau -> neu ausrichten
                _preview()
        self._small_button(dbr, "◀ Beat", lambda: _nudge(-1)).pack(side="left")
        dblbl.pack(side="left", padx=4)
        self._small_button(dbr, "Beat ▶", lambda: _nudge(+1)).pack(side="left")
        lr = tk.Frame(win, bg=COL_BG)
        lr.pack(padx=20, pady=(4, 0), anchor="w")
        leadv = tk.StringVar(value=str(int(lead)))
        tk.Label(lr, text="Vorlauf:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_ACCENT).pack(side="left", padx=(0, 6))
        tk.Entry(lr, textvariable=leadv, width=3, font=self.f_small, bg=COL_SURFACE,
                 fg=COL_FG, insertbackground=COL_FG, bd=0, highlightthickness=0,
                 justify="center").pack(side="left")
        tk.Label(lr, text="Takte vor dem Downbeat (Auftakt liegt im Vorlauf)",
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED).pack(side="left", padx=4)
        # Raster-Bindung: Aus / Takt auf Eins / Groove / Pro Beat
        gr = tk.Frame(win, bg=COL_BG)
        gr.pack(padx=20, pady=(4, 0), anchor="w")
        _gl0 = load_config().get("deluge_gridlock", "off")
        if _gl0 == "bar":                         # altes "Pro Takt" -> Takt auf Eins
            _gl0 = "bar1"
        gridv = tk.StringVar(value=_gl0 if _gl0 in
                             ("off", "bar1", "groove", "beat") else "off")

        def _grid_changed():
            warp.update(ws=None, info=None, mode=None, key=None)  # Modus gewechselt -> Cache weg
            if pv["player"] is not None:
                _preview()
        tk.Label(gr, text="Taktraster:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_ACCENT).pack(side="left", padx=(0, 6))
        for val, lbl in (("off", "Aus"), ("bar1", "Takt auf Eins"),
                         ("groove", "Groove"), ("beat", "Pro Beat")):
            tk.Radiobutton(gr, text=lbl, variable=gridv, value=val,
                           command=_grid_changed, font=self.f_small, bg=COL_BG,
                           fg=COL_FG, selectcolor=COL_SURFACE, activebackground=COL_BG,
                           activeforeground=COL_FG, bd=0, highlightthickness=0).pack(
                               side="left", padx=(0, 4))
        tk.Label(win, text="Gegen Driften (alle halten den Click synchron): „Takt auf "
                 "Eins“ legt je Takt die 1 aufs Raster, Groove voll erhalten; „Groove“ "
                 "zieht 2–4 zusätzlich halb Richtung Raster (straffer, Feeling bleibt, "
                 "empfohlen); „Pro Beat“ jeden Schlag exakt (am straffsten). Stretch "
                 "tonhöhen-erhaltend (Drums evtl. weicher). „Aus“ = Original.",
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED, justify="left",
                 wraplength=440).pack(padx=20, anchor="w", pady=(2, 0))
        # Parts: Strophe/Refrain zusaetzlich per Gesangstext trennen (Whisper);
        # Online-Haekchen = derselbe Config-Wert wie im "Was soll passieren?"-Dialog
        txtv = tk.BooleanVar(value=core.whisper_available())
        onlv = tk.BooleanVar(value=bool(load_config().get("online_ref", False)))
        if core.whisper_available():
            tr = tk.Frame(win, bg=COL_BG)
            tr.pack(padx=20, pady=(4, 0), anchor="w")
            tk.Checkbutton(tr, text="Parts: Strophe/Refrain per Gesangstext trennen "
                           "(genauer, aber langsamer)", variable=txtv,
                           font=self.f_small, bg=COL_BG, fg=COL_FG,
                           selectcolor=COL_SURFACE, activebackground=COL_BG,
                           activeforeground=COL_FG, bd=0, highlightthickness=0).pack(
                               side="left")
            tk.Checkbutton(win, text="Parts: Online-Abgleich (Song im Netz "
                           "identifizieren; Referenz-Text/-Struktur stützt die "
                           "Parterkennung)", variable=onlv,
                           font=self.f_small, bg=COL_BG, fg=COL_FG,
                           selectcolor=COL_SURFACE, activebackground=COL_BG,
                           activeforeground=COL_FG, bd=0, highlightthickness=0,
                           wraplength=440, justify="left").pack(
                               padx=20, anchor="w", pady=(2, 0))
        status = tk.Label(win, text="Tipp: „Probehören“ spielt Stems + Metronom; den "
                          "Downbeat per ◀/▶ so schieben, dass der laute Click auf der 1 "
                          "sitzt – dann speichern.", font=self.f_tiny, bg=COL_BG,
                          fg=COL_MUTED, wraplength=440, justify="left")
        status.pack(pady=(8, 2))
        pvr = tk.Frame(win, bg=COL_BG)
        pvr.pack(pady=(2, 2))
        self._small_button(pvr, "▶ Probehören (mit Click)", _preview).pack(
            side="left", padx=4)
        self._small_button(pvr, "■ Stop", _stop_preview).pack(side="left", padx=4)

        def _do():
            try:
                lead = max(0, int(float(leadv.get().replace(",", "."))))
            except ValueError:
                lead = 2
            g = gridv.get()
            cfg = load_config()
            p = filedialog.asksaveasfilename(
                title="Deluge-Bundle speichern (.XML; Stems daneben)",
                defaultextension=".XML", initialfile="AudioWizard.XML",
                initialdir=cfg.get("last_save_dir") or "",
                filetypes=[("Deluge-Song", "*.XML"), ("Alle", "*.*")])
            if not p:
                return
            _stop_preview()
            status.config(text="schreibe Bundle …")

            def _work():
                try:
                    # gecachtes Warp aus der Vorschau wiederverwenden (kein Neu-Rechnen) –
                    # exakt das, was eben zu hoeren war.
                    ws, info = _ensure_warp(g)
                    wbpm = float(info["bpm"]) if info else (bpm or 120.0)
                    wmidi = ({n: core.warp_notes(list(nt), info)
                              for n, nt in midi.items()} if info else midi)
                    wt_db = _downbeat(info)
                    xmlp, wavs = deluge.write_deluge_bundle(
                        p, ws, sr, wmidi, wbpm, wt_db,
                        lead_bars=lead, instruments=instruments)
                    save_config({**load_config(),
                                 "last_save_dir": os.path.dirname(p),
                                 "deluge_gridlock": g})
                    vtxt = (f"Taktraster {g} ({wbpm:.1f} BPM)"
                            if g != "off" else "Original-Audio")
                    msg = (f"Gespeichert: {os.path.basename(xmlp)} + {len(wavs)} "
                           f"Stems ({vtxt}). XML → SONGS/, Stem-WAVs → "
                           "SAMPLES/AudioWizard/ auf der SD-Karte.")
                    self.root.after(0, lambda: status.config(text=msg))
                except Exception as ex:
                    self.root.after(0, lambda e=ex: status.config(text=f"Fehler: {e}"))
            threading.Thread(target=_work, daemon=True).start()

        def _do_parts():
            """Erkannte Abschnitte als DELUGE-SONG: jeder Abschnitt eine Deluge-Section
            (Launch-Spalte), darin je Stem ein Audio-Clip UND der passende MIDI-Clip –
            alles rastergenau, frei arrangierbar. Schreibt die XML + die Abschnitts-WAVs
            daneben."""
            g = gridv.get()
            cfg = load_config()
            p = filedialog.asksaveasfilename(
                title="Parts-Deluge-Song speichern (.XML; Abschnitts-WAVs daneben)",
                defaultextension=".XML",
                initialfile=core.sanitize_filename(title or "AudioWizard") + "_Parts.XML",
                initialdir=cfg.get("last_save_dir") or "",
                filetypes=[("Deluge-Song", "*.XML"), ("Alle", "*.*")])
            if not p:
                return
            _stop_preview()
            status.config(text="erkenne Abschnitte & ziehe aufs Raster … (kann dauern)")

            use_text = bool(txtv.get())
            use_online = bool(onlv.get())

            def _work():
                try:
                    t0o, bpmo = _auto_db()
                    bpmo = bpmo if bpmo > 0 else (bpm or 120.0)
                    # Parts brauchen ein Taktraster (sonst driften/loopen sie nicht). Bei
                    # „Aus“ daher intern Groove verwenden, damit die 1 sauber sitzt.
                    gp = "groove" if g == "off" else g
                    ws, info = _ensure_warp(gp)
                    wbpm = float(info["bpm"]) if info else bpmo
                    # Downbeat = der GEWARPTE gewaehlte Downbeat (echter Bar-Anker, da der
                    # Warp die Bar-Phase an genau diesen Punkt koppelt) -> Part startet
                    # exakt auf der 1.
                    wt_db = float(info["t_db"]) if info else _db_orig()
                    # Optional: Gesangstext (Strophe/Refrain). Einmal auf ORIGINAL-Vocals,
                    # dann auf die gewarpte Zeit ziehen (gleiche Zeitbasis wie Erkennung).
                    vlines = None
                    if (use_text and core.whisper_available()
                            and stems_dict.get("vocals") is not None):
                        # Online-Korrektur veraendert den gecachten Text: bei
                        # umgeschaltetem Haekchen daher neu transkribieren.
                        if vcache["done"] and vcache.get("online") != use_online:
                            vcache.update(lines=None, done=False, ident=None)
                        if not vcache["done"]:
                            self.root.after(0, lambda: status.config(
                                text="transkribiere Gesang für Strophe/Refrain … (dauert)"))
                            try:
                                vcache["lines"] = core.transcribe_segments(
                                    stems_dict["vocals"], sr, size="small")
                            except Exception:
                                vcache["lines"] = None
                            # Optionaler Online-Abgleich (Haekchen): korrigierter
                            # Text macht die Refrain-Wiederholungen fuer die
                            # Parterkennung sauberer. Faellt still zurueck.
                            if vcache["lines"] and use_online:
                                try:
                                    import online_ref
                                    v = np.asarray(stems_dict["vocals"])
                                    ident = online_ref.identify_song(
                                        vcache["lines"], dur=len(v) / float(sr),
                                        title_hint=title)
                                    if ident and ident.get("conf", 0) >= 0.35:
                                        online_ref.correct_lines(
                                            vcache["lines"], ident["plain"])
                                        vcache["ident"] = ident
                                except Exception:
                                    pass
                            vcache["online"] = use_online
                            vcache["done"] = True
                        if vcache["lines"]:
                            vlines = (core.warp_lines(vcache["lines"], info)
                                      if info else vcache["lines"])
                    self.root.after(0, lambda: status.config(
                        text="erkenne Abschnitte (rastergenau) …"))
                    # Online-STRUKTUR: Strophen-Bloecke der Referenz-Lyrics als
                    # Grenz-/Typ-Anker (auf den GEWARPTEN Zeiten der vlines).
                    oanch = None
                    ident = vcache.get("ident")
                    if vlines and ident:
                        try:
                            import online_ref
                            oanch = online_ref.stanza_anchors(
                                ident.get("plain", ""), vlines,
                                synced=ident.get("synced", "")) or None
                        except Exception:
                            oanch = None
                    # Abschnitte auf den GEWARPTEN Stems (konstantes Tempo) -> Grenzen
                    # liegen auf echten Taktlinien, Schnitt trifft die 1.
                    det_stems = ws if info else stems_dict
                    secs = core.detect_sections(det_stems, sr, t_db=wt_db, bpm=wbpm,
                                                target_bars=8, vocal_lines=vlines,
                                                online_anchors=oanch)
                    if not secs:
                        self.root.after(0, lambda: status.config(
                            text="Keine Abschnitte erkannt – Stück evtl. zu kurz."))
                        return
                    # MIDI wie das Audio aufs Raster ziehen (sonst passt es nicht)
                    wmidi = ({n: core.warp_notes(list(nt), info)
                              for n, nt in midi.items()} if info else midi)
                    xmlp, wavs = deluge.write_deluge_parts(
                        p, ws, sr, wmidi, wbpm, wt_db, secs,
                        instruments=instruments, log=lambda m: None)
                    save_config({**load_config(),
                                 "last_save_dir": os.path.dirname(p),
                                 "deluge_gridlock": g,
                                 "online_ref": use_online})
                    labels = ", ".join(dict.fromkeys(s["label"] for s in secs))
                    gtxt = (" (Raster: Groove, da „Aus“ für Parts nicht loopbar)"
                            if g == "off" else "")
                    msg = (f"Parts-Song: {os.path.basename(xmlp)} – {len(secs)} "
                           f"Abschnitte ({labels}), {len(wavs)} Audio-Clips + MIDI{gtxt}. "
                           "XML → SONGS/, Abschnitts-WAVs → SAMPLES/AudioWizard/ auf die SD.")
                    self.root.after(0, lambda: status.config(text=msg))
                except Exception as ex:
                    self.root.after(0, lambda e=ex: status.config(text=f"Fehler: {e}"))
            threading.Thread(target=_work, daemon=True).start()

        def _do_editor():
            """Part-Editor oeffnen: Warp (wie beim Export) im Hintergrund holen,
            dann die Wellenform-Oberflaeche mit den GEWARPTEN Stems zeigen --
            so entspricht jeder geloopte Part exakt dem, was exportiert wird."""
            g = gridv.get()
            gp = "groove" if g == "off" else g     # Parts brauchen ein Raster
            _stop_preview()
            status.config(text="richte Stems aufs Taktraster aus … (kann dauern)")

            def _work():
                try:
                    ws, info = _ensure_warp(gp)
                    wbpm = float(info["bpm"]) if info else (bpm or 120.0)
                    wt_db = float(info["t_db"]) if info else _db_orig()
                    wmidi = ({n: core.warp_notes(list(nt), info)
                              for n, nt in midi.items()} if info else midi)

                    def _open():
                        status.config(text="Part-Editor geöffnet.")
                        self._open_part_editor(win, dict(ws), sr, wbpm, wt_db,
                                               wmidi, instruments, title,
                                               gridlock=g, orig=stems_dict,
                                               db_orig=_db_orig())
                    self.root.after(0, _open)
                except Exception as ex:
                    self.root.after(0, lambda e=ex: status.config(text=f"Fehler: {e}"))
            threading.Thread(target=_work, daemon=True).start()

        bsr = tk.Frame(win, bg=COL_BG)
        bsr.pack(pady=(2, 2))
        self._small_button(bsr, "Bundle speichern… (1 Stück)", _do).pack(
            side="left", padx=4)
        self._small_button(bsr, "Parts speichern… (Abschnitte)", _do_parts).pack(
            side="left", padx=4)
        self._small_button(bsr, "✂ Part-Editor… (Marker selbst setzen)",
                           _do_editor).pack(side="left", padx=4)

        def _close():
            _stop_preview()
            win.destroy()
        self._small_button(win, "Schließen", _close).pack(pady=(0, 10))
        win.protocol("WM_DELETE_WINDOW", _close)
        win._a2m_deluge = (dblbl, leadv, gridv)   # Tk-Variablen vor GC schuetzen

    # Wellenform-Farbe je Spur im Part-Editor
    WAVE_COL = {"drums": "#EF9F27", "bass": "#9FE1CB", "other": "#8AA9E6",
                "vocals": "#E68AA9", "mix": "#B9B7AE"}

    def on_open_tracks(self):
        """Mehrere fertige Audiodateien (z. B. schon exportierte Stems) als
        SPUREN in den Part-Editor laden -- ohne erneute KI-Trennung. Eine
        einzelne Datei geht genauso (dann eine Spur)."""
        cfg = load_config()
        paths = filedialog.askopenfilenames(
            title="Audiospuren wählen (Mehrfachauswahl: z. B. alle Stems eines Songs)",
            initialdir=cfg.get("last_save_dir") or "",
            filetypes=[("Audio", "*.wav *.flac *.mp3 *.ogg *.m4a *.aif *.aiff"),
                       ("Alle Dateien", "*.*")])
        if not paths:
            return
        paths = list(paths)
        title = os.path.splitext(os.path.basename(paths[0]))[0]
        for suf in ("_drums", "_bass", "_vocals", "_other", "_mix"):
            if title.lower().endswith(suf):
                title = title[:-len(suf)]
                break
        log = self._stem_log_open("Spuren laden")
        self._stem_log(log, f"{len(paths)} Datei(en) → Part-Editor")
        cb = lambda m: self._stem_log(log, m)

        def _work():
            try:
                self._stem_progress(log, 0, 2, "Spuren laden")
                stems, ssr = core.load_audio_tracks(paths, log=cb)
                self._stem_progress(log, 1, 2, "Taktraster")
                pe = self._prepare_part_editor(stems, ssr, title, log, cb)
                self._stem_progress(log, 2, 2, "Fertig")

                def _open():
                    self._open_part_editor(
                        self.root, pe["stems"], pe["sr"], pe["bpm"], pe["t_db"],
                        None, list(pe["stems"].keys()), pe["title"],
                        gridlock=pe.get("gridlock", "groove"),
                        orig=pe.get("orig"), db_orig=pe.get("db_orig"))
                self.root.after(0, _open)
            except Exception as ex:
                self._stem_log_error(log)
                self._msg_later(self.err_label, f"Spuren laden fehlgeschlagen: {ex}")
        threading.Thread(target=_work, daemon=True).start()

    def _msg_later(self, widget, text):
        """Statuszeile thread-sicher setzen (kurze Meldung aus einem Worker)."""
        self.root.after(0, lambda t=str(text): widget.config(text=t[:160]))

    def _part_export_dialog(self, parent, names, label_map=None):
        """Vor dem Deluge-Export fragen, WAS hinein soll: je Spur Audio-Clip
        und/oder MIDI-Clip. Die MIDI-Noten werden anschliessend aus genau den
        gewaehlten Spuren frisch erkannt (basic-pitch bzw. Onset-Erkennung fuer
        Drums) -- deshalb steht die Auswahl erst hier und nicht vorab.
        names sind Schluessel; label_map {key: Anzeigename} beschriftet sie
        (im Mashup z. B. „2. Bass“ fuer den Bass aus Song 2).
        Rueckgabe {'audio': [...], 'midi': [...]} oder None (Abbruch)."""
        label_map = dict(label_map or {})
        bp_ok = core.basic_pitch_available()
        win = tk.Toplevel(self.root)
        win.title("Deluge-Song exportieren")
        win.configure(bg=COL_BG)
        win.transient(parent)
        prev = self.root.grab_current()
        try:
            win.grab_set()
        except Exception:
            pass
        tk.Label(win, text="Was soll in den Deluge-Song?", font=self.f_h1,
                 bg=COL_BG, fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text="Je Part und Spur entsteht ein Audio-Clip; MIDI-Clips "
                 "werden JETZT aus genau diesen Spuren erkannt (dauert je Spur "
                 "etwas).", font=self.f_tiny, bg=COL_BG, fg=COL_MUTED,
                 justify="left", wraplength=430).pack(padx=18, pady=(0, 8))
        grid = tk.Frame(win, bg=COL_BG)
        grid.pack(padx=22, pady=2, anchor="w")
        tk.Label(grid, text="Spur", font=self.f_tiny, bg=COL_BG, fg=COL_ACCENT,
                 width=12, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(grid, text="Audio", font=self.f_tiny, bg=COL_BG,
                 fg=COL_ACCENT).grid(row=0, column=1, padx=8)
        tk.Label(grid, text="MIDI", font=self.f_tiny, bg=COL_BG,
                 fg=COL_ACCENT).grid(row=0, column=2, padx=8)
        av, mv = {}, {}
        for r, n in enumerate(names, start=1):
            # Schluessel koennen "1:bass" sein (Song 2, Bass) -> Stem-Teil fuer
            # Farbe und Vorauswahl herausziehen
            stem = n.split(":", 1)[1] if ":" in n else n
            tk.Label(grid, text=label_map.get(n, core.STEM_LABELS.get(n, n)),
                     font=self.f_small, bg=COL_BG,
                     fg=self.WAVE_COL.get(stem, COL_FG), width=14,
                     anchor="w").grid(row=r, column=0, sticky="w")
            av[n] = tk.BooleanVar(value=True)
            # MIDI kostet Rechenzeit -> nur Bass ist vorausgewaehlt (haeufigster
            # Fall). Drums brauchen KEIN basic-pitch (eigene Onset-Erkennung).
            can_midi = bp_ok or stem == "drums"
            mv[n] = tk.BooleanVar(value=(stem == "bass" and bp_ok))
            # Angehakt = GRUEN gefuelltes Kaestchen: auf dunklem Grund ist ein
            # schwarzer Haken in einem hellen Kaestchen kaum zu erkennen.
            _mk = dict(bg=COL_BG, fg=COL_BG, selectcolor=COL_OK,
                       activebackground=COL_BG, activeforeground=COL_BG,
                       bd=0, highlightthickness=0)
            tk.Checkbutton(grid, variable=av[n], **_mk).grid(row=r, column=1)
            cb = tk.Checkbutton(grid, variable=mv[n], **_mk)
            if not can_midi:
                mv[n].set(False)
                cb.config(state="disabled", selectcolor=COL_SURFACE)
            cb.grid(row=r, column=2)
        note = ("Drums werden per Onset-Erkennung zum 808-Kit, tonale Spuren "
                "per Basic Pitch." if bp_ok else
                "MIDI braucht: pip install basic-pitch (Drums gingen auch ohne).")
        tk.Label(win, text=note, font=self.f_tiny, bg=COL_BG, fg=COL_MUTED,
                 justify="left", wraplength=430).pack(padx=18, pady=(6, 0))
        err = tk.Label(win, text="", font=self.f_tiny, bg=COL_BG, fg=COL_WARN)
        err.pack(pady=(2, 0))
        res = {}

        def _close():
            win.destroy()
            if prev is not None:
                try:
                    prev.grab_set()
                except Exception:
                    pass

        def _ok():
            a = [n for n in names if av[n].get()]
            if not a:
                err.config(text="Mindestens eine Audio-Spur wählen.")
                return
            res.update(audio=a, midi=[n for n in names if mv[n].get()])
            _close()

        row = tk.Frame(win, bg=COL_BG)
        row.pack(pady=(8, 12))
        tk.Button(row, text="Weiter zum Speichern…", command=_ok, font=self.f_btn,
                  bg="#1D9E75", fg="#04342C", activebackground=COL_OK,
                  activeforeground="#04342C", bd=0, padx=18, pady=6,
                  highlightthickness=0, cursor="hand2").pack(side="left", padx=4)
        self._small_button(row, "Abbrechen", _close).pack(side="left", padx=4)
        win.protocol("WM_DELETE_WINDOW", _close)
        win._a2m_exp = (av, mv)
        self.root.wait_window(win)
        return res or None

    # Trennqualitaet -> (Modell, Shifts, Backend, Overlap). Gleiche Stufen wie
    # im Haupt-Dialog „Was soll passieren?“ (dort qual_map).
    SEP_QUAL = [("Hoch – empfohlen", "hi"),
                ("Maximum – fine-tuned + Shift-Trick (langsam)", "max"),
                ("Maximum+ – shifts 2 (sehr langsam)", "max2"),
                ("Ultra – RoFormer SOTA (extrem langsam)", "ultra"),
                ("Schnell", "fast")]

    @staticmethod
    def _sep_params(qual):
        """Trennparameter aus der Qualitaetsstufe (wie in _ask_actions)."""
        if qual == "ultra":
            return {"sep_backend": "roformer", "sep_model": "htdemucs",
                    "overlap": 0.25, "shifts": 0}
        if qual == "max2":
            return {"sep_backend": "demucs", "sep_model": "htdemucs_ft",
                    "overlap": 0.25, "shifts": 2}
        if qual == "max":
            return {"sep_backend": "demucs", "sep_model": "htdemucs_ft",
                    "overlap": 0.25, "shifts": 1}
        if qual == "fast":
            return {"sep_backend": "demucs", "sep_model": "htdemucs",
                    "overlap": 0.1, "shifts": 0}
        return {"sep_backend": "demucs", "sep_model": "htdemucs",
                "overlap": 0.25, "shifts": 0}

    def _add_song_dialog(self, parent, songno, paths):
        """Weiteren Song fuer das Mashup laden: die Datei(en) direkt als Spuren
        uebernehmen ODER vorher die Instrumente trennen (KI). Ohne Trennung gibt
        es nur eine Mix-Spur -- fuer „nur den Bass aus Song 2“ braucht es die
        Trennung. Rueckgabe {'sep': bool, 'qual': str} oder None (Abbruch)."""
        demucs_ok = core.demucs_available()
        rofo_ok = core.roformer_available()
        cfg = load_config()
        win = tk.Toplevel(self.root)
        win.title(f"Song {songno} laden")
        win.configure(bg=COL_BG)
        win.transient(parent)
        prev = self.root.grab_current()
        try:
            win.grab_set()
        except Exception:
            pass
        tk.Label(win, text=f"Song {songno} laden", font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        flist = ", ".join(os.path.basename(str(p)) for p in paths[:4])
        if len(paths) > 4:
            flist += f" … (+{len(paths) - 4})"
        tk.Label(win, text=flist, font=self.f_tiny, bg=COL_BG, fg=COL_MUTED,
                 wraplength=430, justify="left").pack(padx=18, pady=(0, 10))
        # Eine einzelne Datei ist meist ein fertiger Mix -> Trennung anbieten;
        # mehrere Dateien sind meist schon Stems -> direkt uebernehmen.
        sepv = tk.BooleanVar(value=(len(paths) == 1 and demucs_ok))
        _rk = dict(font=self.f_small, bg=COL_BG, fg=COL_FG,
                   selectcolor=COL_SURFACE, activebackground=COL_BG,
                   activeforeground=COL_FG, bd=0, highlightthickness=0,
                   anchor="w", justify="left")
        tk.Radiobutton(win, text="Datei(en) direkt als Spuren übernehmen "
                       "(schnell)", variable=sepv, value=False,
                       **_rk).pack(anchor="w", padx=22)
        tk.Label(win, text="Mehrere Dateien = mehrere Spuren (z. B. schon "
                 "exportierte Stems). Eine Datei = eine Spur.",
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED, wraplength=400,
                 justify="left").pack(anchor="w", padx=48, pady=(0, 8))
        tk.Radiobutton(win, text="Instrumente trennen (KI): Drums · Bass · "
                       "Vocals · Rest", variable=sepv, value=True,
                       state=("normal" if demucs_ok else "disabled"),
                       **_rk).pack(anchor="w", padx=22)
        tk.Label(win, text=("Nötig, wenn aus diesem Song nur EIN Instrument in "
                            "das Mashup soll." if demucs_ok else
                            "braucht: pip install demucs"),
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED, wraplength=400,
                 justify="left").pack(anchor="w", padx=48, pady=(0, 2))
        qrow = tk.Frame(win, bg=COL_BG)
        qrow.pack(anchor="w", padx=48, pady=(2, 0))
        tk.Label(qrow, text="Qualität:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_ACCENT).pack(side="left", padx=(0, 6))
        qmap = [(l, v) for l, v in self.SEP_QUAL if v != "ultra" or rofo_ok]
        q0 = str(cfg.get("stem_quality", "hi"))
        if q0 not in [v for _l, v in qmap]:
            q0 = "hi"
        qvar = tk.StringVar(value=next(l for l, v in qmap if v == q0))
        om = tk.OptionMenu(qrow, qvar, *[l for l, _v in qmap])
        om.config(font=self.f_small, bg=COL_SURFACE, fg=COL_FG, bd=0,
                  highlightthickness=0, activebackground=COL_SURF_HI,
                  activeforeground=COL_FG, width=34, anchor="w")
        om["menu"].config(bg=COL_SURFACE, fg=COL_FG, font=self.f_small)
        om.pack(side="left")
        eta = tk.Label(win, text="", font=self.f_tiny, bg=COL_BG, fg=COL_WARN,
                       wraplength=430, justify="left")
        eta.pack(anchor="w", padx=48, pady=(4, 0))

        def _upd_eta(*_a):
            q = next(v for l, v in qmap if l == qvar.get())
            pr = self._sep_params(q)
            try:
                eta.config(text=core.separation_eta(
                    paths[0], backend=pr["sep_backend"], model=pr["sep_model"],
                    shifts=pr["shifts"]))
            except Exception:
                eta.config(text="")
        qvar.trace_add("write", _upd_eta)
        _upd_eta()
        res = {}

        def _close():
            win.destroy()
            if prev is not None:
                try:
                    prev.grab_set()
                except Exception:
                    pass

        def _ok():
            q = next(v for l, v in qmap if l == qvar.get())
            res.update(sep=bool(sepv.get()), qual=q)
            if res["sep"]:
                save_config({**load_config(), "stem_quality": q})
            _close()

        row = tk.Frame(win, bg=COL_BG)
        row.pack(pady=(10, 12))
        tk.Button(row, text="Laden", command=_ok, font=self.f_btn, bg="#1D9E75",
                  fg="#04342C", activebackground=COL_OK,
                  activeforeground="#04342C", bd=0, padx=22, pady=6,
                  highlightthickness=0, cursor="hand2").pack(side="left", padx=4)
        self._small_button(row, "Abbrechen", _close).pack(side="left", padx=4)
        win.protocol("WM_DELETE_WINDOW", _close)
        win._a2m_addsong = (sepv, qvar)
        self.root.wait_window(win)
        return res or None

    def _open_part_editor(self, parent, stems, sr, bpm, t_db, midi,
                          instruments, title, gridlock="off", orig=None,
                          db_orig=None):
        """Wellenform-Editor („Audio-Editor“) fuer die Deluge-Parts: Abschnitte
        per Start-/End-Marker SELBST in der Wellenform definieren, einzeln
        nahtlos als LOOP vorhoeren (so hoert man sofort, ob der Part als Loop
        traegt) und daraus den Deluge-Parts-Song schreiben.

        stems/midi liegen BEREITS gewarpt vor (rastergenaue Zeit, t_db =
        Downbeat darin) -- dadurch sind Taktraster, Loop-Vorschau und Export
        identisch. Angezeigt werden der Gesamtmix und je Stem eine Spur, die
        sich einzeln zu-/abschalten laesst (Haekchen = sichtbar UND hoerbar)."""
        def _mk_names(sd):
            return ([n for n in core.STEM_NAMES if sd.get(n) is not None]
                    + [n for n in sd if n not in core.STEM_NAMES
                       and sd.get(n) is not None])

        names = _mk_names(stems)
        if not names:
            messagebox.showinfo("Part-Editor", "Keine Stems vorhanden.")
            return
        bpm = float(bpm) if bpm and bpm > 0 else 120.0
        bar_t = 4.0 * 60.0 / bpm                       # Sekunden je Takt (4/4)
        total = max(len(np.asarray(stems[n])) for n in names) / float(sr)
        t_db = max(0.0, float(t_db))
        n_bars = max(1, int((total - t_db) / bar_t))
        MAX_SONGS = 4
        # MASHUP: bis zu vier Songs, je einer hinter einem Reiter. Der
        # Tempo-Master (anfangs Song 1, umwaehlbar) gibt das ZIELTEMPO vor;
        # Loops aus den anderen werden beim Vorhoeren und beim Export darauf
        # gedehnt. Jeder Song haelt seinen eigenen Zustand (Tempo, Downbeat,
        # Auswahl, Ansicht, Wellenform-Daten).
        # WICHTIG: eine KOPIE des dicts -- 'stems' ist die Arbeitsvariable, deren
        # Inhalt beim Reiterwechsel getauscht wird. Ohne Kopie wuerde Song 1
        # dabei mit den Spuren des anderen Songs ueberschrieben.
        songs = [{"title": title, "stems": dict(stems), "orig": orig,
                  "names": list(names),
                  "bpm": bpm, "t_db": t_db, "db_orig": db_orig, "total": total,
                  "grid": gridlock, "mode": "pitch", "sel": None, "bars": 0,
                  "view": (0.0, total), "peaks": None, "mixpk": {},
                  "gen": 0, "midi": midi}]
        cur = 0                                        # aktiver Reiter
        master = 0                                     # Song gibt das Tempo vor

        win = tk.Toplevel(self.root)
        win.title(f"Part-Editor – {title}")
        win.configure(bg=COL_BG)
        win.transient(parent)
        # An den Bildschirm anpassen: Taskleiste und Fensterrahmen abziehen,
        # sonst rutschen die unteren Knoepfe aus dem Bild.
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{min(1180, max(760, sw - 80))}x"
                     f"{min(800, max(520, sh - 150))}+20+20")
        # Untergrenze so, dass neben den Bedienleisten noch brauchbar viel
        # Wellenform uebrig bleibt (zwei Knopfreihen brauchen ihren Platz)
        win.minsize(860, 560)

        def _toggle_max(_ev=None):
            """Vollbild an/aus. Ein Toplevel mit transient() hat unter Windows
            keinen Maximieren-Knopf im Rahmen -- deshalb hier ein eigener
            (zusaetzlich F11 und Doppelklick auf die Kopfzeile)."""
            try:
                zoom = (str(win.state()) == "zoomed")
                win.state("normal" if zoom else "zoomed")
            except Exception:                  # nicht jede Plattform kennt das
                if getattr(win, "_a2m_geo", None):
                    win.geometry(win._a2m_geo)
                    win._a2m_geo = None
                else:
                    win._a2m_geo = win.geometry()
                    win.geometry(f"{win.winfo_screenwidth()}x"
                                 f"{win.winfo_screenheight() - 40}+0+0")
            win.after(60, lambda: (_draw(), _upd_status()))
        win.bind("<F11>", _toggle_max)
        # Zustand: sichtbarer Ausschnitt, Auswahl (Sekunden), Parts (in TAKTEN
        # relativ zum Downbeat -- das ist die Einheit, die die Deluge braucht)
        # Metronom-Einstellungen (gemerkt): an/aus + Lautstaerke in Prozent
        _c0 = load_config()
        click_on = tk.BooleanVar(value=bool(_c0.get("editor_click", False)))
        clickv = tk.IntVar(value=int(_c0.get("editor_click_vol", 60)))
        st = {"t0": 0.0, "dur": total, "sel": None, "parts": [], "peaks": None,
              "player": None, "drag": False, "cursor": None,
              "grid": gridlock,
              # Downbeat in ORIGINAL-Zeit -- Anker fuer einen spaeteren Neu-Warp
              "db_orig": float(db_orig if db_orig is not None else t_db)}

        titf = tk.Frame(win, bg=COL_BG)
        titf.pack(fill="x", padx=14, pady=(10, 0))
        titl = tk.Label(titf, text=f"Part-Editor – {title}", font=self.f_h1,
                        bg=COL_BG, fg=COL_FG)
        titl.pack(side="left", expand=True)
        titl.bind("<Double-Button-1>", _toggle_max)
        self._small_button(titf, "⛶ Vollbild (F11)", _toggle_max).pack(
            side="right")
        head = tk.Label(win, text="", font=self.f_tiny, bg=COL_BG, fg=COL_MUTED)
        head.pack(pady=(0, 2))
        # Reiterleiste: ein Reiter je Song + "Song hinzufuegen"
        tabs = tk.Frame(win, bg=COL_BG)
        tabs.pack(fill="x", padx=14, pady=(2, 4))

        def _mbpm():
            """Zieltempo = Tempo des Master-Songs."""
            return float(songs[master]["bpm"])

        def _upd_head():
            f = (4.0 * 60.0 / _mbpm()) / bar_t
            extra = ("  ·  Tempo-Master" if cur == master else
                     f"  ·  → {_mbpm():.1f} BPM: ×{f:.4f} "
                     f"({'tonhöhentreu' if songs[cur]['mode'] == 'pitch' else 'Tonhöhe wandert'})")
            head.config(text=f"{bpm:.1f} BPM · {n_bars} Takte · "
                        f"{len(names)} Spur(en) · Downbeat bei {t_db:.2f} s"
                        + extra)

        def _upd_tabs():
            for w in tabs.winfo_children():
                w.destroy()
            for i, s in enumerate(songs):
                nm = s["title"]
                nm = nm if len(nm) <= 22 else nm[:21] + "…"
                act = (i == cur)
                b = tk.Button(
                    tabs, text=f"{'▣' if act else '▢'} {i + 1}. {nm}"
                    + ("  ⏱" if i == master else
                       f"  ×{(4.0 * 60.0 / _mbpm()) / (4.0 * 60.0 / s['bpm']):.3f}"),
                    command=(lambda k=i: _switch_song(k)), font=self.f_small,
                    bg=(COL_SURF_HI if act else COL_SURFACE),
                    fg=(COL_ACCENT if act else COL_MUTED),
                    activebackground=COL_SURF_HI, activeforeground=COL_FG,
                    bd=0, padx=10, pady=3, highlightthickness=0, cursor="hand2")
                b.pack(side="left", padx=(0, 4))
                if i == master:
                    tk.Label(tabs, text=f"⏱ gibt das Tempo vor "
                             f"({_mbpm():.1f} BPM)", font=self.f_tiny,
                             bg=COL_BG, fg=COL_MUTED).pack(side="left",
                                                           padx=(0, 8))
            if cur != master:
                # Umwaehlbar: welcher Song ist der Maßstab?
                self._small_button(tabs, "⏱ Dieser Song gibt das Tempo vor",
                                   lambda: _set_master(cur)).pack(side="left",
                                                                  padx=(8, 0))
            if len(songs) < MAX_SONGS:
                self._small_button(tabs, "+ Song laden…",
                                   lambda: _add_song()).pack(side="left",
                                                             padx=(8, 0))
            if len(songs) > 1:
                self._small_button(tabs, "Song entfernen",
                                   lambda: _del_song()).pack(side="right")

        def _set_master(k):
            """Tempo-Master wechseln: ab jetzt gibt Song k das Zieltempo vor.
            Die Taktzahlen aller Loops bleiben -- nur die Dehnfaktoren (und
            damit die Laenge der spaeteren Clips) rechnen sich neu."""
            nonlocal master
            if not (0 <= k < len(songs)) or k == master:
                return
            oldm = master
            old = songs[oldm]["title"]
            master = k
            st["scache"] = {}                  # Ziellaengen aendern sich
            if cur == master:
                songs[cur]["bars"] = 0         # der Master braucht keine Vorgabe
            # Der bisherige Master braucht jetzt selbst eine Taktvorgabe --
            # aus seinem Loop ableiten, damit sein Tempo unveraendert bleibt
            so = songs[oldm]
            if not so.get("bars"):
                sel = st["sel"] if oldm == cur else so.get("sel")
                if sel:
                    so["bars"] = max(1, int(round(
                        abs(sel[1] - sel[0]) / (4.0 * 60.0 / float(so["bpm"])))))
            _upd_head()
            _upd_tabs()
            _upd_tfhint()
            _sync_fields()
            _refresh_list()
            _draw()
            stat.config(text=f"„{songs[k]['title']}“ gibt jetzt das Tempo vor "
                        f"({_mbpm():.2f} BPM statt „{old}“). Alle anderen Loops "
                        "werden darauf gedehnt – die Taktzahlen bleiben.")
        _upd_head()
        _upd_tabs()          # Reiter + „Song laden“ von Anfang an anzeigen

        # ---------------- Werkzeugleiste ----------------
        tb = tk.Frame(win, bg=COL_BG)
        tb.pack(fill="x", padx=14)
        self._small_button(tb, "🔍−", lambda: _zoom(2.0)).pack(side="left")
        self._small_button(tb, "🔍+", lambda: _zoom(0.5)).pack(side="left")
        # Lambda, weil _zoom_sel erst weiter unten definiert wird
        self._small_button(tb, "Auswahl", lambda: _zoom_sel()).pack(side="left")
        self._small_button(tb, "Alles", lambda: _set_view(0.0, total)).pack(
            side="left", padx=(0, 12))
        tk.Label(tb, text="Marker fangen auf:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_ACCENT).pack(side="left", padx=(0, 4))
        snapv = tk.StringVar(value="bar")
        for val, lbl in (("bar", "Takt"), ("beat", "Beat"), ("off", "frei")):
            tk.Radiobutton(tb, text=lbl, variable=snapv, value=val,
                           font=self.f_small, bg=COL_BG, fg=COL_FG,
                           selectcolor=COL_SURFACE, activebackground=COL_BG,
                           activeforeground=COL_FG, bd=0,
                           highlightthickness=0).pack(side="left")
        # Takt-1 (Downbeat) feinjustieren: verschiebt NUR das Raster, kein
        # teures Neu-Warpen. Am besten VOR dem Setzen der Parts machen -- die
        # Parts haengen am Raster und wandern mit.
        tk.Label(tb, text="Takt-1:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_ACCENT).pack(side="left", padx=(14, 4))
        self._small_button(tb, "◀", lambda: _shift_db(-1)).pack(side="left")
        dblbl = tk.Label(tb, text=f"{t_db:.2f} s", font=self.f_tiny, bg=COL_BG,
                         fg=COL_FG, width=7)
        dblbl.pack(side="left")
        self._small_button(tb, "▶", lambda: _shift_db(1)).pack(side="left")

        tb2 = tk.Frame(win, bg=COL_BG)
        tb2.pack(fill="x", padx=14, pady=(2, 0))
        # Taktraster: bestimmt, wie stark das Audio aufs gleichmaessige Raster
        # gezogen wird. Wechsel = neu warpen (Hintergrund), daher hier und nicht
        # im Vorab-Dialog.
        tk.Label(tb2, text="Taktraster (verändert das Audio!):",
                 font=self.f_tiny, bg=COL_BG, fg=COL_ACCENT).pack(side="left",
                                                                  padx=(0, 4))
        gridv = tk.StringVar(value=gridlock if gridlock in
                             ("off", "bar1", "groove", "beat") else "off")
        for val, lbl in (("off", "Aus (Original)"), ("bar1", "Takt-1"),
                         ("groove", "Groove"), ("beat", "Pro Beat")):
            tk.Radiobutton(tb2, text=lbl, variable=gridv, value=val,
                           command=lambda: _regrid(gridv.get()),
                           font=self.f_small, bg=COL_BG, fg=COL_FG,
                           selectcolor=COL_SURFACE, activebackground=COL_BG,
                           activeforeground=COL_FG, bd=0,
                           highlightthickness=0).pack(side="left")
        # Dehn-Methode dieses Songs (nur relevant, wenn er aufs Zieltempo muss)
        tk.Label(tb2, text="  Tempo-Anpassung:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_ACCENT).pack(side="left", padx=(10, 2))
        modev = tk.StringVar(value="pitch")

        def _set_mode():
            songs[cur]["mode"] = modev.get()
            _upd_head()
            _upd_tabs()
        for val, lbl in (("pitch", "Tonhöhe halten"), ("speed", "Tonhöhe mit")):
            tk.Radiobutton(tb2, text=lbl, variable=modev, value=val,
                           command=_set_mode, font=self.f_small, bg=COL_BG,
                           fg=COL_FG, selectcolor=COL_SURFACE,
                           activebackground=COL_BG, activeforeground=COL_FG,
                           bd=0, highlightthickness=0).pack(side="left")
        tk.Label(tb2, text="   Spuren (Häkchen = sichtbar und hörbar):",
                 font=self.f_tiny, bg=COL_BG, fg=COL_ACCENT).pack(side="left",
                                                                  padx=(0, 6))
        # Die Spur-Haekchen gehoeren zum aktiven Song und werden beim
        # Reiterwechsel neu aufgebaut (andere Songs, andere Spuren).
        trkf = tk.Frame(tb2, bg=COL_BG)
        trkf.pack(side="left")
        shown = {}

        def _rebuild_track_boxes():
            for w in trkf.winfo_children():
                w.destroy()
            shown.clear()
            saved = songs[cur].get("shown") or {}
            for n in names:
                v = tk.BooleanVar(value=bool(saved.get(n, True)))
                shown[n] = v
                tk.Checkbutton(trkf, text=core.STEM_LABELS.get(n, n),
                               variable=v,
                               command=lambda: (_apply_gains(), _sched_all(),
                                                _draw()),
                               font=self.f_small, bg=COL_BG,
                               fg=self.WAVE_COL.get(n, COL_FG),
                               selectcolor=COL_SURFACE, activebackground=COL_BG,
                               activeforeground=COL_FG, bd=0,
                               highlightthickness=0).pack(side="left")
        _rebuild_track_boxes()

        # ---------------- Wellenform ----------------
        RULER_H, BAND_H = 20, 26
        # Gepackt wird erst GANZ am Ende (von unten nach oben), damit die
        # Bedienleisten auf kleinen Bildschirmen sichtbar bleiben und nur die
        # Wellenform schrumpft.
        cvs = tk.Canvas(win, bg="#101014", highlightthickness=0, bd=0, height=240)
        sbx = tk.Scrollbar(win, orient="horizontal",
                           command=lambda *a: _xview(*a))

        # Part-Farbe nach TYP (fuehrende Zahl im Label) -- gleiche Namen wie
        # 1a/1b bekommen dieselbe Farbe, so wie spaeter auf der Deluge.
        PART_COLS = ["#2f6f5b", "#2b5f74", "#6b4f7a", "#7a5a35", "#4a6b35",
                     "#7a3f4f"]

        def _pcol(label):
            m = re.match(r"(\d+)", str(label).strip())
            i = (int(m.group(1)) - 1) if m else 0
            return PART_COLS[i % len(PART_COLS)]

        def _t2x(t, W):
            return (t - st["t0"]) / st["dur"] * W

        def _x2t(x, W):
            return st["t0"] + float(x) / max(1, W) * st["dur"]

        def _snap(t):
            m = snapv.get()
            if m == "off":
                return t
            step = bar_t if m == "bar" else bar_t / 4.0
            return t_db + round((t - t_db) / step) * step

        def _bar_of(t):
            return (t - t_db) / bar_t

        def _mix_peaks(act):
            """Peak-Pyramide der Summe der AKTIVEN Spuren -- die Mix-Zeile zeigt
            damit genau das, was auch zu hoeren ist. Je Kombination gecacht
            (Neuberechnung ~50 ms, danach sofort)."""
            key = tuple(act)
            cache = st.setdefault("mixpk", {})
            if key not in cache:
                if len(cache) > 8:                     # Speicher im Zaum halten
                    cache.clear()
                m = None
                for nm in act:
                    a = np.asarray(stems[nm], dtype=np.float32)
                    a = a.mean(axis=1) if a.ndim == 2 else a
                    if m is None:
                        m = a.copy()
                    else:
                        k = min(len(m), len(a))
                        m = m[:k] + a[:k]
                cache[key] = None if m is None else core.waveform_peaks(m)
            return cache[key]

        def _draw():
            cvs.delete("all")
            W = max(1, cvs.winfo_width())
            H = max(1, cvs.winfo_height())
            if st["peaks"] is None:
                cvs.create_text(W // 2, H // 2 - 10,
                                text=st.get("busy") or "berechne Wellenform …",
                                fill=COL_ACCENT, font=self.f_h1)
                cvs.create_text(W // 2, H // 2 + 18,
                                text="bitte warten – das Fenster reagiert "
                                "danach wieder", fill=COL_MUTED,
                                font=self.f_small)
                # Auswahl trotzdem zeigen: sonst sieht es aus, als liesse sich
                # in diesem Song kein Loop ziehen (er laesst sich sehr wohl).
                if st["sel"]:
                    a, b = sorted(st["sel"])
                    xa, xb = _t2x(a, W), _t2x(b, W)
                    cvs.create_rectangle(xa, 0, xb, H, fill=COL_ACCENT,
                                         stipple="gray12", width=0)
                    for x in (xa, xb):
                        cvs.create_line(x, 0, x, H, fill=COL_ACCENT, width=2)
                return
            act = [n for n in names if shown[n].get()]
            rows = ["__mix__"] + act
            row_h = max(38, (H - RULER_H - BAND_H) // max(1, len(rows)))
            # --- Taktlineal + Taktlinien ---
            px_bar = W / (st["dur"] / bar_t)
            step = 1
            while px_bar * step < 55:
                step *= 2
            b0 = int(np.floor(_bar_of(st["t0"])))
            b1 = int(np.ceil(_bar_of(st["t0"] + st["dur"]))) + 1
            for b in range(max(0, b0), max(0, b1)):
                x = _t2x(t_db + b * bar_t, W)
                if x < -2 or x > W + 2:
                    continue
                major = (b % step == 0)
                cvs.create_line(x, 0, x, RULER_H, fill="#4a4a52" if major else "#2a2a30")
                if major:
                    cvs.create_text(x + 3, 2, text=str(b + 1), anchor="nw",
                                    fill=COL_MUTED, font=self.f_tiny)
                    cvs.create_line(x, RULER_H, x, H, fill="#212128")
            # --- Parts-Band ---
            y0 = RULER_H
            cvs.create_rectangle(0, y0, W, y0 + BAND_H, fill="#17171c", width=0)
            # nur die Quellen zeigen, die aus dem SICHTBAREN Song stammen
            for p in st["parts"]:
                for q in _cur_srcs(p):
                    xa = _t2x(q["t0"], W)
                    xb = _t2x(q["t0"] + _src_len(q, p["bars"]), W)
                    if xb < 0 or xa > W:
                        continue
                    cvs.create_rectangle(xa, y0 + 2, xb, y0 + BAND_H - 2,
                                         fill=_pcol(p["label"]),
                                         outline=COL_ACCENT, width=1)
                    if xb - xa > 26:
                        trk = "+".join(t[:2] for t in q["tracks"])
                        cvs.create_text((max(0, xa) + min(W, xb)) / 2,
                                        y0 + BAND_H / 2,
                                        text=f"{p['label']} {trk}", fill=COL_FG,
                                        font=self.f_tiny)
            # --- Wellenformen ---
            for k, n in enumerate(rows):
                top = RULER_H + BAND_H + k * row_h
                mid = top + row_h / 2.0
                amp = (row_h / 2.0) - 4
                cvs.create_line(0, mid, W, mid, fill="#2a2a30")
                pk = _mix_peaks(act) if n == "__mix__" else st["peaks"].get(n)
                if pk is not None:
                    lo, hi = core.peak_columns(pk, st["t0"], st["t0"] + st["dur"],
                                               W, sr)
                    xs = np.arange(W, dtype=np.float64)
                    ytop = mid - np.clip(hi, -1, 1) * amp
                    ybot = mid - np.clip(lo, -1, 1) * amp
                    pts = np.concatenate([
                        np.column_stack([xs, ytop]).ravel(),
                        np.column_stack([xs[::-1], ybot[::-1]]).ravel()])
                    cvs.create_polygon(
                        pts.tolist(), width=0,
                        fill=(COL_MUTED if n == "__mix__"
                              else self.WAVE_COL.get(n, COL_ACCENT)))
                if n == "__mix__":
                    lbl = ("Mix (alle Spuren)" if len(act) == len(names)
                           else f"Mix ({len(act)} von {len(names)} Spuren)"
                           if act else "Mix (keine Spur aktiv)")
                else:
                    lbl = core.STEM_LABELS.get(n, n)
                cvs.create_text(6, top + 2, text=lbl, anchor="nw",
                                fill=COL_MUTED, font=self.f_tiny)
                cvs.create_line(0, top, W, top, fill="#212128")
            # --- Auswahl (mit greifbaren Marker-Griffen an den Raendern) ---
            if st["sel"]:
                a, b = sorted(st["sel"])
                xa, xb = _t2x(a, W), _t2x(b, W)
                cvs.create_rectangle(xa, RULER_H, xb, H, fill=COL_ACCENT,
                                     stipple="gray12", width=0)
                for x, side in ((xa, 1), (xb, -1)):
                    cvs.create_line(x, 0, x, H, fill=COL_ACCENT, width=2)
                    cvs.create_rectangle(x, RULER_H + 2, x + side * 7,
                                         RULER_H + 14, fill=COL_ACCENT, width=0)
            _draw_cursor()
            sbx.set(st["t0"] / total, (st["t0"] + st["dur"]) / total)

        def _draw_cursor():
            """Nur die Abspiellinie -- waehrend der Wiedergabe wird 20x/s NUR
            diese aktualisiert (die Wellenform bleibt stehen)."""
            cvs.delete("cursor")
            if st["cursor"] is None:
                return
            W, H = max(1, cvs.winfo_width()), max(1, cvs.winfo_height())
            x = _t2x(st["cursor"], W)
            if -2 <= x <= W + 2:
                cvs.create_line(x, 0, x, H, fill=COL_WARN, width=2,
                                tags="cursor")

        def _set_view(t0, dur):
            dur = max(bar_t * 0.5, min(total, float(dur)))
            st["dur"] = dur
            st["t0"] = max(0.0, min(total - dur, float(t0)))
            _draw()

        def _zoom(f, center=None):
            dur = max(bar_t * 0.5, min(total, st["dur"] * f))
            if center is not None:
                # Zoom am Mauszeiger: der Punkt unter dem Zeiger bleibt stehen
                _set_view(center - (center - st["t0"]) * (dur / st["dur"]), dur)
                return
            # Zoom-Knoepfe: ist ein Loop markiert, kommt DESSEN Mitte in die
            # Bildmitte -- man zoomt immer auf den Bereich, an dem man arbeitet.
            c = ((st["sel"][0] + st["sel"][1]) / 2.0 if st["sel"]
                 else st["t0"] + st["dur"] / 2.0)
            _set_view(c - dur / 2.0, dur)

        def _zoom_sel():
            """Ansicht genau auf den markierten Loop legen (mit etwas Rand)."""
            if not st["sel"]:
                stat.config(text="Erst einen Bereich markieren.")
                return
            a, b = st["sel"]
            pad = max(bar_t * 0.25, (b - a) * 0.15)
            _set_view(a - pad, (b - a) + 2 * pad)

        def _shift_db(n):
            """Takt-1 um einen Beat verschieben (nur das Raster, kein Neu-Warp)."""
            nonlocal t_db
            t_db = max(0.0, min(total, t_db + n * bar_t / 4.0))
            dblbl.config(text=f"{t_db:.2f} s")
            _refresh_click()               # Klick sitzt am Raster
            _upd_head()
            _draw()
            _upd_status()

        def _regrid(g):
            """Taktraster wechseln: die ORIGINAL-Spuren neu aufs Raster ziehen
            (Phase-Vocoder, daher im Hintergrund). Parts bleiben gueltig -- sie
            haengen an Takten, nicht an Sekunden."""
            if orig is None or g == st.get("grid"):
                st["grid"] = g
                return
            _stop()
            lbl = {"off": "Original", "bar1": "Takt-1", "groove": "Groove",
                   "beat": "Pro Beat"}.get(g, g)
            # Sofort SICHTBAR machen, dass gerechnet wird: die Wellenform
            # verschwindet und der Canvas zeigt gross, was gerade laeuft.
            st["busy"] = (f"richte Spuren aufs Taktraster „{lbl}“ aus …"
                          if g != "off" else "stelle das Original-Audio her …")
            st["peaks"] = None
            st["mixpk"] = {}
            _draw()
            stat.config(text=st["busy"] + "  (Phase-Vocoder – bei langen "
                        "Stücken dauert das ein bis zwei Minuten)")
            win.update_idletasks()

            def _work():
                try:
                    if g == "off":
                        ws, info = dict(orig), None
                    else:
                        ws, info = core.warp_stems_to_grid(
                            orig, sr, per=g, db_orig=st.get("db_orig", t_db))

                    def _apply():
                        nonlocal bpm, t_db, total, bar_t, n_bars
                        # Player muss weg (er haelt die alten Arrays)
                        p = st["player"]
                        if p is not None:
                            try:
                                p.stop()
                            except Exception:
                                pass
                            if p in self._stem_players:
                                try:
                                    self._stem_players.remove(p)
                                except ValueError:
                                    pass
                            st["player"] = None
                        # dict-INHALT tauschen -> alle Referenzen bleiben gueltig
                        stems.clear()
                        stems.update({n: ws[n] for n in ws if n in names})
                        bpm = float(info["bpm"]) if info else bpm
                        t_db = float(info["t_db"]) if info else st.get("db_orig", t_db)
                        bar_t = 4.0 * 60.0 / bpm
                        total = max(len(np.asarray(stems[n])) for n in names) / float(sr)
                        n_bars = max(1, int((total - t_db) / bar_t))
                        st["grid"] = g
                        st["parts"] = []           # Zeiten passen nicht mehr
                        st["busy"] = "berechne Wellenform …"
                        # Das Audio dieses Songs ist neu -> im Song-Zustand
                        # ablegen und alle gedehnten Ausschnitte verwerfen
                        songs[cur]["stems"] = dict(stems)
                        songs[cur]["gen"] = int(songs[cur].get("gen", 0)) + 1
                        st["scache"] = {}
                        save_config({**load_config(), "editor_gridlock": g})
                        dblbl.config(text=f"{t_db:.2f} s")
                        _upd_head()
                        _set_sel(None, None)
                        _set_view(0.0, total)
                        _refresh_list()
                        msg = (f"Taktraster „{g}“ – {bpm:.1f} BPM. "
                               + ("Audio unverändert (Original)." if g == "off"
                                  else "ACHTUNG: Das Audio wurde zeitlich "
                                  "gedehnt (Phase-Vocoder) – klingt anders als "
                                  "das Original.")
                               + " Parts wurden zurückgesetzt, weil sich die "
                               "Zeiten geändert haben.")
                        stat.config(text=msg)
                        threading.Thread(target=_peaks_work, args=(msg,),
                                         daemon=True).start()
                    self.root.after(0, _apply)
                except Exception as ex:
                    self.root.after(0, lambda e=ex: stat.config(
                        text=f"Taktraster-Wechsel fehlgeschlagen: {e}"))
            threading.Thread(target=_work, daemon=True).start()

        def _xview(*a):
            if not a:
                return
            if a[0] == "moveto":
                _set_view(float(a[1]) * total, st["dur"])
            elif a[0] == "scroll":
                d = float(a[1]) * st["dur"] * (0.15 if a[2] == "units" else 0.9)
                _set_view(st["t0"] + d, st["dur"])

        # ---------------- Maus ----------------
        GRAB = 6                                       # Fangbreite der Marker (px)

        def _hit(ev):
            """Was liegt unter dem Zeiger? 'left'/'right' = Loop-Marker,
            'move' = innerhalb des Loops, sonst None."""
            if not st["sel"] or ev.y < RULER_H:
                return None
            W = max(1, cvs.winfo_width())
            a, b = sorted(st["sel"])
            xa, xb = _t2x(a, W), _t2x(b, W)
            if abs(ev.x - xa) <= GRAB:
                return "left"
            if abs(ev.x - xb) <= GRAB:
                return "right"
            if xa < ev.x < xb:
                return "move"
            return None

        def _on_motion(ev):
            """Mauszeiger zeigt an, was ein Ziehen jetzt bewirken wuerde."""
            if st["drag"]:
                return
            h = _hit(ev)
            cvs.config(cursor=("sb_h_double_arrow" if h in ("left", "right")
                               else "fleur" if h == "move" else ""))

        def _on_press(ev):
            W = max(1, cvs.winfo_width())
            if ev.y < RULER_H + BAND_H and _hit(ev) not in ("left", "right"):
                t = _x2t(ev.x, W)                      # Klick ins Parts-Band
                for i, p in enumerate(st["parts"]):
                    for q in _cur_srcs(p):
                        if q["t0"] <= t <= q["t0"] + _src_len(q, p["bars"]):
                            _select_part(i)
                            return
                return
            h = _hit(ev)
            t = _x2t(ev.x, W)
            if h in ("left", "right", "move"):
                # bestehende Auswahl greifen statt eine neue aufzuziehen
                st["drag"] = {"mode": h, "t": t, "sel": tuple(sorted(st["sel"]))}
            else:
                ts = _snap(t)
                _set_sel(ts, ts)
                st["drag"] = {"mode": "new", "t": ts, "sel": (ts, ts)}

        def _on_drag(ev):
            d = st["drag"]
            if not d:
                return
            W = max(1, cvs.winfo_width())
            if ev.x < 12:                              # am Rand mitscrollen
                _set_view(st["t0"] - st["dur"] * 0.03, st["dur"])
            elif ev.x > W - 12:
                _set_view(st["t0"] + st["dur"] * 0.03, st["dur"])
            t = _x2t(ev.x, W)
            a0, b0 = d["sel"]
            if d["mode"] == "move":
                # ganzen Loop verschieben: Laenge bleibt EXAKT erhalten
                na = _snap(a0 + (t - d["t"]))
                na = max(0.0, min(total - (b0 - a0), na))
                _set_sel(na, na + (b0 - a0))
            elif d["mode"] == "left":
                _set_sel(min(_snap(t), b0 - 1e-3), b0)
            elif d["mode"] == "right":
                _set_sel(a0, max(_snap(t), a0 + 1e-3))
            else:
                _set_sel(a0, _snap(t))

        def _on_release(_ev):
            was_new = bool(st["drag"]) and st["drag"]["mode"] == "new"
            st["drag"] = None
            if was_new and st["sel"] and abs(st["sel"][1] - st["sel"][0]) < 1e-3:
                _set_sel(None, None)                   # reiner Klick = Auswahl weg
            if st.pop("tempo_dirty", False):
                # waehrend des Ziehens aufgeschoben (zu teuer pro Mausbewegung)
                _upd_tabs()
                _refresh_click()
                _upd_status()
            cvs.config(cursor="")

        def _on_wheel(ev):
            W = max(1, cvs.winfo_width())
            if ev.state & 0x0004:                      # Strg = Zoom am Zeiger
                _zoom(0.8 if ev.delta > 0 else 1.25, center=_x2t(ev.x, W))
            else:
                _set_view(st["t0"] - np.sign(ev.delta) * st["dur"] * 0.15,
                          st["dur"])

        cvs.bind("<Button-1>", _on_press)
        cvs.bind("<B1-Motion>", _on_drag)
        cvs.bind("<ButtonRelease-1>", _on_release)
        cvs.bind("<Motion>", _on_motion)
        cvs.bind("<MouseWheel>", _on_wheel)
        cvs.bind("<Configure>", lambda e: _draw())

        # ---------------- Auswahl / Status ----------------
        stat = tk.Label(win, text="Auswahl: –", font=self.f_small, bg=COL_BG,
                        fg=COL_MUTED, anchor="w")
        # Marker auch per Zeitangabe setzen (exakt, ohne Fangraster)
        tf = tk.Frame(win, bg=COL_BG)
        tk.Label(tf, text="Loop-Marker:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_ACCENT).pack(side="left", padx=(0, 6))
        e_a, e_b, e_len = tk.StringVar(), tk.StringVar(), tk.StringVar()
        e_bars = tk.StringVar()            # Laenge in TAKTEN (Deluge-Einheit)

        def _nudge_btn(parent, text, cmd):
            """Kleiner Schrittknopf -- gedrueckt halten wiederholt (Spinner)."""
            return tk.Button(parent, text=text, command=cmd, font=self.f_tiny,
                             bg=COL_SURFACE, fg=COL_FG,
                             activebackground=COL_SURF_HI, activeforeground=COL_FG,
                             bd=0, padx=5, pady=0, highlightthickness=0,
                             cursor="hand2", repeatdelay=400, repeatinterval=45)

        for lbl, var, which, w in (("Start", e_a, "a", 10), ("Ende", e_b, "b", 10),
                                   ("Länge", e_len, "len", 10),
                                   ("Takte", e_bars, "bars", 4)):
            tk.Label(tf, text=lbl, font=self.f_tiny, bg=COL_BG,
                     fg=(COL_ACCENT if which == "bars" else COL_MUTED)).pack(
                         side="left", padx=(8, 2))
            _nudge_btn(tf, "−", lambda x=which: _nudge(x, -1)).pack(side="left")
            ent = tk.Entry(tf, textvariable=var, width=w, font=self.f_small,
                           bg=COL_SURFACE, fg=COL_FG, insertbackground=COL_FG,
                           bd=0, highlightthickness=0, justify="right")
            ent.pack(side="left", padx=1)
            ent.bind("<Return>", lambda e: _apply_times())
            _nudge_btn(tf, "+", lambda x=which: _nudge(x, 1)).pack(side="left")
        self._small_button(tf, "Übernehmen", lambda: _apply_times()).pack(
            side="left", padx=(8, 0))
        tk.Button(tf, text="⟲ Tempo aus Auswahl",
                  command=lambda: _tempo_from_sel(), font=self.f_small,
                  bg=COL_SURFACE, fg=COL_ACCENT, activebackground=COL_SURF_HI,
                  activeforeground=COL_FG, bd=0, padx=8, pady=2,
                  highlightthickness=0, cursor="hand2").pack(side="left",
                                                             padx=(10, 0))
        tfhint = tk.Label(tf, text="", font=self.f_tiny, bg=COL_BG, fg=COL_MUTED,
                          anchor="w", justify="left")
        tfhint.pack(side="left", padx=(8, 0))

        def _upd_tfhint():
            """Der Hinweis an der Markerzeile haengt davon ab, welcher Song
            gerade offen ist -- der Tempo-Master SETZT das Tempo, die anderen
            richten sich danach."""
            tfhint.config(
                text=("Loop markieren → Takte eintragen → „Tempo aus Auswahl“: "
                      "dann bestimmt DEIN Loop das BPM-Raster"
                      if cur == master else
                      f"„{songs[master]['title']}“ gibt {_mbpm():.1f} BPM vor – "
                      "hier nur eintragen, um wie viele TAKTE es sich beim Loop "
                      "handelt; der Bereich wird automatisch gedehnt."))
        _upd_tfhint()

        def _cur_bars():
            """Taktzahl der aktuellen Auswahl.

            Tempo-Master: aus der LAENGE gerechnet. Alle anderen Songs: die
            ANGEGEBENE Zahl -- dort ist die Taktzahl die Vorgabe, und der
            Bereich wird beim Hoeren/Export aufs Zieltempo gedehnt."""
            if not st["sel"]:
                return 0
            if cur != master:
                n = int(songs[cur].get("bars") or 0)
                if n >= 1:
                    return n
            a, b = sorted(st["sel"])
            return max(0, int(round((b - a) / bar_t)))

        def _sel_bars():
            """Auswahl als (start_takt, end_takt) fuer den Deluge-Export.

            Gerundet wird die LAENGE, nicht die beiden Grenzen einzeln -- sonst
            entsteht aus 4,36 Takten schnell ein 5-Takt-Part, wenn der Start
            knapp unter und das Ende knapp ueber einer Taktlinie liegt."""
            if not st["sel"]:
                return None
            bars = _cur_bars()
            if bars < 1:
                return None
            s = max(0, int(round(_bar_of(sorted(st["sel"])[0]))))
            return (s, s + bars)

        def _retempo_cur():
            """Songs, die nicht der Tempo-Master sind: Tempo aus Auswahl-Laenge
            und ANGEGEBENER Taktzahl.

            Der Master gibt das Zieltempo vor. Bei allen anderen sagt man nur,
            um wie viele TAKTE es sich beim Loop handelt -- daraus folgen sein
            Tempo und der Dehnfaktor, mit dem der Bereich beim Hoeren und beim
            Export aufs Zieltempo gebracht (also verkuerzt oder gestreckt)
            wird. Laeuft bei JEDER Auswahl-Aenderung mit.
            Rueckgabe: Dehnfaktor oder None."""
            nonlocal bpm, bar_t, t_db, n_bars
            if cur == master or not st["sel"]:
                return None
            a, b = sorted(st["sel"])
            if b - a < 1e-3:
                # Beim Aufziehen mit der Maus hat die Auswahl zuerst die Laenge
                # 0 -- daraus laesst sich kein Tempo rechnen (und eine Division
                # durch 0 wuerde das Ziehen abbrechen).
                return None
            n = int(songs[cur].get("bars") or 0)
            if n < 1:
                # Erste Auswahl in diesem Song: so viele Takte annehmen wie der
                # Loop des Tempo-Masters hat -- meist sucht man ja denselben
                # Ausschnitt noch einmal. Sonst aus der (unsicheren) BPM-
                # Schaetzung. Die Statuszeile sagt, dass das nur eine Annahme
                # ist.
                ms = songs[master].get("sel") if master != cur else None
                if ms:
                    n = max(1, int(round(abs(ms[1] - ms[0])
                                         / (4.0 * 60.0 / _mbpm()))))
                    songs[cur]["bars_auto"] = f"{n} (wie „{songs[master]['title']}“)"
                else:
                    n = max(1, int(round((b - a) / bar_t)))
                    songs[cur]["bars_auto"] = f"{n} (aus der Tempo-Schätzung)"
                songs[cur]["bars"] = n
            new_bar = (b - a) / n
            new_bpm = 4.0 * 60.0 / new_bar
            if not (20.0 <= new_bpm <= 400.0):
                return None
            bpm, bar_t = new_bpm, new_bar
            # Raster so legen, dass der Loop-Start auf eine Taktlinie faellt
            t_db = a - np.floor(a / bar_t) * bar_t
            n_bars = max(1, int((total - t_db) / bar_t))
            songs[cur]["bpm"], songs[cur]["t_db"] = bpm, t_db
            dblbl.config(text=f"{t_db:.2f} s")
            _upd_head()
            if st["drag"]:
                # Beim Ziehen nur die billigen Anzeigen -- Reiterleiste neu
                # bauen und die Klickspur neu rechnen erst beim Loslassen.
                st["tempo_dirty"] = True
            else:
                _upd_tabs()
                _refresh_click()
            return _ref_bar() / bar_t

        def _set_sel(a, b):
            """Zentrale Stelle fuer JEDE Auswahl-Aenderung (Maus, Zeitfelder,
            Schrittknoepfe, Part-Klick): begrenzen, Felder nachziehen, neu
            zeichnen -- und einen LAUFENDEN Loop sofort mitziehen."""
            if a is None or b is None:
                st["sel"] = None
            else:
                lo, hi = (float(a), float(b)) if a <= b else (float(b), float(a))
                st["sel"] = (max(0.0, lo), min(total, hi))
            _retempo_cur()                     # Faktor folgt der Auswahl
            _sched_all()                       # Mashup-Vorschau nachziehen
            p = st["player"]
            # NUR beim Song-Player: dessen Zeitachse ist der Song. Der Part-/
            # Mashup-Player kennt nur den fertigen Ausschnitt -- dort wuerden
            # Song-Zeiten den Loop zerstoeren (er wird ueber _sched_all neu
            # aufgebaut).
            if (st.get("looping") and p is not None and not st.get("pmode")
                    and st["sel"] and st["sel"][1] - st["sel"][0] > 0.05):
                # Der Player springt selbst hinein, wenn die Abspielposition
                # ausserhalb liegt -- winzige Korrekturen laufen nahtlos weiter.
                p.set_loop(st["sel"][0], st["sel"][1])
            _sync_fields()
            _draw()
            _upd_status()

        def _sync_fields():
            if not st["sel"]:
                vals = ("", "", "", "")
            else:
                a, b = st["sel"]
                if cur != master:
                    # Nicht-Master: die ANGEGEBENE Taktzahl (Vorgabe, nie krumm)
                    bt = str(_cur_bars())
                else:
                    # Taktzahl aus der LAENGE (mit einer Stelle, damit man sieht,
                    # ob die Auswahl krumm ist: 4.4 statt glatt 4)
                    nb = (b - a) / bar_t
                    bt = (f"{nb:.0f}" if abs(nb - round(nb)) < 0.02
                          else f"{nb:.1f}")
                vals = (_fmt_time(a), _fmt_time(b), _fmt_time(b - a), bt)
            e_a.set(vals[0])
            e_b.set(vals[1])
            e_len.set(vals[2])
            e_bars.set(vals[3])
            st["shown"] = vals             # Merker: was steht gerade drin?

        def _apply_times():
            """Zeitfelder uebernehmen. Entscheidend ist, WELCHES Feld der Nutzer
            angefasst hat: eine geaenderte Laenge zieht das Ende nach, sonst
            gelten Start und Ende."""
            sa, sb, sl, sbar = st.get("shown", ("", "", "", ""))
            ga, gb, gl = e_a.get().strip(), e_b.get().strip(), e_len.get().strip()
            gbar = e_bars.get().strip()
            a, b, ln = (_parse_time_str(ga), _parse_time_str(gb),
                        _parse_time_str(gl))
            try:
                nb = float(gbar.replace(",", ".")) if gbar else None
            except ValueError:
                nb = None
            if a is None and b is None and ln is None and nb is None:
                stat.config(text="Zeitangabe nicht verstanden – z. B. 1:23.456, "
                            "83,4 oder 83")
                _sync_fields()
                return
            sel0 = st["sel"] or (0.0, 0.0)
            if a is None:
                a = sel0[0]
            bars_new = (gbar != sbar and nb is not None and nb > 0)
            # „Übernehmen“ mit unveraenderter Zahl BESTAETIGT eine nur
            # angenommene Taktzahl (der Hinweis verschwindet dann)
            if (nb is not None and nb > 0 and cur != master
                    and songs[cur].get("bars_auto")):
                bars_new = True
            if bars_new and cur != master:
                # Nicht-Master: die Taktzahl ist die VORGABE. Der Bereich bleibt
                # wie markiert -- er wird spaeter aufs Zieltempo gedehnt.
                songs[cur]["bars"] = max(1, int(round(nb)))
                songs[cur].pop("bars_auto", None)      # jetzt vom Nutzer gesetzt
                if gl != sl and ln is not None:
                    b = a + ln
                elif b is None:
                    b = sel0[1]
            elif bars_new:
                # Tempo-Master: Laenge EXAKT auf ganze Takte legen -> geloopter
                # Bereich und exportierter Part sind dann identisch
                b = a + max(1, int(round(nb))) * bar_t
            elif gl != sl and ln is not None:          # Laenge wurde geaendert
                b = a + ln
            elif b is None:
                b = sel0[1]
            if b <= a:
                stat.config(text="Das Ende muss nach dem Start liegen.")
                _sync_fields()
                return
            _set_sel(a, b)
            if bars_new and cur != master:
                _msg_factor(f"{songs[cur]['bars']} Takte für den Loop "
                            f"({_fmt_time(b - a)})")

        def _msg_factor(what):
            """Statuszeile fuer die Nicht-Master-Songs: was gilt -- und welcher
            Dehnfaktor sich daraus zum Zieltempo ergibt."""
            f = _ref_bar() / bar_t
            stat.config(text=f"„{songs[cur]['title']}“: {what} → {bpm:.2f} BPM. "
                        f"Zieltempo {_mbpm():.2f} BPM "
                        f"(„{songs[master]['title']}“): "
                        f"×{f:.4f} ({(f - 1) * 100:+.1f} %) – "
                        + ("der Bereich wird beim Hören und beim Export "
                           "entsprechend gedehnt." if abs(f - 1.0) > 1e-4
                           else "passt genau, keine Dehnung nötig."))

        def _tempo_from_sel():
            """DER musikalische Weg: Der markierte Loop IST der Maßstab. Man
            sagt, wie viele Takte er umfasst -- daraus folgen Tempo und
            Taktraster (Takt-1 auf den Loop-Start). Die automatische
            BPM-Schaetzung wird damit ueberstimmt, und weil das Raster fuer den
            ganzen Song gilt, haben alle weiteren Parts dasselbe Tempo.

            Bei Songs ab 2 passiert genau das schon automatisch (jede
            Auswahl-Aenderung rechnet den Faktor neu) -- der Knopf uebernimmt
            dort nur die eingetragene Taktzahl."""
            nonlocal bpm, bar_t, t_db, n_bars
            if not st["sel"]:
                stat.config(text="Erst den Loop markieren, dann die Taktzahl "
                            "eintragen und „Tempo aus Auswahl“ drücken.")
                return
            a, b = st["sel"]
            try:
                nb = int(round(float(e_bars.get().strip().replace(",", "."))))
            except ValueError:
                nb = 0
            if nb < 1:
                stat.config(text="Bitte im Feld „Takte“ eintragen, wie viele "
                            "Takte der Loop umfasst (z. B. 4).")
                return
            if cur != master:
                # Nicht das Zieltempo aendern, sondern die Taktzahl uebernehmen
                if not (20.0 <= 4.0 * 60.0 / ((b - a) / nb) <= 400.0):
                    stat.config(text=f"{nb} Takte in {_fmt_time(b - a)} ergäben "
                                "ein unmögliches Tempo – Taktzahl prüfen.")
                    return
                songs[cur]["bars"] = nb
                songs[cur].pop("bars_auto", None)
                _retempo_cur()
                _sync_fields()
                _refresh_list()
                _draw()
                _msg_factor(f"{nb} Takte für den Loop ({_fmt_time(b - a)})")
                return
            new_bar = (b - a) / nb
            new_bpm = 4.0 * 60.0 / new_bar
            if not (20.0 <= new_bpm <= 400.0):
                stat.config(text=f"Daraus ergäbe sich {new_bpm:.1f} BPM – das "
                            "passt nicht. Taktzahl prüfen.")
                return
            old = bpm
            bpm, bar_t = new_bpm, new_bar
            # Raster so legen, dass der Loop-Start GENAU auf eine Taktlinie
            # faellt (Takt-1 bleibt dabei im ersten Takt des Stuecks)
            t_db = a - np.floor(a / bar_t) * bar_t
            n_bars = max(1, int((total - t_db) / bar_t))
            dblbl.config(text=f"{t_db:.2f} s")
            _refresh_click()               # Klick auf das neue Tempo bringen
            _upd_head()
            _sync_fields()
            _refresh_list()
            _draw()
            songs[cur]["bpm"] = bpm
            songs[cur]["t_db"] = t_db
            _upd_tabs()
            msg = (f"Tempo aus dem Loop: {nb} Takte in {_fmt_time(b - a)} → "
                   f"{bpm:.2f} BPM (vorher {old:.2f}). Takt-1 auf "
                   f"{_fmt_time(t_db)}.")
            if cur == master:
                msg += (" Dieser Song ist der Tempo-Master – alle anderen "
                        "richten sich danach.")
            else:
                f = _ref_bar() / bar_t
                msg += (f" Zum Zieltempo {_mbpm():.2f} BPM: ×{f:.4f} "
                        f"({(f - 1) * 100:+.1f} %).")
            stat.config(text=msg)

        NUDGE = 0.001                      # kleinster Schritt = 1 ms (Anzeige)

        def _nudge(which, sign):
            """Marker per Knopf um den kleinsten Zeitschritt verschieben.
            Start/Ende bleiben dabei immer mindestens einen Schritt auseinander;
            bei „Länge“ wandert das Ende."""
            if not st["sel"]:
                stat.config(text="Erst einen Bereich markieren.")
                return
            a, b = st["sel"]
            d = sign * NUDGE
            if which == "a":
                _set_sel(min(max(0.0, a + d), b - NUDGE), b)
            elif which == "b":
                _set_sel(a, max(b + d, a + NUDGE))
            elif which == "bars":
                if cur != master:
                    # Nicht-Master: die VORGABE aendern (Auswahl bleibt stehen,
                    # der Dehnfaktor zieht nach)
                    songs[cur]["bars"] = max(1, _cur_bars() + sign)
                    songs[cur].pop("bars_auto", None)
                    _set_sel(a, b)
                    _msg_factor(f"{songs[cur]['bars']} Takte für den Loop "
                                f"({_fmt_time(b - a)})")
                else:
                    # Tempo-Master: ganze Takte, Laenge wandert mit
                    nb = max(1, int(round((b - a) / bar_t)) + sign)
                    _set_sel(a, a + nb * bar_t)
            else:                          # Laenge: Ende nachziehen
                _set_sel(a, max(a + NUDGE, b + d))

        def _upd_status():
            if not st["sel"]:
                stat.config(text="Auswahl: – (in die Wellenform ziehen; Ränder "
                            "greifen verschiebt einzeln, Mitte den ganzen Loop; "
                            "Strg+Mausrad zoomt)")
                return
            a, b = st["sel"]
            sb = _sel_bars()
            txt = (f"Auswahl: {_fmt_time(a)} – {_fmt_time(b)}  "
                   f"(Länge {_fmt_time(b - a)})")
            if sb:
                nb = sb[1] - sb[0]
                soll = nb * bar_t
                txt += f"   ·   als Part: {nb} Takte ({_fmt_time(soll)})"
                if cur != master:
                    # Nicht-Master: was daraus im Zieltempo wird
                    f = _ref_bar() / bar_t
                    txt += (f"   ·   ×{f:.4f} → {_fmt_time(nb * _ref_bar())} "
                            f"bei {_mbpm():.1f} BPM")
                    ga = songs[cur].get("bars_auto")
                    if ga:
                        txt += (f"   ⚠ Taktzahl angenommen: {ga} – im Feld "
                                "„Takte“ prüfen")
                # Weicht die Auswahl von ganzen Takten ab, wird der Part
                # entsprechend gekuerzt/verlaengert -- das muss man sehen.
                elif abs(soll - (b - a)) > 0.02:
                    d = soll - (b - a)
                    txt += (f"  ⚠ {abs(d) * 1000:.0f} ms "
                            f"{'länger' if d > 0 else 'kürzer'} als die "
                            "Auswahl – „Takte“ setzt es exakt")
            else:
                txt += "   ·   für einen Part zu kurz (unter einem Takt)"
            p = st["player"]
            if st.get("looping") and p is not None and p.is_playing():
                txt += ("   ·   ▶ Mashup-Vorschau läuft (zieht automatisch nach)"
                        if st.get("pmode") == "all"
                        else "   ·   ▶ Loop läuft (folgt den Markern)")
            stat.config(text=txt)

        # ---------------- Parts ----------------
        lst = None                                     # Vorwaertsdeklaration

        # Ein PART hat eine gemeinsame Taktzahl und eine oder mehrere QUELLEN
        # {'song', 't0', 'tracks'} -- so entsteht das Mashup: Drums aus Song 1,
        # Bass aus Song 2 usw. Jede Quelle liefert dieselbe Anzahl Takte, im
        # Tempo IHRES Songs; beim Hoeren/Export wird aufs Zieltempo gedehnt.
        def _ref_bar():
            return 4.0 * 60.0 / _mbpm()

        def _song_bar(i):
            return 4.0 * 60.0 / float(songs[i]["bpm"])

        def _src_len(q, bars):
            """Dauer, die eine Quelle in IHREM Song belegt (Sekunden)."""
            return bars * _song_bar(q["song"])

        def _cur_srcs(p):
            """Quellen eines Parts, die zum gerade sichtbaren Song gehoeren."""
            return [q for q in p.get("srcs", []) if q["song"] == cur]

        def _refresh_list(sel_i=None):
            lst.delete(0, "end")
            for p in st["parts"]:
                bits = []
                for q in p.get("srcs", []):
                    trk = "+".join(core.STEM_LABELS.get(t, t)
                                   for t in q["tracks"])
                    bits.append(f"{trk}←{q['song'] + 1}")
                srcs = p.get("srcs") or []
                t0 = _fmt_time(srcs[0]["t0"]) if srcs else "–"
                # Dauer = Takte im ZIELTEMPO (so lang wird der Deluge-Clip)
                dur = _fmt_time(p["bars"] * _ref_bar())
                lst.insert("end", f"{p['label']:>4}  {t0}  {p['bars']} Takte "
                           f"({dur})  " + ", ".join(bits))
            if sel_i is not None and 0 <= sel_i < len(st["parts"]):
                lst.selection_clear(0, "end")
                lst.selection_set(sel_i)
            _draw()

        def _sel_src():
            """Aktuelle Auswahl als QUELLE beschreiben (Song, Start, Spuren)."""
            if not st["sel"]:
                stat.config(text="Erst einen Bereich in der Wellenform ziehen.")
                return None, 0
            a, b = st["sel"]
            bars = _cur_bars()
            if bars < 1:
                stat.config(text="Der Bereich ist kürzer als ein Takt.")
                return None, 0
            trk = [n for n in names if shown[n].get()]
            if not trk:
                stat.config(text="Keine Spur aktiv – was gehört werden soll, "
                            "kommt in den Part.")
                return None, 0
            return {"song": cur, "t0": float(a), "tracks": trk}, bars

        def _next_num():
            """Naechste freie Part-Nummer (1a, 2a, …)."""
            used = set()
            for p in st["parts"]:
                m = re.match(r"(\d+)", p["label"])
                if m:
                    used.add(int(m.group(1)))
            return next(i for i in range(1, 999) if i not in used)

        def _add_part():
            """Neuen Part aus der Auswahl anlegen (erste Quelle = aktive Spuren)."""
            q, bars = _sel_src()
            if q is None:
                return
            num = _next_num()
            st["parts"].append({"label": f"{num}a", "bars": bars, "srcs": [q]})
            _refresh_list(len(st["parts"]) - 1)
            trk = ", ".join(core.STEM_LABELS.get(t, t) for t in q["tracks"])
            krumm = abs((st["sel"][1] - st["sel"][0]) - bars * bar_t) > 0.02
            stat.config(text=f"Part {num}a angelegt: {bars} Takte, {trk} aus "
                        f"„{songs[cur]['title']}“ ab {_fmt_time(q['t0'])}"
                        + ("  ⚠ die Auswahl passt nicht genau auf ganze Takte – "
                           "„⟲ Tempo aus Auswahl“ macht daraus ein exaktes "
                           "Raster" if krumm else "")
                        + ". Weitere Spuren aus anderen Songs mit „+ zu Part“.")

        def _add_to_part():
            """Auswahl als WEITERE Quelle an den gewaehlten Part haengen -- so
            entsteht die Kombination (z. B. Bass aus Song 2 zu Drums aus 1)."""
            i = _cur_index()
            if i is None:
                stat.config(text="Erst in der Liste den Part wählen, zu dem die "
                            "Auswahl gehören soll.")
                return
            q, bars = _sel_src()
            if q is None:
                return
            p = st["parts"][i]
            if bars != p["bars"]:
                stat.config(text=f"Part „{p['label']}“ hat {p['bars']} Takte, "
                            f"die Auswahl {bars}. Alle Quellen eines Parts "
                            "müssen gleich viele Takte umfassen – Taktzahl "
                            "anpassen (Feld „Takte“).")
                return
            # gleiche Spur aus demselben Song ersetzen statt doppeln
            p["srcs"] = [x for x in p["srcs"]
                         if not (x["song"] == q["song"]
                                 and set(x["tracks"]) == set(q["tracks"]))]
            p["srcs"].append(q)
            _refresh_list(i)
            trk = ", ".join(core.STEM_LABELS.get(t, t) for t in q["tracks"])
            stat.config(text=f"{trk} aus „{songs[cur]['title']}“ zu Part "
                        f"„{p['label']}“ hinzugefügt ({len(p['srcs'])} Quellen). "
                        "„▶ Part hören“ spielt die Kombination im Zieltempo.")

        def _add_part_all():
            """Einen Part aus den Loops ALLER Songs auf einmal anlegen -- der
            direkte Weg zum Mashup: in jedem Song den passenden Loop markieren,
            die Spuren anhaken, hier einmal druecken."""
            p, note = _live_part()
            if p is None:
                stat.config(text=note)
                return
            num = _next_num()
            st["parts"].append({"label": f"{num}a", "bars": p["bars"],
                                "srcs": [dict(q) for q in p["srcs"]]})
            _refresh_list(len(st["parts"]) - 1)
            bits = ", ".join(
                "+".join(core.STEM_LABELS.get(t, t) for t in q["tracks"])
                + f"←{q['song'] + 1}" for q in p["srcs"])
            stat.config(text=f"Part {num}a angelegt: {p['bars']} Takte aus "
                        f"{len(p['srcs'])} Song(s) – {bits}."
                        + (f"  ⚠ {note}" if note else "")
                        + "  „▶ Part hören“ spielt die Kombination im "
                        "Zieltempo.")

        def _select_part(i):
            if not (0 <= i < len(st["parts"])):
                return
            p = st["parts"][i]
            namev.set(p["label"])
            lst.selection_clear(0, "end")
            lst.selection_set(i)
            # Hat der Part eine Quelle im SICHTBAREN Song, dessen Bereich zeigen
            q = _cur_srcs(p)
            if q:
                _set_sel(q[0]["t0"], q[0]["t0"] + _src_len(q[0], p["bars"]))
            else:
                sng = sorted({x["song"] + 1 for x in p.get("srcs", [])})
                stat.config(text=f"Part „{p['label']}“ hat hier keine Quelle – "
                            f"er nutzt Song {', '.join(map(str, sng))}. "
                            "„▶ Part hören“ spielt ihn trotzdem.")

        def _cur_index():
            s = lst.curselection()
            return int(s[0]) if s else None

        def _del_part():
            i = _cur_index()
            if i is None:
                stat.config(text="Erst einen Part in der Liste wählen.")
                return
            st["parts"].pop(i)
            _refresh_list()
            stat.config(text=f"Part gelöscht – noch {len(st['parts'])}.")

        def _rename_part():
            i = _cur_index()
            # Das Label landet im WAV-Dateinamen -> Pfad-Sonderzeichen raus
            nm = re.sub(r"[^0-9A-Za-zÄÖÜäöüß _-]", "", namev.get()).strip()[:12]
            if i is None or not nm:
                stat.config(text="Part wählen und einen Namen eintragen "
                            "(Buchstaben/Ziffern).")
                return
            st["parts"][i]["label"] = nm
            _refresh_list(i)
            stat.config(text="Umbenannt. Gleiche Namen = gleiche Farbe auf der "
                        "Deluge (z. B. 1a und 1b für zwei Strophen).")

        def _auto_parts():
            stat.config(text="erkenne Abschnitte automatisch … (dauert kurz)")

            def _work():
                try:
                    secs = core.detect_sections(stems, sr, t_db=t_db, bpm=bpm,
                                                target_bars=8)

                    def _apply():
                        if not secs:
                            stat.config(text="Keine Abschnitte erkannt.")
                            return
                        st["parts"] = [
                            {"t0": t_db + int(s["start_bar"]) * bar_t,
                             "t1": t_db + int(s["end_bar"]) * bar_t,
                             "label": s.get("label", "1a")} for s in secs]
                        _refresh_list()
                        stat.config(text=f"{len(secs)} Abschnitte vorgeschlagen – "
                                    "jetzt anhören und nachjustieren.")
                    self.root.after(0, _apply)
                except Exception as ex:
                    self.root.after(0, lambda e=ex: stat.config(
                        text=f"Automatik fehlgeschlagen: {e}"))
            threading.Thread(target=_work, daemon=True).start()

        # ---------------- Wiedergabe ----------------
        def _click_track():
            """Metronom ueber die ganze Laenge: Klick auf jedem Beat, jede
            Takt-1 hoeher und lauter (accent=4). Sitzt der Loop im Tempo,
            laeuft der Klick synchron zur Musik -- driftet er, stimmt das
            Raster (oder die Loop-Laenge) nicht."""
            return core.metronome_click(t_db, bar_t / 4.0, total, sr, accent=4)

        def _apply_gains():
            p = st["player"]
            if p is None:
                return
            if st.get("pmode"):
                # Part-/Mashup-Player: andere Spuren in anderer Reihenfolge --
                # regelbar ist hier nur der Klick. Spur-Haekchen loesen bei der
                # Mashup-Vorschau stattdessen einen Neuaufbau aus (_sched_all).
                p.set_gain(int(st.get("ptracks") or 0),
                           (clickv.get() / 100.0) if click_on.get() else 0.0)
                return
            for k, n in enumerate(names):
                p.set_gain(k, 1.0 if shown[n].get() else 0.0)
            # letzte Spur = Klick
            p.set_gain(len(names),
                       (clickv.get() / 100.0) if click_on.get() else 0.0)

        def _refresh_click():
            """Klickspur nach Tempo-/Downbeat-/Raster-Wechsel neu erzeugen.
            Nur beim Song-Player: im Part-/Mashup-Player liegt die Klickspur an
            anderer Stelle (und ist dort ohnehin schon im Zieltempo) -- ein
            blinder Austausch wuerde eine Musikspur ueberschreiben."""
            p = st["player"]
            if p is not None and not st.get("pmode"):
                p.replace_stem(len(names), _click_track())

        def _ensure_player():
            if st.get("pmode"):            # Part-Player hat andere Spuren
                _kill_player()
            if st["player"] is None:
                # Grosszuegig puffern: hier wird nur vorgehoert, Latenz ist
                # egal -- Aussetzer waeren fatal (siehe StemPlayer.latency).
                # Letzte Spur ist die Klickspur (per Regler dazumischbar).
                p = core.StemPlayer([stems[n] for n in names] + [_click_track()],
                                    sr, names=list(names) + ["click"],
                                    blocksize=4096, latency="high")
                p.start_stream()
                st["player"] = p
                self._stem_players.append(p)
                _apply_gains()
            return st["player"]

        def _play(loop=True):
            # EXAKT die eingestellte Auswahl loopen (nicht die Takt-Rundung) --
            # sonst hoert man nicht das, was man gerade eingestellt hat.
            if not st["sel"]:
                stat.config(text="Erst einen Bereich (oder Part) wählen.")
                return
            a, b = st["sel"]
            if b - a < 0.05:
                stat.config(text="Der Bereich ist zu kurz zum Abspielen.")
                return
            try:
                p = _ensure_player()
            except Exception as ex:
                stat.config(text=f"Wiedergabe nicht möglich: {ex}")
                return
            st["looping"] = bool(loop)     # merkt: Loop live nachfuehren
            if loop:
                p.set_loop(a, b)
            else:
                p.set_loop(None, None)
            p.seek(a)
            p.play()
            _tick()
            stat.config(text=(f"Loop läuft: {_fmt_time(a)} – {_fmt_time(b)} "
                              f"(Länge {_fmt_time(b - a)}) – trägt der Part?"
                              if loop else f"Wiedergabe ab {_fmt_time(a)}."))

        def _stop():
            p = st["player"]
            if p is not None:
                p.pause()
            st["looping"] = False
            st["cursor"] = None
            _draw_cursor()

        def _tick():
            p = st["player"]
            if p is None or not win.winfo_exists():
                return
            if p.is_playing():
                st["cursor"] = p.position()[0]
                _draw_cursor()
                # 10x/s reicht fuer die Laufmarke und laesst dem Audio-Thread
                # mehr Luft (jedes Zeichnen haelt kurz den GIL)
                win.after(100, _tick)
            else:
                st["cursor"] = None
                _draw_cursor()

        # ---------------- Part (Mashup) vorhoeren ----------------
        def _fit(a, n):
            """Array auf genau n Samples bringen (kuerzen/mit Stille fuellen)."""
            a = np.asarray(a, dtype=np.float32)
            if a.ndim == 1:
                a = a.reshape(-1, 1)
            if a.shape[0] >= n:
                return np.ascontiguousarray(a[:n])
            return np.concatenate(
                [a, np.zeros((n - a.shape[0], a.shape[1]), dtype=np.float32)])

        def _stretched(si, t0, bars, nm, n, log=None):
            """EINE Quellspur ausschneiden und aufs Zieltempo dehnen -- mit
            Zwischenspeicher. Der Phase-Vocoder ist teuer; beim Nachziehen der
            Mashup-Vorschau darf nur der Song neu gerechnet werden, dessen Loop
            sich geaendert hat."""
            s = songs[si]
            sb = _song_bar(si)
            mode = s.get("mode", "pitch")
            key = (si, round(float(t0), 6), int(bars), nm, mode,
                   int(s.get("gen", 0)), round(float(s["bpm"]), 6), int(n))
            cache = st.setdefault("scache", {})
            got = cache.get(key)
            if got is not None:
                return got
            raw = s["stems"].get(nm)
            if raw is None:                    # Spur gibt es in dem Song nicht
                return None
            a = np.asarray(raw)
            i0 = int(round(float(t0) * sr))
            i1 = i0 + int(round(bars * sb * sr))
            seg = a[i0:min(i1, len(a))]
            f = _ref_bar() / sb                # >1 = langsamer machen
            if abs(f - 1.0) > 1e-4:
                if log:
                    log(f"  {s['title']}/{nm}: ×{f:.4f} ({mode})")
                seg = core.stretch_audio(seg, sr, f, mode=mode)
            seg = _fit(seg, n)
            if len(cache) > 48:                # Speicher im Zaum halten
                cache.clear()
            cache[key] = seg
            return seg

        def _build_part_audio(p, log=None):
            """Alle Quellen eines Parts ausschneiden, aufs ZIELTEMPO (Master)
            dehnen und auf gleiche Laenge bringen. Rueckgabe (spuren, namen)."""
            n = int(round(p["bars"] * _ref_bar() * sr))
            tracks, labs = [], []
            for q in p.get("srcs", []):
                for nm in q["tracks"]:
                    seg = _stretched(q["song"], q["t0"], p["bars"], nm, n,
                                     log=log)
                    if seg is None:
                        continue
                    tracks.append(seg)
                    labs.append(f"{q['song'] + 1}.{nm}")
            return tracks, labs

        def _part_player_from(p, mode, what, note=""):
            """Fertige Part-/Vorschau-Spuren im Loop abspielen: im Hintergrund
            schneiden und dehnen, dann im Tk-Thread starten."""
            def _work():
                try:
                    tracks, labs = _build_part_audio(p)
                    if not tracks:
                        self.root.after(0, lambda: stat.config(
                            text="Keine brauchbare Quelle – ist in den Songs "
                            "eine Spur aktiv?"))
                        return
                    n = tracks[0].shape[0]
                    # Klick genau so lang wie der Part (metronome_click gibt
                    # ein Sample mehr zurueck)
                    clk = _fit(core.metronome_click(0.0, _ref_bar() / 4.0,
                                                    n / sr, sr, accent=4), n)

                    def _go():
                        _kill_player()
                        try:
                            pl = core.StemPlayer(tracks + [clk], sr,
                                                 names=labs + ["click"],
                                                 blocksize=4096, latency="high")
                            pl.start_stream()
                        except Exception as ex:
                            stat.config(text=f"Wiedergabe nicht möglich: {ex}")
                            return
                        st["player"] = pl
                        self._stem_players.append(pl)
                        pl.set_gain(len(tracks),
                                    (clickv.get() / 100.0)
                                    if click_on.get() else 0.0)
                        pl.set_loop(0.0, n / sr)
                        st["looping"] = True
                        pl.seek(0.0)
                        pl.play()
                        # Part-Player: andere Spuren als der Song-Player
                        st["pmode"] = mode
                        st["plabs"], st["pn"] = list(labs), n
                        st["ptracks"] = len(tracks)
                        stat.config(
                            text=f"▶ {what} läuft im Loop: {p['bars']} Takte, "
                            f"{_mbpm():.2f} BPM, " + ", ".join(labs)
                            + ".  („■ Stop“ beendet)"
                            + (f"   ⚠ {note}" if note else ""))
                    self.root.after(0, _go)
                except Exception as ex:
                    self.root.after(0, lambda e=ex: stat.config(
                        text=f"Vorschau fehlgeschlagen: {e}"))
            threading.Thread(target=_work, daemon=True).start()

        def _play_part():
            """Den gewaehlten Part als KOMBINATION hoeren: alle Quellen im
            Zieltempo, gemischt und geloopt -- so beurteilt man das Mashup,
            bevor etwas exportiert wird."""
            i = _cur_index()
            if i is None:
                stat.config(text="Erst einen Part in der Liste wählen.")
                return
            p = st["parts"][i]
            _stop()
            stat.config(text=f"bereite Part „{p['label']}“ vor "
                        f"({len(p.get('srcs', []))} Quellen, dehnen …)")
            win.update_idletasks()
            _part_player_from(p, "part", f"Part „{p['label']}“")

        # ------- Alle Songs parallel: die eigentliche Mashup-Arbeit -------
        def _live_part():
            """Aus JEDEM Song die aktuelle Auswahl als Quelle -- das ist die
            Mashup-Vorschau: die markierten Loops laufen PARALLEL, jeder mit
            seinen aktiven Spuren und aufs Zieltempo gedehnt.
            Rueckgabe (part-dict oder None, Meldung)."""
            _save_song_state()                 # aktiven Song mitnehmen
            srcs, bars, miss = [], {}, []
            for i, s in enumerate(songs):
                sel = s.get("sel")
                if not sel:
                    miss.append(f"{i + 1}. {s['title']}: kein Loop markiert")
                    continue
                shw = s.get("shown") or {}
                trk = [n for n in s["names"] if shw.get(n, True)]
                if not trk:
                    miss.append(f"{i + 1}. {s['title']}: keine Spur aktiv")
                    continue
                a, b = sorted(sel)
                n = int(s.get("bars") or 0) if i != master else 0
                if n < 1:
                    n = int(round((b - a) / (4.0 * 60.0 / float(s["bpm"]))))
                if n < 1:
                    miss.append(f"{i + 1}. {s['title']}: kürzer als ein Takt")
                    continue
                bars[i] = n
                srcs.append({"song": i, "t0": float(a), "tracks": trk})
            if not srcs:
                return None, ("Kein Song hat einen brauchbaren Loop – "
                              + "; ".join(miss))
            uniq = sorted(set(bars.values()))
            if len(uniq) > 1:
                txt = ", ".join(f"{i + 1}. {songs[i]['title']} = {n}"
                                for i, n in sorted(bars.items()))
                return None, ("Die Loops umfassen verschieden viele Takte "
                              f"({txt}). Für einen gemeinsamen Part müssen alle "
                              "gleich lang sein – im Feld „Takte“ anpassen.")
            return ({"label": "Vorschau", "bars": uniq[0], "srcs": srcs},
                    "; ".join(miss))

        def _play_all():
            """Die Loops ALLER Songs gleichzeitig hoeren (nur aktive Spuren).
            Waehrend das laeuft, kann man die Reiter wechseln und die Loops
            einzeln verschieben -- die Vorschau zieht automatisch nach."""
            p, note = _live_part()
            if p is None:
                stat.config(text=note)
                return
            st["live"] = p
            stat.config(text=f"bereite Mashup-Vorschau vor: {len(p['srcs'])} "
                        f"Song-Loops à {p['bars']} Takte, dehnen …")
            win.update_idletasks()
            _part_player_from(p, "all", "Mashup-Vorschau (alle Songs)", note)

        def _sched_all():
            """Auswahl waehrend der Mashup-Vorschau geaendert: kurz warten (es
            wird ja noch gezogen) und dann neu aufbauen."""
            if st.get("pmode") != "all" or not st.get("looping"):
                return
            j = st.pop("alljob", None)
            if j:
                try:
                    win.after_cancel(j)
                except Exception:
                    pass
            st["alljob"] = win.after(400, _rebuild_all)

        def _rebuild_all():
            """Mashup-Vorschau an die neuen Loops anpassen. Bleiben Spurzahl und
            Laenge gleich, werden die Spuren im laufenden Player getauscht --
            dann hoert man die Aenderung ohne Unterbrechung."""
            st.pop("alljob", None)
            if st.get("pmode") != "all" or not st.get("looping"):
                return
            if st.get("allbusy"):              # laeuft noch -> danach nochmal
                st["allpend"] = True
                return
            p, note = _live_part()
            if p is None:
                stat.config(text=note)
                return
            st["allbusy"] = True

            def _work():
                try:
                    tracks, labs = _build_part_audio(p)
                except Exception as ex:
                    tracks, labs = [], []
                    self.root.after(0, lambda e=ex: stat.config(
                        text=f"Vorschau-Aktualisierung fehlgeschlagen: {e}"))

                def _apply():
                    st["allbusy"] = False
                    pl = st["player"]
                    if st.get("pmode") != "all" or pl is None or not tracks:
                        return
                    n = tracks[0].shape[0]
                    if labs == st.get("plabs") and n == st.get("pn"):
                        for k, a in enumerate(tracks):
                            pl.replace_stem(k, a)          # nahtlos
                        st["live"] = p
                        _upd_status()
                    else:
                        # Spuren oder Laenge anders -> Player neu aufbauen
                        _part_player_from(p, "all",
                                          "Mashup-Vorschau (alle Songs)", note)
                    if st.pop("allpend", False):
                        _sched_all()
                self.root.after(0, _apply)
            threading.Thread(target=_work, daemon=True).start()

        # ---------------- Speichern ----------------
        def _save():
            if not st["parts"]:
                stat.config(text="Noch keine Parts definiert.")
                return
            # Zustand des offenen Songs sichern -- der Export liest die Spuren
            # aus songs[...], nicht aus der Arbeitsvariablen
            _save_song_state()
            # Welche Spuren kommen ueberhaupt vor? (Song+Spur eindeutig benennen)
            occ = []
            for p in st["parts"]:
                for q in p.get("srcs", []):
                    for nm in q["tracks"]:
                        key = (q["song"], nm)
                        if key not in occ:
                            occ.append(key)
            labels = {}
            for si, nm in occ:
                labels[f"{si}:{nm}"] = (
                    core.STEM_LABELS.get(nm, nm) if len(songs) == 1 else
                    f"{si + 1}. {core.STEM_LABELS.get(nm, nm)}")
            sel = self._part_export_dialog(win, list(labels.keys()),
                                           label_map=labels)
            if not sel:
                return
            cfg = load_config()
            p = filedialog.asksaveasfilename(
                title="Parts-Deluge-Song speichern (.XML; Abschnitts-WAVs daneben)",
                defaultextension=".XML",
                initialfile=core.sanitize_filename(title or "AudioWizard") + "_Parts.XML",
                initialdir=cfg.get("last_save_dir") or "",
                filetypes=[("Deluge-Song", "*.XML"), ("Alle", "*.*")])
            if not p:
                return
            _stop()
            a_keys, m_keys = set(sel["audio"]), set(sel["midi"])
            ref_bpm = _mbpm()
            stat.config(text="dehne Quellen & schreibe Deluge-Song …")
            savebtn.config(state="disabled", text="speichert …")

            def _work():
                try:
                    cb = lambda m: self._msg_later(stat, m)
                    # Je Part und Quellspur EINEN fertigen Clip bauen: aus dem
                    # jeweiligen Song schneiden und aufs Zieltempo dehnen.
                    clips, notes = {}, {}
                    min_ms = float(load_config().get("bass_min_ms", 130))
                    for pi, prt in enumerate(st["parts"]):
                        self._msg_later(
                            stat, f"Part {prt['label']} ({pi + 1}/"
                            f"{len(st['parts'])}): dehne Quellen …")
                        ref = 4.0 * 60.0 / ref_bpm
                        n = int(round(prt["bars"] * ref * sr))
                        for q in prt.get("srcs", []):
                            s = songs[q["song"]]
                            sb = 4.0 * 60.0 / float(s["bpm"])
                            f = ref / sb
                            i0 = int(round(q["t0"] * sr))
                            i1 = i0 + int(round(prt["bars"] * sb * sr))
                            for nm in q["tracks"]:
                                key = f"{q['song']}:{nm}"
                                if key not in a_keys and key not in m_keys:
                                    continue
                                raw = s["stems"].get(nm)
                                if raw is None:
                                    continue
                                a = np.asarray(raw)
                                seg = a[i0:min(i1, len(a))]
                                if abs(f - 1.0) > 1e-4:
                                    seg = core.stretch_audio(
                                        seg, sr, f, mode=s.get("mode", "pitch"))
                                seg = _fit(seg, n)
                                if key in a_keys:
                                    clips.setdefault(key, {})[pi] = seg
                                if key in m_keys:
                                    try:
                                        if nm == "drums":
                                            dmap, dsens = self._drum_settings()
                                            nt = core.drums_to_midi_notes(
                                                seg, sr, mapping=dmap,
                                                sensitivity=dsens)
                                        else:
                                            lo, hi = core.STEM_MIDI_RANGE.get(
                                                nm, (40.0, 2000.0))
                                            nt = core.stem_to_midi_notes(
                                                seg, sr, min_freq=lo, max_freq=hi,
                                                min_note_ms=min_ms,
                                                label=labels.get(key, nm))
                                        notes.setdefault(key, {})[pi] = nt
                                    except Exception as ex:
                                        cb(f"{labels.get(key, key)}→MIDI "
                                           f"übersprungen: {ex}")
                    if not clips:
                        raise RuntimeError("Keine Audio-Spur gewählt.")
                    self._msg_later(stat, "schreibe Deluge-Song …")
                    xmlp, wavs, n_notes = deluge.write_deluge_mashup(
                        p, clips, notes, sr, ref_bpm,
                        [pp["label"] for pp in st["parts"]],
                        [pp["bars"] for pp in st["parts"]],
                        names={k: core.sanitize_filename(v)
                               for k, v in labels.items()}, log=cb)
                    save_config({**load_config(),
                                 "last_save_dir": os.path.dirname(p)})
                    secs = st["parts"]
                    extra = ("  ACHTUNG: Die Deluge kennt 12 Sections – die "
                             "weiteren Parts landen in der letzten."
                             if len(secs) > 12 else "")
                    if len(songs) > 1:
                        extra += (f"  Zieltempo {ref_bpm:.2f} BPM "
                                  f"(„{songs[master]['title']}“); Quellen aus "
                                  "den anderen Songs wurden darauf gedehnt.")
                    msg = (f"Gespeichert: {os.path.basename(xmlp)} – {len(secs)} "
                           f"Parts, {len(wavs)} Audio-Clips"
                           + (f" + {n_notes} MIDI-Noten" if n_notes else
                              " (ohne MIDI)")
                           + ". XML → SONGS/, WAVs → SAMPLES/AudioWizard/ "
                           "auf die SD." + extra)

                    def _done():
                        stat.config(text=msg)
                        savebtn.config(state="normal",
                                       text="Deluge-Song speichern…")
                        # Klare Rueckmeldung -- die Statuszeile allein wird
                        # leicht uebersehen
                        messagebox.showinfo("Deluge-Song gespeichert",
                                            msg.replace("  ", "\n\n"),
                                            parent=win)
                    self.root.after(0, _done)
                except Exception as ex:
                    def _err(e=ex):
                        stat.config(text=f"Fehler: {e}")
                        savebtn.config(state="normal",
                                       text="Deluge-Song speichern…")
                        messagebox.showerror("Speichern fehlgeschlagen",
                                             str(e), parent=win)
                    self.root.after(0, _err)
            threading.Thread(target=_work, daemon=True).start()

        # ---------------- Bedienleisten ----------------
        # Zeile 1: Parts anlegen
        r1 = tk.Frame(win, bg=COL_BG)
        self._small_button(r1, "➕ neuer Part", _add_part).pack(side="left")
        self._small_button(r1, "➕ Part aus ALLEN Songs", _add_part_all).pack(
            side="left", padx=(4, 0))
        self._small_button(r1, "＋ zu Part", _add_to_part).pack(
            side="left", padx=(4, 0))
        self._small_button(r1, "Automatisch vorschlagen", _auto_parts).pack(
            side="left", padx=(16, 0))
        # Zeile 2: Hoeren
        r1b = tk.Frame(win, bg=COL_BG)
        self._small_button(r1b, "▶ Auswahl loopen", lambda: _play(True)).pack(
            side="left")
        self._small_button(r1b, "▶ Alle Songs parallel", _play_all).pack(
            side="left", padx=(4, 0))
        self._small_button(r1b, "▶ Part hören", _play_part).pack(
            side="left", padx=(4, 0))
        self._small_button(r1b, "■ Stop", _stop).pack(side="left", padx=(4, 0))
        # Metronom zum Beurteilen des Loops: Klick im Tempo, Takt-1 betont
        tk.Checkbutton(r1b, text="🥁 Click", variable=click_on,
                       command=lambda: _apply_gains(), font=self.f_small,
                       bg=COL_BG, fg=COL_FG, selectcolor=COL_SURFACE,
                       activebackground=COL_BG, activeforeground=COL_FG, bd=0,
                       highlightthickness=0).pack(side="left", padx=(16, 0))
        tk.Scale(r1b, variable=clickv, from_=0, to=100, orient="horizontal",
                 length=90, showvalue=False, command=lambda _v: _apply_gains(),
                 bg=COL_BG, fg=COL_FG, troughcolor=COL_SURFACE,
                 activebackground=COL_ACCENT, highlightthickness=0, bd=0,
                 sliderrelief="flat", width=10).pack(side="left")
        cvlbl = tk.Label(r1b, text=f"{clickv.get()}%", font=self.f_tiny,
                         bg=COL_BG, fg=COL_MUTED, width=4)
        cvlbl.pack(side="left")
        clickv.trace_add("write",
                         lambda *_a: cvlbl.config(text=f"{clickv.get()}%"))

        body = tk.Frame(win, bg=COL_BG)
        lst = tk.Listbox(body, height=4, width=52, bg=COL_SURFACE, fg=COL_FG,
                         font=self.f_tiny, bd=0, highlightthickness=0,
                         selectbackground=COL_SURF_HI, activestyle="none")
        lst.pack(side="left", fill="x", expand=True)
        lst.bind("<<ListboxSelect>>",
                 lambda e: (_select_part(_cur_index())
                            if _cur_index() is not None else None))
        lst.bind("<Double-Button-1>", lambda e: _play(True))
        side = tk.Frame(body, bg=COL_BG)
        side.pack(side="left", padx=(10, 0))
        namev = tk.StringVar(value="")
        tk.Entry(side, textvariable=namev, width=10, font=self.f_small,
                 bg=COL_SURFACE, fg=COL_FG, insertbackground=COL_FG, bd=0,
                 highlightthickness=0).pack(anchor="w")
        self._small_button(side, "Umbenennen", _rename_part).pack(anchor="w")
        self._small_button(side, "Part löschen", _del_part).pack(anchor="w")
        self._small_button(side, "Alle löschen",
                           lambda: (st.update(parts=[]), _refresh_list())).pack(
                               anchor="w")

        hint = tk.Label(win, text="Bereich ziehen → „Auswahl loopen“ zum Prüfen "
                        "→ „neuer Part“. Ränder greifen verschiebt einzeln, "
                        "Mitte den ganzen Loop. Gleiche Namen (1a/1b) = gleiche "
                        "Farbe auf der Deluge.   MASHUP: „+ Song laden…“ → in "
                        "jedem Song Loop markieren und „Takte“ eintragen → "
                        "„▶ Alle Songs parallel“ (Reiter wechseln geht dabei) → "
                        "„➕ Part aus ALLEN Songs“.",
                        font=self.f_tiny, bg=COL_BG, fg=COL_MUTED,
                        justify="left", wraplength=1050)

        r2 = tk.Frame(win, bg=COL_BG)
        savebtn = tk.Button(r2, text="Deluge-Song speichern…", command=_save,
                            font=self.f_btn, bg="#1D9E75", fg="#04342C",
                            activebackground=COL_OK, activeforeground="#04342C",
                            bd=0, padx=20, pady=6, highlightthickness=0,
                            cursor="hand2")
        savebtn.pack(side="left", padx=4)

        # ---------------- Songs (Mashup) ----------------
        def _kill_player():
            p = st["player"]
            if p is not None:
                try:
                    p.stop()
                except Exception:
                    pass
                if p in self._stem_players:
                    try:
                        self._stem_players.remove(p)
                    except ValueError:
                        pass
                st["player"] = None
            st["looping"] = False
            st["cursor"] = None
            st["pmode"] = False

        def _save_song_state():
            # stems/names MITSICHERN: nach einem Raster-Wechsel enthaelt die
            # Arbeitsvariable andere Arrays als beim Laden -- ohne das wuerden
            # Vorschau und Export das alte Audio dieses Songs benutzen.
            songs[cur].update(
                sel=st["sel"], view=(st["t0"], st["dur"]), peaks=st["peaks"],
                mixpk=st["mixpk"], bpm=bpm, t_db=t_db, grid=st["grid"],
                db_orig=st.get("db_orig"), orig=orig,
                stems=dict(stems), names=list(names),
                shown={n: bool(v.get()) for n, v in shown.items()})

        def _load_song_state(msg=None):
            """Zustand des aktiven Songs in die Arbeitsvariablen holen. msg wird
            ZULETZT gesetzt (nach dem Wellenform-Aufbau), sonst ueberschreibt
            ihn die Standard-Statuszeile."""
            nonlocal stems, names, bpm, bar_t, t_db, total, n_bars, orig
            s = songs[cur]
            # dict-INHALT tauschen, damit bestehende Referenzen gueltig bleiben
            stems.clear()
            stems.update(s["stems"])
            names[:] = list(s["names"])
            orig = s.get("orig")
            bpm = float(s["bpm"])
            bar_t = 4.0 * 60.0 / bpm
            t_db = float(s["t_db"])
            total = max(len(np.asarray(stems[n])) for n in names) / float(sr)
            n_bars = max(1, int((total - t_db) / bar_t))
            st["peaks"] = s.get("peaks")
            st["mixpk"] = s.get("mixpk") or {}
            st["grid"] = s.get("grid", "off")
            st["db_orig"] = s.get("db_orig") if s.get("db_orig") is not None else t_db
            st["sel"] = s.get("sel")
            v = s.get("view") or (0.0, total)
            st["t0"], st["dur"] = float(v[0]), float(v[1])
            _rebuild_track_boxes()
            gridv.set(st["grid"])
            modev.set(s.get("mode", "pitch"))
            dblbl.config(text=f"{t_db:.2f} s")
            _upd_tabs()
            _upd_head()
            _upd_tfhint()
            _sync_fields()
            _refresh_list()
            _draw()
            _upd_status()
            if st["peaks"] is None:
                st["busy"] = f"lese Wellenform von „{s['title']}“ …"
                _draw()
                threading.Thread(target=_peaks_work, args=(msg,),
                                 daemon=True).start()
            elif msg:
                stat.config(text=msg)

        def _switch_song(k):
            nonlocal cur
            if not (0 <= k < len(songs)) or k == cur:
                return
            if st.get("pmode") != "all":
                # anderer Song = andere Spuren. Die Mashup-Vorschau laeuft
                # dagegen WEITER -- genau dafuer wechselt man ja den Reiter:
                # um den Loop eines anderen Songs im laufenden Mix zu justieren.
                _kill_player()
            _save_song_state()
            cur = k
            _load_song_state()

        def _add_song():
            """Weiteren Song laden (Stems, eine Mixdatei -- oder eine Datei, die
            hier gleich in Instrumente GETRENNT wird) -- er kommt als neuer
            Reiter dazu. Die Samplerate wird auf die des ersten Songs gebracht,
            damit alles synchron gemischt werden kann."""
            if len(songs) >= MAX_SONGS:
                stat.config(text=f"Maximal {MAX_SONGS} Songs.")
                return
            paths = filedialog.askopenfilenames(
                title=f"Song {len(songs) + 1}: Audiodatei(en) wählen "
                      "(mehrere = einzelne Spuren)",
                initialdir=load_config().get("last_save_dir") or "",
                filetypes=[("Audio", "*.wav *.flac *.mp3 *.ogg *.m4a *.aif *.aiff"),
                           ("Alle Dateien", "*.*")])
            if not paths:
                return
            paths = list(paths)
            # Trennen oder direkt uebernehmen? (Fuer „nur der Bass aus Song 2“
            # braucht es die KI-Trennung -- die kostet aber Zeit.)
            opt = self._add_song_dialog(win, len(songs) + 1, paths)
            if not opt:
                return
            nm = os.path.splitext(os.path.basename(paths[0]))[0]
            for suf in ("_drums", "_bass", "_vocals", "_other", "_mix"):
                if nm.lower().endswith(suf):
                    nm = nm[:-len(suf)]
                    break
            sep = bool(opt.get("sep"))
            # Nur beim Trennen ein Log-Fenster: dort laeuft die KI minutenlang
            # und man will Fortschritt und Fehler sehen.
            log = (self._stem_log_open(f"Instrumente trennen – {nm}")
                   if sep else None)
            stat.config(text=(f"trenne „{nm}“ in Instrumentspuren – siehe "
                              "Fortschrittsfenster …" if sep
                              else f"lade „{nm}“ …"))
            win.update_idletasks()

            def _work():
                try:
                    if sep:
                        pr = self._sep_params(opt.get("qual", "hi"))
                        cb = lambda m: self._stem_log(log, m)
                        self._stem_progress(log, 0, 2, "Stems trennen")
                        self._stem_log(log, f"== {nm}: Instrumente trennen ==")
                        self._stem_log(log, core.separation_eta(
                            paths[0], backend=pr["sep_backend"],
                            model=pr["sep_model"], shifts=pr["shifts"]))
                        if pr["sep_backend"] == "roformer":
                            tr, tsr = core.separate_stems_roformer(paths[0],
                                                                   log=cb)
                        else:
                            tr, tsr = core.separate_stems(
                                paths[0], model=pr["sep_model"], log=cb,
                                overlap=pr["overlap"], shifts=pr["shifts"])
                        self._stem_progress(log, 1, 2, "Spuren aufbereiten")
                        tr = core.resample_tracks(tr, tsr, sr, log=cb)
                    else:
                        tr, _sr = core.load_audio_tracks(paths, sr=sr)
                    tb_, tbpm = 0.0, 0.0
                    try:
                        tb_, tbpm = core.detect_downbeat(tr, sr)
                    except Exception:
                        pass
                    if sep:
                        self._stem_progress(log, 2, 2, "Fertig")

                    def _done():
                        songs.append({
                            "title": nm, "stems": tr, "orig": dict(tr),
                            "names": _mk_names(tr),
                            "bpm": float(tbpm) if tbpm and tbpm > 0 else 120.0,
                            "t_db": float(max(0.0, tb_)), "db_orig": float(max(0.0, tb_)),
                            "total": max(len(np.asarray(a)) for a in tr.values()) / float(sr),
                            "grid": "off", "mode": "pitch", "sel": None,
                            "bars": 0, "view": None, "peaks": None, "mixpk": {},
                            "midi": None})
                        _kill_player()
                        _save_song_state()
                        nonlocal_set_cur(
                            len(songs) - 1,
                            msg=f"Song {len(songs)} „{nm}“ geladen "
                            f"({len(tr)} Spur(en)"
                            + (", KI-getrennt" if sep else "") + "). Loop "
                            "markieren und im Feld „Takte“ eintragen, wie viele "
                            "Takte er umfasst – der Bereich wird dann "
                            f"automatisch auf {_mbpm():.1f} BPM gedehnt.")
                    self.root.after(0, _done)
                except Exception as ex:
                    if sep:
                        self._stem_log_error(log)
                    self.root.after(0, lambda e=ex: stat.config(
                        text=f"Laden fehlgeschlagen: {e}"))
            threading.Thread(target=_work, daemon=True).start()

        def nonlocal_set_cur(k, msg=None):
            """Reiter wechseln OHNE den (schon gesicherten) Zustand zu ueber-
            schreiben -- fuer den Wechsel direkt nach dem Laden."""
            nonlocal cur
            cur = k
            _load_song_state(msg)

        def _del_song():
            nonlocal cur, master
            if len(songs) <= 1:
                return
            if cur == master:
                stat.config(text="Dieser Song gibt das Tempo vor. Erst einen "
                            "anderen Song zum Tempo-Master machen (⏱), dann "
                            "lässt er sich entfernen.")
                return
            used = [p["label"] for p in st["parts"]
                    if any(q["song"] == cur for q in p.get("srcs", []))]
            if used and not messagebox.askyesno(
                    "Song entfernen",
                    f"„{songs[cur]['title']}“ wird in {len(used)} Part(s) "
                    f"benutzt ({', '.join(used[:4])}). Diese Quellen werden "
                    "mitgelöscht. Fortfahren?", parent=win):
                return
            _kill_player()
            gone = cur
            songs.pop(gone)
            st["scache"] = {}                  # Schluessel enthalten den Index
            if master > gone:                  # Master-Index mitschieben
                master -= 1
            # Quellen dieses Songs entfernen, hoehere Indizes verschieben
            for p in st["parts"]:
                p["srcs"] = [q for q in p.get("srcs", []) if q["song"] != gone]
                for q in p["srcs"]:
                    if q["song"] > gone:
                        q["song"] -= 1
            st["parts"] = [p for p in st["parts"] if p.get("srcs")]
            cur = max(0, gone - 1)
            _load_song_state()
            stat.config(text="Song entfernt.")

        def _close():
            save_config({**load_config(),
                         "editor_click": bool(click_on.get()),
                         "editor_click_vol": int(clickv.get())})
            p = st["player"]
            if p is not None:
                try:
                    p.stop()
                except Exception:
                    pass
                if p in self._stem_players:
                    try:
                        self._stem_players.remove(p)
                    except ValueError:
                        pass
                st["player"] = None
            win.destroy()

        self._small_button(r2, "Schließen", _close).pack(side="left", padx=4)
        win.protocol("WM_DELETE_WINDOW", _close)
        # Tk-Variablen vor dem GC schuetzen
        win._a2m_editor = (snapv, shown, namev, gridv, click_on, clickv)

        # --- Layout: von UNTEN nach oben packen ---
        # Pack-Reihenfolge = Prioritaet bei knappem Platz. Die Bedienleisten
        # kommen zuerst (side=bottom) und bleiben damit auch auf kleinen
        # Bildschirmen vollstaendig sichtbar; die Wellenform bekommt den Rest
        # und schrumpft als erste.
        r2.pack(side="bottom", pady=(6, 8))
        hint.pack(side="bottom", fill="x", padx=14, pady=(4, 0))
        body.pack(side="bottom", fill="x", padx=14, pady=(6, 0))
        r1b.pack(side="bottom", fill="x", padx=14, pady=(4, 0))
        r1.pack(side="bottom", fill="x", padx=14, pady=(6, 0))
        tf.pack(side="bottom", fill="x", padx=14, pady=(2, 0))
        stat.pack(side="bottom", fill="x", padx=14, pady=(6, 0))
        sbx.pack(side="bottom", fill="x", padx=14)
        cvs.pack(side="top", fill="both", expand=True, padx=14, pady=(6, 0))

        # Peak-Pyramiden im Hintergrund (Mix + je Stem), dann erst zeichnen
        def _peaks_work(after_msg=None):
            """Peak-Pyramiden im Hintergrund. after_msg wird ZULETZT in die
            Statuszeile geschrieben -- sonst ueberschreibt der Neuaufbau eine
            wichtige Meldung (z. B. die Warnung nach einem Raster-Wechsel)."""
            try:
                pk = {}
                mix = None
                for n in names:
                    a = np.asarray(stems[n], dtype=np.float32)
                    m = a.mean(axis=1) if a.ndim == 2 else a
                    pk[n] = core.waveform_peaks(m)
                    if mix is None:
                        mix = m.copy()
                    else:
                        k = min(len(mix), len(m))
                        mix = mix[:k] + m[:k]
                # Mix ALLER Spuren gleich in den Kombinations-Cache legen
                full = core.waveform_peaks(
                    mix if mix is not None else np.zeros(1, dtype=np.float32))

                def _done():
                    st["peaks"] = pk
                    st["mixpk"] = {tuple(names): full}
                    st["busy"] = None
                    _draw()
                    _upd_status()
                    if after_msg:
                        stat.config(text=after_msg)
                self.root.after(0, _done)
            except Exception as ex:
                self.root.after(0, lambda e=ex: stat.config(
                    text=f"Wellenform fehlgeschlagen: {e}"))
        threading.Thread(target=_peaks_work, daemon=True).start()

    def _save_stems_dialog(self, parent_win, stems_dict, sr, bpm=0.0):
        """Dialog: welche Stems speichern (einzeln/alle) + optional „auf Takt
        schneiden" (2 Takte Vorlauf, Sample). Jede gewaehlte Spur wird eine eigene
        WAV (`<Name>_<stem>.wav`) im gewaehlten Ordner."""
        names = ([n for n in core.STEM_NAMES if n in stems_dict]
                 + [n for n in stems_dict if n not in core.STEM_NAMES])
        if not names:
            messagebox.showinfo("Stems speichern", "Keine Stems vorhanden.")
            return
        win = tk.Toplevel(self.root)
        win.title("Stems speichern")
        win.configure(bg=COL_BG)
        win.transient(parent_win)
        tk.Label(win, text="Stems speichern", font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text="Welche Spuren? Jede wird eine eigene WAV.",
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED).pack(pady=(0, 8))
        body = tk.Frame(win, bg=COL_BG)
        body.pack(padx=20, pady=4)
        sel = {}
        for nm in names:
            v = tk.BooleanVar(value=True)
            sel[nm] = v
            tk.Checkbutton(body, text=core.STEM_LABELS.get(nm, nm), variable=v,
                           font=self.f_small, bg=COL_BG, fg=COL_FG,
                           selectcolor=COL_SURFACE, activebackground=COL_BG,
                           activeforeground=COL_FG, bd=0, highlightthickness=0,
                           anchor="w", width=12).pack(anchor="w")
        nfr = tk.Frame(win, bg=COL_BG)
        nfr.pack(padx=20, pady=(6, 0), anchor="w")
        tk.Label(nfr, text="Name:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED).pack(side="left", padx=(0, 6))
        namev = tk.StringVar(value="stems")
        tk.Entry(nfr, textvariable=namev, width=18, font=self.f_small, bg=COL_SURFACE,
                 fg=COL_FG, insertbackground=COL_FG, bd=0,
                 highlightthickness=0).pack(side="left")
        cutv = tk.BooleanVar(value=False)
        tk.Checkbutton(win, text="Auf Takt schneiden (Sample, 2 Takte Vorlauf – "
                       "Tempo wird automatisch erkannt)", variable=cutv,
                       font=self.f_small, bg=COL_BG, fg=COL_FG,
                       selectcolor=COL_SURFACE, activebackground=COL_BG,
                       activeforeground=COL_FG, bd=0, highlightthickness=0,
                       anchor="w").pack(anchor="w", padx=20, pady=(8, 0))
        status = tk.Label(win, text="", font=self.f_tiny, bg=COL_BG, fg=COL_MUTED)
        status.pack(pady=(6, 2))

        def _do_save():
            chosen = [nm for nm in names if sel[nm].get()]
            if not chosen:
                status.config(text="Keine Spur gewählt.")
                return
            cfg = load_config()
            out_dir = filedialog.askdirectory(
                title="Zielordner für die Stems",
                initialdir=cfg.get("last_save_dir") or "")
            if not out_dir:
                return
            base = core.sanitize_filename(namev.get() or "stems")
            do_cut = bool(cutv.get())
            status.config(text="speichere …")

            def _work():
                try:
                    src = stems_dict
                    if do_cut:
                        src = core.bar_aligned_stems(stems_dict, sr)  # alle gemeinsam
                    to_write = {nm: src[nm] for nm in chosen if nm in src}
                    paths = core.write_stems_to_files(to_write, sr, out_dir, base=base)
                    save_config({**load_config(), "last_save_dir": out_dir})
                    self.root.after(0, lambda: status.config(
                        text=f"{len(paths)} Datei(en) gespeichert."))
                except Exception as ex:
                    self.root.after(0, lambda e=ex: status.config(
                        text=f"Fehler: {e}"))
            threading.Thread(target=_work, daemon=True).start()

        self._small_button(win, "Speichern…", _do_save).pack(pady=(2, 2))
        self._small_button(win, "Schließen", win.destroy).pack(pady=(0, 10))
        win._a2m_save_vars = (sel, cutv, namev)   # Tk-Variablen vor GC schuetzen

    def _bh_text_dialog(self, parent, store, lbl=None, short=False):
        """Kleines Fenster: Songtext aus BandHelper einfuegen (Karaoke-Automation,
        Modus 'vorgegebener Text'). Ergebnis in store['text']; lbl zeigt den
        Status an (short=True: Kurzform fuer Listenzeilen im Stapel-Dialog).
        Leer = AudioWizard erzeugt selbst ChordPro (+Zip)."""
        win = tk.Toplevel(self.root)
        win.title("Text aus BandHelper")
        win.configure(bg=COL_BG)
        win.transient(parent)
        # Der "Was soll passieren?"-Dialog haelt einen modalen Grab -- ohne
        # eigenen Grab kaeme hier KEINE Eingabe an (nichts einfuegbar). Grab
        # uebernehmen und beim Schliessen an den Eltern-Dialog zurueckgeben.
        prev_grab = self.root.grab_current()
        try:
            win.grab_set()
        except Exception:
            pass

        def _close():
            win.destroy()
            if prev_grab is not None:
                try:
                    prev_grab.grab_set()
                except Exception:
                    pass

        win.protocol("WM_DELETE_WINDOW", _close)
        tk.Label(win, text="Songtext aus BandHelper einfügen", font=self.f_h1,
                 bg=COL_BG, fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text="Den kompletten Songtext aus BandHelper hierher "
                 "kopieren (Strg+V) – die Zeilennummern der Automation beziehen "
                 "sich dann exakt auf diesen Text (alle Zeilen zählen, auch "
                 "leere und Akkordzeilen). Leer lassen: AudioWizard erzeugt "
                 "selbst einen ChordPro-Text als Zip.",
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED, justify="left",
                 wraplength=440).pack(padx=16, pady=(0, 6))
        txt = tk.Text(win, width=60, height=18, bg=COL_SURFACE, fg=COL_FG,
                      insertbackground=COL_FG, bd=0, highlightthickness=0,
                      font=self.f_tiny)
        txt.pack(padx=16, pady=4, fill="both", expand=True)
        if store.get("text"):
            txt.insert("1.0", store["text"])
        txt.focus_set()                    # sofort Strg+V moeglich

        def _ok():
            store["text"] = txt.get("1.0", "end").rstrip("\n")
            if lbl is not None:
                if store["text"].strip():
                    n = len(store["text"].split("\n"))
                    lbl.config(text=(f"✓ {n} Zeilen" if short else
                                     f"✓ BandHelper-Text übernommen ({n} Zeilen)"))
                else:
                    lbl.config(text=("– ChordPro" if short else
                                     "kein Text – ChordPro-Zip wird miterzeugt"))
            _close()

        row = tk.Frame(win, bg=COL_BG)
        row.pack(pady=(4, 10))
        self._small_button(row, "Übernehmen", _ok).pack(side="left", padx=4)
        self._small_button(row, "Abbrechen", _close).pack(side="left", padx=4)

    def _mixout_dialog(self, parent_win, stems_dict, sr, base=""):
        """Dialog: Instrumente aus dem Gesamtmix AUSBLENDEN (Play-Along) -- die
        uebrigen Stems werden wieder zu EINER Datei summiert (z. B. ohne Gesang =
        Karaoke, ohne Bass = Uebe-Playback). Format: MP3 (320 kbit/s) oder WAV."""
        names = [n for n in core.STEM_NAMES if stems_dict.get(n) is not None]
        if not names:
            messagebox.showinfo("Play-Along-Mix", "Keine Stems vorhanden.")
            return
        cfg = load_config()
        mp3_ok = core.mp3_supported()
        win = tk.Toplevel(self.root)
        win.title("Play-Along-Mix")
        win.configure(bg=COL_BG)
        win.transient(parent_win)
        tk.Label(win, text="Play-Along-Mix", font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text="Welche Spuren sollen aus dem Mix verschwinden?\n"
                 "Die übrigen werden zu einer Datei zusammengemischt.",
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED,
                 justify="left").pack(pady=(0, 8))
        body = tk.Frame(win, bg=COL_BG)
        body.pack(padx=20, pady=4, anchor="w")
        last_drop = cfg.get("mixout_drop", ["vocals"])
        drop = {}
        for nm in names:
            v = tk.BooleanVar(value=(nm in last_drop))
            drop[nm] = v
            tk.Checkbutton(body, text=core.STEM_LABELS.get(nm, nm) + " ausblenden",
                           variable=v, font=self.f_small, bg=COL_BG, fg=COL_FG,
                           selectcolor=COL_SURFACE, activebackground=COL_BG,
                           activeforeground=COL_FG, bd=0, highlightthickness=0,
                           anchor="w", width=20).pack(anchor="w")
        fmtf = tk.Frame(win, bg=COL_BG)
        fmtf.pack(padx=20, pady=(6, 0), anchor="w")
        tk.Label(fmtf, text="Format:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED).pack(side="left", padx=(0, 6))
        fmtv = tk.StringVar(value=(cfg.get("mixout_fmt", "mp3")
                                   if mp3_ok else "wav"))
        for val, lbl in (("mp3", "MP3 (320 kbit/s)"), ("wav", "WAV")):
            rb = tk.Radiobutton(fmtf, text=lbl, variable=fmtv, value=val,
                                font=self.f_small, bg=COL_BG, fg=COL_FG,
                                selectcolor=COL_SURFACE, activebackground=COL_BG,
                                activeforeground=COL_FG, bd=0, highlightthickness=0)
            if val == "mp3" and not mp3_ok:
                rb.config(state="disabled", fg=COL_MUTED)
            rb.pack(side="left", padx=(0, 8))
        # BandHelper: Karaoke-Automationsspur (+ ChordPro-Zip) mit erzeugen
        whisper_ok = core.whisper_available()
        v_bh = tk.BooleanVar(value=bool(cfg.get("mixout_bh", False)) and whisper_ok)
        bh_ref = {"text": ""}
        bhr = tk.Frame(win, bg=COL_BG)
        bhr.pack(padx=20, pady=(6, 0), anchor="w")
        cb_bh = tk.Checkbutton(bhr, text="BandHelper-Automation (Karaoke)",
                               variable=v_bh, font=self.f_small, bg=COL_BG,
                               fg=COL_FG if whisper_ok else COL_MUTED,
                               selectcolor=COL_SURFACE, activebackground=COL_BG,
                               activeforeground=COL_FG, bd=0, highlightthickness=0)
        if not whisper_ok:
            v_bh.set(False)
            cb_bh.config(state="disabled")
        cb_bh.pack(side="left")
        bh_lbl = tk.Label(win, text=("kein Text – ChordPro-Zip wird miterzeugt"
                                     if whisper_ok
                                     else "braucht: pip install faster-whisper"),
                          font=self.f_tiny, bg=COL_BG, fg=COL_MUTED)
        self._small_button(bhr, "Text aus BandHelper…",
                           lambda: self._bh_text_dialog(win, bh_ref, bh_lbl)).pack(
                               side="left", padx=(8, 0))
        bh_lbl.pack(padx=24, anchor="w")
        status = tk.Label(win, text="", font=self.f_tiny, bg=COL_BG, fg=COL_MUTED)
        status.pack(pady=(6, 2))

        def _do_save():
            drops = [n for n in names if drop[n].get()]
            if not drops:
                status.config(text="Nichts zum Ausblenden gewählt.")
                return
            if len(drops) >= len(names):
                status.config(text="Mindestens eine Spur muss übrig bleiben.")
                return
            fmt = fmtv.get()
            use_bh = bool(v_bh.get())
            bh_text = bh_ref.get("text", "")
            cfg2 = load_config()
            ohne = "-".join(core.STEM_LABELS.get(n, n) for n in drops)
            init = core.sanitize_filename((base or "Mix") + "_ohne_" + ohne)
            p = filedialog.asksaveasfilename(
                title="Play-Along-Mix speichern",
                defaultextension="." + fmt, initialfile=init + "." + fmt,
                initialdir=cfg2.get("last_save_dir") or "",
                filetypes=[("MP3", "*.mp3"), ("WAV", "*.wav"), ("Alle", "*.*")])
            if not p:
                return
            status.config(text="mische & speichere …")

            def _work():
                try:
                    mix = core.mix_from_stems(stems_dict, drop=drops)
                    core.save_mix_file(p, mix, sr)
                    save_config({**load_config(),
                                 "last_save_dir": os.path.dirname(p),
                                 "mixout_drop": drops, "mixout_fmt": fmt,
                                 "mixout_bh": use_bh})
                    if use_bh:
                        # Transkription (+ Akkorde) nur fuer die Automation --
                        # Status zeigt den Fortschritt der Pipeline an
                        slog = lambda m: self.root.after(
                            0, lambda mm=str(m): status.config(text=mm[:90]))
                        cfg3 = load_config()
                        lang = cfg3.get("sheet_lang", "auto")
                        base0 = os.path.splitext(os.path.basename(p))[0]
                        base0 = re.sub(r"_ohne_[^.]*$", "", base0) or "Mix"
                        sh = core.song_sheet_from_stems(
                            stems_dict, sr, title=base0,
                            whisper_size=cfg3.get("sheet_model", "medium"),
                            language=None if lang == "auto" else lang,
                            log=slog, online=bool(cfg3.get("online_ref")))
                        d = os.path.dirname(p)
                        bb = core.sanitize_filename(base0)
                        _tp, cptxt = core.write_bandhelper_automation(
                            d, bb, sh, len(mix) / float(sr),
                            ref_text=bh_text, log=slog)
                        if cptxt is not None:
                            core.write_bandhelper_zip(
                                os.path.join(d, bb + "_bandhelper.zip"),
                                [(base0, cptxt)], log=slog)
                    self.root.after(0, lambda: status.config(
                        text="Gespeichert: " + os.path.basename(p)
                        + (" + BandHelper-Dateien" if use_bh else "")))
                except Exception as ex:
                    self.root.after(0, lambda e=ex: status.config(
                        text=f"Fehler: {e}"))
            threading.Thread(target=_work, daemon=True).start()

        self._small_button(win, "Speichern…", _do_save).pack(pady=(2, 2))
        self._small_button(win, "Schließen", win.destroy).pack(pady=(0, 10))
        win._a2m_mix_vars = (drop, fmtv, v_bh)    # Tk-Variablen vor GC schuetzen

    def on_batch_playalong(self):
        """Stapelverarbeitung: beliebig viele Audiodateien nacheinander in Stems
        trennen und je Datei als Play-Along-Mixe speichern -- wahlweise (1) nur
        Drums + Bass (Rhythmusgruppe zum Mitspielen) und/oder (2) alles ohne
        Gesang (Karaoke). Format MP3 (320 kbit/s) oder WAV."""
        if not core.demucs_available():
            messagebox.showinfo("Stapel: Play-Along-Mixe",
                                "Stem-Trennung nicht verfügbar – bitte zuerst "
                                "'pip install demucs' ausführen.")
            return
        cfg = load_config()
        paths = filedialog.askopenfilenames(
            title="Audiodateien für Play-Along-Mixe wählen (Mehrfachauswahl)",
            initialdir=cfg.get("last_save_dir") or "",
            filetypes=[("Audio", "*.wav *.flac *.mp3 *.ogg *.m4a *.aif *.aiff"),
                       ("Alle Dateien", "*.*")])
        if not paths:
            return
        paths = list(paths)
        mp3_ok = core.mp3_supported()
        win = tk.Toplevel(self.root)
        win.title("Stapel: Play-Along-Mixe")
        win.configure(bg=COL_BG)
        win.transient(self.root)
        tk.Label(win, text="Stapel: Play-Along-Mixe", font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text=f"{len(paths)} Datei(en) gewählt. Jede wird einmal in "
                 "Stems getrennt (hohe Qualität, dauert einige Minuten pro Stück) "
                 "und dann als Mix gespeichert.", font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED, justify="left", wraplength=420).pack(
                     padx=20, pady=(0, 8))
        body = tk.Frame(win, bg=COL_BG)
        body.pack(padx=20, pady=4, anchor="w")
        v_db = tk.BooleanVar(value=True)
        v_kar = tk.BooleanVar(value=True)
        for var, lbl in ((v_db, "Nur Drums + Bass (Gesang und Rest ausgeblendet)"),
                         (v_kar, "Alles ohne Gesang (Karaoke)")):
            tk.Checkbutton(body, text=lbl, variable=var, font=self.f_small,
                           bg=COL_BG, fg=COL_FG, selectcolor=COL_SURFACE,
                           activebackground=COL_BG, activeforeground=COL_FG,
                           bd=0, highlightthickness=0, anchor="w").pack(anchor="w")
        # BandHelper: je Datei Automationsspur + ChordPro sammeln (Karaoke)
        whisper_ok = core.whisper_available()
        v_bh = tk.BooleanVar(value=False)
        cb_bh = tk.Checkbutton(body, text="BandHelper-Automation + ChordPro je "
                               "Datei (Karaoke; braucht Transkription – deutlich "
                               "langsamer)", variable=v_bh, font=self.f_small,
                               bg=COL_BG, fg=COL_FG if whisper_ok else COL_MUTED,
                               selectcolor=COL_SURFACE, activebackground=COL_BG,
                               activeforeground=COL_FG, bd=0, highlightthickness=0,
                               anchor="w", wraplength=400, justify="left")
        if not whisper_ok:
            v_bh.set(False)
            cb_bh.config(state="disabled")
        cb_bh.pack(anchor="w", pady=(4, 0))
        # Pro Datei optional ein BandHelper-Text (wie im Einzel-Dialog); Dateien
        # ohne Text landen als ChordPro im Sammel-Zip. Liste ist scrollbar.
        bh_texts = {p: {"text": ""} for p in paths}
        bhf = tk.Frame(win, bg=COL_BG)
        tk.Label(bhf, text="Vorhandene BandHelper-Texte zuweisen (optional):",
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED,
                 anchor="w").pack(anchor="w")
        lstf = tk.Frame(bhf, bg=COL_BG)
        lstf.pack(fill="x")
        cvs = tk.Canvas(lstf, bg=COL_BG, highlightthickness=0, bd=0,
                        width=440, height=min(160, 30 * len(paths) + 4))
        sb = tk.Scrollbar(lstf, orient="vertical", command=cvs.yview)
        inner = tk.Frame(cvs, bg=COL_BG)
        cvs.create_window((0, 0), window=inner, anchor="nw")
        cvs.configure(yscrollcommand=sb.set)
        cvs.pack(side="left", fill="x", expand=True)
        if len(paths) > 5:
            sb.pack(side="right", fill="y")

        def _mk_row(p):
            row = tk.Frame(inner, bg=COL_BG)
            row.pack(anchor="w", fill="x", pady=1)
            nm = os.path.basename(p)
            if len(nm) > 34:
                nm = nm[:33] + "…"
            tk.Label(row, text=nm, font=self.f_tiny, bg=COL_BG, fg=COL_FG,
                     anchor="w", width=36).pack(side="left")
            st = tk.Label(row, text="– ChordPro", font=self.f_tiny, bg=COL_BG,
                          fg=COL_MUTED, anchor="w", width=12)
            self._small_button(row, "Text…",
                               lambda p=p, st=st: self._bh_text_dialog(
                                   win, bh_texts[p], st, short=True)).pack(
                                       side="left", padx=(4, 0))
            st.pack(side="left", padx=(6, 0))

        for p in paths:
            _mk_row(p)
        inner.update_idletasks()
        cvs.configure(scrollregion=cvs.bbox("all"))

        def _wheel(ev):
            cvs.yview_scroll(int(-ev.delta / 120), "units")

        bhf.bind("<Enter>", lambda e: cvs.bind_all("<MouseWheel>", _wheel))
        bhf.bind("<Leave>", lambda e: cvs.unbind_all("<MouseWheel>"))

        def _toggle_bh():
            if v_bh.get():
                bhf.pack(padx=20, pady=(4, 0), anchor="w", fill="x",
                         before=fmtf)
            else:
                bhf.pack_forget()

        cb_bh.config(command=_toggle_bh)
        fmtf = tk.Frame(win, bg=COL_BG)
        fmtf.pack(padx=20, pady=(6, 0), anchor="w")
        tk.Label(fmtf, text="Format:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED).pack(side="left", padx=(0, 6))
        fmtv = tk.StringVar(value=(cfg.get("mixout_fmt", "mp3")
                                   if mp3_ok else "wav"))
        for val, lbl in (("mp3", "MP3 (320 kbit/s)"), ("wav", "WAV")):
            rb = tk.Radiobutton(fmtf, text=lbl, variable=fmtv, value=val,
                                font=self.f_small, bg=COL_BG, fg=COL_FG,
                                selectcolor=COL_SURFACE, activebackground=COL_BG,
                                activeforeground=COL_FG, bd=0, highlightthickness=0)
            if val == "mp3" and not mp3_ok:
                rb.config(state="disabled", fg=COL_MUTED)
            rb.pack(side="left", padx=(0, 8))
        status = tk.Label(win, text="", font=self.f_tiny, bg=COL_BG, fg=COL_MUTED)
        status.pack(pady=(6, 2))

        def _go():
            variants = []
            if v_db.get():
                variants.append(("nur_Drums-Bass", ("other", "vocals")))
            if v_kar.get():
                variants.append(("ohne_Gesang", ("vocals",)))
            if not variants:
                status.config(text="Keine Variante gewählt.")
                return
            out_dir = filedialog.askdirectory(
                title="Zielordner für die Play-Along-Mixe",
                initialdir=load_config().get("last_save_dir") or "")
            if not out_dir:
                return
            fmt = fmtv.get()
            bh = bool(v_bh.get())
            btexts = {p: bh_texts[p]["text"] for p in paths}  # Snapshot
            save_config({**load_config(), "last_save_dir": out_dir,
                         "mixout_fmt": fmt})
            win.destroy()
            n_ref = sum(1 for t in btexts.values() if t.strip())
            log = self._stem_log_open("Stapel: Play-Along-Mixe")
            self._stem_log(log, f"{len(paths)} Datei(en) → {len(variants)} "
                           f"Variante(n) je Datei, Format {fmt.upper()}"
                           + (f", + BandHelper-Automation ({n_ref} mit "
                              "eigenem Text, Rest ChordPro)" if bh else "")
                           + ".")
            threading.Thread(target=self._batch_playalong_worker,
                             args=(paths, variants, fmt, out_dir, bh, btexts,
                                   log),
                             daemon=True).start()

        self._small_button(win, "Los…", _go).pack(pady=(2, 2))
        self._small_button(win, "Abbrechen", win.destroy).pack(pady=(0, 10))
        win._a2m_batch_vars = (v_db, v_kar, fmtv, v_bh)  # Tk-Vars vor GC schuetzen

    def _batch_playalong_worker(self, paths, variants, fmt, out_dir, bh,
                                bh_texts, log):
        """Hintergrund-Stapel: je Datei EINMAL trennen, dann alle gewaehlten
        Varianten mischen und speichern; mit bh zusaetzlich je Datei die
        BandHelper-Automationsspur (Karaoke) -- mit zugewiesenem BandHelper-Text
        (bh_texts[pfad]) passend zu DESSEN Anzeige-Zeilen, sonst mit eigenem
        ChordPro, das am Ende gesammelt als EIN Zip fuer den Import entsteht.
        Ein Fehler bei einer Datei stoppt den Stapel nicht."""
        cb = lambda m: self._stem_log(log, m)
        total = len(paths)
        ok = 0
        pros = []                          # (Titel, ChordPro) fuers Sammel-Zip
        cfgb = load_config()
        lang = cfgb.get("sheet_lang", "auto")
        for i, path in enumerate(paths):
            name = os.path.splitext(os.path.basename(path))[0]
            self._stem_progress(log, i, total, name)
            self._stem_log(log, f"== [{i + 1}/{total}] {os.path.basename(path)} ==")
            try:
                stems, ssr = core.separate_stems(path, model="htdemucs",
                                                 log=cb, overlap=0.25)
                mix = None
                for suffix, drop in variants:
                    mix = core.mix_from_stems(stems, drop=drop, log=cb)
                    p = os.path.join(out_dir,
                                     core.sanitize_filename(f"{name}_{suffix}")
                                     + "." + fmt)
                    core.save_mix_file(p, mix, ssr, log=cb)
                ok += 1
                if bh and mix is not None:
                    # BandHelper-Fehler zaehlen die Datei NICHT als gescheitert
                    try:
                        self._stem_log(log, "-- BandHelper-Automation "
                                       "(Transkription + Akkorde) --")
                        sh = core.song_sheet_from_stems(
                            stems, ssr, title=name,
                            whisper_size=cfgb.get("sheet_model", "medium"),
                            language=None if lang == "auto" else lang,
                            log=cb, online=bool(cfgb.get("online_ref")))
                        _tp, cptxt = core.write_bandhelper_automation(
                            out_dir, core.sanitize_filename(name), sh,
                            len(mix) / float(ssr),
                            ref_text=(bh_texts or {}).get(path, ""), log=cb)
                        if cptxt is not None:
                            pros.append((name, cptxt))
                    except Exception as ex:
                        self._stem_log(log, f"BandHelper-Automation "
                                       f"fehlgeschlagen: {ex}")
            except Exception as ex:
                self._stem_log(log, f"FEHLER bei {os.path.basename(path)}: {ex} "
                               "– weiter mit der nächsten Datei.")
        if pros:
            try:
                core.write_bandhelper_zip(
                    os.path.join(out_dir, "chordsheets_bandhelper.zip"),
                    pros, log=cb)
            except Exception as ex:
                self._stem_log(log, f"ChordPro-Zip fehlgeschlagen: {ex}")
        self._stem_progress(log, total, total, "fertig")
        self._stem_log(log, f"Stapel fertig – {ok} von {total} Datei(en) "
                       "erfolgreich verarbeitet.")

    def _drum_settings(self):
        """Schlagzeug-Zuordnung {key:{'on','note'}} + Empfindlichkeit (0..1) aus
        der Konfiguration, mit Defaults aus core.DRUM_COMPONENTS."""
        cfg = load_config()
        mapping = {}
        for key, _lab, _band, note, on, _dur in core.DRUM_COMPONENTS:
            mapping[key] = {"on": bool(cfg.get(f"drum_on_{key}", on)),
                            "note": int(cfg.get(f"drum_note_{key}", note))}
        return mapping, float(cfg.get("drum_sensitivity", 0.5))

    def _open_drum_window(self, parent_win, mp, stems_dict, sr):
        """Separates Fenster: je Schlagzeug-Komponente (Kick/Snare/HiHat/Tom/Crash)
        an/aus + frei waehlbare MIDI-Note, dazu ein Empfindlichkeits-Regler.
        „Anwenden" erkennt die Schlaege neu (band-weise Onsets) und schickt sie als
        Spur „drums" synchron zur Wiedergabe. Einstellungen werden gemerkt."""
        drums = stems_dict.get("drums")
        if drums is None:
            messagebox.showinfo("Schlagzeug → MIDI", "Kein Schlagzeug-Stem vorhanden.")
            return
        cfg = load_config()
        names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

        def note_name(n):
            n = int(n)
            return f"{names[n % 12]}{n // 12 - 1}"

        def _mk_namelbl(var, lbl):
            def _upd(*_a):
                try:
                    lbl.config(text=note_name(var.get()))
                except Exception:
                    pass
            return _upd

        def _test_note(nv):
            """Die aktuell gewaehlte Note kurz auf dem Drum-Kanal senden -- so
            laesst sich pruefen, ob sie das richtige Geraet/Instrument triggert."""
            try:
                note = max(0, min(127, int(nv.get())))
            except Exception:
                return
            c = load_config()
            name = c.get("midi_output") or None
            ch = int(c.get("midi_ch_drums", core.DRUM_DEFAULT_CHANNEL)) - 1

            def _work():
                port = None
                try:
                    port = self._acquire_midi_out(name)
                    core.play_note(port, note, channel=ch)
                except Exception:
                    pass
                finally:
                    self._release_midi_out(port)
            threading.Thread(target=_work, daemon=True).start()

        win = tk.Toplevel(self.root)
        win.title("Schlagzeug → MIDI")
        win.configure(bg=COL_BG)
        win.transient(parent_win)
        tk.Label(win, text="Schlagzeug → MIDI", font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text="Note je Komponente wählen · Kick/Snare/HiHat sicher, "
                 "Tom/Crash „best effort“", font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED).pack(pady=(0, 8))

        body = tk.Frame(win, bg=COL_BG)
        body.pack(padx=20, pady=4)
        comp_vars = {}
        for r, (key, label, _band, dnote, don, _dur) in enumerate(core.DRUM_COMPONENTS):
            onv = tk.BooleanVar(value=bool(cfg.get(f"drum_on_{key}", don)))
            nv = tk.IntVar(value=int(cfg.get(f"drum_note_{key}", dnote)))
            comp_vars[key] = (onv, nv)
            tk.Checkbutton(body, text=label, variable=onv, font=self.f_small,
                           bg=COL_BG, fg=COL_FG, selectcolor=COL_SURFACE,
                           activebackground=COL_BG, activeforeground=COL_FG, bd=0,
                           highlightthickness=0, anchor="w", width=12).grid(
                               row=r, column=0, sticky="w", pady=2)
            tk.Label(body, text="Note", font=self.f_tiny, bg=COL_BG,
                     fg=COL_MUTED).grid(row=r, column=1, padx=(8, 2))
            tk.Spinbox(body, from_=0, to=127, textvariable=nv, width=4,
                       font=self.f_small, bg=COL_SURFACE, fg=COL_FG,
                       buttonbackground=COL_SURFACE, highlightthickness=0, bd=0,
                       insertbackground=COL_FG, justify="center").grid(row=r, column=2)
            nm_lbl = tk.Label(body, text=note_name(nv.get()), font=self.f_tiny,
                              bg=COL_BG, fg=COL_ACCENT, width=5)
            nm_lbl.grid(row=r, column=3, padx=(6, 0), sticky="w")
            nv.trace_add("write", _mk_namelbl(nv, nm_lbl))
            self._small_button(body, "▸ Test",
                               lambda v=nv: _test_note(v)).grid(
                                   row=r, column=4, padx=(10, 0))

        sens0 = float(cfg.get("drum_sensitivity", 0.5))
        sensv = tk.IntVar(value=int(round(max(0.0, min(1.0, sens0)) * 100)))
        sfr = tk.Frame(win, bg=COL_BG)
        sfr.pack(padx=20, pady=(10, 2), fill="x")
        slbl = tk.Label(sfr, text=f"Empfindlichkeit: {sensv.get()} %",
                        font=self.f_tiny, bg=COL_BG, fg=COL_FG)
        slbl.pack(anchor="w")
        tk.Scale(sfr, from_=0, to=100, resolution=5, orient="horizontal",
                 variable=sensv, showvalue=False, length=220,
                 command=lambda v: slbl.config(
                     text=f"Empfindlichkeit: {int(float(v))} %"),
                 bg=COL_BG, fg=COL_FG, troughcolor=COL_SURFACE, highlightthickness=0,
                 bd=0, sliderrelief="flat", activebackground=COL_OK,
                 width=12).pack(anchor="w")

        status = tk.Label(win, text="", font=self.f_tiny, bg=COL_BG, fg=COL_MUTED)
        status.pack(pady=(6, 2))

        def _apply():
            cfg2 = load_config()
            mapping = {}
            for key, (onv, nv) in comp_vars.items():
                try:
                    note = max(0, min(127, int(nv.get())))
                except Exception:
                    note = core.drum_default_mapping()[key]["note"]
                mapping[key] = {"on": bool(onv.get()), "note": note}
                cfg2[f"drum_on_{key}"] = bool(onv.get())
                cfg2[f"drum_note_{key}"] = note
            sens = max(0.0, min(1.0, sensv.get() / 100.0))
            cfg2["drum_sensitivity"] = sens
            save_config(cfg2)
            status.config(text="erkenne Schläge …")

            def _work():
                try:
                    notes = core.drums_to_midi_notes(drums, sr, mapping=mapping,
                                                     sensitivity=sens)
                except Exception as ex:
                    self.root.after(0, lambda e=ex: status.config(
                        text=f"Fehler: {e}"))
                    return

                def _done():
                    try:
                        mp.set_notes("drums", notes)
                    except Exception:
                        pass
                    if status.winfo_exists():
                        status.config(text=f"{len(notes)} Schläge erkannt – aktiv.")
                self.root.after(0, _done)
            threading.Thread(target=_work, daemon=True).start()

        self._small_button(win, "Anwenden", _apply).pack(pady=(2, 2))
        self._small_button(win, "Schließen", win.destroy).pack(pady=(0, 10))
        win._a2m_drum_vars = (comp_vars, sensv)   # Tk-Variablen vor GC schuetzen

    # General-MIDI-Schlagzeugnamen (Auszug) fuer die Remap-Anzeige
    _GM_DRUMS = {35: "Bassdrum", 36: "Kick", 37: "Side Stick", 38: "Snare",
                 39: "Clap", 40: "E-Snare", 41: "Floor Tom", 42: "HiHat zu",
                 43: "Floor Tom hi", 44: "Pedal HiHat", 45: "Tom tief",
                 46: "HiHat offen", 47: "Tom mittel", 48: "Tom hoch", 49: "Crash",
                 50: "Tom hoch", 51: "Ride", 52: "China", 53: "Ride Bell",
                 54: "Tamburin", 55: "Splash", 56: "Cowbell", 57: "Crash 2",
                 59: "Ride 2"}

    def _open_midi_remap_window(self, parent_win, mp, key, tr, chv):
        """Tonhoehen-Remap fuer eine (Schlagzeug-)Spur einer GELADENEN MIDI-Datei:
        je vorhandener Note eine neue waehlen + ▸ Test. Aendert die abgespielten
        Noten live (mp.set_notes); arbeitet immer auf den Original-Tonhoehen, damit
        man beliebig oft neu zuordnen kann. (Neuerkennung aus Audio ist nach dem
        Speichern nicht mehr moeglich -- nur Verschieben.)"""
        orig = list(tr.get("notes", []))
        pitches = sorted({int(p) for _s, _e, p, _v in orig})
        if not pitches:
            messagebox.showinfo("Schlagzeug-Noten", "Keine Noten in dieser Spur.")
            return
        names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

        def note_name(n):
            n = int(n)
            return f"{names[n % 12]}{n // 12 - 1}"

        def _mk_namelbl(var, lbl):
            def _upd(*_a):
                try:
                    lbl.config(text=note_name(var.get()))
                except Exception:
                    pass
            return _upd

        def _test_note(nv):
            try:
                note = max(0, min(127, int(nv.get())))
            except Exception:
                return
            name = load_config().get("midi_output") or None
            ch = int(chv.get()) - 1

            def _work():
                port = None
                try:
                    port = self._acquire_midi_out(name)
                    core.play_note(port, note, channel=ch)
                except Exception:
                    pass
                finally:
                    self._release_midi_out(port)
            threading.Thread(target=_work, daemon=True).start()

        win = tk.Toplevel(self.root)
        win.title("Schlagzeug-Noten (MIDI-Datei)")
        win.configure(bg=COL_BG)
        win.transient(parent_win)
        tk.Label(win, text="Schlagzeug-Noten neu zuordnen", font=self.f_h1,
                 bg=COL_BG, fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text="je vorhandene Note eine neue wählen · ▸ Test sendet sie "
                 "kurz auf dem Spur-Kanal", font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED).pack(pady=(0, 8))
        body = tk.Frame(win, bg=COL_BG)
        body.pack(padx=20, pady=4)
        row_vars = {}
        for r, p in enumerate(pitches):
            nv = tk.IntVar(value=p)
            row_vars[p] = nv
            lab = self._GM_DRUMS.get(p, "")
            tk.Label(body, text=f"{p}  {note_name(p)}" + (f"  · {lab}" if lab else ""),
                     font=self.f_small, bg=COL_BG, fg=COL_FG, anchor="w",
                     width=20).grid(row=r, column=0, sticky="w", pady=2)
            tk.Label(body, text="→", font=self.f_small, bg=COL_BG,
                     fg=COL_MUTED).grid(row=r, column=1, padx=(6, 6))
            tk.Spinbox(body, from_=0, to=127, textvariable=nv, width=4,
                       font=self.f_small, bg=COL_SURFACE, fg=COL_FG,
                       buttonbackground=COL_SURFACE, highlightthickness=0, bd=0,
                       insertbackground=COL_FG, justify="center").grid(row=r, column=2)
            nm = tk.Label(body, text=note_name(p), font=self.f_tiny, bg=COL_BG,
                          fg=COL_ACCENT, width=5)
            nm.grid(row=r, column=3, padx=(6, 0), sticky="w")
            nv.trace_add("write", _mk_namelbl(nv, nm))
            self._small_button(body, "▸ Test",
                               lambda v=nv: _test_note(v)).grid(
                                   row=r, column=4, padx=(10, 0))
        status = tk.Label(win, text="", font=self.f_tiny, bg=COL_BG, fg=COL_MUTED)
        status.pack(pady=(6, 2))

        def _apply():
            mapping = {}
            for p, nv in row_vars.items():
                try:
                    mapping[p] = max(0, min(127, int(nv.get())))
                except Exception:
                    mapping[p] = p
            new = [(s, e, mapping.get(int(p), int(p)), v) for (s, e, p, v) in orig]
            try:
                mp.set_notes(key, new)
                status.config(text="Zuordnung übernommen.")
            except Exception as ex:
                status.config(text=f"Fehler: {ex}")

        self._small_button(win, "Anwenden", _apply).pack(pady=(2, 2))
        self._small_button(win, "Schließen", win.destroy).pack(pady=(0, 10))
        win._a2m_remap_vars = row_vars            # Tk-Variablen vor GC schuetzen

    def _open_midi_file_player(self, path):
        """Laedt eine MIDI-Datei und spielt sie INSTRUMENTENWEISE ueber den
        eingestellten MIDI-Ausgang ab: Transport (Play/Pause/Anfang) + pro Spur
        an/aus und frei waehlbarer Kanal. Kein Audio -- reine MIDI-Ausgabe."""
        try:
            tracks, bpm, dur = core.read_midi_tracks(path)
        except Exception as e:
            messagebox.showerror("MIDI laden", f"Konnte MIDI nicht lesen:\n{e}")
            return
        if not tracks:
            messagebox.showinfo("MIDI laden", "Keine Noten-Spuren gefunden.")
            return
        cfg = load_config()
        try:
            port = self._acquire_midi_out(cfg.get("midi_output") or None)
        except Exception as e:
            messagebox.showerror(
                "MIDI laden", f"Kein MIDI-Ausgang verfügbar:\n{e}\n\n"
                "Bitte in den Einstellungen einen MIDI-Ausgang wählen.")
            return
        transport = core.MidiTransport(dur)
        mp = core.MultiStemMidiPlayer(port, lambda: transport.position()[0],
                                      transport.is_playing)
        keyed = []                             # (key, track) -- Schluessel eindeutig
        for i, tr in enumerate(tracks):
            key = f"{i}:{tr['name']}"
            keyed.append((key, tr))
            mp.set_track(key, tr["notes"], channel=tr["channel"], enabled=True)
        mp.start()
        self._midi_players.append((mp, port))

        win = tk.Toplevel(self.root)
        win.title("MIDI abspielen")
        win.configure(bg=COL_BG)
        win.transient(self.root)
        tk.Label(win, text="MIDI abspielen", font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text=f"{os.path.basename(path)} · {bpm:.0f} BPM · "
                 f"{len(tracks)} Spuren", font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED).pack(pady=(0, 8))

        trb = tk.Frame(win, bg=COL_BG)
        trb.pack(pady=(0, 6))
        playbtn = tk.Button(trb, text="▶", font=self.f_btn, bg=COL_SURFACE, fg=COL_FG,
                            activebackground=COL_SURF_HI, activeforeground=COL_FG,
                            bd=0, padx=18, pady=4, highlightthickness=0,
                            cursor="hand2", command=lambda: transport.toggle())
        playbtn.pack(side="left", padx=(0, 8))

        def _restart():
            transport.seek(0.0)
            transport.play()
        self._small_button(trb, "⏮ Anfang", _restart).pack(side="left", padx=4)
        poslbl = tk.Label(trb, text="0:00 / 0:00", font=self.f_small, bg=COL_BG,
                          fg=COL_MUTED)
        poslbl.pack(side="left", padx=10)
        sentlbl = tk.Label(trb, text="gesendet: 0", font=self.f_tiny, bg=COL_BG,
                           fg=COL_MUTED)
        sentlbl.pack(side="left", padx=(0, 4))

        midf = tk.Frame(win, bg=COL_BG)
        midf.pack(padx=20, pady=4, fill="x")

        def _mk_enable(k, var):
            return lambda: mp.set_enabled(k, bool(var.get()))

        def _mk_channel(k, var):
            def _f(_v=None):
                mp.set_channel(k, int(var.get()) - 1)
            return _f

        midi_vars = {}
        for r, (key, tr) in enumerate(keyed):
            onv = tk.BooleanVar(value=True)
            chv = tk.IntVar(value=int(tr["channel"]) + 1)
            midi_vars[key] = (onv, chv)
            tk.Checkbutton(midf, text=str(tr["name"])[:26], variable=onv,
                           command=_mk_enable(key, onv), font=self.f_small,
                           bg=COL_BG, fg=COL_FG, selectcolor=COL_SURFACE,
                           activebackground=COL_BG, activeforeground=COL_FG, bd=0,
                           highlightthickness=0, anchor="w", width=22).grid(
                               row=r, column=0, sticky="w", padx=6)
            tk.Label(midf, text="Kanal", font=self.f_tiny, bg=COL_BG,
                     fg=COL_MUTED).grid(row=r, column=1, padx=(8, 2))
            om = tk.OptionMenu(midf, chv, *range(1, 17), command=_mk_channel(key, chv))
            om.config(bg=COL_SURFACE, fg=COL_FG, activebackground=COL_SURF_HI,
                      activeforeground=COL_FG, bd=0, highlightthickness=0,
                      font=self.f_tiny, width=2, cursor="hand2")
            om["menu"].config(bg=COL_SURFACE, fg=COL_FG)
            om.grid(row=r, column=2, sticky="w")
            # Schlagzeug-Spur (GM-Kanal 10 oder Name „drum") -> Tonhoehen-Remap+Test
            if int(tr["channel"]) == 9 or "drum" in str(tr["name"]).lower():
                self._small_button(
                    midf, "Schlagzeug-Noten…",
                    lambda k=key, t=tr, c=chv:
                    self._open_midi_remap_window(win, mp, k, t, c)).grid(
                        row=r, column=3, sticky="w", padx=(10, 0))
        win._a2m_midi_vars = midi_vars         # Tk-Variablen vor GC schuetzen

        def _upd():
            if not win.winfo_exists():
                return
            pos, total = transport.position()
            poslbl.config(text=f"{self._fmt_pos(pos)} / {self._fmt_pos(total)}")
            playbtn.config(text="⏸" if transport.is_playing() else "▶")
            sentlbl.config(text=f"gesendet: {mp.sent}")
            win.after(150, _upd)

        def _close():
            try:
                mp.stop()
            except Exception:
                pass
            self._release_midi_out(port)
            if (mp, port) in self._midi_players:
                self._midi_players.remove((mp, port))
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _close)
        self._small_button(win, "Schließen", _close).pack(pady=8)
        _upd()

    def _segment_rec_thread(self):
        rec, sr = self._rec_audio, self._rec_sr
        try:
            segs = core.segment_recording(rec, sr, core.MIN_BPM, core.MAX_BPM)
        except Exception:
            n = len(rec)
            segs = [{"start": 0, "end": n, "bpm": 0.0, "key": "",
                     "key_margin": 0.0, "confident": True, "name": "Aufnahme"}]
        self._rec_segs = segs

    def _poll_rec_segs(self):
        if self._rec_save_win is None or not self._rec_save_win.winfo_exists():
            return
        if self._rec_segs is None:
            self._rec_save_win.after(250, self._poll_rec_segs)
            return
        self._render_rec_segs()

    def _render_rec_segs(self):
        segs, sr = self._rec_segs, self._rec_sr
        self._rec_info.config(
            text=(f"{len(segs)} Stücke erkannt" if len(segs) > 1 else "ein Stück")
            + " — Namen anpassen, dann speichern. Unsichere Grenzen sind gedimmt.")
        self._rec_name_vars = []
        for idx, seg in enumerate(segs):
            row = tk.Frame(self._rec_listf, bg=COL_BG)
            row.pack(fill="x", pady=3)
            dur = (seg["end"] - seg["start"]) / sr
            bpm = f"{int(round(seg['bpm']))}" if seg["bpm"] else "–"
            key = seg["key"] or "?"
            meta = (f"{idx + 1}. {self._fmt_pos(seg['start'] / sr)}"
                    f"–{self._fmt_pos(seg['end'] / sr)} · {self._fmt_pos(dur)}"
                    f" · {bpm} BPM · {key}")
            fg = COL_FG if seg.get("confident") else COL_MUTED
            tk.Label(row, text=meta, font=self.f_small, bg=COL_BG, fg=fg,
                     anchor="w").pack(side="left")
            var = tk.StringVar(value=seg["name"])
            self._rec_name_vars.append(var)
            self._small_button(row, "Speichern",
                               lambda i=idx: self._save_one_rec(i)).pack(side="right")
            tk.Entry(row, textvariable=var, font=self.f_small, bg=COL_SURFACE,
                     fg=COL_FG, width=18, bd=0, insertbackground=COL_FG
                     ).pack(side="right", padx=(0, 8), ipady=2)
        self._rec_all_btn.config(state="normal")

    def _save_one_rec(self, idx):
        seg = self._rec_segs[idx]
        base = core.sanitize_filename(self._rec_name_vars[idx].get())
        cfg = load_config()
        path = filedialog.asksaveasfilename(
            title="Stück speichern", defaultextension=".wav",
            initialfile=base + ".wav",
            initialdir=cfg.get("last_save_dir") or None,
            filetypes=[("WAV-Audio", "*.wav")])
        if not path:
            return
        try:
            core.save_wav_slice(self._rec_audio, self._rec_sr,
                                seg["start"], seg["end"], path)
            save_config({**cfg, "last_save_dir": os.path.dirname(path)})
            self._rec_info.config(text=f"Gespeichert: {os.path.basename(path)}")
        except Exception as e:
            self._rec_info.config(text=f"Fehler beim Speichern: {e}")

    def _save_all_rec(self):
        cfg = load_config()
        d = filedialog.askdirectory(title="Ordner für alle Stücke wählen",
                                    initialdir=cfg.get("last_save_dir") or None)
        if not d:
            return
        ok = 0
        for idx, seg in enumerate(self._rec_segs):
            base = core.sanitize_filename(self._rec_name_vars[idx].get())
            try:
                core.save_wav_slice(self._rec_audio, self._rec_sr,
                                    seg["start"], seg["end"],
                                    os.path.join(d, base + ".wav"))
                ok += 1
            except Exception:
                pass
        save_config({**cfg, "last_save_dir": d})
        self._rec_info.config(
            text=f"{ok} von {len(self._rec_segs)} Stück(en) im Ordner gespeichert.")

    # ------------------------------------------------------------------
    # DJ-Modus: zwei Decks, Crossfade, Clock folgt dem Ziel-Deck
    # ------------------------------------------------------------------
    def open_dj(self):
        """DJ-Fenster oeffnen: zwei Decks in einem gemischten Ausgabe-Stream,
        Crossfade per Klick aufs Deck oder Fader; die MIDI-Clock folgt dem
        dominierenden Deck. Beendet eine laufende Live-Sitzung."""
        if self.dj_win is not None:
            try:
                self.dj_win.lift()
            except Exception:
                pass
            return
        self.stop_session()
        try:
            self.dj_engine = core.DJEngine(channels=2)
            self.dj_engine.start_stream()
        except Exception as e:
            self.dj_engine = None
            self.show_setup(error=f"DJ-Audioausgabe fehlgeschlagen: {e}")
            return
        # MIDI aus der Konfiguration
        self.dj_midi = None
        cfg = load_config()
        midi_name = cfg.get("midi_output") or None
        if midi_name and (midi_name == core.VIRTUAL_MIDI
                          or midi_name in mido.get_output_names()):
            try:
                self.dj_midi = self._acquire_midi_out(midi_name)
            except Exception:
                self.dj_midi = None
        self.dj_clock_stop = threading.Event()
        self.dj_clock_thread = threading.Thread(
            target=core.dj_clock_worker,
            args=(self.dj_engine, self.dj_midi, self.dj_clock_stop), daemon=True)
        self.dj_clock_thread.start()
        self._build_dj_window()
        self._dj_tick()

    def _build_dj_window(self):
        win = tk.Toplevel(self.root)
        win.title("DJ-Modus")
        win.configure(bg=COL_BG)
        win.geometry("800x680")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._dj_close)
        self.dj_win = win
        tk.Label(win, text="DJ-Modus", font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text="Datei je Deck laden · Klick aufs Deck (oder Fader) "
                           "blendet über · die Clock folgt dem lauteren Deck",
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED).pack(pady=(0, 8))
        decks = tk.Frame(win, bg=COL_BG)
        decks.pack(fill="both", expand=True, padx=16)
        decks.columnconfigure(0, weight=1, uniform="d")
        decks.columnconfigure(1, weight=1, uniform="d")
        for idx in (0, 1):
            self._build_dj_deck(decks, idx)
        # Crossfader
        cf = tk.Frame(win, bg=COL_BG)
        cf.pack(fill="x", padx=24, pady=(8, 4))
        tk.Label(cf, text="A", font=self.f_small, bg=COL_BG,
                 fg=COL_ACCENT).pack(side="left")
        self.dj_cross = tk.Scale(cf, from_=0, to=100, orient="horizontal",
                                 showvalue=False, command=self._dj_cross,
                                 bg=COL_BG, fg=COL_FG, troughcolor=COL_SURFACE,
                                 highlightthickness=0, bd=0, sliderrelief="flat",
                                 activebackground=COL_OK)
        self.dj_cross.pack(side="left", fill="x", expand=True, padx=10)
        tk.Label(cf, text="B", font=self.f_small, bg=COL_BG,
                 fg=COL_ACCENT).pack(side="left")
        self.dj_clock_lbl = tk.Label(win, text="Clock: –", font=self.f_small,
                                     bg=COL_BG, fg=COL_MUTED)
        self.dj_clock_lbl.pack(pady=(2, 2))
        bf = tk.Frame(win, bg=COL_BG)
        bf.pack(fill="x", padx=16, pady=(4, 12))
        self._small_button(bf, "Schließen", self._dj_close).pack(side="right")

    def _build_dj_deck(self, parent, idx):
        letter = "A" if idx == 0 else "B"
        panel = tk.Frame(parent, bg=COL_SURFACE, bd=0, highlightthickness=2,
                         highlightbackground=COL_BG)
        panel.grid(row=0, column=idx, sticky="nsew", padx=8, pady=4)
        w = self.dj_w[idx]
        w["panel"] = panel
        head = tk.Label(panel, text=f"DECK {letter}", font=self.f_small,
                        bg=COL_SURFACE, fg=COL_MUTED)
        head.pack(pady=(10, 0))
        w["name"] = tk.Label(panel, text="keine Datei", font=self.f_small,
                             bg=COL_SURFACE, fg=COL_FG, wraplength=300)
        w["name"].pack(pady=(2, 6))
        w["bpm"] = tk.Label(panel, text="—", font=self.f_key, bg=COL_SURFACE,
                            fg=COL_MUTED)
        w["bpm"].pack()
        tk.Label(panel, text="BPM", font=self.f_tiny, bg=COL_SURFACE,
                 fg=COL_MUTED).pack()
        w["key"] = tk.Label(panel, text="", font=self.f_small, bg=COL_SURFACE,
                            fg=COL_ACCENT)
        w["key"].pack(pady=(4, 0))
        w["pos"] = tk.Label(panel, text="–", font=self.f_small, bg=COL_SURFACE,
                            fg=COL_MUTED)
        w["pos"].pack(pady=(2, 6))
        lvl = tk.Canvas(panel, height=8, bg=COL_BAR_BG, highlightthickness=0,
                        bd=0)
        lvl.pack(fill="x", padx=18, pady=(0, 8))
        w["lvl"] = lvl
        w["lvlrect"] = lvl.create_rectangle(0, 0, 0, 10, fill=COL_OK, width=0)
        def _db(parent, text, cmd, **kw):
            return tk.Button(parent, text=text, command=cmd, font=self.f_small,
                             bg=COL_BG, fg=COL_FG, activebackground=COL_SURF_HI,
                             activeforeground=COL_FG, bd=0, pady=4,
                             highlightthickness=0, cursor="hand2",
                             padx=kw.get("padx", 12), state=kw.get("state", "normal"))
        bar = tk.Frame(panel, bg=COL_SURFACE)        # Reihe 1: Laden / Play / Stems
        bar.pack(pady=(0, 4))
        _db(bar, "Laden …", lambda i=idx: self._dj_load(i)).pack(side="left", padx=4)
        w["play"] = _db(bar, "▶", lambda i=idx: self._dj_play(i), padx=14, state="disabled")
        w["play"].pack(side="left", padx=4)
        w["stems"] = _db(bar, "Stems", lambda i=idx: self._dj_stems(i), state="disabled")
        w["stems"].pack(side="left", padx=4)
        bar2 = tk.Frame(panel, bg=COL_SURFACE)       # Reihe 2: Sync / Uebergang
        bar2.pack(pady=(0, 10))
        w["sync"] = _db(bar2, "Sync", lambda i=idx: self._dj_sync_toggle(i))
        w["sync"].pack(side="left", padx=4)
        w["glide"] = _db(bar2, "Übergang", lambda i=idx: self._dj_glide(i))
        w["glide"].pack(side="left", padx=4)
        # EQ-Isolator: senkrechte Slider (Bass/Mitte/Höhen), kontinuierlich
        # regelbar von +6 dB (oben) bis -40 dB (unten, praktisch aus).
        eqf = tk.Frame(panel, bg=COL_SURFACE)
        eqf.pack(pady=(0, 10))
        w["eqvar"] = []
        w["eqval"] = []
        for bi, nm in enumerate(("Bass", "Mitte", "Höhen")):
            col = tk.Frame(eqf, bg=COL_SURFACE)
            col.pack(side="left", padx=10)
            val = tk.Label(col, text="0", font=self.f_tiny, bg=COL_SURFACE,
                           fg=COL_FG)          # kleiner Wert ueber dem Fader
            val.pack()
            v = tk.DoubleVar(value=0.0)
            sc = tk.Scale(col, from_=6, to=-40, resolution=1, orient="vertical",
                          variable=v, showvalue=False, length=90,
                          command=lambda _val, i=idx: self._dj_eq_change(i),
                          bg=COL_SURFACE, fg=COL_FG, troughcolor=COL_BG,
                          highlightthickness=0, bd=0, sliderrelief="flat",
                          activebackground=COL_OK, width=14)
            sc.pack()
            # Doppelklick -> auf den Ausgangswert (0 dB) zuruecksetzen
            sc.bind("<Double-Button-1>",
                    lambda e, var=v, i=idx: self._dj_eq_reset(i, var))
            tk.Label(col, text=nm, font=self.f_tiny, bg=COL_SURFACE,
                     fg=COL_MUTED).pack()
            w["eqvar"].append(v)
            w["eqval"].append(val)
        # Klick aufs Deck (Anzeigebereich) blendet hierher
        for el in (panel, head, w["name"], w["bpm"], w["key"], w["pos"]):
            el.bind("<Button-1>", lambda e, i=idx: self._dj_fade(i))

    def _dj_sync_toggle(self, idx):
        """Deck in Echtzeit auf das Tempo des anderen Decks einrasten/loesen
        (tonhöhen-erhaltend). Status zeigt _dj_tick."""
        if self.dj_engine is None:
            return
        d = self.dj_engine.decks[idx]
        if d.synced:
            self.dj_engine.set_sync(idx, False)
        else:
            self.dj_engine.set_sync(idx, True)   # False, wenn anderes Deck fehlt

    def _dj_stems(self, idx):
        """KI-Stem-Trennung (Demucs) fuer ein Deck anstoßen; danach öffnet sich
        ein Stem-Mischer (Pegel je Instrument, live)."""
        if self.dj_engine is None:
            return
        w = self.dj_w[idx]
        path = w.get("path")
        if not path:
            return
        if not core.demucs_available():
            messagebox.showinfo(
                "Stem-Trennung nicht verfügbar",
                "Für die Stem-Trennung wird das lokale KI-Modell 'demucs' "
                "benötigt – es ist nicht installiert.\n\n"
                "Installieren mit:\n    pip install demucs\n\n"
                "(zieht PyTorch nach, größerer Download). Danach den DJ-Modus "
                "neu öffnen und die Datei erneut laden.")
            return
        if w.get("stems"):
            w["stems"].config(text="trennt …", state="disabled")
        self.dj_clock_lbl.config(
            text=f"Deck {'A' if idx == 0 else 'B'}: trenne Stems (KI, lokal) … "
                 "siehe Fortschrittsfenster.",
            fg=COL_WARN)
        log = self._stem_log_open(f"Stems – Deck {'A' if idx == 0 else 'B'}")
        self._stem_log(log, f"Datei: {path}")
        threading.Thread(target=self._dj_stems_thread, args=(idx, path, log),
                         daemon=True).start()

    def _dj_stems_thread(self, idx, path, log):
        try:
            # DJ-Stems werden live abgespielt (Audio) -> volle Trennqualitaet.
            stems, sr = core.separate_stems(
                path, model="htdemucs", overlap=0.25,
                log=lambda m: self._stem_log(log, m))
            self._dj_stems_res = (idx, stems, sr, None)
        except Exception as e:
            self._stem_log_error(log)
            self._dj_stems_res = (idx, None, 0, str(e))

    def _dj_poll_stems(self):
        res = self._dj_stems_res
        if res is None:
            return
        self._dj_stems_res = None
        idx, stems, sr, err = res
        w = self.dj_w[idx]
        if not w or self.dj_engine is None:
            return
        if err or not stems:
            if w.get("stems"):
                w["stems"].config(text="Stems", state="normal")
            self.dj_clock_lbl.config(text="Stem-Trennung fehlgeschlagen", fg=COL_WARN)
            messagebox.showerror("Stem-Trennung fehlgeschlagen",
                                 f"Die Trennung ist fehlgeschlagen:\n\n{err}")
            return
        try:
            names = self.dj_engine.load_stems(idx, stems, sr)
        except Exception as e:
            if w.get("stems"):
                w["stems"].config(text="Stems", state="normal")
            messagebox.showerror("Stems", f"Stems konnten nicht geladen werden:\n{e}")
            return
        if w.get("stems"):
            w["stems"].config(text="Stems ✓", state="normal")
        self.dj_clock_lbl.config(text=f"Deck {'A' if idx == 0 else 'B'}: Stems bereit",
                                 fg=COL_OK)
        self._open_stem_mixer(idx, names)

    def _open_stem_mixer(self, idx, names):
        letter = "A" if idx == 0 else "B"
        win = tk.Toplevel(self.root)
        win.title(f"Stems – Deck {letter}")
        win.configure(bg=COL_BG)
        win.transient(self.root)
        tk.Label(win, text=f"Stems – Deck {letter}", font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text="Pegel je Instrument (live)", font=self.f_tiny,
                 bg=COL_BG, fg=COL_MUTED).pack(pady=(0, 8))
        body = tk.Frame(win, bg=COL_BG)
        body.pack(padx=20, pady=8)
        for k, nm in enumerate(names):
            col = tk.Frame(body, bg=COL_BG)
            col.pack(side="left", padx=12)
            v = tk.DoubleVar(value=1.0)
            tk.Scale(col, from_=1.5, to=0.0, resolution=0.01, orient="vertical",
                     variable=v, showvalue=False, length=150,
                     command=lambda val, i=idx, kk=k: (
                         self.dj_engine.set_stem_gain(i, kk, float(val))
                         if self.dj_engine else None),
                     bg=COL_BG, fg=COL_FG, troughcolor=COL_SURFACE,
                     highlightthickness=0, bd=0, sliderrelief="flat",
                     activebackground=COL_OK, width=16).pack()
            tk.Label(col, text=core.STEM_LABELS.get(nm, nm), font=self.f_small,
                     bg=COL_BG, fg=COL_ACCENT).pack()
        self._small_button(win, "Schließen", win.destroy).pack(pady=12)

    def _dj_glide(self, idx):
        """Tempo-Übergang anstoßen: Deck gleitet vom Master-Tempo auf sein
        Eigentempo (in den Puffer eingebacken, die Clock gleitet mit)."""
        if self.dj_engine is None:
            return
        self.dj_engine.set_glide(idx)

    def _dj_eq_change(self, idx):
        """EQ-Slider eines Decks anwenden (Bass/Mitte/Höhen, dB) + Werte anzeigen."""
        w = self.dj_w[idx]
        v = w["eqvar"]
        for k, lbl in enumerate(w.get("eqval", [])):
            lbl.config(text=f"{int(round(v[k].get()))}")
        if self.dj_engine is not None:
            self.dj_engine.set_eq(idx, v[0].get(), v[1].get(), v[2].get())

    def _dj_eq_reset(self, idx, var):
        """Doppelklick auf einen EQ-Fader: zurueck auf 0 dB (neutral)."""
        var.set(0.0)
        self._dj_eq_change(idx)
        return "break"

    def _dj_load(self, idx):
        path = filedialog.askopenfilename(
            title=f"Datei für Deck {'A' if idx == 0 else 'B'}",
            filetypes=[("Audio", "*.wav *.flac *.mp3 *.ogg *.m4a *.aif *.aiff"),
                       ("Alle Dateien", "*.*")])
        if not path or self.dj_engine is None:
            return
        w = self.dj_w[idx]
        w["path"] = path                              # fuer die Stem-Trennung merken
        w["name"].config(text=os.path.basename(path))
        w["bpm"].config(text="…", fg=COL_MUTED)
        w["pos"].config(text="analysiere …")
        w["play"].config(state="disabled")
        if w.get("stems"):
            w["stems"].config(state="disabled")
        if not self.warmed:
            self._warm_blocking()
        threading.Thread(target=self._dj_analyze_thread,
                         args=(idx, path), daemon=True).start()

    def _warm_blocking(self):
        try:
            ww = np.zeros(int(core.ANALYSIS_SR * core.WINDOW_SECONDS),
                          dtype=np.float32)
            ww[::core.ANALYSIS_SR // 4] = 0.5
            core.estimate_tempo(ww, core.ANALYSIS_SR)
            core.chroma_pcp(ww, core.ANALYSIS_SR)
        except Exception:
            pass
        self.warmed = True

    def _dj_analyze_thread(self, idx, path):
        try:
            y_an, audio, sr_play = core.load_audio_file(path)
            info = core.analyze_file_beatmap(y_an, core.ANALYSIS_SR,
                                             core.MIN_BPM, core.MAX_BPM)
        except Exception:
            info = None
            audio, sr_play = None, 0
        key = ""
        if info is not None:
            try:
                key = core.estimate_key(y_an, core.ANALYSIS_SR)
            except Exception:
                key = ""
        self._dj_load_res = (idx, audio, sr_play, info, key,
                             os.path.basename(path))

    def _dj_poll_load(self):
        res = self._dj_load_res
        if res is None:
            return
        self._dj_load_res = None
        idx, audio, sr_play, info, key, name = res
        w = self.dj_w[idx]
        if info is None or audio is None or self.dj_engine is None:
            w["bpm"].config(text="—", fg=COL_MUTED)
            w["pos"].config(text="kein Tempo / Format?")
            return
        try:
            self.dj_engine.load(idx, audio, sr_play, info, key, name)
        except Exception as e:
            w["pos"].config(text=f"Fehler: {e}")
            return
        w["bpm"].config(text=f"{int(round(info['bpm']))}", fg=COL_FG)
        w["key"].config(text=key or "")
        w["play"].config(state="normal")
        if w.get("stems") and w.get("path"):
            w["stems"].config(state="normal")        # klickbar; Hinweis bei Klick, falls demucs fehlt
        dur = info.get("duration", 0.0)
        w["pos"].config(text=f"0:00 / {self._fmt_pos(dur)}")

    def _dj_play(self, idx):
        if self.dj_engine is None:
            return
        d = self.dj_engine.decks[idx]
        if d.audio is None:
            return
        if d.playing:
            self.dj_engine.stop(idx)
            self.dj_w[idx]["play"].config(text="▶")
        else:
            self.dj_engine.play(idx)
            self.dj_w[idx]["play"].config(text="⏸")

    def _dj_fade(self, idx):
        if self.dj_engine is None or self.dj_engine.decks[idx].audio is None:
            return
        self.dj_engine.fade_to(idx)
        self.dj_w[idx]["play"].config(text="⏸")

    def _dj_cross(self, val):
        if self.dj_engine is None:
            return
        try:
            x = float(val) / 100.0
        except (TypeError, ValueError):
            return
        with self.dj_engine.lock:
            self.dj_engine.cross_target = x

    def _dj_tick(self):
        eng = self.dj_engine
        if eng is None or self.dj_win is None or not self.dj_win.winfo_exists():
            return
        self._dj_poll_load()
        self._dj_poll_stems()
        for idx in (0, 1):
            d = eng.decks[idx]
            w = self.dj_w[idx]
            if d.audio is not None:
                dur = d.frames_total / float(core.DJ_SR)
                pos = max(0.0, min(dur, eng.play_pos(idx)))
                w["pos"].config(text=f"{self._fmt_pos(pos)} / {self._fmt_pos(dur)}")
                if not d.playing:
                    w["play"].config(text="▶")
            db = 20.0 * math.log10(d.level) if d.level > 1e-6 else -120.0
            frac = max(0.0, min(1.0, (db + 60.0) / 60.0))
            cw = w["lvl"].winfo_width()
            w["lvl"].coords(w["lvlrect"], 0, 0, int(cw * frac), 10)
            dom = eng.dominant() == idx and eng.any_playing()
            w["panel"].config(highlightbackground=COL_OK if dom else COL_BG)
            sb = w.get("sync")
            if sb is not None:
                if d.synced:
                    sb.config(text=f"Sync ✓ {int(round(eng.decks[1-idx].native_bpm))}",
                              bg=COL_OK, fg="#04342C")
                else:
                    sb.config(text="Sync", bg=COL_BG, fg=COL_FG)
            gb = w.get("glide")
            if gb is not None:
                if d.gliding:
                    gb.config(text="Übergang ✓", bg=COL_OK, fg="#04342C")
                else:
                    gb.config(text="Übergang", bg=COL_BG, fg=COL_FG)
        # Fader-Position dem (geglaetteten) Crossfade nachführen
        with eng.lock:
            cx = eng.cross
        try:
            if abs(self.dj_cross.get() / 100.0 - cx) > 0.01:
                self.dj_cross.set(int(round(cx * 100)))
        except Exception:
            pass
        if eng.any_playing():
            letter = "B" if eng.dominant() else "A"
            self.dj_clock_lbl.config(
                text=f"Clock folgt: Deck {letter}"
                + ("" if self.dj_midi else "  (ohne MIDI)"),
                fg=COL_OK if self.dj_midi else COL_MUTED)
        else:
            self.dj_clock_lbl.config(text="Clock: –", fg=COL_MUTED)
        self.dj_win.after(150, self._dj_tick)

    def _dj_teardown(self):
        if self.dj_clock_stop is not None:
            self.dj_clock_stop.set()
        if self.dj_clock_thread is not None:
            self.dj_clock_thread.join(timeout=1.5)
        self.dj_clock_thread = self.dj_clock_stop = None
        if self.dj_engine is not None:
            try:
                self.dj_engine.teardown()
            except Exception:
                pass
            self.dj_engine = None
        if self.dj_midi is not None:
            self._release_midi_out(self.dj_midi)
            self.dj_midi = None
        if self.dj_win is not None:
            try:
                self.dj_win.destroy()
            except Exception:
                pass
            self.dj_win = None
        self.dj_w = [{}, {}]
        self._dj_load_res = None

    def _dj_close(self):
        self._dj_teardown()
        self.show_setup()

    # ------------------------------------------------------------------
    # Noten-Kalibrierung (Slider): Tracking-Parameter der Noten-/Akkord-Modi
    # ------------------------------------------------------------------
    _CALIB_DEFAULTS = {"note_silence_db": -48, "note_sustain_db": -56,
                       "note_off_frames": 3, "note_change_frames": 2,
                       "note_max_poly": 6, "yin_threshold": 0.15}
    _CALIB_INT = ("note_off_frames", "note_change_frames", "note_max_poly")

    def open_note_calib(self):
        """Slider-Fenster für die Tracking-Parameter der Noten-/Akkord-Modi.
        Die Werte landen in der Konfiguration und wirken beim nächsten Start
        des Noten-/Akkord-Modus (note_calib() liest sie in _begin)."""
        cfg = load_config()
        win = tk.Toplevel(self.root)
        win.title("Noten-Kalibrierung")
        win.configure(bg=COL_BG)
        win.geometry("470x430")
        win.transient(self.root)
        tk.Label(win, text="Noten-Kalibrierung", font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text="Für die Noten-/Akkord-Modi · wirkt beim nächsten Start",
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED).pack(pady=(0, 8))
        body = tk.Frame(win, bg=COL_BG)
        body.pack(fill="both", expand=True, padx=20)
        self._calib_vars = {}

        def row(key, label, frm, to, res):
            f = tk.Frame(body, bg=COL_BG)
            f.pack(fill="x", pady=4)
            tk.Label(f, text=label, font=self.f_small, bg=COL_BG, fg=COL_FG,
                     width=24, anchor="w").pack(side="left")
            v = tk.DoubleVar(value=float(cfg.get(key, self._CALIB_DEFAULTS[key])))
            self._calib_vars[key] = v
            tk.Scale(f, from_=frm, to=to, orient="horizontal", resolution=res,
                     variable=v, bg=COL_BG, fg=COL_FG, troughcolor=COL_SURFACE,
                     highlightthickness=0, bd=0, length=200,
                     activebackground=COL_OK).pack(side="right")

        row("note_silence_db", "Stille-Schwelle (dB)", -70, -30, 1)
        row("note_sustain_db", "Halte-Schwelle (dB)", -75, -40, 1)
        row("note_off_frames", "Note-Off-Frames", 1, 10, 1)
        row("note_change_frames", "Wechsel-Frames", 1, 6, 1)
        row("note_max_poly", "Max. Polyphonie", 1, 8, 1)
        row("yin_threshold", "YIN-Strenge (klein = streng)", 0.05, 0.40, 0.01)

        bf = tk.Frame(win, bg=COL_BG)
        bf.pack(fill="x", padx=16, pady=12)
        tk.Button(bf, text="Speichern", command=lambda: self._save_calib(win),
                  font=self.f_small, bg="#1D9E75", fg="#04342C",
                  activebackground=COL_OK, activeforeground="#04342C", bd=0,
                  padx=18, pady=6, highlightthickness=0,
                  cursor="hand2").pack(side="right")
        self._small_button(bf, "Abbrechen", win.destroy).pack(side="right",
                                                              padx=(0, 8))
        self._small_button(bf, "Standardwerte", self._reset_calib).pack(side="left")

    def _save_calib(self, win):
        cfg = load_config()
        for k, v in self._calib_vars.items():
            val = v.get()
            cfg[k] = int(round(val)) if k in self._CALIB_INT else round(val, 2)
        save_config(cfg)
        win.destroy()

    def _reset_calib(self):
        for k, v in self._calib_vars.items():
            v.set(self._CALIB_DEFAULTS[k])

    # ------------------------------------------------------------------
    # Zentraler "Was tun?"-Dialog (nach Datei-Import / Aufnahme)
    # ------------------------------------------------------------------
    def _ask_actions(self, subtitle, allow_clock=True):
        """Fragt nach dem Import einer Datei / nach einer Aufnahme, was damit
        passieren soll. Mehrfachauswahl moeglich; die teure Stem-Trennung laeuft
        anschliessend nur EINMAL fuer alle Stem-Aktionen. Rueckgabe dict
        {clock, export, sheet, play, out_dir, language, model} oder None."""
        demucs_ok = core.demucs_available()
        whisper_ok = core.whisper_available()
        bass_ok = core.basic_pitch_available()
        cfg = load_config()
        midi_ok = bool(cfg.get("midi_output"))
        lang_map = [("Automatisch", "auto"), ("Deutsch", "de"), ("English", "en")]
        model_map = [("Mittel – empfohlen", "medium"),
                     ("Klein – schnell", "small"),
                     ("Groß – beste Qualität (langsam)", "large-v3")]
        # Stem-Trennqualitaet: schnell reicht furs Song-Sheet; fuer Export/MIDI
        # lohnt die volle Demucs-Qualitaet (~20 % langsamer).
        rofo_ok = core.roformer_available()
        qual_map = [("Automatisch", "auto"), ("Hoch – für Export/MIDI", "hi"),
                    ("Maximum – fine-tuned + Shift-Trick (langsam)", "max"),
                    ("Maximum+ – fine-tuned + shifts 2 (sehr langsam)", "max2"),
                    (("Ultra – RoFormer SOTA, bester Bass (extrem langsam)"
                      if rofo_ok else
                      "Ultra – RoFormer (pip install audio-separator[cpu])"),
                     "ultra"),
                    ("Schnell – für Song-Sheet", "fast")]
        win = tk.Toplevel(self.root)
        win.title("Was soll passieren?")
        win.configure(bg=COL_BG)
        win.transient(self.root)
        win.grab_set()
        tk.Label(win, text="Was soll passieren?", font=self.f_h1, bg=COL_BG,
                 fg=COL_FG).pack(pady=(12, 2))
        tk.Label(win, text=subtitle, font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED).pack(pady=(0, 10))
        body = tk.Frame(win, bg=COL_BG)
        body.pack(padx=24, pady=4, anchor="w", fill="x")
        v_clock = tk.BooleanVar(value=allow_clock)
        v_export = tk.BooleanVar(value=False)
        v_sheet = tk.BooleanVar(value=False)
        v_play = tk.BooleanVar(value=False)
        v_stemmidi = tk.BooleanVar(value=False)
        v_barcut = tk.BooleanVar(value=bool(cfg.get("stem_barcut", False)))
        v_deluge = tk.BooleanVar(value=False)
        # Deluge: einzige Vorab-Frage ist, ob die Instrumentspuren getrennt
        # werden sollen (teuer) -- ohne Trennung geht es sofort in den Editor.
        dl_split = tk.BooleanVar(value=bool(cfg.get("deluge_split", False))
                                 and demucs_ok)
        # Play-Along-Mix: welche Spuren AUSBLENDEN + Zielformat (gemerkt)
        mp3_ok = core.mp3_supported()
        v_mixout = tk.BooleanVar(value=False)
        mo_drop = {n: tk.BooleanVar(value=(n in cfg.get("mixout_drop", ["vocals"])))
                   for n in ("bass", "drums", "other", "vocals")}
        mo_fmt = tk.StringVar(value=(cfg.get("mixout_fmt", "mp3")
                                     if mp3_ok else "wav"))

        def _section(text):
            tk.Label(body, text=text, font=self.f_tiny, bg=COL_BG, fg=COL_ACCENT,
                     anchor="w").pack(anchor="w", pady=(10, 2))

        def _cb(text, var, enabled=True, note="", command=None):
            cb = tk.Checkbutton(
                body, text=text, variable=var, command=command, font=self.f_small,
                bg=COL_BG, fg=COL_FG if enabled else COL_MUTED,
                selectcolor=COL_SURFACE, activebackground=COL_BG,
                activeforeground=COL_FG, bd=0, highlightthickness=0, anchor="w")
            if not enabled:
                var.set(False)
                cb.config(state="disabled")
            cb.pack(anchor="w", pady=2)
            if note:
                tk.Label(body, text="      " + note, font=self.f_tiny, bg=COL_BG,
                         fg=COL_MUTED).pack(anchor="w")
            return cb

        def _menu(parent, row, label, options, current):
            tk.Label(parent, text=label, font=self.f_tiny, bg=COL_BG,
                     fg=COL_ACCENT).grid(row=row, column=0, sticky="w", padx=(0, 10))
            var = tk.StringVar(value=next((lbl for lbl, v in options
                                           if v == current), options[0][0]))
            om = tk.OptionMenu(parent, var, *[lbl for lbl, _ in options])
            om.config(bg=COL_SURFACE, fg=COL_FG, activebackground=COL_SURF_HI,
                      activeforeground=COL_FG, bd=0, highlightthickness=0,
                      font=self.f_tiny, cursor="hand2")
            om["menu"].config(bg=COL_SURFACE, fg=COL_FG)
            om.grid(row=row, column=1, sticky="we")
            return var

        # ---- Aktionen ----
        _section("Aktionen (beliebig kombinierbar)")
        if allow_clock:
            _cb("MIDI-Clock-Ausgabe (Datei abspielen, driftfreie Clock)", v_clock)
        _cb("Stems exportieren (einzelne WAVs speichern)", v_export, demucs_ok,
            "" if demucs_ok else "braucht: pip install demucs")

        # Play-Along-Mix + (nur dann sichtbare) Ausblenden-/Format-Auswahl
        mixf = tk.Frame(body, bg=COL_BG)
        mxr1 = tk.Frame(mixf, bg=COL_BG)
        mxr1.pack(anchor="w")
        tk.Label(mxr1, text="Ausblenden:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_ACCENT).pack(side="left", padx=(0, 6))
        for nm, lbl in (("bass", "Bass"), ("drums", "Drums"),
                        ("other", "Rest"), ("vocals", "Gesang")):
            tk.Checkbutton(mxr1, text=lbl, variable=mo_drop[nm], font=self.f_small,
                           bg=COL_BG, fg=COL_FG, selectcolor=COL_SURFACE,
                           activebackground=COL_BG, activeforeground=COL_FG, bd=0,
                           highlightthickness=0).pack(side="left", padx=(0, 4))
        mxr2 = tk.Frame(mixf, bg=COL_BG)
        mxr2.pack(anchor="w", pady=(2, 0))
        tk.Label(mxr2, text="Format:", font=self.f_tiny, bg=COL_BG,
                 fg=COL_ACCENT).pack(side="left", padx=(0, 6))
        for val, lbl in (("mp3", "MP3 (320 kbit/s)"), ("wav", "WAV")):
            rb = tk.Radiobutton(mxr2, text=lbl, variable=mo_fmt, value=val,
                                font=self.f_small, bg=COL_BG, fg=COL_FG,
                                selectcolor=COL_SURFACE, activebackground=COL_BG,
                                activeforeground=COL_FG, bd=0, highlightthickness=0)
            if val == "mp3" and not mp3_ok:
                rb.config(state="disabled", fg=COL_MUTED)
            rb.pack(side="left", padx=(0, 8))
        # BandHelper: Karaoke-Automationsspur (+ ChordPro-Zip) mit erzeugen
        v_bh = tk.BooleanVar(value=bool(cfg.get("mixout_bh", False)) and whisper_ok)
        bh_ref = {"text": ""}
        mxr3 = tk.Frame(mixf, bg=COL_BG)
        mxr3.pack(anchor="w", pady=(2, 0))
        cb_bh = tk.Checkbutton(mxr3, text="BandHelper-Automation (Karaoke-"
                               "Zeilenmarkierung)", variable=v_bh,
                               font=self.f_small, bg=COL_BG,
                               fg=COL_FG if whisper_ok else COL_MUTED,
                               selectcolor=COL_SURFACE, activebackground=COL_BG,
                               activeforeground=COL_FG, bd=0, highlightthickness=0)
        if not whisper_ok:
            v_bh.set(False)
            cb_bh.config(state="disabled")
        cb_bh.pack(side="left")
        bh_lbl = tk.Label(mixf, text=("kein Text – ChordPro-Zip wird miterzeugt"
                                      if whisper_ok
                                      else "braucht: pip install faster-whisper"),
                          font=self.f_tiny, bg=COL_BG, fg=COL_MUTED)
        self._small_button(mxr3, "Text aus BandHelper…",
                           lambda: self._bh_text_dialog(win, bh_ref, bh_lbl)).pack(
                               side="left", padx=(8, 0))
        bh_lbl.pack(anchor="w", padx=(18, 0))
        tk.Label(mixf, text="Die übrigen Spuren werden wieder zu EINER Datei "
                 "gemischt – z. B. ohne Gesang = Karaoke-Version, ohne Bass = "
                 "Übe-Playback zum Mitspielen. BandHelper-Automation: Textdatei "
                 "mit Zeilenmarkierungen je Gesangseinsatz (0,3 s Vorlauf) – Inhalt "
                 "in BandHelper unter „Automationsspur → Einfügen“ einsetzen; die "
                 "Zeiten passen zum Play-Along-Mix als angehängter Aufnahme. Ohne "
                 "eingefügten BandHelper-Text entsteht dazu ein ChordPro-Zip für "
                 "den Import.", font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED, justify="left", wraplength=460).pack(
                     anchor="w", pady=(2, 0))

        def _toggle_mixopts():
            if v_mixout.get():
                mixf.pack(after=mix_cb, anchor="w", fill="x", pady=(2, 6))
            else:
                mixf.pack_forget()

        mix_cb = _cb("Play-Along-Mix erstellen (Instrumente aus dem Mix ausblenden)",
                     v_mixout, demucs_ok,
                     "" if demucs_ok else "braucht: pip install demucs",
                     command=_toggle_mixopts)

        _cb("Stems anschließend abspielen (zusammen/getrennt)", v_play, demucs_ok)
        midi_note = ("" if (demucs_ok and bass_ok and midi_ok)
                     else "braucht: pip install basic-pitch" if not bass_ok
                     else "braucht einen MIDI-Ausgang (in den Einstellungen wählen)"
                     if not midi_ok else "")
        _cb("Stems → MIDI senden (Basic Pitch: Bass/Rest/Gesang, je Kanal)",
            v_stemmidi, demucs_ok and bass_ok and midi_ok, midi_note)

        # Song-Sheet + (nur dann sichtbare) Sheet-Optionen Sprache/Modell
        sheetf = tk.Frame(body, bg=COL_BG)
        lvar = _menu(sheetf, 0, "Sheet-Sprache", lang_map,
                     cfg.get("sheet_lang", "auto"))
        mvar = _menu(sheetf, 1, "Sheet-Modell", model_map,
                     cfg.get("sheet_model", "medium"))
        v_online = tk.BooleanVar(value=bool(cfg.get("online_ref", False)))
        tk.Checkbutton(sheetf, text="Online-Abgleich (Song im Netz identifizieren; "
                       "unsichere Text-/Akkord-Stellen gewichtet korrigieren)",
                       variable=v_online, font=self.f_small, bg=COL_BG, fg=COL_FG,
                       selectcolor=COL_SURFACE, activebackground=COL_BG,
                       activeforeground=COL_FG, bd=0, highlightthickness=0,
                       wraplength=430, justify="left").grid(
                           row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        tk.Label(sheetf, text="Tipp: Sprache fest wählen – die Auto-Erkennung liegt "
                 "bei Gesang oft daneben. Online-Abgleich: Quellen lrclib.net + "
                 "cifraclub; sichere eigene Erkennung hat immer Vorrang.",
                 font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED, justify="left", wraplength=430).grid(
                     row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        def _toggle_sheetopts():
            if v_sheet.get():
                sheetf.pack(after=sheet_cb, anchor="w", fill="x", pady=(2, 6))
            else:
                sheetf.pack_forget()

        sheet_cb = _cb("Song-Sheet erstellen (Text + Akkorde)", v_sheet,
                       demucs_ok and whisper_ok,
                       "" if (demucs_ok and whisper_ok)
                       else "braucht: pip install faster-whisper",
                       command=_toggle_sheetopts)

        # Deluge-Song: EINE Frage -- getrennte Instrumentspuren oder nur der
        # Gesamtmix. Alles Weitere (Parts, Taktraster, Downbeat, Spur- und
        # MIDI-Auswahl) passiert danach direkt im Part-Editor.
        delf = tk.Frame(body, bg=COL_BG)
        tk.Checkbutton(delf, text="Instrumentspuren trennen (Bass/Drums/Rest/"
                       "Gesang – dauert je nach Qualität einige Minuten)",
                       variable=dl_split, font=self.f_small, bg=COL_BG,
                       fg=COL_FG if demucs_ok else COL_MUTED,
                       selectcolor=COL_SURFACE, activebackground=COL_BG,
                       activeforeground=COL_FG, bd=0, highlightthickness=0,
                       state=("normal" if demucs_ok else "disabled"),
                       wraplength=460, justify="left").pack(anchor="w")
        tk.Label(delf, text="Ohne Häkchen geht es SOFORT weiter – der Editor "
                 "arbeitet dann mit dem Gesamtmix (eine Spur). Mit Häkchen "
                 "stehen im Editor zusätzlich die einzelnen Instrumente zum "
                 "Anhören und Exportieren bereit.\nDanach öffnet sich der "
                 "Part-Editor: Abschnitte in der Wellenform markieren, als Loop "
                 "prüfen, und beim Speichern wählen, welche Spuren als Audio "
                 "und MIDI in den Deluge-Song kommen.",
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED, justify="left",
                 wraplength=460).pack(anchor="w", pady=(2, 0))

        def _toggle_delopts():
            if v_deluge.get():
                delf.pack(after=del_cb, anchor="w", fill="x", pady=(2, 6))
            else:
                delf.pack_forget()

        del_cb = _cb("Deluge-Song erstellen (.XML für Synthstrom Deluge)", v_deluge,
                     True, "", command=_toggle_delopts)

        # ---- Optionen ----
        _section("Optionen")
        optf = tk.Frame(body, bg=COL_BG)
        optf.pack(anchor="w", fill="x")
        qvar = _menu(optf, 0, "Stem-Qualität", qual_map,
                     cfg.get("stem_quality", "auto"))
        tk.Label(body, text="Stem-Qualität „Automatisch“ = hoch bei Export/"
                 "Abspielen/Stems-MIDI, sonst schnell (fürs Song-Sheet reicht das).",
                 font=self.f_tiny, bg=COL_BG, fg=COL_MUTED,
                 justify="left").pack(anchor="w", pady=(4, 0))
        _cb("Stems auf Takt schneiden – Sample mit 2 Takten Vorlauf (nur Export)",
            v_barcut, demucs_ok)
        tk.Label(body, text="Schneidet alle exportierten Stems gemeinsam: Start "
                 "exakt 2 Takte vor dem ersten Downbeat (4/4); der Auftakt liegt im "
                 "Vorlauf. Ende bleibt unverändert.", font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED, justify="left").pack(anchor="w", pady=(2, 0))
        # Schnelltest: nur die ersten N s verarbeiten -> kurze Wartezeit beim
        # Antesten (v. a. fuer die sehr langsame „Ultra"-Qualitaet).
        qtf = tk.Frame(body, bg=COL_BG)
        qtf.pack(anchor="w", fill="x", pady=(8, 0))
        v_qt = tk.BooleanVar(value=False)
        qt_sec = tk.StringVar(value="30")
        tk.Checkbutton(qtf, text="Schnelltest – nur die ersten", variable=v_qt,
                       font=self.f_small, bg=COL_BG, fg=COL_FG, selectcolor=COL_SURFACE,
                       activebackground=COL_BG, activeforeground=COL_FG, bd=0,
                       highlightthickness=0).pack(side="left")
        tk.Entry(qtf, textvariable=qt_sec, width=4, font=self.f_small, bg=COL_SURFACE,
                 fg=COL_FG, insertbackground=COL_FG, bd=0, highlightthickness=0,
                 justify="center").pack(side="left", padx=4)
        tk.Label(qtf, text="Sekunden verarbeiten (Qualität schnell antesten)",
                 font=self.f_small, bg=COL_BG, fg=COL_MUTED).pack(side="left")
        result = {}

        def _ok():
            if not (v_clock.get() or v_export.get() or v_sheet.get()
                    or v_play.get() or v_stemmidi.get() or v_deluge.get()
                    or v_mixout.get()):
                return                       # nichts gewaehlt -> Dialog offen lassen
            mix_cfg = None
            if v_mixout.get():
                drops = [n for n in ("bass", "drums", "other", "vocals")
                         if mo_drop[n].get()]
                if not drops or len(drops) >= 4:
                    return                   # nichts/alles ausgeblendet -> offen lassen
                mix_cfg = {"drop": drops, "fmt": mo_fmt.get(),
                           "bh": bool(v_bh.get()), "bh_ref": bh_ref.get("text", "")}
            out_dir = None
            if v_export.get() or mix_cfg:
                out_dir = filedialog.askdirectory(
                    title="Zielordner (Stems/Play-Along-Mix)",
                    initialdir=cfg.get("last_save_dir") or "")
                if not out_dir:
                    return                   # Abbruch der Ordnerwahl -> zurueck
            deluge_cfg = None
            if v_deluge.get():
                # KEINE weiteren Fragen: nach (optionaler) Trennung oeffnet sich
                # direkt der Part-Editor -- Parts, Downbeat, Spur-/MIDI-Auswahl
                # und der Speicherpfad kommen dort.
                deluge_cfg = {"split": bool(dl_split.get()) and demucs_ok}
            lang = next(v for lbl, v in lang_map if lbl == lvar.get())
            model = next(v for lbl, v in model_map if lbl == mvar.get())
            qual = next(v for lbl, v in qual_map if lbl == qvar.get())
            new_cfg = {**load_config(), "sheet_lang": lang, "sheet_model": model,
                       "stem_quality": qual, "stem_barcut": bool(v_barcut.get()),
                       "deluge_split": bool(dl_split.get()),
                       "online_ref": bool(v_online.get())}
            if mix_cfg:
                new_cfg["mixout_drop"] = mix_cfg["drop"]
                new_cfg["mixout_fmt"] = mix_cfg["fmt"]
                new_cfg["mixout_bh"] = mix_cfg["bh"]
            if out_dir:
                new_cfg["last_save_dir"] = out_dir
            save_config(new_cfg)
            # "Automatisch": hohe Trennqualitaet, wenn die Stems als Audio/MIDI
            # genutzt werden (Export/Abspielen/Stems-MIDI) -- sonst schnell.
            # "Maximum": fine-tuned Modell + Shift-Trick (kaum Uebersprechen, lahm).
            sep_model, shifts, sep_backend = "htdemucs", 0, "demucs"
            if qual == "ultra":          # RoFormer (SOTA, bester Bass; sehr langsam)
                overlap, sep_backend = 0.25, "roformer"
            elif qual == "max2":         # fine-tuned + doppelter Shift-Trick
                overlap, sep_model, shifts = 0.25, "htdemucs_ft", 2
            elif qual == "max":
                overlap, sep_model, shifts = 0.25, "htdemucs_ft", 1
            elif qual == "hi":
                overlap = 0.25
            elif qual == "fast":
                overlap = 0.1
            else:
                overlap = (0.25 if (v_export.get() or v_play.get()
                                    or v_stemmidi.get() or v_deluge.get()
                                    or v_mixout.get()) else 0.1)
            qts = 0
            if v_qt.get():
                try:
                    qts = int(max(5.0, float(qt_sec.get().replace(",", "."))))
                except ValueError:
                    qts = 30
            result.update(clock=bool(v_clock.get()) if allow_clock else False,
                          export=bool(v_export.get()), sheet=bool(v_sheet.get()),
                          play=bool(v_play.get()), stemmidi=bool(v_stemmidi.get()),
                          barcut=bool(v_barcut.get()), quicktest_s=qts,
                          deluge=deluge_cfg, sheet_online=bool(v_online.get()),
                          mixout=mix_cfg,
                          out_dir=out_dir, overlap=overlap, shifts=shifts,
                          sep_model=sep_model, sep_backend=sep_backend,
                          language=None if lang == "auto" else lang, model=model)
            win.destroy()

        ctl = tk.Frame(win, bg=COL_BG)
        ctl.pack(pady=12)
        tk.Button(ctl, text="Los", command=_ok, font=self.f_btn, bg="#1D9E75",
                  fg="#04342C", activebackground=COL_OK, activeforeground="#04342C",
                  bd=0, padx=24, pady=6, highlightthickness=0,
                  cursor="hand2").pack(side="left", padx=6)
        self._small_button(ctl, "Abbrechen", win.destroy).pack(side="left", padx=6)
        win.wait_window()
        return result or None

    def _run_material(self, source, actions, title):
        """Verarbeitet importierte Musik (Datei-Pfad ODER ('array', rec, sr)) gemaess
        der gewaehlten Aktionen. Stem-Trennung laeuft nur einmal fuer alle Aktionen.
        Reiner Clock-Fall (Datei) geht direkt ohne Trenn-Aufwand in den Datei-Modus.
        Rueckgabe: True, wenn ein Verarbeitungs-Thread gestartet wurde (dann meldet
        sich spaeter _material_res); False sonst (Clock-/Leerfall) -- so weiss die
        Stueck-Schlange, ob sie auf das Ergebnis warten oder gleich weitermachen muss."""
        needs_stems = (actions["export"] or actions["sheet"] or actions["play"]
                       or actions.get("stemmidi") or actions.get("deluge")
                       or actions.get("mixout"))
        if not needs_stems:
            if actions.get("clock") and not isinstance(source, tuple):
                self._begin_file_clock(source)
            return False
        # Clock (falls gewaehlt, nur fuer Dateien) erst NACH der Verarbeitung starten.
        # ABER nicht bei "Stems → MIDI": dort liefert der Stem-Player die Clock ueber
        # DENSELBEN Port -- eine separate Datei-Clock wuerde den (unter Windows oft
        # single-client) Port ein zweites Mal oeffnen und scheitern.
        self._material_clock = source if (actions.get("clock")
                                          and not isinstance(source, tuple)
                                          and not actions.get("stemmidi")) else None
        log = self._stem_log_open("Verarbeitung")
        self._stem_log(log, title)
        bits = [n for n, on in (("Export", actions["export"]),
                                ("Play-Along-Mix", actions.get("mixout")),
                                ("Song-Sheet", actions["sheet"]),
                                ("Abspielen", actions["play"]),
                                ("Stems-MIDI", actions.get("stemmidi")),
                                ("Deluge-Song", actions.get("deluge")),
                                ("MIDI-Clock", actions.get("clock"))) if on]
        self._stem_log(log, "Gewählt: " + ", ".join(bits))
        threading.Thread(target=self._material_worker,
                         args=(source, actions, title, log), daemon=True).start()
        return True

    def _prepare_part_editor(self, stems, ssr, title, log, cb, bpm_hint=0.0):
        """Alles, was der Part-Editor braucht: Tempo + Auto-Downbeat bestimmen
        und die Spuren EINMAL aufs Taktraster ziehen (gleichmaessige Takte ->
        Loops und Schnitte sitzen). Rueckgabe-dict fuer _open_part_editor."""
        self._stem_log(log, "== Deluge-Song vorbereiten ==")
        dbpm = float(bpm_hint or 0.0)
        if dbpm <= 0:
            try:
                ts = stems.get("drums")
                if ts is None:
                    ts = core.accompaniment_from_stems(stems)
                ts = ts.mean(axis=1) if getattr(ts, "ndim", 1) == 2 else ts
                dbpm = float(core.estimate_tempo(ts, ssr) or 0.0)
            except Exception:
                dbpm = 0.0
        # WICHTIG: standardmaessig NICHT warpen. Jede Raster-Stufe zieht das
        # Audio durch einen Phase-Vocoder -- das veraendert den Klang hoerbar
        # (und kann bei "Pro Beat" wie Aussetzer klingen). Bei produzierten
        # Songs mit konstantem Tempo ist das ueberfluessig. Wer es braucht,
        # schaltet das Raster im Editor bewusst ein.
        grid = load_config().get("editor_gridlock", "off")
        if grid not in ("off", "bar1", "groove", "beat"):
            grid = "off"
        t_db, ws, info = 0.0, stems, None
        try:
            t_db, gbpm = core.detect_downbeat(stems, ssr, log=cb)
            if gbpm > 0:
                dbpm = gbpm
        except Exception as ex:
            self._stem_log(log, f"Downbeat-Erkennung fehlgeschlagen: {ex}")
        if grid != "off":
            self._stem_log(log, f"Ziehe aufs Taktraster („{grid}“) – "
                           "gleichmäßige Takte für saubere Loops …")
            try:
                ws, info = core.warp_stems_to_grid(stems, ssr, per=grid,
                                                   db_orig=t_db, log=cb)
            except Exception as ex:
                self._stem_log(log, f"Taktraster übersprungen: {ex}")
                ws, info = stems, None
        wbpm = float(info["bpm"]) if info else (dbpm or 120.0)
        wt_db = float(info["t_db"]) if info else t_db
        self._stem_log(log, f"Bereit – Part-Editor öffnet sich ({wbpm:.1f} BPM).")
        return {"stems": ws, "orig": stems, "sr": ssr, "bpm": wbpm,
                "t_db": wt_db, "db_orig": t_db, "title": title,
                "gridlock": grid}

    def _material_deluge_only(self, stems, ssr, actions, title, out, log, cb,
                              step, total):
        """Kurzweg: NUR Deluge-Song ohne Stem-Trennung -- Vorbereitung und
        Ergebnis setzen, ohne die uebrigen (Stem-)Phasen zu durchlaufen."""
        try:
            self._stem_progress(log, step, total, "Deluge-Song")
            out["part_editor"] = self._prepare_part_editor(
                stems, ssr, title, log, cb)
            self._stem_progress(log, total, total, "Fertig")
            self._material_res = (out, None)
        except Exception as e:
            self._stem_log_error(log)
            self._material_res = (None, str(e))
        return

    def _material_worker(self, source, actions, title, log):
        try:
            cb = lambda m: self._stem_log(log, m)
            out = {"actions": actions, "title": title, "sheet": None,
                   "stems": None, "stem_sr": None, "export_paths": None,
                   "midi_notes": None}
            qt = int(actions.get("quicktest_s", 0) or 0)
            if qt > 0:                             # Schnelltest: nur ersten Ausschnitt
                self._stem_log(log, f"Schnelltest: nur die ersten {qt} s.")
                try:
                    if isinstance(source, tuple):
                        _t, rec, srr = source
                        source = ("array", np.asarray(rec)[:int(qt * srr)], srr)
                    else:
                        data, fsr = core.load_audio_head(source, qt)
                        source = ("array", data, fsr)
                except Exception as ex:
                    self._stem_log(log, f"Schnelltest übersprungen: {ex}")
            ov = float(actions.get("overlap", 0.1))
            sh = int(actions.get("shifts", 0))
            sm = actions.get("sep_model", "htdemucs")
            # Braucht ueberhaupt jemand getrennte Spuren? Der Deluge-Weg kommt
            # auf Wunsch OHNE Trennung aus (nur Gesamtmix) -- das spart bei
            # hoher Qualitaet leicht eine halbe Stunde.
            dcfg0 = actions.get("deluge") or {}
            need_sep = bool(actions["export"] or actions["sheet"]
                            or actions["play"] or actions.get("stemmidi")
                            or actions.get("mixout")
                            or (dcfg0 and dcfg0.get("split")))
            # Phasen fuer den Fortschrittsbalken zaehlen
            total = (1 + int(bool(actions["export"])) + int(bool(actions["sheet"]))
                     + int(bool(actions.get("stemmidi")))
                     + int(bool(actions.get("deluge")))
                     + int(bool(actions.get("mixout"))))
            step = 0
            if not need_sep:
                # --- Ohne Trennung: Audio als EINE Spur laden (sofort) ---
                self._stem_progress(log, step, total, "Audio laden")
                self._stem_log(log, "== Audio laden (ohne Trennung) ==")
                if isinstance(source, tuple):
                    _tag, rec, srr = source
                    stems = {"mix": np.asarray(rec, dtype=np.float32)}
                    ssr = int(srr)
                else:
                    _yan, audio, sr_play = core.load_audio_file(source)
                    stems, ssr = {"mix": np.asarray(audio, dtype=np.float32)}, int(sr_play)
                dur_s = len(stems["mix"]) / float(ssr)
                self._stem_log(log, f"Gesamtmix geladen ({dur_s / 60:.1f} min) – "
                               "keine Stem-Trennung nötig.")
                step += 1
                return self._material_deluge_only(stems, ssr, actions, title,
                                                  out, log, cb, step, total)
            self._stem_progress(log, step, total, "Stems trennen")
            backend = actions.get("sep_backend", "demucs")
            qtag = ("[Ultra: RoFormer SOTA]" if backend == "roformer"
                    else "[Maximum: htdemucs_ft + Shift-Trick x%d]" % sh if sh > 0
                    else "[hohe Qualität]" if ov >= 0.2 else "[schnell]")
            self._stem_log(log, "== Stems trennen (einmalig) == " + qtag)
            self._stem_log(log, core.separation_eta(source, backend=backend,
                                                    model=sm, shifts=sh))
            if backend == "roformer" and isinstance(source, tuple):
                _tag, rec, srr = source
                stems, ssr = core.separate_stems_roformer_array(rec, srr, log=cb)
            elif backend == "roformer":
                stems, ssr = core.separate_stems_roformer(source, log=cb)
            elif isinstance(source, tuple):          # ('array', rec, sr)
                _tag, rec, srr = source
                stems, ssr = core.separate_stems_array(rec, srr, model=sm, log=cb,
                                                       overlap=ov, shifts=sh)
            else:
                stems, ssr = core.separate_stems(source, model=sm, log=cb,
                                                 overlap=ov, shifts=sh)
            step += 1                              # Trennung fertig
            if actions["export"]:
                self._stem_progress(log, step, total, "Stems exportieren")
                self._stem_log(log, "== Stems exportieren ==")
                exp_stems = stems
                if actions.get("barcut"):          # nur die EXPORTIERTEN Stems schneiden
                    self._stem_log(log, "Auf Takt schneiden (2 Takte Vorlauf) …")
                    exp_stems = core.bar_aligned_stems(stems, ssr, log=cb)
                out["export_paths"] = core.write_stems_to_files(
                    exp_stems, ssr, actions["out_dir"], base=title, log=cb)
                step += 1
            if actions["sheet"]:
                self._stem_progress(log, step, total, "Song-Sheet")
                self._stem_log(log, "== Song-Sheet erstellen ==")
                out["sheet"] = core.song_sheet_from_stems(
                    stems, ssr, title=title, whisper_size=actions["model"],
                    language=actions["language"], log=cb,
                    online=bool(actions.get("sheet_online")))
                # Wird kein eigener Stem-Player geoeffnet, spielt das Sheet-Fenster
                # selbst den ganzen Mix ab (Mitlauf + Start/Stopp).
                if not actions["play"]:
                    mix = None
                    for a in stems.values():
                        a = np.asarray(a, dtype=np.float32)
                        if mix is None:
                            mix = a.copy()
                        else:
                            m = min(len(mix), len(a))
                            mix = mix[:m] + a[:m]
                    out["sheet"]["mix"] = mix
                    out["sheet"]["sr"] = ssr
                step += 1
            if actions.get("mixout"):
                self._stem_progress(log, step, total, "Play-Along-Mix")
                mo = actions["mixout"]
                ohne = "-".join(core.STEM_LABELS.get(n, n) for n in mo["drop"])
                self._stem_log(log, f"== Play-Along-Mix (ohne {ohne}) ==")
                # Fehler hier sollen die weiteren Aktionen (MIDI/Deluge) nicht stoppen
                try:
                    pmix = core.mix_from_stems(stems, drop=mo["drop"], log=cb)
                    p = os.path.join(
                        actions["out_dir"],
                        core.sanitize_filename(f"{title}_ohne_{ohne}")
                        + "." + mo["fmt"])
                    core.save_mix_file(p, pmix, ssr, log=cb)
                    if mo.get("bh"):
                        # Karaoke-Automation: das Sheet der Sheet-Aktion wieder-
                        # verwenden, sonst nur dafuer berechnen (oeffnet KEIN
                        # Sheet-Fenster)
                        sh = out.get("sheet")
                        if sh is None:
                            self._stem_log(
                                log, "== Transkription für BandHelper-Automation ==")
                            sh = core.song_sheet_from_stems(
                                stems, ssr, title=title,
                                whisper_size=actions["model"],
                                language=actions["language"], log=cb,
                                online=bool(actions.get("sheet_online")))
                        bb = core.sanitize_filename(title)
                        _tp, cptxt = core.write_bandhelper_automation(
                            actions["out_dir"], bb, sh, len(pmix) / float(ssr),
                            ref_text=mo.get("bh_ref"), log=cb)
                        if cptxt is not None:
                            core.write_bandhelper_zip(
                                os.path.join(actions["out_dir"],
                                             bb + "_bandhelper.zip"),
                                [(sh.get("title") or title, cptxt)], log=cb)
                except Exception as ex:
                    self._stem_log(log, f"Play-Along-Mix fehlgeschlagen: {ex}")
                step += 1
            if actions.get("stemmidi"):
                self._stem_progress(log, step, total, "Stems → MIDI")
                self._stem_log(log, "== Stems → MIDI (Basic Pitch) ==")
                min_ms = float(load_config().get("bass_min_ms", 130))
                out["midi_notes"] = core.stems_to_midi_notes(
                    stems, ssr, min_note_ms=min_ms, log=cb)
                # Schlagzeug separat (band-weise Onsets statt basic-pitch)
                if stems.get("drums") is not None:
                    self._stem_log(log, "== Schlagzeug → MIDI ==")
                    try:
                        dmap, dsens = self._drum_settings()
                        out["midi_notes"]["drums"] = core.drums_to_midi_notes(
                            stems["drums"], ssr, mapping=dmap,
                            sensitivity=dsens, log=cb)
                    except Exception as ex:
                        self._stem_log(log, f"Schlagzeug→MIDI übersprungen: {ex}")
                step += 1                          # Stems→MIDI fertig
            if actions.get("deluge"):
                self._stem_progress(log, step, total, "Deluge-Song")
                dbpm = float((out.get("sheet") or {}).get("bpm", 0.0))
                out["part_editor"] = self._prepare_part_editor(
                    stems, ssr, title, log, cb, bpm_hint=dbpm)
                step += 1                          # Deluge-Vorbereitung fertig
            # Stem-Player oeffnen, wenn Abspielen ODER Stems-MIDI gewaehlt ist
            # (die MIDI-Spuren laufen synchron zur Stem-Position mit).
            if actions["play"] or actions.get("stemmidi"):
                out["stems"], out["stem_sr"] = stems, ssr
                # Tempo fuer eine optionale MIDI-Clock (falls nicht schon vom Sheet)
                bpm = float((out.get("sheet") or {}).get("bpm", 0.0))
                if bpm <= 0:
                    try:
                        src = stems.get("drums")
                        if src is None:
                            src = core.accompaniment_from_stems(stems)
                        src = src.mean(axis=1) if getattr(src, "ndim", 1) == 2 else src
                        bpm = float(core.estimate_tempo(src, ssr) or 0.0)
                    except Exception:
                        bpm = 0.0
                out["bpm"] = bpm
            self._stem_progress(log, total, total, "Fertig")
            self._material_res = (out, None)
        except Exception as e:
            self._stem_log_error(log)
            self._material_res = (None, str(e))

    def _open_sheet_window(self, res, player=None):
        """Zeigt das fertige Chord-Sheet (Monospace). Erlaubt das Feinjustieren
        des Akkord-Versatzes, Start/Stopp der Wiedergabe und markiert beim
        Abspielen die aktuelle Stelle WORTGENAU (Karaoke-Mitlauf).
        player: vorhandener StemPlayer (z. B. der Stem-Mischer); fehlt er, baut das
        Fenster aus res['mix'] einen eigenen Player fuer Start/Stopp."""
        win = tk.Toplevel(self.root)
        title = res.get("title") or "Song-Sheet"
        win.title(f"Song-Sheet – {title}")
        win.configure(bg=COL_BG)
        win.geometry("780x600")

        # Eigenen Mix-Player bauen, falls keiner uebergeben wurde
        owns_player = False
        if player is None and res.get("mix") is not None:
            try:
                player = core.StemPlayer([res["mix"]], res.get("sr", 44100),
                                         names=["Song"])
                player.start_stream()
                owns_player = True
                self._stem_players.append(player)
            except Exception:
                player = None

        meta = []
        if res.get("key"):
            meta.append(res["key"])
        if res.get("bpm"):
            meta.append(f"{res['bpm']:.0f} BPM")
        tk.Label(win, text=title, font=self.f_h1, bg=COL_BG, fg=COL_FG).pack(pady=(12, 2))
        if meta:
            tk.Label(win, text="  ·  ".join(meta), font=self.f_tiny, bg=COL_BG,
                     fg=COL_MUTED).pack(pady=(0, 6))
        frame = tk.Frame(win, bg=COL_BG)
        frame.pack(fill="both", expand=True, padx=14, pady=4)
        sb = tk.Scrollbar(frame)
        sb.pack(side="right", fill="y")
        txt = tk.Text(frame, wrap="none", bg=COL_SURFACE, fg=COL_FG,
                      insertbackground=COL_FG, bd=0, highlightthickness=0,
                      font=("Courier", 11), yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.config(command=txt.yview)
        txt.tag_configure("line", background="#1d3a2e")            # aktuelle Zeile
        txt.tag_configure("word", background="#2f8f6b", foreground="#06231a")

        can_adjust = res.get("lines") is not None and res.get("chords") is not None
        # Startwert des Akkord-Vorlaufs: der beim Bauen verwendete (beat-relative)
        # Wert -- sonst der tempoabhaengige Default, sonst der feste Rueckfall.
        init_lead = res.get("chord_lead")
        if init_lead is None:
            init_lead = core.chord_lead_for_bpm(res.get("bpm", 0.0))
        state = {"lead": float(init_lead),
                 "text": res.get("text", ""), "chordpro": res.get("chordpro", ""),
                 "map": [], "cur": None}

        def _render():
            if can_adjust:
                state["text"], state["chordpro"], state["map"] = \
                    core.build_chord_sheet(
                        res["lines"], res["chords"], title=title,
                        key=res.get("key", ""), bpm=res.get("bpm", 0.0),
                        chord_lead=state["lead"], with_map=True)
            txt.config(state="normal")
            txt.delete("1.0", "end")
            txt.insert("1.0", state["text"])
            txt.config(state="disabled")
            state["cur"] = None
            if can_adjust:
                leadlbl.config(text=f"Akkord-Versatz: {state['lead'] * 1000:+.0f} ms")

        def _highlight(t):
            if t is None:
                return
            entry = None
            for e in state["map"]:
                if e["start"] <= t < e["end"]:
                    entry = e
                    break
            if entry is not state["cur"]:           # Zeile gewechselt
                state["cur"] = entry
                txt.tag_remove("line", "1.0", "end")
                if entry is not None:
                    for r in (entry.get("chord_row"), entry.get("lyric_row")):
                        if r:
                            txt.tag_add("line", f"{r}.0", f"{r}.end")
                    lr = entry.get("lyric_row")
                    if lr:
                        txt.see(f"{lr}.0")
            # Wort-genaue Markierung innerhalb der Zeile
            txt.tag_remove("word", "1.0", "end")
            if entry is not None and entry.get("lyric_row"):
                lr = entry["lyric_row"]
                for w in entry.get("words", []):
                    if w["start"] <= t < w["end"]:
                        txt.tag_add("word", f"{lr}.{w['c0']}", f"{lr}.{w['c1']}")
                        break

        def _nudge(d):
            state["lead"] = max(-2.0, min(2.0, state["lead"] + d))
            _render()

        def _save(kind):
            if kind == "chordpro":
                content = state["chordpro"]
                fname = core.sanitize_filename(title) + ".chordpro"
                types = [("ChordPro", "*.chordpro *.cho *.pro"), ("Alle", "*.*")]
            else:
                content = state["text"]
                fname = core.sanitize_filename(title) + ".txt"
                types = [("Textdatei", "*.txt"), ("Alle", "*.*")]
            cfg = load_config()
            p = filedialog.asksaveasfilename(
                title="Song-Sheet speichern", initialfile=fname,
                initialdir=cfg.get("last_save_dir") or "", filetypes=types)
            if not p:
                return
            try:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(content)
                save_config({**cfg, "last_save_dir": os.path.dirname(p)})
            except Exception as e:
                messagebox.showerror("Speichern", f"Konnte nicht speichern:\n{e}")

        # --- Transport (Start/Stopp/Anfang) ---
        if player is not None:
            trans = tk.Frame(win, bg=COL_BG)
            trans.pack(pady=(6, 0))
            playbtn = tk.Button(trans, text="▶", font=self.f_btn, bg=COL_SURFACE,
                                fg=COL_FG, activebackground=COL_SURF_HI,
                                activeforeground=COL_FG, bd=0, padx=18, pady=4,
                                highlightthickness=0, cursor="hand2",
                                command=lambda: player.toggle())
            playbtn.pack(side="left", padx=(0, 8))

            def _restart():
                player.seek(0.0)
                player.play()
            self._small_button(trans, "⏮ Anfang", _restart).pack(side="left", padx=4)
            poslbl = tk.Label(trans, text="0:00 / 0:00", font=self.f_small,
                              bg=COL_BG, fg=COL_MUTED)
            poslbl.pack(side="left", padx=10)

        # --- Akkord-Versatz ---
        if can_adjust:
            adj = tk.Frame(win, bg=COL_BG)
            adj.pack(pady=(6, 0))
            self._small_button(adj, "◀ Akkorde früher",
                               lambda: _nudge(0.1)).pack(side="left", padx=4)
            leadlbl = tk.Label(adj, text="", font=self.f_tiny, bg=COL_BG,
                               fg=COL_MUTED, width=20)
            leadlbl.pack(side="left", padx=4)
            self._small_button(adj, "Akkorde später ▶",
                               lambda: _nudge(-0.1)).pack(side="left", padx=4)
        _render()

        # --- Mitlauf-Schleife (Position -> Markierung + Transport-Anzeige) ---
        if player is not None:
            def _follow():
                if not win.winfo_exists():
                    return
                try:
                    pos, total = player.position()
                    playbtn.config(text="⏸" if player.is_playing() else "▶")
                    poslbl.config(text=f"{self._fmt_pos(pos)} / {self._fmt_pos(total)}")
                    if player.is_playing():
                        _highlight(pos)
                except Exception:
                    pass
                win.after(120, _follow)
            _follow()

        def _close():
            if owns_player and player is not None:
                try:
                    player.stop()
                except Exception:
                    pass
                if player in self._stem_players:
                    self._stem_players.remove(player)
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _close)
        ctl = tk.Frame(win, bg=COL_BG)
        ctl.pack(pady=8)
        self._small_button(ctl, "Als Text speichern …",
                           lambda: _save("text")).pack(side="left", padx=6)
        self._small_button(ctl, "Als ChordPro speichern …",
                           lambda: _save("chordpro")).pack(side="left", padx=6)
        self._small_button(ctl, "Schließen", _close).pack(side="left", padx=6)

    def on_setup_start(self):
        sel = self.lb_in.curselection()
        if not sel or not self.sources:
            self.err_label.config(text="Bitte eine Audioquelle waehlen.")
            return
        kind, ident, name, _text = self.sources[sel[0]]
        msel = self.lb_midi.curselection()
        midi = None
        if msel and msel[0] > 0:
            midi = self.midi_names[msel[0] - 1]
        try:
            mn = float(self.ent_min.get().replace(",", "."))
            mx = float(self.ent_max.get().replace(",", "."))
        except ValueError:
            self.err_label.config(text="BPM-Bereich: bitte Zahlen eingeben.")
            return
        if not (30.0 <= mn < mx <= 300.0):
            self.err_label.config(
                text="BPM-Bereich ungueltig (30 bis 300, von < bis).")
            return
        save_config({**load_config(),    # vorhandene Keys (z. B. last_save_dir) erhalten
                     "input_type": kind, "input_name": name,
                     "midi_output": midi or "",
                     "bpm_dezimal": bool(self.var_dec.get()),
                     "beat_sync": bool(self.var_beat.get()),
                     "akkorde": bool(self.var_chord.get()),
                     "akkorde_datei": bool(self.var_chordlog.get()),
                     "akkorde_schnell": bool(self.var_chordfast.get()),
                     "note_mode": MODE_FROM_LABEL.get(self.var_mode.get(), "clock"),
                     "min_bpm": mn, "max_bpm": mx})
        self._load_options()
        self.start_session((kind, ident), midi)

    # ------------------------------------------------------------------
    # Sitzung: Aufnahme + Clock starten/stoppen
    # ------------------------------------------------------------------
    def start_session(self, src, midi_name):
        """src: ('input', sd-Index) oder ('loopback', Lautsprechername)."""
        self.show_main()
        self.status_override = "INITIALISIERE ANALYSE …"
        self.src_label.config(text="")
        threading.Thread(target=self._warmup_then_begin,
                         args=(src, midi_name, self._session_gen),
                         daemon=True).start()

    def _warmup_then_begin(self, src, midi_name, gen):
        # librosa/numba einmalig aufwaermen (erster Aufruf kompiliert sonst
        # mitten im Betrieb und blockiert die Analyse mehrere Sekunden).
        if not self.warmed:
            try:
                w = np.zeros(int(core.ANALYSIS_SR * core.WINDOW_SECONDS),
                             dtype=np.float32)
                w[::core.ANALYSIS_SR // 4] = 0.5
                core.estimate_tempo(w, core.ANALYSIS_SR)
                core.chroma_pcp(w, core.ANALYSIS_SR)
            except Exception:
                pass
            self.warmed = True
        if not self.app_stop.is_set():
            self._begin_args = (gen, src, midi_name)

    def _begin(self, src, midi_name):
        if self.app_stop.is_set():
            return
        kind, ident = src
        if kind == "loopback":
            if sc is None:
                self.status_override = None
                self.show_setup(error="Loopback braucht das Paket 'soundcard'"
                                      " (pip install soundcard).")
                return
            try:
                source_arg = sc.get_microphone(id=str(ident),
                                               include_loopback=True)
            except Exception as e:
                self.status_override = None
                self.show_setup(error=f"Loopback fehlgeschlagen: {e}")
                return
            mode = "2"
            sr = float(core.LOOPBACK_SR)
            name = f"Loopback {ident}"
        else:
            mode = "1"
            source_arg = ident
            try:
                sr = float(core.pick_input_samplerate(ident))
            except Exception:
                sr = float(core.INPUT_SR)
            try:
                name = sd.query_devices(ident)['name']
            except Exception:
                name = f"Geraet #{ident}"

        with self.shared.lock:
            self.shared.capture_sr = sr
            self.shared.have_estimate = False
            self.shared.raw_bpm = 0.0
            self.shared.key = "—"
            self.shared.key_confident = False
            self.shared.chord = "—"
            self.shared.beat_sync = self.opt_beat_sync
        core.drain_queue(self.audio_q)

        note_mode = self.opt_note_mode != "clock"
        cap_bs = core.NOTE_BLOCKSIZE if note_mode else core.AUDIO_BLOCKSIZE

        try:
            self.stream, self.cap_thread, self.cap_stop = core.start_capture(
                mode, source_arg, sr, self.audio_q, None, self.shared,
                blocksize=cap_bs)
        except Exception as e:
            self.status_override = None
            self.show_setup(error=f"Quelle konnte nicht geoeffnet werden: {e}")
            return

        self.midi_out = None
        self.midi_name = midi_name
        if midi_name:
            try:
                self.midi_out = self._acquire_midi_out(midi_name)
            except Exception as e:
                core.stop_capture(self.stream, self.cap_thread, self.cap_stop)
                self.stream = self.cap_thread = self.cap_stop = None
                self.status_override = None
                self.show_setup(error=f"MIDI-Ausgang fehlgeschlagen: {e}")
                return

        if note_mode:
            # Noten-Modus: nur der schlanke Noten-Worker, KEINE Tempo-/Tonart-
            # Analyse und KEINE Clock (minimale Latenz).
            with self.shared.lock:
                self.shared.note_display = "—"
            self.note_stop = threading.Event()
            self.note_thread = threading.Thread(
                target=core.note_worker,
                args=(self.shared, self.audio_q, self.midi_out, self.note_stop,
                      self.opt_note_mode, self.note_calib()), daemon=True)
            self.note_thread.start()
        else:
            self.clock_stop = threading.Event()
            self.clock_thread = threading.Thread(
                target=core.clock_worker,
                args=(self.shared, self.midi_out, self.clock_stop), daemon=True)
            self.clock_thread.start()

            if self.analysis_thread is None:
                self.analysis_thread = threading.Thread(
                    target=core.analysis_worker_safe,
                    args=(self.shared, self.audio_q, self.app_stop), daemon=True)
                self.analysis_thread.start()

        if len(name) > 38:
            name = name[:37] + "…"
        self.src_label.config(text=f"QUELLE: {name}  @ {int(sr)} Hz")
        self.status_override = None

    def stop_session(self):
        self._session_gen += 1                # laufenden Warmup entwerten
        self._begin_args = None
        self.status_override = None
        with self.shared.lock:                # laufende Aufnahme verwerfen
            self.shared.rec_active = False
            self.shared.rec_blocks = []
        try:
            self._rec_btn_idle()
        except Exception:
            pass
        if self.file_mode or self.file_player is not None:
            self.stop_file()                  # ggf. Datei-Wiedergabe beenden
        if self.dj_engine is not None or self.dj_win is not None:
            self._dj_teardown()               # ggf. DJ-Fenster/Engine beenden
        if self.hold:
            self._set_hold(False)
        if (self.stream is not None or self.cap_thread is not None
                or self.cap_stop is not None):
            core.stop_capture(self.stream, self.cap_thread, self.cap_stop)
            self.stream = self.cap_thread = self.cap_stop = None
        if self.clock_stop is not None:
            self.clock_stop.set()
        if self.clock_thread is not None:
            self.clock_thread.join(timeout=1.5)
            self.clock_thread = self.clock_stop = None
        if self.note_stop is not None:
            self.note_stop.set()
        if self.note_thread is not None:
            self.note_thread.join(timeout=1.5)
            self.note_thread = self.note_stop = None
        if self.midi_out is not None:
            self._release_midi_out(self.midi_out)
            self.midi_out = None
        core.drain_queue(self.audio_q)
        with self.shared.lock:
            self.shared.have_estimate = False
            self.shared.raw_bpm = 0.0
            self.shared.key = "—"
            self.shared.chord = "—"

    def quit_app(self):
        try:
            self.stop_session()
        except Exception:
            pass
        for p in list(self._stem_players):    # offene Stem-Player schliessen
            try:
                p.stop()
            except Exception:
                pass
        self._stem_players = []
        for mp, port in list(self._midi_players):   # offene MIDI-Datei-Player
            try:
                mp.stop()
            except Exception:
                pass
            self._release_midi_out(port)
        self._midi_players = []
        self.app_stop.set()
        if self.analysis_thread is not None:
            self.analysis_thread.join(timeout=1.0)
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Anzeige-Aktualisierung (~6x pro Sekunde)
    # ------------------------------------------------------------------
    def _tick(self):
        # Verarbeitung (Export/Sheet/Abspielen) fertig? -> Ergebnisse oeffnen
        if self._material_res is not None:
            out, err = self._material_res
            self._material_res = None
            if err:
                self.err_label.config(text=f"Verarbeitung fehlgeschlagen: {err}")
                self._material_clock = None
            elif out:
                msgs = []
                if out.get("export_paths"):
                    msgs.append(f"{len(out['export_paths'])} Stems gespeichert")
                # Stem-Player zuerst -> seine Position kann das Sheet mitlaufen lassen
                player = None
                if out.get("stems"):
                    mbpm = out.get("bpm") or (out.get("sheet") or {}).get("bpm", 0.0)
                    player = self._open_stem_player(
                        out["stems"], out["stem_sr"],
                        midi_notes=out.get("midi_notes"), bpm=mbpm,
                        clock_default=bool(out["actions"].get("clock")))
                    msgs.append("Stem-Player offen")
                    if out.get("midi_notes"):
                        tot = sum(len(v) for v in out["midi_notes"].values())
                        msgs.append(f"{tot} MIDI-Noten ({len(out['midi_notes'])} Spuren)")
                if out.get("sheet"):
                    self._open_sheet_window(out["sheet"], player=player)
                    msgs.append("Song-Sheet erstellt")
                # Deluge: direkt in den Part-Editor (Wellenform, Marker, Loops);
                # Spur-/MIDI-Auswahl und Speicherpfad kommen dort beim Export.
                pe = out.get("part_editor")
                if pe:
                    msgs.append("Part-Editor offen")
                    self._open_part_editor(
                        self.root, pe["stems"], pe["sr"], pe["bpm"], pe["t_db"],
                        None, list(pe["stems"].keys()),
                        pe.get("title", "AudioWizard"),
                        gridlock=pe.get("gridlock", "groove"),
                        orig=pe.get("orig"), db_orig=pe.get("db_orig"))
                if msgs:
                    self.err_label.config(text="Fertig: " + ", ".join(msgs))
                # MIDI-Clock (nur Datei) erst jetzt starten, nach der Verarbeitung
                if self._material_clock is not None:
                    src = self._material_clock
                    self._material_clock = None
                    self._begin_file_clock(src)
            # Naechstes Stueck der Aufnahme-Schlange (leer = nichts) erst JETZT,
            # nachdem das vorige Ergebnis konsumiert ist (ein Ergebnis-Slot).
            self._material_start_next()
        # Datei-Modus: verzoegerter Start (Analyse-Thread -> Main-Thread, Tk-only)
        if self._file_begin_args is not None:
            kind, gen, payload = self._file_begin_args
            self._file_begin_args = None
            if gen == self._session_gen and self.file_mode:
                if kind == "error":
                    self.file_mode = False
                    self.status_override = None
                    self.show_setup(error=payload)
                else:
                    self._file_begin(*payload)
        if self.file_mode:
            self._tick_file()
            self.root.after(150, self._tick)
            return

        if self._begin_args is not None:
            gen, src, midi_name = self._begin_args
            self._begin_args = None
            if gen == self._session_gen:      # sonst: Session wurde inzwischen
                self._begin(src, midi_name)   # gestoppt -> Warmup verfallen

        if (self.cap_stop is not None and self.cap_stop.is_set()
                and (self.stream is not None or self.cap_thread is not None)):
            # Aufnahme hat sich selbst beendet (z. B. Geraet getrennt)
            core.log_message("[GUI: Aufnahme unterbrochen, zurueck zum Setup]")
            self.stop_session()
            self.show_setup(error="Aufnahme wurde unterbrochen "
                                  "(Geraet getrennt?).")

        # Watchdog: sollte der Analyse-Thread trotz Absturzschutz sterben,
        # wird er hier neu gestartet, statt dass die Anzeige stumm einfriert.
        if (self.analysis_thread is not None
                and not self.analysis_thread.is_alive()
                and not self.app_stop.is_set()):
            core.log_message("[GUI-Watchdog: Analyse-Thread tot, Neustart]")
            self.analysis_thread = threading.Thread(
                target=core.analysis_worker_safe,
                args=(self.shared, self.audio_q, self.app_stop), daemon=True)
            self.analysis_thread.start()

        with self.shared.lock:
            bpm = self.shared.target_bpm
            key = self.shared.key
            key_conf = self.shared.key_confident
            chord = self.shared.chord
            level = self.shared.level
            level_time = self.shared.level_time
            have = self.shared.have_estimate
            note_disp = self.shared.note_display
            rec_active = self.shared.rec_active

        # Aufnahme-Knopf: laufende Dauer anzeigen
        if rec_active:
            el = int(core.time.perf_counter() - self.rec_start_perf)
            self.rec_btn.config(text=f"■ Aufnahme {el // 60}:{el % 60:02d}")

        age = core.time.perf_counter() - level_time
        if age > 0.3:
            level *= core.math.exp(-(age - 0.3) / 0.4)
        db, _ = core.level_bar(level)

        note_mode = self.opt_note_mode != "clock"
        running = self.stream is not None or self.cap_thread is not None
        if note_mode:
            # Noten-Modus: aktuelle Note(n) in mittlerer Schrift; mehrere
            # Namen passen sonst nicht in die BPM-Riesenschrift.
            self.bpm_cap_label.config(
                text="AKKORD" if self.opt_note_mode == "chord"
                else "NOTEN" if self.opt_note_mode == "poly" else "NOTE")
            if self._bpm_big:
                self.bpm_label.config(font=self.f_key)
                self._bpm_big = False
            shown = note_disp if running else "—"
            self.bpm_label.config(
                text=shown, fg=COL_ACCENT if (running and shown != "—") else COL_MUTED)
            self.key_label.config(text="")
            self.key_par_label.config(text="")
            if self.opt_chords:
                self.chord_label.config(text="")
        else:
            self.bpm_cap_label.config(text="BPM")
            # BPM: gross und hell, sobald eine Schaetzung da ist; davor ein
            # dezenter kleiner Platzhalter (das riesige "—" sah wie ein
            # Renderfehler aus). Nachkommastelle nur, wenn als Option gewaehlt.
            if have:
                if not self._bpm_big:
                    self.bpm_label.config(font=self.f_bpm, fg=COL_FG)
                    self._bpm_big = True
                self.bpm_label.config(
                    text=f"{bpm:.1f}" if self.opt_bpm_decimal else f"{bpm:.0f}")
            else:
                if self._bpm_big:
                    self.bpm_label.config(font=self.f_key, fg=COL_MUTED)
                    self._bpm_big = False
                self.bpm_label.config(text="—")
            # Tonart: gedimmt, solange die Erkennung noch unsicher ist
            self.key_label.config(text=key,
                                  fg=COL_ACCENT if key_conf else COL_MUTED)
            par = parallel_key(key)
            self.key_par_label.config(text=f"   {par}" if par else "")
            if self.opt_chords:
                self.chord_label.config(text=chord,
                                        fg=COL_FG if chord != "—" else COL_MUTED)
        self.db_label.config(text=f"{db:4.0f} dB")

        w = self.level_canvas.winfo_width()
        frac = max(0.0, min(1.0, (db + 60.0) / 60.0))
        self.level_canvas.coords(self.level_rect, 0, 0, int(w * frac), 14)

        if self.status_override:
            self.status_label.config(text=self.status_override, fg=COL_MUTED)
        elif not running:
            self.status_label.config(text="", fg=COL_MUTED)
        elif note_mode:
            if db <= -55.0:
                self.status_label.config(text="KEIN SIGNAL", fg=COL_WARN)
            elif self.midi_out is not None:
                self.status_label.config(text="● NOTEN → MIDI", fg=COL_OK)
            else:
                self.status_label.config(text="NOTEN (OHNE MIDI)", fg=COL_MUTED)
        elif self.hold:
            self.status_label.config(
                text="ANGEHALTEN · CLOCK LAEUFT" if self.midi_out is not None
                else "ANALYSE ANGEHALTEN", fg=COL_WARN)
        elif db <= -55.0:
            self.status_label.config(text="KEIN SIGNAL", fg=COL_WARN)
        elif not have:
            self.status_label.config(text="ANALYSIERE …", fg=COL_MUTED)
        elif self.midi_out is not None:
            self.status_label.config(text="● MIDI-CLOCK LAEUFT", fg=COL_OK)
        else:
            self.status_label.config(text="OHNE MIDI", fg=COL_MUTED)

        self.root.after(150, self._tick)

    # ------------------------------------------------------------------
    # Fenster-Verwaltung
    # ------------------------------------------------------------------
    def set_fullscreen(self, on):
        self.fullscreen = on
        self.root.attributes("-fullscreen", on)
        # Im Kiosk-Betrieb den Mauszeiger ausblenden
        self.root.config(cursor="none" if on else "")

    def _on_resize(self, event):
        if event.widget is not self.root:
            return
        h, w = event.height, event.width
        changed = False
        if abs(h - self._last_height) >= 8:
            self._last_height = h
            changed = True
            self.f_bpm.configure(size=-max(60, int(h * 0.28)))
            self.f_key.configure(size=-max(28, int(h * 0.11)))
            self.f_key_par.configure(size=-max(15, int(h * 0.045)))
            self.f_cap.configure(size=-max(12, int(h * 0.028)))
            self.f_small.configure(size=-max(12, int(h * 0.024)))
            self.f_tiny.configure(size=-max(9, int(h * 0.016)))
        if abs(w - self._last_width) >= 8:
            self._last_width = w
            changed = True
        if changed and not self._flow_pending:
            # Optionen-Umbruch erst neu rechnen, wenn Tk die neuen
            # Widget-Breiten (auch nach Schriftaenderung) verrechnet hat.
            self._flow_pending = True
            self.root.after_idle(self._reflow)


def _show_splash(root):
    """Zeigt kurz das Startbild (audiowizard.jpg) randlos und zentriert, waehrend
    das Hauptfenster im Hintergrund aufgebaut wird. Fehlt das Bild oder Pillow,
    passiert nichts (kein Splash, kein Fehler). Rueckgabe: Splash-Fenster oder None."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audiowizard.jpg")
    if not os.path.exists(path):
        return None
    try:
        from PIL import Image, ImageTk
        img = Image.open(path)
    except Exception:
        return None
    try:
        root.withdraw()                       # Hauptfenster verdeckt aufbauen
        splash = tk.Toplevel(root)
        splash.withdraw()                      # erst unsichtbar aufbauen (kein Aufblitzen 0,0)
        splash.overrideredirect(True)          # randlos
        splash.configure(bg=COL_BG)
        target_w = 480                         # feste Breite, Seitenverhaeltnis bleibt
        w, h = img.size
        scale = target_w / float(w)
        img = img.resize((target_w, max(1, int(h * scale))), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        lbl = tk.Label(splash, image=photo, bd=0, bg=COL_BG)
        lbl.image = photo                      # Referenz halten (sonst GC)
        lbl.pack()
        # kleine, aber deutliche Wortmarke unter dem Bild
        tk.Label(splash, text="AudioWizard", font=("Segoe UI", 18, "bold"),
                 fg=COL_ACCENT, bg=COL_BG).pack(pady=(8, 14))
        splash.update_idletasks()
        # ANGEFORDERTE Groesse (gueltig auch vor dem Mappen) -> mittig zentrieren
        ww, hh = splash.winfo_reqwidth(), splash.winfo_reqheight()
        x = max(0, (splash.winfo_screenwidth() - ww) // 2)
        y = max(0, (splash.winfo_screenheight() - hh) // 2)
        splash.geometry(f"{ww}x{hh}+{x}+{y}")
        splash.deiconify()                     # jetzt sichtbar an der richtigen Stelle
        splash.lift()
        splash.update()
        return splash
    except Exception:
        try:
            root.deiconify()
        except Exception:
            pass
        return None


def _close_splash(root, splash):
    try:
        splash.destroy()
    except Exception:
        pass
    try:
        root.deiconify()
        root.lift()
    except Exception:
        pass


def main():
    fullscreen = sys.platform.startswith("linux")
    if "--windowed" in sys.argv:
        fullscreen = False
    if "--fullscreen" in sys.argv:
        fullscreen = True
    force_setup = "--setup" in sys.argv

    try:
        mido.set_backend('mido.backends.rtmidi')
    except Exception:
        pass

    root = tk.Tk()
    splash = _show_splash(root)
    DisplayApp(root, fullscreen, force_setup)
    if splash is not None:
        root.after(1600, lambda: _close_splash(root, splash))
    root.mainloop()


if __name__ == "__main__":
    main()

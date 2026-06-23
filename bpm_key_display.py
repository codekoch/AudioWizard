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
import sys
import threading
import traceback

import numpy as np

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox
except ImportError:
    sys.exit("Tkinter fehlt. Raspberry Pi OS: sudo apt install python3-tk")

try:
    import sounddevice as sd
except ImportError:
    sys.exit("Fehlt: 'sounddevice'. Installiere mit: pip install sounddevice")

import mido

import realtime_bpm_key_midiclock as core

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
        root.title("BPM & Tonart")
        root.configure(bg=COL_BG)
        root.geometry("800x600")
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
        self._material_res = None             # (out|None, err) vom Verarbeitungs-Thread
        self._material_clock = None           # Datei-Pfad: Clock NACH Verarbeitung
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
            try:
                n = core.midi_test(name)
            except Exception as e:
                self.root.after(0, lambda e=e: self.err_label.config(
                    text=f"MIDI-Test fehlgeschlagen: {e}", fg=COL_WARN))
                return
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
                self.file_midi = core.open_midi_output(midi_name)
            except Exception:
                self.file_midi = None
        try:
            self.file_player = core.FilePlayer(self.file_audio, self.file_sr)
            self.file_player.start()
        except Exception as e:
            if self.file_midi is not None:
                try:
                    self.file_midi.close()
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
            try:
                self.file_midi.close()
            except Exception:
                pass
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
                 fg=COL_MUTED).pack(pady=(0, 8))
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
        log = {"win": win, "txt": txt, "q": queue.Queue()}

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

    def _stem_log_error(self, log):
        """Vollen Traceback ins Log-Fenster schreiben (im Fehlerfall)."""
        self._stem_log(log, "")
        self._stem_log(log, "── FEHLER ──")
        self._stem_log(log, traceback.format_exc().rstrip())

    def _rec_actions(self):
        """Aufnahme weiterverarbeiten: fragt, was passieren soll (Stems
        exportieren/abspielen, Song-Sheet) – ohne die Aufnahme erst speichern zu
        muessen. Die Stem-Trennung laeuft danach nur einmal fuer alle Aktionen."""
        if self._rec_audio is None:
            return
        actions = self._ask_actions("Aufnahme", allow_clock=False)
        if not actions:
            return
        self._run_material(("array", self._rec_audio, self._rec_sr),
                           actions, "Aufnahme")

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
        poslbl = tk.Label(ctl, text="0:00 / 0:00", font=self.f_small, bg=COL_BG,
                          fg=COL_MUTED)
        poslbl.pack(side="left")
        playbtn.config(command=lambda: playbtn.config(
            text="⏸" if player.toggle() else "▶"))

        # --- Stems → MIDI (Basic Pitch): mehrere Spuren synchron senden ---
        midi_player = {"obj": None, "port": None}
        if midi_notes:
            cfg = load_config()
            try:
                port = core.open_midi_output(cfg.get("midi_output") or None)
                if port is None:
                    raise RuntimeError("kein MIDI-Ausgang eingestellt")
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
            except Exception as e:
                midi_player["obj"] = None
                tk.Label(win, text=f"MIDI aus: {e}", font=self.f_tiny,
                         bg=COL_BG, fg=COL_MUTED).pack(pady=(0, 6))

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
                try:
                    midi_player["port"].close()
                except Exception:
                    pass
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
            port = core.open_midi_output(cfg.get("midi_output") or None)
            if port is None:
                raise RuntimeError("kein MIDI-Ausgang eingestellt")
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
            try:
                port.close()
            except Exception:
                pass
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
                self.dj_midi = core.open_midi_output(midi_name)
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
            try:
                self.dj_midi.close()
            except Exception:
                pass
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
        qual_map = [("Automatisch", "auto"), ("Hoch – für Export/MIDI", "hi"),
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
        tk.Label(sheetf, text="Tipp: Sprache fest wählen – die Auto-Erkennung liegt "
                 "bei Gesang oft daneben.", font=self.f_tiny, bg=COL_BG,
                 fg=COL_MUTED, justify="left").grid(row=2, column=0, columnspan=2,
                                                    sticky="w", pady=(4, 0))

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
        result = {}

        def _ok():
            if not (v_clock.get() or v_export.get() or v_sheet.get()
                    or v_play.get() or v_stemmidi.get()):
                return                       # nichts gewaehlt -> Dialog offen lassen
            out_dir = None
            if v_export.get():
                out_dir = filedialog.askdirectory(
                    title="Zielordner für die Stems",
                    initialdir=cfg.get("last_save_dir") or "")
                if not out_dir:
                    return                   # Abbruch der Ordnerwahl -> zurueck
            lang = next(v for lbl, v in lang_map if lbl == lvar.get())
            model = next(v for lbl, v in model_map if lbl == mvar.get())
            qual = next(v for lbl, v in qual_map if lbl == qvar.get())
            new_cfg = {**load_config(), "sheet_lang": lang, "sheet_model": model,
                       "stem_quality": qual}
            if out_dir:
                new_cfg["last_save_dir"] = out_dir
            save_config(new_cfg)
            # "Automatisch": hohe Trennqualitaet, wenn die Stems als Audio/MIDI
            # genutzt werden (Export/Abspielen/Stems-MIDI) -- sonst schnell.
            if qual == "hi":
                hi = True
            elif qual == "fast":
                hi = False
            else:
                hi = bool(v_export.get() or v_play.get() or v_stemmidi.get())
            result.update(clock=bool(v_clock.get()) if allow_clock else False,
                          export=bool(v_export.get()), sheet=bool(v_sheet.get()),
                          play=bool(v_play.get()), stemmidi=bool(v_stemmidi.get()),
                          out_dir=out_dir, overlap=0.25 if hi else 0.1,
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
        Reiner Clock-Fall (Datei) geht direkt ohne Trenn-Aufwand in den Datei-Modus."""
        needs_stems = (actions["export"] or actions["sheet"] or actions["play"]
                       or actions.get("stemmidi"))
        if not needs_stems:
            if actions.get("clock") and not isinstance(source, tuple):
                self._begin_file_clock(source)
            return
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
                                ("Song-Sheet", actions["sheet"]),
                                ("Abspielen", actions["play"]),
                                ("Stems-MIDI", actions.get("stemmidi")),
                                ("MIDI-Clock", actions.get("clock"))) if on]
        self._stem_log(log, "Gewählt: " + ", ".join(bits))
        threading.Thread(target=self._material_worker,
                         args=(source, actions, title, log), daemon=True).start()

    def _material_worker(self, source, actions, title, log):
        try:
            cb = lambda m: self._stem_log(log, m)
            out = {"actions": actions, "title": title, "sheet": None,
                   "stems": None, "stem_sr": None, "export_paths": None,
                   "midi_notes": None}
            ov = float(actions.get("overlap", 0.1))
            self._stem_log(log, "== Stems trennen (einmalig) == "
                           + ("[hohe Qualität]" if ov >= 0.2 else "[schnell]"))
            if isinstance(source, tuple):            # ('array', rec, sr)
                _tag, rec, srr = source
                stems, ssr = core.separate_stems_array(rec, srr, log=cb, overlap=ov)
            else:
                stems, ssr = core.separate_stems(source, log=cb, overlap=ov)
            if actions["export"]:
                self._stem_log(log, "== Stems exportieren ==")
                out["export_paths"] = core.write_stems_to_files(
                    stems, ssr, actions["out_dir"], base=title, log=cb)
            if actions["sheet"]:
                self._stem_log(log, "== Song-Sheet erstellen ==")
                out["sheet"] = core.song_sheet_from_stems(
                    stems, ssr, title=title, whisper_size=actions["model"],
                    language=actions["language"], log=cb)
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
            if actions.get("stemmidi"):
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
                self.midi_out = core.open_midi_output(midi_name)
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
            try:
                self.midi_out.close()
            except Exception:
                pass
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
            try:
                port.close()
            except Exception:
                pass
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
                if msgs:
                    self.err_label.config(text="Fertig: " + ", ".join(msgs))
                # MIDI-Clock (nur Datei) erst jetzt starten, nach der Verarbeitung
                if self._material_clock is not None:
                    src = self._material_clock
                    self._material_clock = None
                    self._begin_file_clock(src)
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
    DisplayApp(root, fullscreen, force_setup)
    root.mainloop()


if __name__ == "__main__":
    main()

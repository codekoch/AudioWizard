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
import sys
import threading

import numpy as np

try:
    import tkinter as tk
    import tkinter.font as tkfont
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
        tk.Label(lvl, text="PEGEL", font=self.f_small,
                 bg=COL_BG, fg=COL_MUTED).pack(side="left")
        self.db_label = tk.Label(lvl, text="-60 dB", font=self.f_small,
                                 bg=COL_BG, fg=COL_MUTED, width=7, anchor="e")
        self.db_label.pack(side="right")
        self.level_canvas = tk.Canvas(lvl, height=12, bg=COL_BAR_BG,
                                      highlightthickness=0, bd=0)
        self.level_canvas.pack(side="left", fill="x", expand=True, padx=12)
        self.level_rect = self.level_canvas.create_rectangle(
            0, 0, 0, 14, fill=COL_OK, width=0)

        btns = tk.Frame(f, bg=COL_BG)
        btns.grid(row=9, column=0, sticky="ew", padx=24, pady=(0, 12))
        self.hold_btn = tk.Button(btns, text="Analyse anhalten",
                                  command=self.toggle_hold,
                                  font=self.f_small, bg=COL_SURFACE,
                                  fg=COL_FG, activebackground=COL_SURF_HI,
                                  activeforeground=COL_FG, bd=0, padx=16,
                                  pady=6, highlightthickness=0, takefocus=0,
                                  cursor="hand2")
        self.hold_btn.pack(side="left")
        self.reset_btn = tk.Button(btns, text="Analyse neu starten",
                                   command=self.reset_analysis,
                                   font=self.f_small, bg=COL_SURFACE,
                                   fg=COL_FG, activebackground=COL_SURF_HI,
                                   activeforeground=COL_FG, bd=0, padx=16,
                                   pady=6, highlightthickness=0, takefocus=0,
                                   cursor="hand2")
        self.reset_btn.pack(side="left", padx=(8, 0))
        self._small_button(btns, "Beenden", self.quit_app).pack(side="right")
        self._small_button(btns, "Einstellungen",
                           self.on_settings).pack(side="right", padx=(0, 8))

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
                 bg=COL_BG, fg=COL_FG).pack(pady=(20, 12))

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
        save_config({"input_type": kind, "input_name": name,
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

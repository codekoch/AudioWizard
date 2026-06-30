# -*- coding: utf-8 -*-
"""Erzeugt Synthstrom-Deluge-Songdateien (.XML, Community-Firmware c1.2.x) aus den
von AudioWizard erkannten MIDI-Noten je Stem. Melodische Stems werden zu internen
Synth-Spuren, das Schlagzeug zu einem Kit (Sample-Slots). Jede Spur ist ein
Session-Clip (= ein Pattern).

WICHTIG: Das Format ist eng an ein echtes c1.2.1-Beispiel angelehnt. Das Noten-
Encoding (11 Byte big-endian je Note) und die Tempo-Umrechnung wurden byte-genau
gegen das Beispiel verifiziert. Ob die Firmware die Datei akzeptiert, muss am Geraet
geprueft werden -- die Default-Synth-/Kit-Bloecke stammen 1:1 aus dem Beispiel.

Tick-Aufloesung: 96 Ticks/Viertel, 384 Ticks/Takt (4/4).
Drum-Samples: Standard-808-Kit (Factory-Content auf der SD-Karte noetig).
"""
import struct
import os
import numpy as np
try:
    import soundfile as sf
except Exception:                                # nur fuer das Stem-Bundle noetig
    sf = None

TICKS_PER_QUARTER = 96
TICKS_PER_BAR = TICKS_PER_QUARTER * 4
DELUGE_SR = 44100.0                      # interne Abtastrate (fuer die Tempo-Formel)

# GM-Drum-Note -> (Kit-Slot-Name, Factory-Sample-Pfad, endSamplePos). Die
# Sample-Laengen stammen 1:1 aus dem c1.2.1-Beispiel (Standard-808-Kit), damit das
# Kit der Vorlage exakt entspricht. Reihenfolge = Kit-Slot-Reihenfolge.
DELUGE_DRUM_MAP = {
    36: ("KICK", "SAMPLES/DRUMS/Kick/808 Kick.wav", 22051),
    38: ("SNARE", "SAMPLES/DRUMS/Snare/808 Snare.wav", 22051),
    42: ("HATC", "SAMPLES/DRUMS/HatC/808 Closed hihat.wav", 11025),
    46: ("HATO", "SAMPLES/DRUMS/HatO/808 Open hihat.wav", 33076),
    45: ("TOML", "SAMPLES/DRUMS/TomL/808 Tom low.wav", 44101),
    49: ("CRAS", "SAMPLES/DRUMS/Crash/808 Cymbal.wav", 66151),
}


def _tempo_params(bpm):
    """(timePerTimerTick, timerTickFraction) aus BPM. Verifiziert: 120 BPM ->
    (229, -1342177280) wie im Beispiel."""
    bpm = float(bpm) if bpm and bpm > 0 else 120.0
    spt = DELUGE_SR * 60.0 / (bpm * TICKS_PER_QUARTER)   # Samples je Tick
    ipart = int(spt)
    frac = int(round((spt - ipart) * (2 ** 32)))
    if frac >= 2 ** 31:                                   # als signed int32 ablegen
        frac -= 2 ** 32
    return ipart, frac


def _sec_to_ticks(sec, bpm):
    return int(round(float(sec) * float(bpm) / 60.0 * TICKS_PER_QUARTER))


def _enc_notes(notes, lift=64, prob=20):
    """notes = [(pos_ticks, len_ticks, velocity), ...] -> 0x..-Hexstring.
    Je Note 11 Byte big-endian: pos(4) len(4) vel(1) lift(1) prob(1)."""
    b = bytearray()
    for pos, length, vel in sorted(notes, key=lambda n: n[0]):
        b += struct.pack(">I", max(0, int(pos)))
        b += struct.pack(">I", max(1, int(length)))
        b += bytes([int(vel) & 0x7F, lift & 0xFF, prob & 0xFF])
    return "0x" + b.hex().upper()


# --- Default-Bloecke (1:1 aus dem c1.2.1-Beispiel) -------------------------
_DELAY = ('<delay pingPong="1" analog="0" syncLevel="7" syncType="0" />')
_SIDECHAIN = ('<sidechain attack="327244" release="936" syncLevel="6" '
              'syncType="0" />')
_AUDIOCOMP = ('<audioCompressor attack="83886080" release="83886080" thresh="0" '
              'ratio="1073741824" compHPF="0" compBlend="2147483647" />')
_MODKNOBS = """<modKnobs>
<modKnob controlsParam="pan" />
<modKnob controlsParam="volumePostFX" />
<modKnob controlsParam="lpfResonance" />
<modKnob controlsParam="lpfFrequency" />
<modKnob controlsParam="env1Release" />
<modKnob controlsParam="env1Attack" />
<modKnob controlsParam="delayFeedback" />
<modKnob controlsParam="delayRate" />
<modKnob controlsParam="reverbAmount" />
<modKnob controlsParam="volumePostReverbSend" patchAmountFromSource="compressor" />
<modKnob controlsParam="pitch" patchAmountFromSource="lfo1" />
<modKnob controlsParam="lfo1Rate" />
<modKnob controlsParam="pitch" />
<modKnob controlsParam="stutterRate" />
<modKnob controlsParam="bitcrushAmount" />
<modKnob controlsParam="sampleRateReduction" />
</modKnobs>"""

# Per-Sound-Parameter eines Kit-Drums im Clip (Defaults aus dem Beispiel)
_DRUM_SOUNDPARAMS = """<soundParams arpeggiatorGate="0x00000000" portamento="0x80000000" compressorShape="0xDC28F5B2" oscAVolume="0x7FFFFFFF" oscAPulseWidth="0x00000000" oscAWavetablePosition="0x00000000" oscBVolume="0x80000000" oscBPulseWidth="0x00000000" oscBWavetablePosition="0x00000000" noiseVolume="0x80000000" volume="0x3851EB64" pan="0x00000000" lpfFrequency="0x7FFFFFFF" lpfResonance="0x80000000" hpfFrequency="0x80000000" hpfResonance="0x80000000" lfo1Rate="0x1999997E" lfo2Rate="0x00000000" modulator1Amount="0x80000000" modulator1Feedback="0x80000000" modulator2Amount="0x80000000" modulator2Feedback="0x80000000" carrier1Feedback="0x80000000" carrier2Feedback="0x80000000" modFXRate="0x00000000" modFXDepth="0x00000000" delayRate="0x00000000" delayFeedback="0x80000000" reverbAmount="0x80000000" arpeggiatorRate="0x00000000" stutterRate="0x00000000" sampleRateReduction="0x80000000" bitCrush="0x80000000" modFXOffset="0x00000000" modFXFeedback="0x00000000" compressorThreshold="0x00000000" lpfMorph="0x80000000" hpfMorph="0x80000000" waveFold="0x80000000" ratchetProbability="0x80000000" ratchetAmount="0x80000000" sequenceLength="0x80000000" rhythm="0x80000000"><envelope1 attack="0x80000000" decay="0xE6666654" sustain="0x7FFFFFD2" release="0x80000000" /><envelope2 attack="0xE6666654" decay="0xE6666654" sustain="0xFFFFFFE9" release="0xE6666654" /><patchCables><patchCable source="velocity" destination="volume" amount="0x3FFFFFE8" /><patchCable source="aftertouch" destination="volume" amount="0x2A3D7094" /><patchCable source="y" destination="lpfFrequency" amount="0x19999990" /></patchCables><equalizer bass="0x00000000" treble="0x00000000" bassFrequency="0x00000000" trebleFrequency="0x00000000" /></soundParams>"""

# Synth-Clip-Parameter (Default-Subtraktiv-Synth, Defaults aus dem Beispiel)
_SYNTH_SOUNDPARAMS = """<soundParams arpeggiatorGate="0x00000000" portamento="0x80000000" compressorShape="0xDC28F5B2" oscAVolume="0x7FFFFFFF" oscAPulseWidth="0x00000000" oscAWavetablePosition="0x00000000" oscBVolume="0x80000000" oscBPulseWidth="0x00000000" oscBWavetablePosition="0x00000000" noiseVolume="0x80000000" volume="0x4E000000" pan="0x00000000" lpfFrequency="0x7FFFFFFF" lpfResonance="0x80000000" hpfFrequency="0x80000000" hpfResonance="0x80000000" lfo1Rate="0x1999997E" lfo2Rate="0x00000000" modulator1Amount="0x80000000" modulator1Feedback="0x80000000" modulator2Amount="0x80000000" modulator2Feedback="0x80000000" carrier1Feedback="0x80000000" carrier2Feedback="0x80000000" modFXRate="0x00000000" modFXDepth="0x00000000" delayRate="0x00000000" delayFeedback="0x80000000" reverbAmount="0x80000000" arpeggiatorRate="0x00000000" stutterRate="0x00000000" sampleRateReduction="0x80000000" bitCrush="0x80000000" modFXOffset="0x00000000" modFXFeedback="0x00000000" compressorThreshold="0x00000000" lpfMorph="0x80000000" hpfMorph="0x80000000" waveFold="0x80000000" ratchetProbability="0x80000000" ratchetAmount="0x80000000" sequenceLength="0x80000000" rhythm="0x80000000"><envelope1 attack="0x80000000" decay="0xE6666654" sustain="0x7FFFFFFF" release="0x80000000" /><envelope2 attack="0xE6666654" decay="0xE6666654" sustain="0xFFFFFFE9" release="0xE6666654" /><patchCables><patchCable source="velocity" destination="volume" amount="0x3FFFFFE8" /><patchCable source="aftertouch" destination="volume" amount="0x2A3D7094" /><patchCable source="y" destination="lpfFrequency" amount="0x19999990" /></patchCables><equalizer bass="0x00000000" treble="0x00000000" bassFrequency="0x00000000" trebleFrequency="0x00000000" /></soundParams>"""


def _kit_sound_source(name, sample_path, end_pos):
    """Ein Kit-Slot (<sound> mit Sample-Oszillator) -- Default-Drum aus dem Beispiel."""
    return f"""<sound name="{name}" polyphonic="auto" voicePriority="1" mode="subtractive" modFXType="none" lpfMode="24dB" hpfMode="HPLadder" filterRoute="H2L" path="" maxVoices="8">
<osc1 type="sample" loopMode="1" reversed="0" timeStretchEnable="0" timeStretchAmount="0" fileName="{sample_path}"><zone startSamplePos="0" endSamplePos="{end_pos}" /></osc1>
<osc2 type="sample" loopMode="0" reversed="0" timeStretchEnable="0" timeStretchAmount="0" fileName=""><zone startSamplePos="0" endSamplePos="0" /></osc2>
<lfo1 type="sine" syncLevel="0" syncType="0" /><lfo2 type="sine" syncLevel="0" syncType="0" />
<unison num="1" detune="8" spread="0" />
<arpeggiator mode="off" numOctaves="2" syncLevel="7" syncType="0" arpMode="off" noteMode="up" octaveMode="up" mpeVelocity="off" />
{_MODKNOBS}
{_DELAY}{_SIDECHAIN}{_AUDIOCOMP}
</sound>"""


def _synth_instrument(preset_name):
    """Eine interne Synth-Spur (Default-Subtraktiv: 2 Saw-Oszillatoren). Spur-Farbe =
    Stem-Grundfarbe (z.B. Vocals roetlich)."""
    return f"""<sound presetName="{preset_name}" presetFolder="SYNTHS" defaultVelocity="64" isArmedForRecording="0" activeModFunction="1" colour="{_stem_hue(preset_name)}" polyphonic="poly" voicePriority="1" mode="subtractive" modFXType="none" lpfMode="24dB" hpfMode="HPLadder" filterRoute="H2L" maxVoices="8">
<osc1 type="saw" transpose="0" cents="0" retrigPhase="0" />
<osc2 type="saw" transpose="0" cents="0" retrigPhase="-1" />
<lfo1 type="triangle" syncLevel="0" syncType="0" /><lfo2 type="triangle" syncLevel="0" syncType="0" />
<unison num="1" detune="2" spread="0" />
{_MODKNOBS}
{_DELAY}{_SIDECHAIN}{_AUDIOCOMP}
</sound>"""


_SONG_PARAMS = """<songParams reverbAmount="0x80000000" volume="0x3504F334" pan="0x00000000" sidechainCompressorShape="0xDC28F5B2" modFXDepth="0x00000000" modFXRate="0xE0000000" stutterRate="0x00000000" sampleRateReduction="0x80000000" bitCrush="0x80000000" modFXOffset="0x00000000" modFXFeedback="0x80000000" compressorThreshold="0x00000000" lpfMorph="0x80000000" hpfMorph="0x80000000" tempo="0x00002EE0"><delay rate="0x00000000" feedback="0x80000000" /><lpf frequency="0x7FFFFFFF" resonance="0x80000000" /><hpf frequency="0x80000000" resonance="0x80000000" /><equalizer bass="0x00000000" treble="0x00000000" bassFrequency="0x00000000" trebleFrequency="0x00000000" /></songParams>"""

_KIT_PARAMS = """<kitParams reverbAmount="0x80000000" volume="0x3504F334" pan="0x00000000" sidechainCompressorShape="0xDC28F5B2" modFXDepth="0x00000000" modFXRate="0xE0000000" stutterRate="0x00000000" sampleRateReduction="0x80000000" bitCrush="0x80000000" modFXOffset="0x00000000" modFXFeedback="0x80000000" compressorThreshold="0x00000000" lpfMorph="0x80000000" hpfMorph="0x80000000" tempo="0x00000000"><delay rate="0x00000000" feedback="0x80000000" /><lpf frequency="0x7FFFFFFF" resonance="0x80000000" /><hpf frequency="0x80000000" resonance="0xC0000000" /><equalizer bass="0x00000000" treble="0x00000000" bassFrequency="0x00000000" trebleFrequency="0x00000000" /></kitParams>"""


def _drum_clip_xml(slots, notes_by_slot, length, section, colour=0, label=""):
    """Ein Kit-Clip: je Slot eine noteRow. notes_by_slot: {slot_pitch:[(pos,len,vel)]}.
    colour = kleine Part-VARIATION (Stem-Grundfarbe wird addiert); label -> in den
    Clip-Namen (z.B. 'Drums 1a')."""
    rows = []
    for idx, slot_pitch in enumerate(slots):
        ns = notes_by_slot.get(slot_pitch, [])
        data = f' noteDataWithLift="{_enc_notes(ns)}"' if ns else ""
        rows.append(f'<noteRow colourOffset="{(idx * 17) % 72}" drumIndex="{idx}"'
                    f'{data}>{_DRUM_SOUNDPARAMS}</noteRow>')
    co = (_stem_hue("drums") + int(colour)) % 72
    cn = f"Drums {label}".strip()[:20]
    return (f'<instrumentClip clipName="{cn}" inKeyMode="0" yScroll="0" '
            f'instrumentPresetName="AudioWizard Drums" instrumentPresetFolder="KITS" '
            f'isPlaying="1" isSoloing="0" isArmedForRecording="0" length="{length}" '
            f'colourOffset="{co}" section="{section}">{_KIT_PARAMS}'
            f'<noteRows>{"".join(rows)}</noteRows></instrumentClip>')


def _synth_clip_xml(name, ti, notes_by_pitch, length, section, colour=0, label=""):
    """Ein Synth-Clip: je Tonhoehe eine noteRow. colour = kleine Part-VARIATION (die
    Stem-Grundfarbe von 'name' wird addiert -> Vocals roetlich usw.); label -> in den
    Clip-Namen (z.B. 'Vocals 1a')."""
    rows = [f'<noteRow y="{pitch}" noteDataWithLift="{_enc_notes(notes_by_pitch[pitch])}" />'
            for pitch in sorted(notes_by_pitch)]
    co = (_stem_hue(name) + int(colour)) % 72
    cn = f"{name} {label}".strip()[:20]
    return (f'<instrumentClip clipName="{cn}" inKeyMode="0" yScroll="{40 + ti}" '
            f'instrumentPresetName="{name[:30]}" instrumentPresetFolder="SYNTHS" '
            f'isPlaying="1" isSoloing="0" isArmedForRecording="0" length="{length}" '
            f'colourOffset="{co}" section="{section}">{_SYNTH_SOUNDPARAMS}'
            f'<noteRows>{"".join(rows)}</noteRows></instrumentClip>')


_AUDIO_PARAMS = """<params reverbAmount="0x80000000" volume="0xE0000000" pan="0x00000000" sidechainCompressorShape="0xDC28F5B2" modFXDepth="0x00000000" modFXRate="0xE0000000" stutterRate="0x00000000" sampleRateReduction="0x80000000" bitCrush="0x80000000" modFXOffset="0x00000000" modFXFeedback="0x80000000" compressorThreshold="0x00000000" lpfMorph="0x80000000" hpfMorph="0x80000000" tempo="0x00000000"><delay rate="0x00000000" feedback="0x80000000" /><lpf frequency="0x7FFFFFFF" resonance="0x80000000" /><hpf frequency="0x80000000" resonance="0x80000000" /><equalizer bass="0x00000000" treble="0x00000000" bassFrequency="0x00000000" trebleFrequency="0x00000000" /></params>"""


_AUDIO_DISP = {"bass": "Bass", "other": "Rest", "vocals": "Vocals", "drums": "Drums"}


def _audio_clip_xml(track_name, file_path, end_sample_pos, length, section, colour=0,
                    label=""):
    """Ein <audioClip>: spielt einen Stem-Sample taktgenau ab (Laenge = ganze Takte
    -> Deluge legt ihn aufs Raster, kein Versatz). pitchSpeedIndependent=1 wie im
    Beispiel. colour = kleine Part-VARIATION (Stem-Grundfarbe aus track_name addiert ->
    z.B. Vocals-Clips roetlich). label -> in den Clip-Namen (z.B. 'Drums 1a')."""
    co = (_stem_hue(track_name) + int(colour)) % 72
    stem = track_name[6:] if str(track_name).startswith("AUDIO_") else str(track_name)
    disp = _AUDIO_DISP.get(_STEM_ALIAS.get(stem, stem), stem)
    cn = f"{disp} {label}".strip()[:20]
    return (f'<audioClip clipName="{cn}" trackName="{track_name}" filePath="{file_path}" '
            f'startSamplePos="0" endSamplePos="{int(end_sample_pos)}" '
            f'pitchSpeedIndependent="1" attack="-2147483648" priority="1" '
            f'isPlaying="0" isSoloing="0" isArmedForRecording="0" '
            f'length="{int(length)}" colourOffset="{co}" '
            f'section="{int(section)}">{_AUDIO_PARAMS}</audioClip>')


_STEM_HUE = {"bass": 36, "other": 54, "vocals": 0, "drums": 18}   # Farbrad 0-71 je Stem
_STEM_ALIAS = {"Bass": "bass", "Rest": "other", "Vocals": "vocals", "Drums": "drums"}


def _stem_hue(name):
    """Grundfarbe (colourOffset 0-71) eines Stems/einer Spur -> Stem-Identitaet (z.B.
    Vocals roetlich). Nimmt 'bass'/'other'/'vocals'/'drums', die Synth-Namen
    (Bass/Rest/Vocals/Drums) oder 'AUDIO_<stem>'."""
    s = str(name)
    if s.startswith("AUDIO_"):
        s = s[6:]
    s = _STEM_ALIAS.get(s, s)
    return int(_STEM_HUE.get(s, 0))


def _part_colour(label):
    """Kleine Farb-VARIATION (relativ zur Stem-Grundfarbe) je Part: Typ (fuehrende Zahl
    im Label, z.B. 1 in '1a') + Instanz-Buchstabe geben einen kleinen Versatz -> Strophe
    vs. Refrain innerhalb der Stem-Farbe leicht unterscheidbar, die Stem-Identitaet
    bleibt dominant. Rueckgabe 0..~10."""
    import re
    m = re.match(r"(\d+)([a-z]*)", str(label).strip())
    if not m:
        return 0
    num = int(m.group(1))
    li = (ord(m.group(2)[-1]) - ord("a")) if m.group(2) else 0
    return ((num - 1) * 3 + li) % 12


def _audio_track_xml(name, colour=None):
    """<audioTrack>-Instrument zu einem Stem-audioClip. Die Bindung erfolgt ueber
    'name' == audioClip-trackName; ohne diesen Track meldet die Deluge „File
    corrupted". Kind-Bloecke 1:1 aus dem c1.2.1-Beispiel. colour = Spur-Grundfarbe
    (Default: Stem-Farbe, z.B. Vocals roetlich)."""
    col = _stem_hue(name) if colour is None else int(colour)
    return (f'<audioTrack name="{name}" inputChannel="left" '
            f'outputRecordingIndex="0" isArmedForRecording="0" '
            f'activeModFunction="0" colour="{col}" '
            f'modFXCurrentParam="feedback" currentFilterType="lpf" '
            f'modFXType="none" lpfMode="24dB" hpfMode="HPLadder" '
            f'filterRoute="H2L">{_DELAY}{_SIDECHAIN}{_AUDIOCOMP}</audioTrack>')


def write_deluge_song(path, bpm, synth_tracks=None, drum_track=None,
                      bars_per_clip=0, audio_clips=None, audio_tracks=None,
                      force_song_bars=0, section_ranges=None):
    """Schreibt eine Deluge-Songdatei (.XML).
    synth_tracks: Liste {name, notes=[(start_s,end_s,pitch,vel),...]} -> je eine
                  interne Synth-Spur (ein Clip).
    drum_track:   {notes=[(start_s,end_s,pitch,vel),...]} mit GM-Drum-Pitches
                  (36/38/42 ...) -> ein Kit-Clip.
    bars_per_clip: 0 = ein Clip ueber den ganzen Song; >0 = in N-Takt-Loops
                   zerlegen (mehrere Clips je Spur, Sektionen 0..).
    section_ranges: optional Liste (lo_tick, hi_tick, length_tick, section) -> je
                   Bereich MIDI-Clips (Noten relativ zum Bereich); fuer „Parts"
                   (erkannte Abschnitte variabler Laenge). Hat Vorrang vor bars_per_clip.
    """
    synth_tracks = synth_tracks or []
    tpt, tfrac = _tempo_params(bpm)

    # --- Instrumente ---
    instr = []
    for nm in (audio_tracks or []):          # je Stem-audioClip ein <audioTrack>
        instr.append(_audio_track_xml(nm))
    if drum_track is not None:
        used = sorted({int(p) % 128 for *_x, p, _v in drum_track.get("notes", [])
                       if int(p) in DELUGE_DRUM_MAP},
                      key=lambda p: list(DELUGE_DRUM_MAP).index(p))
        if not used:
            used = [36, 38, 42]
        srcs = "\n".join(_kit_sound_source(*DELUGE_DRUM_MAP[p]) for p in used)
        instr.append(f'<kit presetName="AudioWizard Drums" presetFolder="KITS" '
                     f'defaultVelocity="64" colour="{_stem_hue("drums")}" modFXType="none" '
                     f'lpfMode="24dB" hpfMode="HPLadder" filterRoute="H2L">'
                     f'{_DELAY}{_SIDECHAIN}{_AUDIOCOMP}'
                     f'<soundSources>{srcs}</soundSources>'
                     f'<selectedDrumIndex>0</selectedDrumIndex></kit>')
        drum_track = {**drum_track, "_slots": used}
    for t in synth_tracks:
        instr.append(_synth_instrument(t["name"][:30] or "AW Synth"))

    # --- Noten je Spur einmal in Ticks vorberechnen ---
    def _ticked(notes):
        out = []
        for s, e, p, v in notes:
            pos = _sec_to_ticks(s, bpm)
            out.append((pos, max(1, _sec_to_ticks(e, bpm) - pos), int(p), int(v)))
        return out
    synth_ticked = [(t["name"], _ticked(t.get("notes", []))) for t in synth_tracks]
    drum_ticked = _ticked(drum_track.get("notes", [])) if drum_track is not None else []
    slots = drum_track["_slots"] if drum_track is not None else []
    total = TICKS_PER_BAR
    for _n, tn in synth_ticked:
        total = max([total] + [p + l for p, l, _pp, _v in tn])
    total = max([total] + [p + l for p, l, _pp, _v in drum_ticked])

    def _clips_for_range(lo, hi, length, section, colour=0, label=""):
        """Clips fuer alle Spuren im Tick-Bereich [lo, hi); Positionen relativ zu lo.
        Leere Clips (keine Noten im Bereich) werden weggelassen. Notenlaengen werden auf
        das Clip-Ende begrenzt -> kein Ueberhang ueber die Loop-Grenze (sauber loopbar).
        colour: kleine Part-Variation (Stem-Grundfarbe addiert der Encoder). label: in
        den Clip-Namen (z.B. 'Vocals 1a')."""
        cl = []
        col = 0 if colour is None else int(colour)
        if drum_track is not None:
            by_slot = {}
            for p, l, pitch, v in drum_ticked:
                if lo <= p < hi and pitch in slots:
                    by_slot.setdefault(pitch, []).append(
                        (p - lo, max(1, min(l, hi - p)), v))
            if any(by_slot.values()):
                cl.append(_drum_clip_xml(slots, by_slot, length, section,
                                         colour=col, label=label))
        for ti, (name, tn) in enumerate(synth_ticked):
            by_pitch = {}
            for p, l, pitch, v in tn:
                if lo <= p < hi:
                    by_pitch.setdefault(pitch % 128, []).append(
                        (p - lo, max(1, min(l, hi - p)), v))
            if by_pitch:
                cl.append(_synth_clip_xml(name, ti, by_pitch, length, section,
                                          colour=col, label=label))
        return cl

    if force_song_bars and int(force_song_bars) > 0:   # feste Songlaenge (Bundle)
        total = max(total, int(force_song_bars) * TICKS_PER_BAR)
    clips = []
    if section_ranges:                              # Parts: erkannte Abschnitte
        for rng in section_ranges:
            lo, hi, length, sec = rng[0], rng[1], rng[2], rng[3]
            col = rng[4] if len(rng) > 4 else 0       # kleine Part-Variation
            lbl = rng[5] if len(rng) > 5 else ""      # Label (-> Clip-Name)
            clips += _clips_for_range(int(lo), int(hi), int(length), int(sec),
                                      colour=col, label=lbl)
    elif bars_per_clip and int(bars_per_clip) > 0:  # Takt-Loops: mehrere Clips/Spur
        chunk = int(bars_per_clip) * TICKS_PER_BAR
        for c in range(max(1, -(-total // chunk))):
            clips += _clips_for_range(c * chunk, (c + 1) * chunk, chunk, min(c, 11))
    else:                                           # ein Clip je Spur (ganzer Song)
        clip_len = -(-total // TICKS_PER_BAR) * TICKS_PER_BAR
        clips = _clips_for_range(0, clip_len, clip_len, 0)
    if audio_clips:                                 # Stem-audioClips (Bundle)
        clips = list(audio_clips) + clips

    sections = "".join(f'<section id="{i}" numRepeats="0" />' for i in range(12))
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<song firmwareVersion="c1.2.1" earliestCompatibleFirmware="4.1.0-alpha" arrangementAutoScrollOn="0" xScroll="0" xZoom="24" timePerTimerTick="{tpt}" timerTickFraction="{tfrac}" rootNote="0" inputTickMagnitude="2" swingAmount="0" swingInterval="7" affectEntire="0" activeModFunction="1" modFXType="none" currentFilterType="lpf" lpfMode="24dB" hpfMode="HPLadder" filterRoute="H2L" sessionLayout="0">
<modeNotes><modeNote>0</modeNote><modeNote>2</modeNote><modeNote>4</modeNote><modeNote>5</modeNote><modeNote>7</modeNote><modeNote>9</modeNote><modeNote>11</modeNote></modeNotes>
<reverb roomSize="1288490112" dampening="1546188288" width="2147483647" hpf="0" pan="0" model="1"><compressor attack="327244" release="936" volume="-21474836" shape="-601295438" syncLevel="5" /></reverb>
{_DELAY}{_SIDECHAIN}{_AUDIOCOMP}
{_SONG_PARAMS}
<instruments>{"".join(instr)}</instruments>
<sections>{sections}</sections>
<sessionClips>{"".join(clips)}</sessionClips>
<scales><userScale>0</userScale><disabledPresetScales>0</disabledPresetScales></scales>
</song>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)
    return path


def write_deluge_bundle(xml_path, stems, sr, midi_notes, bpm, t_db,
                        lead_bars=2, instruments=None,
                        sample_subdir="SAMPLES/AudioWizard", log=None):
    """GEMEINSAMES Bundle: richtet Stems UND MIDI mit DEMSELBEN Versatz aus, sodass
    sie auf der Deluge garantiert synchron laufen und der Groove-Downbeat exakt auf
    Takt (lead_bars+1) liegt.
      * Versatz cut so, dass der Downbeat-Sample (t_db) genau bei lead_bars Takten
        landet; Stems werden vorne getrimmt/gepolstert und hinten auf GANZE Takte
        aufgefuellt (-> Deluge laedt sie gridgenau, kein Stretch).
      * MIDI-Noten werden um cut nach vorne gezogen (gleicher Downbeat).
    Schreibt die ausgerichteten Stem-WAVs NEBEN die XML; die audioClips referenzieren
    sie unter 'sample_subdir' (dorthin auf die SD-Karte kopieren). Rueckgabe
    (xml_path, [wav_paths])."""
    if sf is None:
        raise RuntimeError("soundfile nicht verfuegbar (pip install soundfile)")
    instruments = list(instruments) if instruments else list(stems.keys())
    sr = int(sr)
    bpm = float(bpm) if bpm and bpm > 0 else 120.0
    bar_n = int(round(4.0 * 60.0 / bpm * sr))            # Samples je Takt
    lead_n = int(lead_bars) * bar_n
    cut_n = int(round(float(t_db) * sr)) - lead_n        # Downbeat-Sample -> lead_n
    cut_s = cut_n / float(sr)

    def _align(a):
        a = np.asarray(a, dtype=np.float32)
        if a.ndim == 1:
            a = a[:, None]
        if cut_n >= 0:
            a = a[cut_n:]
        else:
            a = np.concatenate([np.zeros((-cut_n,) + a.shape[1:], dtype=np.float32),
                                a], axis=0)
        return a

    sel = [n for n in instruments if n in stems]         # nur vorhandene Stems
    if not sel:
        raise RuntimeError("keine Stems zum Ausrichten.")
    aligned = {n: _align(stems[n]) for n in sel}
    W = max(1, -(-max(len(a) for a in aligned.values()) // bar_n))   # ganze Takte
    total_n = W * bar_n
    for n, a in list(aligned.items()):
        if len(a) < total_n:
            a = np.concatenate([a, np.zeros((total_n - len(a),) + a.shape[1:],
                                            dtype=np.float32)], axis=0)
        aligned[n] = a[:total_n]

    out_dir = os.path.dirname(xml_path) or "."
    base = os.path.splitext(os.path.basename(xml_path))[0]
    wavs, audio_clips, audio_tracks = [], [], []
    for i, n in enumerate(sel):
        wp = os.path.join(out_dir, f"{base}_{n}.wav")
        sf.write(wp, aligned[n], sr, subtype="PCM_16")
        wavs.append(wp)
        fpath = f"{sample_subdir.rstrip('/')}/{base}_{n}.wav"
        tname = f"AUDIO_{n}"
        audio_tracks.append(tname)               # zugehoeriger <audioTrack>
        audio_clips.append(_audio_clip_xml(
            tname, fpath, total_n, W * TICKS_PER_BAR, section=1,
            colour=0))                               # Stem-Grundfarbe
        if log:
            log(f"  Stem ausgerichtet: {os.path.basename(wp)}")

    # MIDI-Spuren mit DEMSELBEN Versatz (cut) -> gleicher Downbeat
    label = {"bass": "Bass", "other": "Rest", "vocals": "Vocals"}
    synth_tracks, drum_track = [], None
    for n in instruments:
        notes = (midi_notes or {}).get(n)
        if not notes:
            continue
        shifted = [(max(0.0, s - cut_s), e - cut_s, p, v) for (s, e, p, v) in notes
                   if (e - cut_s) > 0.0]
        if n == "drums":
            drum_track = {"notes": shifted}
        else:
            synth_tracks.append({"name": label.get(n, n), "notes": shifted})

    write_deluge_song(xml_path, bpm, synth_tracks=synth_tracks,
                      drum_track=drum_track, bars_per_clip=0,
                      audio_clips=audio_clips, audio_tracks=audio_tracks,
                      force_song_bars=W)
    if log:
        log(f"Bundle: {len(wavs)} Stems + MIDI, Downbeat bei Takt {lead_bars + 1}, "
            f"Songlaenge {W} Takte.")
    return xml_path, wavs


def _loop_xfade(a, s0, s1, nxf):
    """Clip a[s0:s1] (auf Laenge s1-s0 gepolstert) mit kurzer Loop-Kreuzblende AM ENDE:
    die letzten nxf Samples blenden zum Audio direkt VOR s0 -> beim Loopen geht
    Clip-Ende nahtlos in den Clip-Anfang ueber (kein Klick), der Downbeat am Anfang
    bleibt unberuehrt. a erwartet (N, ch)."""
    need = s1 - s0
    lo, hi = max(0, s0), max(0, min(len(a), s1))
    seg = a[lo:hi].copy()
    if len(seg) < need:                                   # ueber den Rand -> auffuellen
        pad = np.zeros((need - len(seg),) + seg.shape[1:], dtype=np.float32)
        seg = (np.concatenate([pad, seg], 0) if s0 < 0
               else np.concatenate([seg, pad], 0))
    x = int(min(nxf, max(0, need // 4)))
    if x > 1 and s0 - x >= 0 and s1 <= len(a):
        pre = a[s0 - x:s0]                                # Audio direkt vor dem Start
        if len(pre) == x:
            f = np.linspace(0.0, 1.0, x, dtype=np.float32)
            f = f.reshape((-1,) + (1,) * (seg.ndim - 1))
            seg[need - x:] = seg[need - x:] * (1.0 - f) + pre * f
    return seg


def write_deluge_parts(xml_path, warped_stems, sr, midi_notes, bpm, t_db, sections,
                       instruments=None, sample_subdir="SAMPLES/AudioWizard", log=None):
    """Deluge-Song aus erkannten Song-ABSCHNITTEN: jeder Abschnitt wird eine Deluge-
    SECTION (Launch-Spalte). Pro Abschnitt liegt je Stem ein AUDIO-Clip UND – falls
    Noten vorhanden – ein MIDI-Clip (Synth fuer tonale Stems, Kit fuer Drums), beide
    aus den GEWARPTEN (rastergenauen) Stems/Noten geschnitten und gleich lang. So
    lassen sich die Teile auf der Deluge frei arrangieren/launchen.
    warped_stems/midi_notes erwarten BEREITS gewarpte (rastergenaue) Daten in der
    gewarpten Zeit; t_db = Downbeat in gewarpter Zeit. Rueckgabe (xml_path, [wav_paths])."""
    if sf is None:
        raise RuntimeError("soundfile nicht verfuegbar (pip install soundfile)")
    sr = int(sr)
    bpm = float(bpm) if bpm and bpm > 0 else 120.0
    bar_n = int(round(4.0 * 60.0 / bpm * sr))             # Samples je Takt
    bar_t = 4.0 * 60.0 / bpm                              # Sekunden je Takt
    db_n = int(round(float(t_db) * sr))
    instruments = list(instruments) if instruments else list(warped_stems.keys())
    audio_stems = [n for n in instruments if warped_stems.get(n) is not None]
    out_dir = os.path.dirname(xml_path) or "."
    base = os.path.splitext(os.path.basename(xml_path))[0]

    wavs, audio_clips = [], []
    audio_tracks = [f"AUDIO_{n}" for n in audio_stems]
    nxf = int(0.015 * sr)                                 # ~15 ms Loop-Kreuzblende
    valid = [s for s in sections if int(s["end_bar"]) > int(s["start_bar"])]
    # kleine Part-Variation je Abschnitt (Stem-Grundfarbe kommt im Encoder dazu)
    sec_col = [_part_colour(s.get("label", "")) for s in valid]
    for i, sec in enumerate(valid):
        s, e = int(sec["start_bar"]), int(sec["end_bar"])
        lab = sec.get("label", "X")
        W = e - s                                         # Takte des Abschnitts
        sect = min(i, 11)                                 # Deluge-Section 0..11
        s0, s1 = db_n + s * bar_n, db_n + e * bar_n
        for k, n in enumerate(audio_stems):
            a = np.asarray(warped_stems[n], dtype=np.float32)
            if a.ndim == 1:
                a = a[:, None]
            seg = _loop_xfade(a, s0, s1, nxf)             # bar-genau + nahtlos loopbar
            wp = os.path.join(out_dir, f"{base}_{i + 1:02d}_{lab}_{n}.wav")
            sf.write(wp, seg, sr, subtype="PCM_16")
            wavs.append(wp)
            fpath = f"{sample_subdir.rstrip('/')}/{os.path.basename(wp)}"
            audio_clips.append(_audio_clip_xml(
                f"AUDIO_{n}", fpath, len(seg), W * TICKS_PER_BAR,
                section=sect, colour=sec_col[i], label=lab))  # Farbe + Name "Drums 1a"

    # MIDI-Spuren (gewarpte Noten, absolute gewarpte Zeit) + Abschnitts-Bereiche
    label = {"bass": "Bass", "other": "Rest", "vocals": "Vocals"}
    synth_tracks, drum_track = [], None
    for n in instruments:
        notes = (midi_notes or {}).get(n)
        if not notes:
            continue
        if n == "drums":
            drum_track = {"notes": list(notes)}
        else:
            synth_tracks.append({"name": label.get(n, n), "notes": list(notes)})
    section_ranges = []
    for i, sec in enumerate(valid):
        s, e = int(sec["start_bar"]), int(sec["end_bar"])
        lo = _sec_to_ticks(float(t_db) + s * bar_t, bpm)
        hi = _sec_to_ticks(float(t_db) + e * bar_t, bpm)
        section_ranges.append((lo, hi, (e - s) * TICKS_PER_BAR, min(i, 11),
                               sec_col[i], sec.get("label", "")))   # Farbe + Clip-Name

    write_deluge_song(xml_path, bpm, synth_tracks=synth_tracks, drum_track=drum_track,
                      audio_clips=audio_clips, audio_tracks=audio_tracks,
                      section_ranges=section_ranges)
    if log:
        labs = ", ".join(f"{i + 1:02d}{s.get('label', '')}" for i, s in enumerate(valid))
        log(f"Parts-Song: {len(valid)} Abschnitte ({labs}), {len(wavs)} Audio-Clips + "
            f"MIDI je Abschnitt. Stem-WAVs nach {sample_subdir} kopieren.")
    return xml_path, wavs

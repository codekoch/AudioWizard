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
    """Eine interne Synth-Spur (Default-Subtraktiv: 2 Saw-Oszillatoren)."""
    return f"""<sound presetName="{preset_name}" presetFolder="SYNTHS" defaultVelocity="64" isArmedForRecording="0" activeModFunction="1" colour="0" polyphonic="poly" voicePriority="1" mode="subtractive" modFXType="none" lpfMode="24dB" hpfMode="HPLadder" filterRoute="H2L" maxVoices="8">
<osc1 type="saw" transpose="0" cents="0" retrigPhase="0" />
<osc2 type="saw" transpose="0" cents="0" retrigPhase="-1" />
<lfo1 type="triangle" syncLevel="0" syncType="0" /><lfo2 type="triangle" syncLevel="0" syncType="0" />
<unison num="1" detune="2" spread="0" />
{_MODKNOBS}
{_DELAY}{_SIDECHAIN}{_AUDIOCOMP}
</sound>"""


_SONG_PARAMS = """<songParams reverbAmount="0x80000000" volume="0x3504F334" pan="0x00000000" sidechainCompressorShape="0xDC28F5B2" modFXDepth="0x00000000" modFXRate="0xE0000000" stutterRate="0x00000000" sampleRateReduction="0x80000000" bitCrush="0x80000000" modFXOffset="0x00000000" modFXFeedback="0x80000000" compressorThreshold="0x00000000" lpfMorph="0x80000000" hpfMorph="0x80000000" tempo="0x00002EE0"><delay rate="0x00000000" feedback="0x80000000" /><lpf frequency="0x7FFFFFFF" resonance="0x80000000" /><hpf frequency="0x80000000" resonance="0x80000000" /><equalizer bass="0x00000000" treble="0x00000000" bassFrequency="0x00000000" trebleFrequency="0x00000000" /></songParams>"""

_KIT_PARAMS = """<kitParams reverbAmount="0x80000000" volume="0x3504F334" pan="0x00000000" sidechainCompressorShape="0xDC28F5B2" modFXDepth="0x00000000" modFXRate="0xE0000000" stutterRate="0x00000000" sampleRateReduction="0x80000000" bitCrush="0x80000000" modFXOffset="0x00000000" modFXFeedback="0x80000000" compressorThreshold="0x00000000" lpfMorph="0x80000000" hpfMorph="0x80000000" tempo="0x00000000"><delay rate="0x00000000" feedback="0x80000000" /><lpf frequency="0x7FFFFFFF" resonance="0x80000000" /><hpf frequency="0x80000000" resonance="0xC0000000" /><equalizer bass="0x00000000" treble="0x00000000" bassFrequency="0x00000000" trebleFrequency="0x00000000" /></kitParams>"""


def _drum_clip_xml(slots, notes_by_slot, length, section):
    """Ein Kit-Clip: je Slot eine noteRow. notes_by_slot: {slot_pitch:[(pos,len,vel)]}."""
    rows = []
    for idx, slot_pitch in enumerate(slots):
        ns = notes_by_slot.get(slot_pitch, [])
        data = f' noteDataWithLift="{_enc_notes(ns)}"' if ns else ""
        rows.append(f'<noteRow colourOffset="{(idx * 17) % 72}" drumIndex="{idx}"'
                    f'{data}>{_DRUM_SOUNDPARAMS}</noteRow>')
    return (f'<instrumentClip clipName="Drums" inKeyMode="0" yScroll="0" '
            f'instrumentPresetName="AudioWizard Drums" instrumentPresetFolder="KITS" '
            f'isPlaying="1" isSoloing="0" isArmedForRecording="0" length="{length}" '
            f'colourOffset="70" section="{section}">{_KIT_PARAMS}'
            f'<noteRows>{"".join(rows)}</noteRows></instrumentClip>')


def _synth_clip_xml(name, ti, notes_by_pitch, length, section):
    """Ein Synth-Clip: je Tonhoehe eine noteRow."""
    rows = [f'<noteRow y="{pitch}" noteDataWithLift="{_enc_notes(notes_by_pitch[pitch])}" />'
            for pitch in sorted(notes_by_pitch)]
    return (f'<instrumentClip clipName="{name[:20]}" inKeyMode="0" yScroll="{40 + ti}" '
            f'instrumentPresetName="{name[:30]}" instrumentPresetFolder="SYNTHS" '
            f'isPlaying="1" isSoloing="0" isArmedForRecording="0" length="{length}" '
            f'colourOffset="{(ti * 23) % 72}" section="{section}">{_SYNTH_SOUNDPARAMS}'
            f'<noteRows>{"".join(rows)}</noteRows></instrumentClip>')


def write_deluge_song(path, bpm, synth_tracks=None, drum_track=None,
                      bars_per_clip=0):
    """Schreibt eine Deluge-Songdatei (.XML).
    synth_tracks: Liste {name, notes=[(start_s,end_s,pitch,vel),...]} -> je eine
                  interne Synth-Spur (ein Clip).
    drum_track:   {notes=[(start_s,end_s,pitch,vel),...]} mit GM-Drum-Pitches
                  (36/38/42 ...) -> ein Kit-Clip.
    bars_per_clip: 0 = ein Clip ueber den ganzen Song; >0 = in N-Takt-Loops
                   zerlegen (mehrere Clips je Spur, Sektionen 0..).
    """
    synth_tracks = synth_tracks or []
    tpt, tfrac = _tempo_params(bpm)

    # --- Instrumente ---
    instr = []
    if drum_track is not None:
        used = sorted({int(p) % 128 for *_x, p, _v in drum_track.get("notes", [])
                       if int(p) in DELUGE_DRUM_MAP},
                      key=lambda p: list(DELUGE_DRUM_MAP).index(p))
        if not used:
            used = [36, 38, 42]
        srcs = "\n".join(_kit_sound_source(*DELUGE_DRUM_MAP[p]) for p in used)
        instr.append(f'<kit presetName="AudioWizard Drums" presetFolder="KITS" '
                     f'defaultVelocity="64" colour="0" modFXType="none" '
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

    def _clips_for_range(lo, hi, length, section):
        """Clips fuer alle Spuren im Tick-Bereich [lo, hi); Positionen relativ zu lo.
        Leere Clips (keine Noten im Bereich) werden weggelassen."""
        cl = []
        if drum_track is not None:
            by_slot = {}
            for p, l, pitch, v in drum_ticked:
                if lo <= p < hi and pitch in slots:
                    by_slot.setdefault(pitch, []).append((p - lo, l, v))
            if any(by_slot.values()):
                cl.append(_drum_clip_xml(slots, by_slot, length, section))
        for ti, (name, tn) in enumerate(synth_ticked):
            by_pitch = {}
            for p, l, pitch, v in tn:
                if lo <= p < hi:
                    by_pitch.setdefault(pitch % 128, []).append((p - lo, l, v))
            if by_pitch:
                cl.append(_synth_clip_xml(name, ti, by_pitch, length, section))
        return cl

    clips = []
    if bars_per_clip and int(bars_per_clip) > 0:    # Takt-Loops: mehrere Clips/Spur
        chunk = int(bars_per_clip) * TICKS_PER_BAR
        for c in range(max(1, -(-total // chunk))):
            clips += _clips_for_range(c * chunk, (c + 1) * chunk, chunk, min(c, 11))
    else:                                           # ein Clip je Spur (ganzer Song)
        clip_len = -(-total // TICKS_PER_BAR) * TICKS_PER_BAR
        clips = _clips_for_range(0, clip_len, clip_len, 0)

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

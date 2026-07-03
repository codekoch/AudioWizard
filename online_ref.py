# -*- coding: utf-8 -*-
"""Optionaler ONLINE-ABGLEICH fuer Song-Sheet und Parterkennung (AudioWizard).

Idee (User): Wenn der gesungene Text (Whisper) bekannt ist, laesst sich das Stueck
im Netz identifizieren; unsichere Stellen (Text/Akkorde) werden dann mit den
Referenzen abgeglichen -- WAHRSCHEINLICHKEITS-GEWICHTET, weil auch die
Internet-Quellen falsch sein koennen:
  * TEXT: nur Whisper-Woerter mit kleiner Wort-Wahrscheinlichkeit werden ersetzt,
    und nur dort, wo die Referenz per Sequenz-Alignment eindeutig verankert ist.
  * AKKORDE: nur Segmente mit kleiner Konfidenz-Margin der eigenen Erkennung
    uebernehmen den Referenz-Akkord; die Referenz wird vorher per
    STUFEN-TRANSPOSITION auf unsere erkannte Lage gedreht (Quellen sind oft
    transponiert/Capo, z. B. 99 Luftballons bei UG in D statt E) -- die richtige
    Verschiebung ist die mit maximaler Uebereinstimmung zur eigenen Erkennung,
    und diese Uebereinstimmung IST zugleich das Vertrauensmass der Quelle.
  * Ist nichts (Sicheres) zu finden, gewinnt IMMER die interne Erkennung.

Quellen (frei, ohne API-Key; alle Zugriffe fehlertolerant mit Timeout):
  * lrclib.net       -- Gesangstexte, auch zeilen-synchronisiert (Song-Findung
                        + Text-Korrektur). Suche geht ueber Titel/Artist ->
                        als Suchbegriff dient der HOOK (meist = Titel).
  * cifraclub.com.br -- Akkord-Sheets (Akkorde ueber Textzeilen, wie Ultimate
                        Guitar; UG selbst blockt maschinelle Zugriffe mit 403/
                        Cloudflare und verbietet sie in den ToS -- auch ein
                        Login aendert daran nichts).
  * chordie.com      -- META-Suche ueber mehrere Archive (guitartabs.cc,
                        guitaretab, ...); rendert die Sheets einheitlich mit
                        INLINE-Akkorden an der exakten Silbe (chordline/absc).
  * e-chords.com     -- Akkord-Sheets (data-chord-Spans); nur teilweise
                        server-gerendert -> billiger Zusatzversuch per Slug.
Werden MEHRERE Sheets gefunden, gewinnt das mit der hoechsten Ueberein-
stimmung zur eigenen Erkennung (Wahrscheinlichkeits-Voting ueber Quellen).
Alles laeuft NUR, wenn der Nutzer den Online-Abgleich einschaltet."""

import difflib
import json
import re
import urllib.parse
import urllib.request

UA = "AudioWizard/1.0 (+https://github.com/codekoch/AudioWizard)"
TIMEOUT = 12
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_FLAT = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#",
         "Cb": "B", "Fb": "E", "H": "B"}
# Woraus ein Akkord-Token bestehen darf (fuer die Chord-Zeilen-Heuristik)
_CHORD_RE = re.compile(
    r"^([A-H][#b]?)(m|maj|min|dim|aug|sus|add|M)?[0-9]*"
    r"(?:(?:maj|min|sus|add|dim|aug|b|#|/|[0-9])[0-9A-Ha-h#b]*)?$")


def _emit(log, msg):
    if log:
        try:
            log(msg)
        except Exception:
            pass


def _get(url, as_json=True):
    """GET mit User-Agent + Timeout; None bei JEDEM Fehler (offline-sicher).
    EIN Wiederholungsversuch, weil vereinzelte TLS-Handshake-Timeouts bei
    schnellen Request-Folgen beobachtet wurden (transient)."""
    for attempt in (0, 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = r.read().decode("utf-8", errors="replace")
            if not as_json:
                return data
            s = data.strip()
            if s.startswith("(") and s.endswith(")"):    # JSONP (cifraclub-Suche)
                s = s[1:-1]
            return json.loads(s)
        except Exception:
            if attempt:
                return None
            import time
            time.sleep(1.0)
    return None


def _tokens(text):
    """Kleingeschriebene Wort-Tokens (nur Buchstaben) eines Texts."""
    return re.findall(r"[^\W\d_]+", str(text).lower())


def _word_stream(lines):
    """Whisper-Zeilen -> flacher Wortstrom [(line_idx, word_idx, token)] --
    je Wort EIN Token (Buchstaben, verkettete Teiltokens), Leere ausgelassen."""
    out = []
    for li, ln in enumerate(lines or []):
        for wi, w in enumerate(ln.get("words", [])):
            tk = "".join(_tokens(w.get("word", "")))
            if tk:
                out.append((li, wi, tk))
    return out


def hook_candidates(lines, max_q=3):
    """Suchbegriffe fuer die Song-Findung: die am haeufigsten WIEDERHOLTEN
    Zeilen (der Hook ist fast immer der Titel), dann die laengste Zeile."""
    from collections import Counter
    cnt = Counter()
    first = {}
    for ln in lines or []:
        key = " ".join(_tokens(ln.get("text", "")))
        if len(key.split()) < 2:
            continue
        cnt[key] += 1
        first.setdefault(key, ln.get("text", ""))
    out = []
    for key, n in cnt.most_common():
        if n >= 2:
            out.append(" ".join(key.split()[:8]))
        if len(out) >= max_q - 1:
            break
    longest = max(cnt.keys(), key=lambda k: len(k), default="")
    if longest and " ".join(longest.split()[:8]) not in out:
        out.append(" ".join(longest.split()[:8]))
    return out[:max_q]


def _title_queries(title_hint):
    """Suchbegriffe aus dem DATEINAMEN/Titel: Trenner aufloesen, BPM-/Tonart-
    Muster entfernen ('93BPM_G_Dur_Creep' -> 'creep'; '99Luftballons_97BPM_E_Dur'
    -> '99 luftballons'). Der Dateiname enthaelt sehr oft den echten Songtitel."""
    s = re.sub(r"[_\-.]+", " ", str(title_hint or ""))
    s = re.sub(r"(?<=[a-zäöü])(?=[A-ZÄÖÜ])", " ", s)      # CamelCase aufloesen
    s = re.sub(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", " ", s)
    toks = []
    for t in s.split():
        tl = t.lower()
        if re.fullmatch(r"\d+\s*bpm|bpm|dur|moll|minor|major|[a-h][#b]?|\d{2,3}",
                        tl):
            continue                                      # Tempo-/Tonart-Muell
        toks.append(tl)
    q = " ".join(toks).strip()
    return [q] if len(q) >= 3 else []


def identify_song(lines, dur=None, title_hint="", log=None):
    """Song bei LRCLIB suchen (Suchbegriffe: bereinigter DATEINAME zuerst, dann
    die Hook-Zeilen) und Kandidaten GEWICHTET bewerten: Text-Ueberlappung
    (Whisper vs. Referenz-Lyrics) + Dauer-Naehe. Rueckgabe
    {artist, title, plain, synced, conf} oder None (dann bleibt alles intern)."""
    toks_w = set(t for _, _, t in _word_stream(lines))
    if len(toks_w) < 10:
        return None
    best = None
    for q in _title_queries(title_hint) + hook_candidates(lines):
        hits = _get("https://lrclib.net/api/search?q=" + urllib.parse.quote(q))
        for h in hits or []:
            plain = h.get("plainLyrics") or ""
            if not plain:
                continue
            toks_r = set(_tokens(plain))
            inter = len(toks_w & toks_r)
            ov = inter / (len(toks_w | toks_r) or 1)      # Text-Ueberlappung
            sd = 0.5
            if dur and h.get("duration"):
                sd = max(0.0, 1.0 - abs(float(h["duration"]) - dur) / 20.0)
            conf = 0.75 * ov + 0.25 * sd
            if best is None or conf > best["conf"]:
                best = {"artist": h.get("artistName", ""),
                        "title": h.get("trackName", ""),
                        "plain": plain, "synced": h.get("syncedLyrics") or "",
                        "conf": conf}
        if best and best["conf"] >= 0.5:
            break                                         # klarer Treffer reicht
    if best:
        _emit(log, f"Online erkannt: {best['artist']} – {best['title']} "
                   f"(Vertrauen {best['conf']:.0%}).")
    else:
        _emit(log, "Online-Abgleich: Song nicht sicher identifiziert – "
                   "interne Erkennung bleibt.")
    return best


def correct_lines(lines, ref_text, min_prob=0.6, log=None):
    """Ersetzt UNSICHERE Whisper-Woerter (prob < min_prob) durch die Referenz --
    aber nur dort, wo das Sequenz-Alignment die Referenz eindeutig verankert
    (kurzer Ersetzungs-Block zwischen uebereinstimmenden Passagen). Sichere
    Whisper-Woerter bleiben IMMER (die Quelle kann falsch/andere Version sein).
    Aendert lines in place; Rueckgabe Anzahl ersetzter Woerter."""
    stream = _word_stream(lines)
    wtoks = [t for _, _, t in stream]
    rraw = re.findall(r"\S+", str(ref_text))              # Original-Schreibweise
    rtoks, rmap = [], []
    for i, rw in enumerate(rraw):
        tk = "".join(_tokens(rw))
        if tk:
            rtoks.append(tk)
            rmap.append(i)
    if len(wtoks) < 10 or len(rtoks) < 10:
        return 0
    sm = difflib.SequenceMatcher(None, wtoks, rtoks, autojunk=False)
    fixed = 0
    for tag, i0, i1, j0, j1 in sm.get_opcodes():
        if tag != "replace" or (i1 - i0) > 6 or (j1 - j0) > 6:
            continue                                      # nur kurze, verankerte Bloecke
        n = min(i1 - i0, j1 - j0)
        for k in range(n):
            li, wi, _tk = stream[i0 + k]
            w = lines[li]["words"][wi]
            if float(w.get("prob", 1.0)) >= min_prob:
                continue                                  # Whisper ist sich sicher
            neu = rraw[rmap[j0 + k]].strip()
            if not neu or neu == w.get("word"):
                continue
            w["word"] = neu
            w["prob"] = min_prob                          # jetzt 'geliehen sicher'
            fixed += 1
    if fixed:
        for ln in lines:                                  # Zeilentext neu aufbauen
            ln["text"] = " ".join(w["word"] for w in ln.get("words", []))
    _emit(log, f"Online-Text: {fixed} unsichere Woerter korrigiert.")
    return fixed


def _parse_pre_sheet(html):
    """<pre>-basierte Songseite (cifraclub, e-chords, Archiv-Texte) -> Liste von
    Referenz-Tokens mit Akkord-Ankern: [(token, chord|None)] -- chord haengt am
    ERSTEN Text-Token nach/unter der Akkordposition (Akkordzeile UEBER Textzeile,
    Spalten-genau). Es wird der <pre>-Block mit den meisten Ankern genommen
    (Seiten enthalten oft weitere <pre> mit Tabulaturen/Deko)."""
    best = []
    for m in re.finditer(r"<pre[^>]*>(.*?)</pre>", html, re.S):
        out = _parse_pre_block(m.group(1))
        if sum(1 for _t, c in out if c) > sum(1 for _t, c in best if c):
            best = out
    return best


def _parse_pre_block(txt):
    """Ein <pre>-Inhalt -> [(token, chord|None)] (Spalten-Heuristik s.o.)."""
    txt = re.sub(r"<b[^>]*>(.*?)</b>", r"\1", txt)        # Akkorde stehen in <b>
    txt = re.sub(r"<[^>]+>", "", txt)                     # Rest-Tags: Inhalt behalten
    txt = (txt.replace("&amp;", "&").replace("&lt;", "<")
              .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    lines = txt.splitlines()

    def _is_chord_line(parts):
        if not parts:
            return False
        ok = sum(1 for p in parts if _CHORD_RE.match(p))
        return ok / len(parts) >= 0.6

    out = []
    pend = []                                             # (Spalte, Akkord) der Akkordzeile
    for raw in lines:
        parts = raw.split()
        if _is_chord_line(parts):
            pend = [(mm.start(), mm.group(0))
                    for mm in re.finditer(r"\S+", raw) if _CHORD_RE.match(mm.group(0))]
            continue
        if not parts:
            if pend:                                      # Akkordzeile ohne Text (Intro)
                out.extend((None, c) for _col, c in pend)
            pend = []
            continue
        words = [(mm.start(), mm.group(0)) for mm in re.finditer(r"\S+", raw)]
        anch = {}                                         # Wort-Index -> Akkord
        for col, ch in pend:                              # Akkord -> naechstes Wort
            best_i, best_d = None, 1e9
            for i, (wc, _w) in enumerate(words):
                if i in anch:
                    continue
                # Wort AB der Akkordspalte bevorzugen (Akkord gilt ab da)
                d = abs(wc - col) if wc >= col - 2 else (col - wc) * 3
                if d < best_d:
                    best_i, best_d = i, d
            if best_i is not None:
                anch[best_i] = ch
        for i, (_c, w) in enumerate(words):
            tk = "".join(_tokens(w))
            if tk:
                out.append((tk, anch.get(i)))
        pend = []
    return out


def _nw_word_align(rtoks, wtoks, wtimes):
    """Monotones WORT-Alignment (Needleman-Wunsch, fuzzy) Referenz -> Whisper.
    Monotonie ist entscheidend: difflib klebt bei wiederholten Refrains den
    ersten Referenz-Refrain an den letzten gesungenen. Rueckgabe
    {ref_idx: zeit_s} fuer gematchte Referenz-Tokens."""
    import difflib
    n, m = len(rtoks), len(wtoks)
    if n < 5 or m < 5:
        return {}

    def tsim(a, b):
        if a == b:
            return 1.0
        r = difflib.SequenceMatcher(None, a, b).ratio()
        return r if r >= 0.7 else -0.45

    GAP = -0.3
    import numpy as np
    dp = np.zeros((n + 1, m + 1))
    bt = np.zeros((n + 1, m + 1), dtype=np.int8)
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + GAP
        bt[i][0] = 2
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + GAP
        bt[0][j] = 3
    for i in range(1, n + 1):
        gi = rtoks[i - 1]
        row, prow, btr = dp[i], dp[i - 1], bt[i]
        for j in range(1, m + 1):
            c1 = prow[j - 1] + tsim(gi, wtoks[j - 1])
            c2 = prow[j] + GAP
            c3 = row[j - 1] + GAP
            if c1 >= c2 and c1 >= c3:
                row[j], btr[j] = c1, 1
            elif c2 >= c3:
                row[j], btr[j] = c2, 2
            else:
                row[j], btr[j] = c3, 3
    out = {}
    i, j = n, m
    while i > 0 or j > 0:
        b = bt[i][j]
        if b == 1:
            if tsim(rtoks[i - 1], wtoks[j - 1]) > 0:
                out[i - 1] = float(wtimes[j - 1])
            i, j = i - 1, j - 1
        elif b == 2:
            i -= 1
        else:
            j -= 1
    return out


def stanza_anchors(plain, lines, synced="", log=None):
    """PART-Anker aus der ONLINE-Referenzstruktur: die plainLyrics (LRCLIB)
    sind in STROPHEN-Bloecke gegliedert (Leerzeilen). Wiederholte Bloecke =
    Refrain. Jeder Blockanfang wird per monotonem Wort-Alignment auf die
    Whisper-Zeiten gelegt -> zuverlaessige Grenz- und Typ-Anker fuer die
    Parterkennung (unabhaengig von Whisper-Verhoerern). Hat der plain-Text
    KEINE Leerzeilen (LRCLIB-Qualitaet variiert), werden die Bloecke aus den
    ZEITLUECKEN der synchronisierten Lyrics abgeleitet (Instrumental-Pause
    zwischen den Zeilen = Blockgrenze); die Anker-ZEITEN kommen trotzdem aus
    UNSEREN Whisper-Zeiten (Referenz-Aufnahme kann versetzt sein). Rueckgabe
    [{'t': s, 'gid': gruppe, 'kind': 'R'|'S'}] (R = wiederholt/Refrain-artig),
    zeitlich sortiert; [] wenn nicht verankerbar."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", plain or "") if b.strip()]
    if len(blocks) < 2 and synced:
        rows = []
        for ln in str(synced).splitlines():
            m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", ln)
            if m and m.group(3).strip():
                rows.append((int(m.group(1)) * 60 + float(m.group(2)),
                             m.group(3).strip()))
        if len(rows) >= 4:
            blocks, cur = [], [rows[0][1]]
            for (ta, _xa), (tb, xb) in zip(rows, rows[1:]):
                if tb - ta > 6.0:                        # Luecke = Blockgrenze
                    blocks.append("\n".join(cur))
                    cur = [xb]
                else:
                    cur.append(xb)
            blocks.append("\n".join(cur))
            blocks = [b for b in blocks if b.strip()]
    if len(blocks) < 2:
        return []
    btoks = [_tokens(b) for b in blocks]
    # wiederholte Bloecke gruppieren (Jaccard/Containment wie beim Textvergleich)
    nb = len(blocks)
    parent = list(range(nb))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i in range(nb):
        si = set(btoks[i])
        for j in range(i + 1, nb):
            sj = set(btoks[j])
            if len(si) < 4 or len(sj) < 4:
                continue
            inter = len(si & sj)
            if (inter / (len(si | sj) or 1) >= 0.5
                    or inter / (min(len(si), len(sj)) or 1) >= 0.6):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    grp = [find(i) for i in range(nb)]
    from collections import Counter
    gcnt = Counter(grp)
    # flacher Referenz-Strom + Whisper-Strom
    rtoks, starts = [], []
    for toks in btoks:
        starts.append(len(rtoks))
        rtoks.extend(toks)
    wtoks, wtimes = [], []
    for ln in lines or []:
        for w in ln.get("words", []):
            tk = "".join(_tokens(w.get("word", "")))
            if tk:
                wtoks.append(tk)
                wtimes.append(float(w.get("start", 0.0)))
    g2t = _nw_word_align(rtoks, wtoks, wtimes)
    out = []
    for bi, s0 in enumerate(starts):
        s1 = starts[bi + 1] if bi + 1 < len(starts) else len(rtoks)
        ts = [g2t[k] for k in range(s0, min(s0 + 12, s1)) if k in g2t]
        if len(ts) < 2:                                   # Block nicht verankert
            continue
        out.append({"t": float(min(ts)), "gid": int(grp[bi]),
                    "kind": "R" if gcnt[grp[bi]] >= 2 else "S"})
    out.sort(key=lambda a: a["t"])
    # Vertrauens-Gate: sind zu WENIGE Bloecke verankert (Whisper-Verhoerer,
    # abweichende Version), ist die Struktur-Info irrefuehrend -- ein einzelner
    # Anker wuerde minutenlang "halten" und alles zu einem Typ verschmelzen
    # (an Creep beobachtet: 2/5 Bloecke -> 2 Riesenbloecke). Dann lieber gar
    # nicht eingreifen (interne Erkennung hat Vorrang).
    if len(out) < 3 or len(out) < 0.6 * nb:
        _emit(log, f"Online-Struktur zu duenn verankert ({len(out)}/{nb} "
                   "Bloecke) – interne Parterkennung bleibt.")
        return []
    _emit(log, f"Online-Struktur: {len(out)}/{nb} Referenz-Bloecke verankert "
               f"({sum(1 for a in out if a['kind'] == 'R')} Refrain-artig).")
    return out


def _parse_chordie(html):
    """Chordie-Viewer -> [(token, chord|None)]: Akkorde stehen INLINE an der
    exakten Silbe (<div class="chordline"> ... befo<span class="absc G">G</span>re).
    POSITIONS-basiert geparst, damit ein Akkord MITTEN im Wort das Wort nicht
    zerreisst (das Alignment braucht ganze Woerter)."""
    out = []
    for m in re.finditer(r'<div class="(chordline|textline)">(.*?)</div>',
                         html, re.S):
        kind, body = m.group(1), m.group(2)
        if kind == "textline":
            body = re.sub(r"<[^>]+>", "", body)
            for w in re.findall(r"\S+", body):
                tk = "".join(_tokens(w))
                if tk:
                    out.append((tk, None))
            continue
        # Akkord-Spans -> Marker \x00<akkord>\x01 (Anzeige-Text faellt weg),
        # restliche Tags strippen (Inhalt bleibt, z. B. die [ ]-Klammern).
        body = re.sub(r'<span class="absc ([^"]+)">[^<]*</span>',
                      lambda mm: "\x00" + mm.group(1) + "\x01", body)
        body = re.sub(r"<[^>]+>", "", body)
        clean_parts, chords_at, i = [], [], 0
        pos = 0
        while i < len(body):
            if body[i] == "\x00":
                j = body.find("\x01", i)
                if j < 0:
                    break
                chords_at.append((pos, body[i + 1:j]))
                i = j + 1
            else:
                clean_parts.append(body[i])
                pos += 1
                i += 1
        clean = "".join(clean_parts)
        toks = [(t.start(), t.end(), t.group(0))
                for t in re.finditer(r"\S+", clean)]
        anch = {}
        for p, ch in chords_at:
            ti = next((k for k, (a, b, _w) in enumerate(toks) if p < b), None)
            if ti is None:
                out.append((None, ch))                    # Akkord nach Zeilenende
            elif ti not in anch:
                anch[ti] = ch
        for k, (_a, _b, w) in enumerate(toks):
            tk = "".join(_tokens(w))
            if tk:
                out.append((tk, anch.get(k)))
    return out


def _fetch_cifraclub(artist, title):
    """Cifraclub-Suche + Songseite -> (quelle, sheet) oder None."""
    q = urllib.parse.quote(f"{artist} {title}")
    res = _get(f"https://solr.sscdn.co/cc/h2/?q={q}")
    docs = (((res or {}).get("response") or {}).get("docs")) or []
    want = " ".join(_tokens(artist))
    for d in docs:
        if str(d.get("t")) != "2" or not d.get("d") or not d.get("u"):
            continue
        got = " ".join(_tokens(d.get("a", "")))
        if want and difflib.SequenceMatcher(None, want, got).ratio() < 0.5:
            continue
        html = _get(f"https://www.cifraclub.com.br/{d['d']}/{d['u']}/",
                    as_json=False)
        if not html:
            continue
        sheet = _parse_pre_sheet(html)
        if sum(1 for _t, c in sheet if c) >= 8:
            return (f"cifraclub {d['d']}/{d['u']}", sheet)
    return None


def _fetch_chordie(artist, title, max_hits=2):
    """Chordie-META-Suche (aggregiert mehrere Archive) -> Liste (quelle, sheet)."""
    q = urllib.parse.quote_plus(f"{title} {artist}")
    html = _get(f"https://www.chordie.com/results.php?q={q}", as_json=False)
    if not html:
        return []
    out = []
    seen = set()
    want = set(_tokens(artist) + _tokens(title))
    for m in re.finditer(r'href="(/chord\.pere/[^"]+)"[^>]*>([^<]{3,120})', html):
        url, label = m.group(1), m.group(2)
        if url in seen:
            continue
        seen.add(url)
        lab = set(_tokens(label))
        if len(want & lab) < min(2, len(want)):           # Treffer muss passen
            continue
        page = _get("https://www.chordie.com" + url, as_json=False)
        if not page:
            continue
        sheet = _parse_chordie(page)
        if sum(1 for _t, c in sheet if c) >= 8:
            out.append((f"chordie {url.split('/')[-1][:40]}", sheet))
        if len(out) >= max_hits:
            break
    return out


def _fetch_echords(artist, title):
    """e-chords per Slug-URL (Suche ist JS-gerendert) -> (quelle, sheet)|None.
    Viele Seiten sind nur client-seitig gerendert -> oft leer, kostet wenig."""
    def slug(s):
        s = re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")
        return s
    u = f"https://www.e-chords.com/chords/{slug(artist)}/{slug(title)}"
    html = _get(u, as_json=False)
    if not html:
        return None
    sheet = _parse_pre_sheet(html)
    if sum(1 for _t, c in sheet if c) >= 8:
        return (f"e-chords {slug(artist)}/{slug(title)}", sheet)
    return None


def fetch_chord_sheets(artist, title, log=None):
    """Alle Quellen abklappern -> Liste (quelle, sheet). Die AUSWAHL trifft
    danach best_sheet_fusion (hoechste Uebereinstimmung mit der eigenen
    Erkennung gewinnt -- Wahrscheinlichkeits-Voting ueber Quellen)."""
    sheets = []
    try:
        r = _fetch_cifraclub(artist, title)
        if r:
            sheets.append(r)
    except Exception:
        pass
    try:
        sheets.extend(_fetch_chordie(artist, title))
    except Exception:
        pass
    try:
        r = _fetch_echords(artist, title)
        if r:
            sheets.append(r)
    except Exception:
        pass
    if sheets:
        _emit(log, "Referenz-Sheets: " + ", ".join(
            f"{src} ({sum(1 for _t, c in sh if c)} Anker)" for src, sh in sheets))
    else:
        _emit(log, "Kein brauchbares Referenz-Sheet gefunden – "
                   "Akkorde bleiben intern.")
    return sheets


def fetch_chord_sheet(artist, title, log=None):
    """Rueckwaerts-kompatibel: bestes einzelnes Sheet (erste Quelle) oder []."""
    sheets = fetch_chord_sheets(artist, title, log=log)
    return sheets[0][1] if sheets else []


def _chord_root_suffix(name):
    """'C#m7' -> (Halbton 0-11, 'm'); None bei Nicht-Akkorden. Fuers Sheet zaehlt
    nur Dur/Moll (alles mit 'm'/'min'/'dim' am Anfang des Suffix = Moll-artig)."""
    mm = re.match(r"^([A-H][#b]?)(.*)$", str(name))
    if not mm:
        return None
    root = _FLAT.get(mm.group(1), mm.group(1))
    if root not in NOTE_NAMES:
        return None
    suf = mm.group(2) or ""
    minor = bool(re.match(r"^(m(?!aj)|min|dim)", suf))
    return NOTE_NAMES.index(root), ("m" if minor else "")


def _transpose(name, k):
    rs = _chord_root_suffix(name)
    if rs is None:
        return None
    return NOTE_NAMES[(rs[0] + k) % 12] + rs[1]


def align_ref_chords_to_time(sheet, lines):
    """Referenz-Akkorde ueber das TEXT-Alignment zeitlich verankern: Referenz-
    Token i (mit Akkord) <-> Whisper-Wort -> dessen Startzeit. Akkorde ohne
    Text-Anker (Intro/Instrumental) fallen weg. Rueckgabe [(sekunde, akkord)]."""
    stream = _word_stream(lines)
    wtoks = [t for _, _, t in stream]
    rtoks = [t for t, _c in sheet if t]
    ridx = [i for i, (t, _c) in enumerate(sheet) if t]
    if len(wtoks) < 10 or len(rtoks) < 10:
        return []
    sm = difflib.SequenceMatcher(None, wtoks, rtoks, autojunk=False)
    r2w = {}
    for tag, i0, i1, j0, j1 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i1 - i0):
                r2w[j0 + k] = i0 + k
    timed = []
    for jr, i_sheet in enumerate(ridx):
        ch = sheet[i_sheet][1]
        if not ch:
            continue
        iw = r2w.get(jr)
        if iw is None:
            continue
        li, wi, _t = stream[iw]
        t = float(lines[li]["words"][wi].get("start", 0.0))
        timed.append((t, ch))
    timed.sort()
    return timed


def fuse_chords(seq, timed_ref, min_margin=0.10, min_agree=0.45, log=None,
                apply=True):
    """Gewichtete Fusion: Die Referenz-Anker bilden eine STUFENFUNKTION (der
    Sheet-Akkord gilt ab seinem Anker-Wort bis zum naechsten Anker). Verglichen
    wird in der MITTE jedes eigenen Segments -- die Anker markieren den
    WECHSELPUNKT, das eigene Segment traegt dort noch den Vorgaenger (am
    Creep-Abgleich diagnostiziert). (1) beste Transposition k = argmax
    Uebereinstimmung (= VERTRAUEN in die Quelle); (2) nur wenn Vertrauen >=
    min_agree, uebernehmen Segmente mit kleiner eigener Margin (< min_margin)
    den Referenz-Akkord. Rueckgabe (n_ersetzt, vertrauen, k)."""
    if not seq or len(timed_ref) < 8:
        _emit(log, "Akkord-Fusion uebersprungen (zu wenig Referenz-Anker).")
        return 0, 0.0, 0
    t_first = timed_ref[0][0]
    t_last = timed_ref[-1][0] + 8.0                       # letzter Akkord klingt nach
    # Wie lange darf ein Anker-Akkord "halten"? In grossen Anker-LUECKEN (z. B.
    # Instrumental ohne Text-Anker) laeuft die Akkordfolge weiter -- dort ist die
    # Stufenfunktion FALSCH, also weder werten noch ersetzen.
    gaps = [b - a for (a, _x), (b, _y) in zip(timed_ref, timed_ref[1:]) if b > a]
    med_gap = sorted(gaps)[len(gaps) // 2] if gaps else 4.0
    max_hold = max(6.0, 2.0 * med_gap)

    def _ref_at(t, k):
        last, t_a = None, None
        for ta, ch in timed_ref:
            if ta <= t:
                last, t_a = ch, ta
            else:
                break
        if last is None or (t - t_a) > max_hold:          # Anker zu weit weg
            return None
        return _transpose(last, k)

    # Vergleichspunkte: Segment-Mitten aller eigenen (nicht-stillen) Segmente
    # im von Ankern abgedeckten Bereich.
    mids = [(0.5 * (s["start"] + s["end"]), s["chord"]) for s in seq
            if s["chord"] not in ("", "—")
            and t_first <= 0.5 * (s["start"] + s["end"]) <= t_last]
    if len(mids) < 6:
        _emit(log, "Akkord-Fusion uebersprungen (zu wenig Vergleichspunkte).")
        return 0, 0.0, 0
    best_k, best_a = 0, -1.0
    for k in range(12):
        hit = tot = 0
        for t, own in mids:
            tr = _ref_at(t, k)
            if tr is None:
                continue
            tot += 1
            if own == tr:
                hit += 1
        a = hit / tot if tot else 0.0
        if a > best_a:
            best_k, best_a = k, a
    if not apply:                                         # nur Scoring (Quellen-Voting)
        return 0, best_a, best_k
    if best_a < min_agree:
        _emit(log, f"Referenz-Sheet zu unsicher (beste Uebereinstimmung "
                   f"{best_a:.0%} < {min_agree:.0%}) – interne Akkorde bleiben.")
        return 0, best_a, best_k
    n = 0
    for seg in seq:
        if float(seg.get("margin", 1.0)) >= min_margin or seg["chord"] in ("", "—"):
            continue
        mid = 0.5 * (seg["start"] + seg["end"])
        if not (t_first <= mid <= t_last):
            continue
        ref_ch = _ref_at(mid, best_k)
        if ref_ch and ref_ch != seg["chord"]:
            seg["chord"] = ref_ch
            n += 1
    _emit(log, f"Akkord-Fusion: Quelle passt zu {best_a:.0%} "
               f"(Transposition {best_k:+d} HT); {n} unsichere Stellen "
               "an die Referenz angeglichen.")
    return n, best_a, best_k


def best_sheet_fusion(chords, sheets, lines, log=None):
    """WAHRSCHEINLICHKEITS-VOTING ueber mehrere Referenz-Sheets: jedes Sheet
    wird zeitlich verankert und gegen die eigene Erkennung gescort; das Sheet
    mit der hoechsten Uebereinstimmung GEWINNT und wird (nur bei ausreichendem
    Vertrauen) auf die unsicheren Stellen angewandt. Rueckgabe (n, agree, quelle)."""
    best = None                                           # (agree, timed, quelle)
    for src, sheet in sheets or []:
        timed = align_ref_chords_to_time(sheet, lines)
        if len(timed) < 8:
            continue
        _n, agree, _k = fuse_chords(chords, timed, log=None, apply=False)
        _emit(log, f"  Quelle {src}: {len(timed)} Anker, "
                   f"Uebereinstimmung {agree:.0%}.")
        if best is None or agree > best[0]:
            best = (agree, timed, src)
    if best is None:
        _emit(log, "Akkord-Fusion uebersprungen (keine verankerbare Quelle).")
        return 0, 0.0, ""
    n, agree, _k = fuse_chords(chords, best[1], log=log)
    return n, agree, best[2]

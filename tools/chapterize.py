#!/usr/bin/env python3
"""Add chapter markers to an audiobook whose narrator announces the chapters.

    chapterize.py BOOK.mp3                      detect, review, write "BOOK (chapters).mp3"
    chapterize.py BOOK.mp3 --dry-run            detect and write the chapter list only
    chapterize.py BOOK.mp3 --chapters LIST.json write chapters from an edited list
    chapterize.py BOOK.mp3 --lang fr            skip language detection

How it works: the book is transcribed locally with whisper.cpp (cached, so a
second run is instant). The transcript is searched for spoken headings such as
"Chapter 12", "Chapitre douze" or "Zwölftes Kapitel" at the start of a sentence.
Each mark is moved into the pause just before the announcement, the numbering is
checked for gaps and duplicates, and the result is written as ID3 chapter frames
into a copy of the file. The original is never modified.

Every run also writes "BOOK (chapters).json". Edit titles or times there, delete
or add entries, then run again with --chapters to apply your version.

Needs ffmpeg and whisper-cli (brew install ffmpeg whisper-cpp). The Whisper model
is downloaded on first use to ~/Library/Caches/ribbon-chapterize.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, unicodedata, urllib.request

CACHE = os.path.expanduser('~/Library/Caches/ribbon-chapterize')
MODEL_URL = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{}.bin'

# ---------------------------------------------------------------- languages ---

LANGS = {
    'en': {
        'chapter': {'chapter'}, 'part': {'part'},
        'standalone': {'prologue': 'Prologue', 'epilogue': 'Epilogue', 'interlude': 'Interlude', 'afterword': 'Afterword'},
        'label': {'chapter': 'Chapter', 'part': 'Part'}, 'sep': ': ',
        'opening': 'Opening credits',
    },
    'fr': {
        'chapter': {'chapitre'}, 'part': {'partie'},
        'standalone': {'prologue': 'Prologue', 'épilogue': 'Épilogue', 'epilogue': 'Épilogue', 'interlude': 'Interlude',
                       'postface': 'Postface', 'avant-propos': 'Avant-propos'},
        'label': {'chapter': 'Chapitre', 'part': 'Partie'}, 'sep': ' : ',
        'opening': 'Générique de début',
    },
    'de': {
        'chapter': {'kapitel'}, 'part': {'teil'},
        'standalone': {'prolog': 'Prolog', 'epilog': 'Epilog', 'nachwort': 'Nachwort', 'vorwort': 'Vorwort',
                       'zwischenspiel': 'Zwischenspiel'},
        'label': {'chapter': 'Kapitel', 'part': 'Teil'}, 'sep': ': ',
        'opening': 'Vorspann',
    },
}

EN_NUM = {w: i for i, w in enumerate('zero one two three four five six seven eight nine ten eleven twelve thirteen '
                                      'fourteen fifteen sixteen seventeen eighteen nineteen'.split())}
EN_NUM.update({'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90})
EN_ORD = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5, 'sixth': 6, 'seventh': 7, 'eighth': 8, 'ninth': 9,
          'tenth': 10, 'eleventh': 11, 'twelfth': 12}

FR_NUM = {'zéro': 0, 'un': 1, 'une': 1, 'deux': 2, 'trois': 3, 'quatre': 4, 'cinq': 5, 'six': 6, 'sept': 7, 'huit': 8,
          'neuf': 9, 'dix': 10, 'onze': 11, 'douze': 12, 'treize': 13, 'quatorze': 14, 'quinze': 15, 'seize': 16,
          'vingt': 20, 'vingts': 20, 'trente': 30, 'quarante': 40, 'cinquante': 50, 'soixante': 60, 'cent': 100, 'cents': 100}

DE_NUM = {'null': 0, 'ein': 1, 'eins': 1, 'eine': 1, 'einen': 1, 'zwei': 2, 'drei': 3, 'vier': 4, 'fünf': 5, 'sechs': 6,
          'sieben': 7, 'acht': 8, 'neun': 9, 'zehn': 10, 'elf': 11, 'zwölf': 12, 'dreizehn': 13, 'vierzehn': 14,
          'fünfzehn': 15, 'sechzehn': 16, 'siebzehn': 17, 'achtzehn': 18, 'neunzehn': 19, 'zwanzig': 20, 'dreißig': 30,
          'dreissig': 30, 'vierzig': 40, 'fünfzig': 50, 'sechzig': 60, 'siebzig': 70, 'achtzig': 80, 'neunzig': 90}
DE_ORD = {'erst': 1, 'zweit': 2, 'dritt': 3, 'viert': 4, 'fünft': 5, 'sechst': 6, 'siebt': 7, 'siebent': 7, 'acht': 8,
          'neunt': 9, 'zehnt': 10, 'elft': 11, 'zwölft': 12}

ROMAN = {'i': 1, 'v': 5, 'x': 10, 'l': 50, 'c': 100}


def roman(w):
    if not re.fullmatch(r'[ivxlc]+', w):
        return None
    total, prev = 0, 0
    for ch in reversed(w):
        v = ROMAN[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total if 0 < total < 400 else None


def en_words(ws):
    """Cardinal number from a list of English words, e.g. ['twenty', 'one']."""
    total, cur, seen = 0, 0, False
    for w in ws:
        for p in w.split('-'):
            if p == 'and' and seen:
                continue
            if p == 'hundred' and seen:
                cur = (cur or 1) * 100
            elif p in EN_NUM:
                cur += EN_NUM[p]
            else:
                return None
            seen = True
    return total + cur if seen else None


def fr_words(ws):
    vals = []
    for w in ws:
        for p in w.split('-'):
            if p == 'et' and vals:
                continue
            if p not in FR_NUM:
                return None
            vals.append(FR_NUM[p])
    if not vals:
        return None
    total = 0
    i = 0
    while i < len(vals):
        v = vals[i]
        if v == 4 and i + 1 < len(vals) and vals[i + 1] == 20:  # quatre-vingt(s) = 80
            total += 80; i += 2; continue
        if v == 100:
            total = (total or 1) * 100
        else:
            total += v
        i += 1
    return total


DE_NUM_SS = {k.replace('ß', 'ss'): v for k, v in DE_NUM.items()}


def de_word(w):
    w = w.replace('ß', 'ss')
    if w in DE_NUM_SS:
        return DE_NUM_SS[w]
    if 'hundert' in w:
        left, right = w.split('hundert', 1)
        lv = de_word(left) if left else 1
        rv = de_word(right.removeprefix('und')) if right else 0
        return None if lv is None or rv is None else lv * 100 + rv
    if 'und' in w:
        u, t = w.split('und', 1)
        uv, tv = de_word(u), de_word(t)
        return None if uv is None or tv is None else uv + tv
    return None


def ordinal(w, lang):
    """Value of an ordinal word ('third', 'troisième', 'dritten'), or None."""
    if lang == 'en':
        if w in EN_ORD:
            return EN_ORD[w]
        m = re.fullmatch(r'(\w+?)-?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)', w)
        if m:
            tens = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80,
                    'ninety': 90}.get(m.group(1))
            if tens:
                return tens + EN_ORD[m.group(2)]
        m = re.fullmatch(r'(\w+)ieth', w)
        if m:
            return en_words([m.group(1) + 'y'])
        m = re.fullmatch(r'(\w+)th', w)
        return en_words([m.group(1)]) if m else None
    if lang == 'fr':
        if w in ('premier', 'première', 'premiere'):
            return 1
        if w in ('second', 'seconde'):
            return 2
        m = re.fullmatch(r'(.+?)i[eè]me', w)
        if not m:
            return None
        stem = m.group(1)  # quatr(ième), neuv(ième), cinqu(ième), vingt-et-un(ième)
        for cand in (stem, stem + 'e', stem.replace('v', 'f'), stem[:-1] if stem.endswith('qu') else stem):
            v = fr_words(re.split(r'[- ]', cand))
            if v is not None:
                return v
        return None
    if lang == 'de':
        w = re.sub(r'(e[rsnm]?)$', '', w)  # dritten, zweiter, erstes -> dritt, zweit, erst
        if w in DE_ORD:
            return DE_ORD[w]
        if w.endswith('st'):
            return de_word(w[:-2])
        if w.endswith('t'):
            return de_word(w[:-1])
    return None


def cardinal(words, lang):
    """(value, words consumed) for the longest number at the start of words, or (None, 0)."""
    if not words:
        return None, 0
    w0 = words[0]
    if re.fullmatch(r'\d{1,3}', w0):
        return int(w0), 1
    r = roman(w0)
    if r is not None:
        return r, 1
    if lang == 'de':
        v = de_word(w0)
        return (v, 1) if v is not None else (None, 0)
    fn = en_words if lang == 'en' else fr_words
    best = (None, 0)
    for n in range(1, min(5, len(words)) + 1):
        v = fn(words[:n])
        if v is None:
            break
        best = (v, n)
    if lang == 'en' and best[1] and words[best[1] - 1] == 'and':  # never end on a dangling "and"
        best = (en_words(words[:best[1] - 1]), best[1] - 1)
    return best


# --------------------------------------------------------------- transcript ---

def norm(w):
    w = unicodedata.normalize('NFC', w).lower()
    return w.strip('.,:;!?"\'“”‘’«»()[]…—–-')


def words_from(transcript):
    """Flatten whisper's token list into words with start and end times in seconds."""
    words = []
    for si, seg in enumerate(transcript['transcription']):
        first = True
        for tok in seg.get('tokens', []):
            text = tok['text']
            if text.startswith('[_') or not text.strip():
                continue
            t0, t1 = tok['offsets']['from'] / 1000, tok['offsets']['to'] / 1000
            if text.startswith(' ') or first or not words:
                words.append({'raw': text.strip(), 'start': t0, 'end': t1, 'seg': si, 'seg_first': first})
                first = False
            else:
                words[-1]['raw'] += text
                words[-1]['end'] = t1
    for i, w in enumerate(words):
        w['n'] = norm(w['raw'])
        w['seg_last'] = i + 1 == len(words) or words[i + 1]['seg'] != w['seg']
    return words


def sentence_start(words, i):
    if i == 0 or words[i]['seg_first']:
        return True
    prev = words[i - 1]
    return bool(re.search(r'[.!?:;»”"]$', prev['raw'])) or words[i]['start'] - prev['end'] >= 0.7


def phrase_end(words, i, limit=12):
    """Index after the heading phrase that starts at i: ends at a pause, a segment end, or gives up after limit."""
    for j in range(i, min(len(words), i + limit)):
        if words[j]['seg_last'] or (j + 1 < len(words) and words[j + 1]['start'] - words[j]['end'] >= 0.7):
            return j + 1
    return None


def detect(words, lang, strict=True):
    L = LANGS[lang]
    hits = []
    i = 0
    while i < len(words):
        w = words[i]['n']
        if strict and not sentence_start(words, i):
            i += 1
            continue
        kind = num = None
        after = i + 1
        # "Chapter 12", "Chapitre douze", "Kapitel 3", "Part Two", "Chapitre premier"
        if w in L['chapter'] or w in L['part']:
            kind = 'chapter' if w in L['chapter'] else 'part'
            rest = [x['n'] for x in words[i + 1:i + 6]]
            num, used = cardinal(rest, lang)
            if num is None and rest:
                num, used = ordinal(rest[0], lang), 1
            after = i + 1 + (used if num is not None else 0)
        # "Zwölftes Kapitel", "Première partie", "Part the First" is rare enough to ignore
        elif i + 1 < len(words) and (words[i + 1]['n'] in L['chapter'] or words[i + 1]['n'] in L['part']):
            num = ordinal(w, lang)
            if num is not None:
                kind = 'chapter' if words[i + 1]['n'] in L['chapter'] else 'part'
                after = i + 2
        elif w in L['standalone'] and strict:
            # "Prologue." or "Prologue: The Fire", then a pause. Not "Prologue" inside a sentence.
            if phrase_end(words, i, limit=10):
                hits.append({'i': i, 'kind': 'standalone', 'num': None, 'name': L['standalone'][w], 'after': i + 1})
            i += 1
            continue
        if kind == 'part' and num is not None and not phrase_end(words, i, limit=10):
            kind = None  # "Part two of the plan was simple" is running text, not a heading
        if kind and num is not None:
            hits.append({'i': i, 'kind': kind, 'num': num, 'after': after})
            i = after
            continue
        i += 1
    for h in hits:
        h['title'] = title_after(words, h['after'], lang) if strict else ''

        h['start_word'] = words[h['i']]['start']
        h['heard'] = ' '.join(x['raw'] for x in words[h['i']:(phrase_end(words, h['i'], 16) or h['after'])])
    return hits


def title_after(words, k, lang='en'):
    """The spoken chapter title. Whisper ends a segment where the narrator finishes a sentence, which is a better
    guide than pauses: narrators often pause inside a title ("A Strange ... Proposal")."""
    if k >= len(words):
        return ''
    seg = words[k]['seg']
    same = seg == words[k - 1]['seg']
    end = k
    while end < len(words) and words[end]['seg'] == seg and (end == k or words[end]['start'] - words[end - 1]['end'] < 1.5):
        end += 1
    t = ' '.join(x['raw'] for x in words[k:end]).strip(' .,:;-–—')
    if not t or end - k > (12 if same else 6):
        return ''  # too long for a title: the story has started
    if not same and (words[k]['start'] - words[k - 1]['end'] >= 1.2 or re.search(r'[,"“”«»?!]|\bsaid\b', t)):
        return ''  # the next sentence is dialogue or narration, not a title
    return tidy(t, lang)


SMALL_WORDS = {'en': {'a', 'an', 'the', 'of', 'and', 'or', 'in', 'on', 'at', 'to', 'for', 'by', 'with', 'from'},
               'fr': {'le', 'la', 'les', 'l', 'de', 'du', 'des', 'd', 'et', 'ou', 'à', 'au', 'aux', 'un', 'une', 'en'},
               'de': set()}


def tidy(t, lang):
    """Consistent capitals: title case for English, and no SHOUTED titles in any language."""
    if lang == 'en':
        small = SMALL_WORDS['en']
        out = []
        for i, w in enumerate(t.split()):
            w = w.lower() if w.isupper() and len(w) > 1 else w
            out.append(w.lower() if i and w.lower() in small else w[0].upper() + w[1:])
        return ' '.join(out)
    if t.isupper() and len(t) > 3:
        small = SMALL_WORDS.get(lang, set())
        t = ' '.join(w.lower() if i and w.lower() in small else w.capitalize() for i, w in enumerate(t.split()))
    return t[0].upper() + t[1:]


# ---------------------------------------------------------------- numbering ---

def check_numbering(hits, words, lang):
    """Keep the best hit per chapter number, report gaps, try a relaxed search for missing numbers."""
    warnings, gaps = [], []
    chapters = [h for h in hits if h['kind'] == 'chapter']
    by_num = {}
    for h in chapters:
        by_num.setdefault(h['num'], []).append(h)
    keep = set()
    for n, hs in by_num.items():
        keep.add(id(hs[0]))
        if len(hs) > 1:
            warnings.append(f'Chapter {n} was heard {len(hs)} times '
                            f'({", ".join(ts(h["start_word"]) for h in hs)}); kept the first.')
    hits = [h for h in hits if h['kind'] != 'chapter' or id(h) in keep]
    nums = sorted(by_num)
    if nums:
        expected = set(range(1 if nums[0] <= 2 else nums[0], nums[-1] + 1))
        missing = sorted(expected - set(nums))
        if missing:
            relaxed = [h for h in detect(words, lang, strict=False) if h['kind'] == 'chapter' and h['num'] in missing]
            for n in missing:
                lo = max([h['start_word'] for h in hits if h['kind'] == 'chapter' and h['num'] < n], default=0)
                hi = min([h['start_word'] for h in hits if h['kind'] == 'chapter' and h['num'] > n], default=1e9)
                found = [h for h in relaxed if h['num'] == n and lo < h['start_word'] < hi]
                if found:
                    h = found[0]
                    h['flag'] = 'found mid-sentence, check it'
                    hits.append(h)
                    warnings.append(f'Chapter {n} was only found mid-sentence at {ts(h["start_word"])}: check it.')
                else:
                    gaps.append({'num': n, 'lo': lo, 'hi': hi})
    order = sorted(hits, key=lambda h: h['start_word'])
    last = 0
    for h in order:
        if h['kind'] == 'chapter':
            if h['num'] < last:
                warnings.append(f'Chapter {h["num"]} at {ts(h["start_word"])} comes after chapter {last}.')
            last = max(last, h['num'])
    return order, warnings, gaps


# -------------------------------------------------------------- second look ---

def long_pauses(path, lo, hi, min_len=1.5):
    """Silences of at least min_len seconds between lo and hi: where a missing chapter would start."""
    r = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-ss', f'{lo:.3f}', '-t', f'{hi - lo:.3f}', '-i', path,
                        '-af', f'silencedetect=noise=-38dB:d={min_len}', '-f', 'null', '-'], capture_output=True, text=True)
    starts = [float(x) + lo for x in re.findall(r'silence_start: ([\d.]+)', r.stderr)]
    ends = [float(x) + lo for x in re.findall(r'silence_end: ([\d.]+)', r.stderr)]
    return list(zip(starts, ends))


def listen_closely(path, work, lang, model, clips):
    """Transcribe short clips [(start, length)] with a better model. Results are cached per clip."""
    snips = os.path.join(work, 'snips')
    os.makedirs(snips, exist_ok=True)
    tag = os.path.basename(model).replace('.bin', '')
    names = [os.path.join(snips, f'{tag}-{lang}-{s:.2f}-{d:.1f}.wav') for s, d in clips]
    todo = [(n, c) for n, c in zip(names, clips) if not os.path.isfile(n + '.json')]
    for n, (s, d) in todo:
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{s:.3f}', '-t', f'{d:.3f}', '-i', path,
                        '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', n], check=True)
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        cmd = ['whisper-cli', '-m', ensure_model(model), '-l', lang, '-ojf', '-np']
        for n, _ in batch:
            cmd += ['-f', n]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        print(f'  {min(i + 40, len(todo))} of {len(todo)} clips', flush=True)
    out = []
    for n in names:
        if os.path.isfile(n):
            os.remove(n)
        out.append(words_from(json.load(open(n + '.json'))) if os.path.isfile(n + '.json') else [])
    return out


def simple(t):
    return re.sub(r'[^\w ]', '', t.lower()).strip()


def second_look(path, work, lang, model, hits, gaps):
    """Re-hear each heading to correct its title, and the long pauses inside gaps to find missing chapters."""
    heads = list(hits)
    clips = [(max(0.0, h['start_word'] - 1.5), 16.0) for h in heads]
    cands = []
    for g in gaps:
        hi = g['hi'] if g['hi'] < 1e9 else duration(path)
        pauses = sorted(long_pauses(path, g['lo'] + 5, hi - 5), key=lambda p: p[1] - p[0], reverse=True)[:60]
        cands += [(g, s, e) for s, e in pauses]
    clips += [(max(0.0, e - 1.0), 12.0) for _, _, e in cands]
    if not clips:
        return [], []
    print(f'Listening again to {len(clips)} short clips with the "{os.path.basename(model)}" model…', flush=True)
    heard = listen_closely(path, work, lang, model, clips)
    fixed = []
    for h, words in zip(heads, heard[:len(heads)]):
        found = [x for x in detect(words, lang) if x['start_word'] < 4]
        same = [x for x in found if x['kind'] == h['kind'] and x['num'] == h['num']]
        if same:
            old, new = h['title'], same[0]['title']
            if new and new != old:  # the larger model usually hears names better; never trade a title for nothing
                h['title'] = new
                if old and simple(old) != simple(new):
                    h['also'] = old  # the two listens disagree: let the listener choose
                fixed.append((h, old, new))
    added = []
    for (g, s, e), words in zip(cands, heard[len(heads):]):
        for x in detect(words, lang):
            if x['kind'] == 'chapter' and x['num'] == g['num'] and x['start_word'] < 4 and not g.get('found'):
                x['start_word'] = e - 1.0 + x['start_word']
                x['pause'] = (s, e)
                x['flag'] = 'found on a second listen, check it'
                g['found'] = True
                added.append(x)
    return fixed, added


# ----------------------------------------------------------------- timing -----

def snap(path, t, prev_end):
    """Move a mark into the pause just before the spoken heading. Whisper's word times drift by a few seconds around
    long silences, so take the longest pause near the heading (the break between chapters), not the nearest one,
    and start half a second before the voice returns."""
    t0 = max(0.0, t - 6)
    r = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-ss', f'{t0:.3f}', '-t', '10', '-i', path,
                        '-af', 'silencedetect=noise=-38dB:d=0.3', '-f', 'null', '-'],
                       capture_output=True, text=True)
    starts = [float(x) + t0 for x in re.findall(r'silence_start: ([\d.]+)', r.stderr)]
    ends = [float(x) + t0 for x in re.findall(r'silence_end: ([\d.]+)', r.stderr)]
    near = [(s, e) for s, e in zip(starts, ends) if t - 3 <= e <= t + 3.5]
    best = max(near, key=lambda p: p[1] - p[0]) if near else None
    if best:  # a measured silence is proof the previous sentence has ended; whisper's word ends are not
        return max(best[0], best[1] - 0.5)
    return max(prev_end, t - 0.4)


# ------------------------------------------------------------------ helpers ---

def ts(sec):
    sec = max(0.0, sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f'{int(h)}:{int(m):02d}:{s:06.3f}'


def parse_ts(v):
    if isinstance(v, (int, float)):
        return float(v)
    parts = [float(p) for p in str(v).strip().split(':')]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def need(tool, hint):
    if not shutil.which(tool):
        sys.exit(f'{tool} not found. Install it with: {hint}')


def ensure_model(name):
    if os.path.isfile(name):
        return name
    path = os.path.join(CACHE, f'ggml-{name}.bin')
    if os.path.isfile(path):
        return path
    os.makedirs(CACHE, exist_ok=True)
    print(f'Downloading the Whisper "{name}" model (once)…', flush=True)
    tmp = path + '.part'
    urllib.request.urlretrieve(MODEL_URL.format(name), tmp)
    os.replace(tmp, path)
    return path


def duration(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def work_dir(path):
    st = os.stat(path)
    key = hashlib.sha1(f'{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}'.encode()).hexdigest()[:12]
    work = os.path.join(CACHE, 'work', key)
    os.makedirs(work, exist_ok=True)
    return work


def transcribe(path, lang, model, accurate):
    work = work_dir(path)
    out = os.path.join(work, 'transcript.json')
    meta_path = os.path.join(work, 'meta.json')
    want = {'lang': lang, 'model': model, 'accurate': accurate}
    if os.path.isfile(out) and os.path.isfile(meta_path) and json.load(open(meta_path)) == want:
        print('Using the cached transcript.')
        return json.load(open(out))
    model_path = ensure_model(model)
    wav = os.path.join(work, 'audio.wav')
    if not os.path.isfile(wav):
        print('Preparing audio…', flush=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', path, '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
                        wav + '.tmp.wav'], check=True)
        os.replace(wav + '.tmp.wav', wav)
    print(f'Transcribing {ts(duration(path))[:-4]} of audio locally. This takes a while.', flush=True)
    cmd = ['whisper-cli', '-m', model_path, '-l', lang, '-ojf', '-of', os.path.join(work, 'transcript'), '-pp',
           '-f', wav]
    if not accurate:
        cmd[1:1] = ['-bs', '1', '-bo', '1']
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for line in p.stderr:
        m = re.search(r'progress =\s*(\d+)%', line)
        if m:
            print(f'\r  {m.group(1)}%', end='', flush=True)
    print()
    if p.wait() != 0:
        sys.exit('whisper-cli failed.')
    json.dump(want, open(meta_path, 'w'))
    os.remove(wav)
    return json.load(open(out))


# --------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input')
    ap.add_argument('--lang', default='auto', help='auto (default), en, fr or de')
    ap.add_argument('--model', default='base', help='Whisper model: base (default), small, or a path to a ggml model')
    ap.add_argument('--accurate', action='store_true', help='beam search: slower, slightly better transcripts')
    ap.add_argument('--second-model', default='small',
                    help='model for re-hearing headings and gaps: small (default), base, or a path')
    ap.add_argument('--no-second-look', action='store_true', help='skip re-hearing headings and gaps')
    ap.add_argument('--chapters', help='apply an edited chapter list instead of detecting')
    ap.add_argument('--out', help='output file (default: "<input> (chapters).m4b")')
    ap.add_argument('--format', choices=['m4b', 'mp3'], default='m4b',
                    help='m4b (default): AAC audio, every player jumps to chapters exactly. mp3: keeps the original '
                         'audio, but players may land up to a minute off in long variable-bitrate files.')
    ap.add_argument('--bitrate', default='64k', help='AAC bitrate for m4b output (default 64k)')
    ap.add_argument('--review', action='store_true',
                    help='open a page to check and adjust every chapter mark by ear, then write the file from there')
    ap.add_argument('--port', type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument('--no-browser', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--dry-run', action='store_true', help='write the chapter list but not the audio file')
    a = ap.parse_args()

    need('ffmpeg', 'brew install ffmpeg')
    need('ffprobe', 'brew install ffmpeg')
    src = os.path.abspath(a.input)
    if not os.path.isfile(src):
        sys.exit(f'No such file: {src}')
    stem, ext = os.path.splitext(src)
    fmt = (os.path.splitext(a.out)[1].lstrip('.').lower() if a.out else a.format).replace('m4a', 'm4b')
    if fmt not in ('m4b', 'mp3'):
        sys.exit('The output must be .m4b or .mp3.')
    out = a.out or f'{stem} (chapters).{fmt}'
    list_path = (os.path.splitext(out)[0] if a.out else f'{stem} (chapters)') + '.json'
    total = duration(src)

    if a.review and not a.chapters and os.path.isfile(list_path):
        a.chapters = list_path  # review an existing list rather than detecting again
    if a.chapters:
        data = json.load(open(a.chapters))
        chapters = [{**c, 'start': parse_ts(c['start'])} for c in data['chapters']]
        chapters.sort(key=lambda c: c['start'])
        lang, warnings = data.get('language', a.lang), data.get('warnings', [])
    else:
        need('whisper-cli', 'brew install whisper-cpp')
        if a.lang not in ('auto', *LANGS):
            sys.exit(f'Unsupported language "{a.lang}". Use auto, {", ".join(LANGS)}.')
        tr = transcribe(src, a.lang, a.model, a.accurate)
        lang = a.lang if a.lang != 'auto' else tr['result']['language']
        if lang not in LANGS:
            sys.exit(f'Whisper heard "{lang}", which has no chapter words yet. Use --lang en, fr or de to override.')
        print(f'Language: {lang}{" (detected)" if a.lang == "auto" else ""}')
        words = words_from(tr)
        hits, warnings, gaps = check_numbering(detect(words, lang), words, lang)
        if not hits:
            sys.exit('No spoken chapter headings were found. Is the language right? Try --lang.')
        fixed = []
        if not a.no_second_look:
            fixed, added = second_look(src, work_dir(src), lang, a.second_model, hits, gaps)
            hits = sorted(hits + added, key=lambda h: h['start_word'])
            for h in added:
                warnings.append(f'Chapter {h["num"]} was missed at first and found on a second listen '
                                f'at {ts(h["start_word"])}: check it.')
        for g in gaps:
            if not g.get('found'):
                warnings.append(f'Chapter {g["num"]} was not found between {ts(g["lo"])} and '
                                f'{ts(g["hi"]) if g["hi"] < 1e9 else "the end"}. Add it to the chapter list by hand.')
        L = LANGS[lang]
        chapters, prev_end = [], 0.0
        print(f'Placing {len(hits)} marks in the pauses before each heading…', flush=True)
        for h in hits:
            if h['kind'] == 'standalone':
                title = h['name'] + (L['sep'] + h['title'] if h['title'] else '')
            else:
                title = f"{L['label'][h['kind']]} {h['num']}" + (L['sep'] + h['title'] if h['title'] else '')
            if 'pause' in h:  # found on a second listen: we know the pause it follows
                start = max(h['pause'][0], h['pause'][1] - 0.5)
            else:
                prev_end = words[h['i'] - 1]['end'] if h['i'] else 0.0
                start = snap(src, h['start_word'], prev_end) if h['start_word'] > 1 else 0.0
            c = {'start': start, 'title': title, 'heard': h['heard']}
            if h.get('flag'):
                c['check'] = h['flag']
            if h.get('also'):
                c['also_heard'] = h['also']
            chapters.append(c)
        if chapters[0]['start'] > 5:
            chapters.insert(0, {'start': 0.0, 'title': L['opening']})
        chapters[0]['start'] = 0.0

    # write the editable list, keeping a backup of one that may hold your edits
    if not a.chapters and os.path.isfile(list_path):
        shutil.copyfile(list_path, list_path[:-5] + ' (backup).json')
        print(f'Kept the previous chapter list as "{os.path.basename(list_path)[:-5]} (backup).json".')
    save_list(list_path, src, lang, total, warnings, chapters)

    print()
    for n, c in enumerate(chapters, 1):
        flag = '  <- ' + c['check'] if c.get('check') else ''
        if c.get('also_heard'):
            flag += f'  <- also heard as "{c["also_heard"]}"'
        print(f'{n:>3}  {ts(c["start"])[:-4]}  {c["title"]}{flag}')
    print()
    for w in warnings:
        print('Warning:', w)
    print(f'Chapter list: {list_path}')
    if a.review:
        import review
        review.serve(src=src, list_path=list_path, out=out, fmt=fmt, bitrate=a.bitrate, total=total,
                     work=work_dir(src), port=a.port, open_browser=not a.no_browser)
        return
    if a.dry_run:
        return
    n = write_output(src, chapters, out, fmt, a.bitrate, total, work_dir(src))
    print(f'Wrote {n} chapters to {out}')


def save_list(list_path, src, lang, total, warnings, chapters):
    data = {'source': src, 'language': lang, 'duration': ts(total),
            'note': 'Edit titles or start times, delete or add entries, then run again with --chapters this file, '
                    'or use --review.',
            'warnings': warnings,
            'chapters': [{**c, 'start': ts(c['start'])} for c in sorted(chapters, key=lambda c: c['start'])]}
    tmp = list_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, list_path)


def aac_encoder():
    r = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], capture_output=True, text=True)
    return 'aac_at' if ' aac_at ' in r.stdout else 'aac'  # Apple's encoder on macOS, ffmpeg's own elsewhere


def ensure_aac(src, work, bitrate, progress=None):
    """Encode the book to AAC once and keep it, so writing the m4b again after edits takes seconds."""
    path = os.path.join(work, f'audio-{bitrate}.m4a')
    if os.path.isfile(path):
        return path
    total = duration(src)
    tmp = path + '.part.m4a'
    p = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-nostats', '-progress', 'pipe:1', '-i', src, '-map', '0:a',
                          '-c:a', aac_encoder(), '-b:a', bitrate, tmp], stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        if line.startswith('out_time_us=') and progress and line.strip()[12:].isdigit():
            progress(min(1.0, int(line.strip()[12:]) / 1e6 / total))
    if p.wait() != 0:
        raise RuntimeError('ffmpeg could not encode the audio')
    os.replace(tmp, path)
    return path


def write_output(src, chapters, out, fmt, bitrate, total, work, progress=None):
    """Write a copy of the book with chapter markers. Returns the number of chapters found in the result."""
    chapters = sorted(chapters, key=lambda c: c['start'])
    ext = os.path.splitext(out)[1]
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, encoding='utf-8') as f:
        f.write(';FFMETADATA1\n')
        for k, c in enumerate(chapters):
            end = chapters[k + 1]['start'] if k + 1 < len(chapters) else total
            t = c['title'].replace('\\', '\\\\').replace('=', '\\=').replace(';', '\\;').replace('#', '\\#').replace('\n', ' ')
            f.write(f'[CHAPTER]\nTIMEBASE=1/1000\nSTART={round(c["start"] * 1000)}\nEND={round(end * 1000)}\ntitle={t}\n')
        meta = f.name
    tmp_out = out + '.part' + ext
    try:
        if fmt == 'm4b':
            if progress is None:
                print('Encoding the audio to AAC (once per book)…', flush=True)
            audio = ensure_aac(src, work, bitrate, progress)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', audio, '-i', meta, '-i', src,
                            '-map', '0:a', '-map', '2:v?', '-map_metadata', '2', '-map_chapters', '1',
                            '-c', 'copy', '-disposition:v', 'attached_pic', '-movflags', '+faststart',
                            '-f', 'ipod', tmp_out], check=True)
        else:
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-i', meta, '-map', '0', '-map_metadata', '0',
                            '-map_chapters', '1', '-c', 'copy', '-id3v2_version', '3', tmp_out], check=True)
        os.replace(tmp_out, out)
    finally:
        os.remove(meta)
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_chapters', '-of', 'json', out], capture_output=True, text=True)
    n = len(json.loads(r.stdout or '{}').get('chapters', []))
    if n != len(chapters):
        raise RuntimeError(f'Expected {len(chapters)} chapters in {out} but found {n}.')
    return n


if __name__ == '__main__':
    main()

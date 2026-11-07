"""Surface lexical caption/audio disagreement independently of semantic judgment."""
from difflib import SequenceMatcher
import re


def tokens(text):
    return re.findall(r'[+-]?\d+(?:[.,]\d+)*%?|[^\W\d_]+(?:\u0027[^\W\d_]+)?|[$€£]',
                      text.casefold().replace('\u2019', "'"), flags=re.UNICODE)


def disagreements(captions, transcript):
    rows = []
    for line in captions.splitlines():
        match = re.fullmatch(r'\[([\d.]+)–([\d.]+)\] (.*)', line)
        if match:
            rows.extend((t,float(match[1]),float(match[2])) for t in tokens(match[3]))
    audio = [(t,s.segment_id,w.start,w.end) for s in transcript.segments for w in s.words for t in tokens(w.word)]
    differences = []
    for op,a,b,c,d in SequenceMatcher(None,[r[0] for r in rows],[w[0] for w in audio],autojunk=False).get_opcodes():
        if op == 'equal':
            continue
        caption_span=rows[a:b]; audio_span=audio[c:d]
        differences.append({'caption':' '.join(r[0] for r in caption_span),
            'final_asr':' '.join(w[0] for w in audio_span),
            'caption_start':caption_span[0][1] if caption_span else None,
            'caption_end':caption_span[-1][2] if caption_span else None,
            'final_segment_ids':list(dict.fromkeys(w[1] for w in audio_span))})
    return differences

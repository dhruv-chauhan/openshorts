"""Measured sentence-case captions in a safe area below full-width footage."""
from pathlib import Path
import re

from .media_common import MediaError

FONT_NAME = 'Montserrat ExtraBold'
FONT_PATH = Path(__file__).resolve().parent.parent / 'fonts' / 'Montserrat-ExtraBold.ttf'


def layout(style, source_width, source_height, width=1080, height=1920):
    from PIL import ImageFont
    font = ImageFont.truetype(str(FONT_PATH), style.font_size)
    content_height = min(height, width * source_height / source_width)
    content_bottom = (height + content_height) / 2
    top = content_bottom + style.content_gap
    bottom = height - style.bottom_safe_margin
    line_height = sum(font.getmetrics()) + 8
    if bottom - top < line_height * style.max_lines + 24:
        raise MediaError('caption_safe_area_unavailable')
    return font, {'x': width / 2, 'y': (top + bottom) / 2,
                  'width': width - 2 * style.horizontal_margin - 24,
                  'top': top, 'bottom': bottom, 'line_height': line_height,
                  'content_bottom': content_bottom}


def wrap(words, font, width, max_lines):
    lines = []
    for word in words:
        # ASR may coalesce several instantaneous tokens into one timed unit.
        # Wrap its visible words without inventing individual timestamps.
        for text in word['word'].strip().split():
            if font.getlength(text) > width:
                return None
            if lines and font.getlength(lines[-1] + ' ' + text) <= width:
                lines[-1] += ' ' + text
            else:
                lines.append(text)
            if len(lines) > max_lines:
                return None
    return lines


def phrases(words, font, box, style):
    result, current = [], []
    def flush():
        if current:
            result.append({'start': current[0]['start'], 'end': current[-1]['end'],
                           'lines': wrap(current, font, box['width'], style.max_lines)})
            current.clear()
    for word in words:
        trial = current + [word]
        if current and (word['start'] - current[-1]['end'] > style.max_gap or
                        word['end'] - current[0]['start'] > style.max_duration or
                        wrap(trial, font, box['width'], style.max_lines) is None):
            flush()
        if wrap([word], font, box['width'], style.max_lines) is None:
            raise MediaError('caption_word_exceeds_safe_width')
        current.append(word)
        if re.search(r'[.!?]["\u201d\u2019]*$', word['word'].strip()):
            flush()
    flush()
    return result


def timestamp(value, srt=False):
    scale = 1000 if srt else 100
    ticks = round(value * scale)
    hours, ticks = divmod(ticks, 3600 * scale)
    minutes, ticks = divmod(ticks, 60 * scale)
    seconds, fraction = divmod(ticks, scale)
    return (f'{hours:02}:{minutes:02}:{seconds:02},{fraction:03}' if srt else
            f'{hours}:{minutes:02}:{seconds:02}.{fraction:02}')


def generate(transcript, style, source_width, source_height, ass, srt):
    from subtitles import _escape_ass_text
    font, box = layout(style, source_width, source_height)
    words = [w for s in transcript['segments'] for w in s['words']]
    rows = phrases(words, font, box, style)
    if not rows:
        raise MediaError('no_caption_words_in_span')
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},{style.font_size},&H00FFFFFF,&H00FFFFFF,&H80000000,&H80000000,0,0,0,0,100,100,0,0,3,10,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events, srt_rows = [], []
    for i, row in enumerate(rows, 1):
        if round(row['end'] * 100) <= round(row['start'] * 100):
            raise MediaError('caption_event_too_short')
        visible = r'\N'.join(_escape_ass_text(t) for t in row['lines'])
        position = r'{\an5\pos(' + f'{box["x"]:.1f},{box["y"]:.1f}' + ')}'
        events.append(f'Dialogue: 0,{timestamp(row["start"])},{timestamp(row["end"])},Default,,0,0,0,,{position}{visible}\n')
        srt_rows.append(f'{i}\n{timestamp(row["start"],True)} --> {timestamp(row["end"],True)}\n' + '\n'.join(row['lines']) + '\n\n')
    ass.write_text(header + ''.join(events), encoding='utf-8')
    srt.write_text(''.join(srt_rows), encoding='utf-8')
    return {'style': style.model_dump(), 'font': FONT_NAME, 'box': box,
            'phrase_count': len(rows), 'max_lines': max(len(r['lines']) for r in rows)}

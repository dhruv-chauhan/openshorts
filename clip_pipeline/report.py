"""Escaped, offline review preview; navigation never approves content."""
from html import escape
from urllib.parse import quote


def story_preview(plan, sequences):
    sections = []
    for sequence in sequences:
        links = ' · '.join(f'<a href="#{escape(s.story.story_id)}">{i + 1}. {escape(s.story.title)}</a>'
                           for i, s in enumerate(sequence.stories))
        cards = []
        previous_end = None
        for i, artifact in enumerate(sequence.stories):
            story = artifact.story
            gap = (f'<p>Omitted source interval before this story: {previous_end:.2f}–{story.span.start:.2f}s. '
                   'Reason: manually excluded; semantic review pending.</p>'
                   if previous_end is not None and story.span.start > previous_end else '')
            previous_end = story.span.end
            navigation = []
            for j, label in ((i - 1, 'Previous story'), (i + 1, 'Next story')):
                if 0 <= j < len(sequence.stories):
                    navigation.append(f'<a href="#{escape(sequence.stories[j].story.story_id)}">{label}</a>')
            metadata = ' · '.join(escape(str(v)) for v in [story.subject or 'Subject unknown',
                story.speaker or 'Speaker unknown', story.timeframe or 'Timeframe unknown', story.stance])
            cards.append(f'''<article id="{escape(story.story_id)}">
<h3>{i + 1}. {escape(story.title)}</h3><p>{metadata}</p>
<p>Source {story.span.start:.2f}–{story.span.end:.2f}s · {story.playback_rate:g}× · {artifact.final.duration_seconds:.2f}s output · {escape(story.role)}</p>
{gap}<video controls preload="metadata" src="{escape(quote(artifact.final.media.path, safe='/'))}"></video>
<p>SHA-256: <code>{artifact.final.media.sha256}</code></p><nav>{' · '.join(navigation)}</nav></article>''')
        sections.append(f'<section><h2>{escape(sequence.sequence.title)}</h2><nav>{links}</nav>{"".join(cards)}</section>')
    body = ''.join(sections) or '<p>The manual plan contains no sequences. No relevance decision was made.</p>'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Unaudited story preview</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; media-src 'self' file:; style-src 'unsafe-inline'">
<style>body{{font:16px system-ui;background:#15171b;color:#eee;max-width:850px;margin:24px auto;padding:0 16px}}a{{color:#9fcbff}}video{{display:block;width:min(100%,360px);max-height:75vh;margin:16px 0}}article{{border-top:1px solid #555;margin:24px 0;padding:16px 0}}code{{overflow-wrap:anywhere}}nav{{line-height:1.8}}.warning{{background:#423719;padding:16px}}</style>
</head><body><h1>Story sequence preview</h1><p class="warning">UNAUDITED · NOT APPROVED. Supplied story plan; no final-media audit or approval. Review every story and the sequence, including omitted context, caption placement and pacing.</p>
<p>Selection method: {escape(plan.selection_method)}. Source: {escape(plan.source.locator)}</p>{body}</body></html>'''


def selection_preview(plan, report, transcript):
    """Text-first editorial review, including withheld proposals and source gaps."""
    accepted = {s.sequence_id for s in plan.sequences}
    sections = []
    for region in report['proposals']:
        for sequence in region['sequences']:
            cards = []
            for story in sequence['stories']:
                span = story['span']
                words = [w.word for segment in transcript.segments for w in segment.words
                         if w.start >= span['start'] and w.end <= span['end']]
                cards.append(f"<article><h3>{escape(story['title'])}</h3><p>{span['start']:.2f}–{span['end']:.2f}s · "
                             f"{escape(story['stance'])} · {escape(story['topic_path'])}</p><p>{escape(' '.join(words))}</p>"
                             f"<p>Market connection: {escape(story['market_connection'])}</p></article>")
            gaps = ''.join(f"<li>{o['start']:.2f}–{o['end']:.2f}s: {escape(o['reason'])}</li>" for o in sequence['omissions'])
            state = 'Selected for unaudited rendering' if sequence['sequence_id'] in accepted else 'Withheld — inspect selection report'
            sections.append(f"<section><h2>{escape(sequence['title'])}</h2><p>{state}</p>{''.join(cards)}<ul>{gaps}</ul></section>")
    reasons = ''.join(f'<li>{escape(r)}</li>' for r in report['review_reasons'])
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Selection review</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<style>body{{font:17px system-ui;max-width:900px;margin:30px auto;padding:20px;line-height:1.6}}section{{border-top:1px solid #999}}article{{padding:10px 20px;background:#f2f4f6}}</style></head><body>
<h1>Pre-render story selection</h1><p>UNAUDITED · NOT APPROVED. Outcome: {escape(report['outcome'])}.
All {len(report['windows'])} transcript windows evaluated. This preview is not a final-audio or visual audit.</p>
<ul>{reasons}</ul>{''.join(sections) or '<p>No story proposals.</p>'}</body></html>'''

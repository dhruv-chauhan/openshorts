"""Escaped, offline review preview; navigation never approves content."""
from html import escape
from urllib.parse import quote


def enrichment_preview(metadata, knowledge=None):
    """Diagnostic associations are labeled separately from publishable metadata."""
    if not metadata:
        return ''
    if hasattr(metadata, 'model_dump'):
        metadata = metadata.model_dump()
    from .audit import grouped_associations
    from .enrichment_contracts import StoryEnrichment
    metadata = dict(metadata)
    metadata['associations'] = grouped_associations(StoryEnrichment.model_validate(metadata))
    import json
    sources = (knowledge or {}).get('sources', [])
    citations = ''.join(f'<li><a href="{escape(s["url"], quote=True)}">{escape(s["source_id"])}</a>: '
                        f'{escape(s["summary"])}</li>' for s in sources)
    return ('<details><summary>Editorial associations — not speaker claims</summary>'
            '<p>Only supported associations are eligible for display. Unsupported/review links below '
            'are diagnostics; final-media approval is still required.</p><pre style="white-space:pre-wrap">'
            + escape(json.dumps(metadata, indent=2)) + '</pre><ul>' + citations + '</ul></details>')


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
{enrichment_preview(story.enrichment)}{gap}<video controls preload="metadata" src="{escape(quote(artifact.final.media.path, safe='/'))}"></video>
<p>SHA-256: <code>{artifact.final.media.sha256}</code></p><nav>{' · '.join(navigation)}</nav></article>''')
        sections.append(f'<section><h2>{escape(sequence.sequence.title)}</h2><nav>{links}</nav>{"".join(cards)}</section>')
    body = ''.join(sections) or '<p>The manual plan contains no sequences. No relevance decision was made.</p>'
    if plan.review_only:
        body = '<p class="warning">REVIEW-ONLY RENDER: selection remains withheld. This render cannot be approved or published. Resolve selection and render a new plan.</p>' + body
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Unaudited story preview</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; media-src 'self' file:; style-src 'unsafe-inline'">
<style>body{{font:16px system-ui;background:#15171b;color:#eee;max-width:850px;margin:24px auto;padding:0 16px}}a{{color:#9fcbff}}video{{display:block;width:min(100%,360px);max-height:75vh;margin:16px 0}}article{{border-top:1px solid #555;margin:24px 0;padding:16px 0}}code{{overflow-wrap:anywhere}}nav{{line-height:1.8}}.warning{{background:#423719;padding:16px}}</style>
</head><body><h1>Story sequence preview</h1><p class="warning">UNAUDITED · NOT APPROVED. Supplied story plan; no final-media audit or approval. Review every story and the sequence, including omitted context, caption placement and pacing.</p>
<p>Selection method: {escape(plan.selection_method)}. Source: {escape(plan.source.locator)}</p>{body}</body></html>'''


def selection_preview(plan, report, transcript):
    """Text-first editorial review, including withheld proposals and source gaps."""
    import json
    accepted = {s.sequence_id for s in plan.sequences}
    sections = []
    if report.get('prompt_version', '').startswith('asset-relevance-selection-'):
        for sequence in plan.sequences:
            cards = []
            for story in sequence.stories:
                cards.append('<article><h3>' + escape(story.title) + '</h3><p>' +
                    escape(', '.join(story.matched_market_ids)) + '</p><pre>' +
                    escape(json.dumps([r.model_dump() for r in story.asset_relations], indent=2)) + '</pre></article>')
            sections.append('<section><h2>' + escape(sequence.title) + '</h2>' + ''.join(cards) + '</section>')
    for region in report['proposals']:
        for sequence in region['sequences']:
            cards = []
            for story in sequence['stories']:
                span = story['span']
                words = [w.word for segment in transcript.segments for w in segment.words
                         if w.start >= span['start'] and w.end <= span['end']]
                cards.append(f"<article><h3>{escape(story['title'])}</h3><p>{span['start']:.2f}–{span['end']:.2f}s · "
                             f"{escape(story['stance'])} · {escape(story['topic_path'])}</p><p>{escape(' '.join(words))}</p>"
                             f"<p>Market connection: {escape(story['market_connection'])}</p>"
                             + enrichment_preview(story.get('enrichment'), report.get('knowledge')) + '</article>')
            gaps = ''.join(f"<li>{o['start']:.2f}–{o['end']:.2f}s: {escape(o['reason'])}</li>" for o in sequence['omissions'])
            state = 'Selected for unaudited rendering' if sequence['sequence_id'] in accepted else 'Withheld — inspect selection report'
            sections.append(f"<section><h2>{escape(sequence['title'])}</h2><p>{state}</p>{''.join(cards)}<ul>{gaps}</ul></section>")
    reasons = ''.join(f'<li>{escape(r)}</li>' for r in report['review_reasons'])
    context = ''
    if report.get('context_resolutions'):
        history = {'initial_reviewed_windows': [w for w in report.get('initial_windows', []) if w['decision']=='review'],
                   'resolutions_and_independent_checks': report['context_resolutions']}
        context = ('<details><summary>Context review evidence and independent checks</summary><pre style="white-space:pre-wrap">'
                   + escape(json.dumps(history,indent=2,ensure_ascii=False)) + '</pre></details>')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Selection review</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<style>body{{font:17px system-ui;max-width:900px;margin:30px auto;padding:20px;line-height:1.6}}section{{border-top:1px solid #999}}article{{padding:10px 20px;background:#f2f4f6}}</style></head><body>
<h1>Pre-render story selection</h1><p>UNAUDITED · NOT APPROVED. Outcome: {escape(report['outcome'])}.
{len(report['windows'])} transcript windows tracked. Coverage complete: {report['coverage_complete']}. This preview is not a final-audio or visual audit.</p>
<ul>{reasons}</ul>{context}{''.join(sections) or '<p>No story proposals.</p>'}</body></html>'''


def audit_preview(plan, bundle, manifest, directory):
    """Playable isolated stories and ordered sequence review; no approval controls."""
    import json
    from .audit import grouped_associations, source_context, display_metadata
    from .artifacts import read_json_model
    from .contracts import TranscriptArtifact
    from .audit_contracts import MediaInspection
    from .config import PipelineConfig
    transcript=read_json_model(directory/manifest.artifacts['render.transcript'].path,TranscriptArtifact)
    config=read_json_model(directory/manifest.artifacts['config'].path,PipelineConfig)
    by_story={x.item_id:x for x in bundle.stories}
    by_sequence={x.item_id:x for x in bundle.sequences}
    cards=[]
    for sequence in plan.sequences:
        seq=by_sequence[sequence.sequence_id]
        cards.append(f'<section><h2>{escape(sequence.title)}</h2><p>Sequence audit: {escape(seq.decision)}</p>')
        previous=None
        for i,story in enumerate(sequence.stories):
            audit=by_story[story.story_id]
            ref=manifest.artifacts['render.story.'+story.story_id+'.final_media']
            gap=f'<p>Omitted source interval: {previous:.2f}–{story.span.start:.2f}s</p>' if previous is not None and previous<story.span.start else ''
            previous=story.span.end
            issues=[x.model_dump() for x in [*audit.deterministic_issues,*audit.semantic.issues]]
            inspection=read_json_model(directory/manifest.artifacts['inspection.'+story.story_id].path,MediaInspection)
            evidence={'displayed_metadata':display_metadata(story),
                'source_context':source_context(transcript,story.span.start,story.span.end,config.audit.context_padding_seconds),
                'final_audio':inspection.transcript.model_dump() if inspection.transcript else None,
                'captions':inspection.captions_text,'rules':[r.model_dump() for r in audit.semantic.rules]}
            navigation=' · '.join(f'<a href="#{escape(sequence.stories[j].story_id)}">{label}</a>'
                for j,label in [(i-1,'Previous story'),(i+1,'Next story')] if 0<=j<len(sequence.stories))
            cards.append(f'<article id="{escape(story.story_id)}"><h3>{i+1}. {escape(story.title)}</h3>'
                f'<p>{escape(audit.decision)} · source {story.span.start:.2f}–{story.span.end:.2f}s · {story.playback_rate:g}×</p>{gap}'
                f'<video controls preload="metadata" src="{escape(quote(ref.path,safe="/"))}"></video>'
                f'<nav>{navigation}</nav><p>Delivered SHA-256: <code>{ref.sha256}</code></p>'
                f'<p>{escape(audit.semantic.summary)}</p><pre>{escape(json.dumps(issues,indent=2))}</pre>'
                f'<details><summary>Source context, final audio, captions and rule results</summary><pre>{escape(json.dumps(evidence,indent=2))}</pre></details>'
                f'<details><summary>One assessment per market; editorial inference is not speaker stance</summary>'
                f'<pre>{escape(json.dumps(grouped_associations(story.enrichment),indent=2))}</pre></details></article>')
        cards.append(f'<p>{escape(seq.semantic.summary)}</p><pre>{escape(json.dumps([x.model_dump() for x in [*seq.deterministic_issues,*seq.semantic.issues]],indent=2))}</pre></section>')
    html = '''<!doctype html><html><head><meta charset="utf-8"><title>Final-media audit review</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; media-src 'self' file:; style-src 'unsafe-inline'">
<style>body{font:16px system-ui;max-width:900px;margin:24px auto;padding:20px}video{width:min(100%,360px);max-height:75vh}pre{white-space:pre-wrap;overflow-wrap:anywhere}article{border-top:1px solid #888;padding:16px}section{margin-bottom:40px}</style>
</head><body><h1>Final-media audit — human review required</h1>
<p>NOT APPROVED. Play every story in full, individually and in sequence. Check framing, charts, captions, audio, sync, boundaries and context. Review JSON binds exact media, metadata and audit hashes. Hard failures require corrected output and a new audit.</p>''' + ''.join(cards) + '</body></html>'
    if config.asset_policy is not None:
        html = html.replace('Final-media audit — human review required', 'Final-media audit — automated policy evaluation')
        html = html.replace('NOT APPROVED. Play every story in full, individually and in sequence. Check framing, charts, captions, audio, sync, boundaries and context. Review JSON binds exact media, metadata and audit hashes. Hard failures require corrected output and a new audit.', 'Audit evidence is not publication authority. An explicit automated decision binds exact media, policy and audit hashes; failed stories cannot publish. Diagnostics remain available for post-publication sampling.')
    return html

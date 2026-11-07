"""Evidence-bound reassessment of reviewed windows; independent confirmation required."""
from .contracts import StorySpec, TimeSpan
from .foundry import FoundryError
from .selection_contracts import (ContextResolution, EnrichedContextResolution,
                                 ContextResolutionReview)


def resolve_context(windows, decisions, transcript, markets, shared, prompts, call, *, enriched):
    from .selection import regions_for, check_evidence, check_markets, check_spoken_market_names
    effective = list(decisions)
    records = []
    for first, last in regions_for(windows, decisions):
        segments = transcript.segments[first:last+1]
        targets = {w['window_id']: (i, w, d) for i, (w, d) in enumerate(zip(windows, decisions))
                   if d.decision == 'review' and first <= w['first'] and w['last'] <= last
                   and (first < w['first'] or w['last'] < last)}
        if not targets:
            continue  # No additional context: never manufacture a resolution.
        payload = {**shared, 'reviewed_windows': [dict(w, original_review=d.model_dump())
                   for _, w, d in targets.values()],
                   'segments': [s.model_dump(exclude={'words'}) for s in segments]}
        record = {'window_ids': list(targets), 'first_segment_id': segments[0].segment_id,
                  'last_segment_id': segments[-1].segment_id, 'resolved_window_ids': []}
        records.append(record)
        model = EnrichedContextResolution if enriched else ContextResolution

        def coverage(items):
            if len(items) != len(targets) or {x.window_id for x in items} != set(targets):
                raise ValueError('incomplete context resolution coverage')

        def adjacent(evidence, window):
            inside = {s.segment_id for s in transcript.segments[window['first']:window['last']+1]}
            return any(e.segment_id not in inside for e in evidence)

        def validate_resolution(result):
            coverage(result.windows)
            for item in result.windows:
                _, window, _ = targets[item.window_id]
                own = transcript.segments[window['first']:window['last']+1]
                check_evidence(item.evidence, own)
                check_evidence(item.context_evidence, segments)
                check_markets(item.matched_market_ids, markets, item.topic_path)
                check_spoken_market_names(StorySpec(story_id='context', title='Context check',
                    span=TimeSpan(start=own[0].start,end=own[-1].end),
                    matched_market_ids=item.matched_market_ids), markets, transcript)
                if item.decision != 'review' and (not item.evidence or not adjacent(item.context_evidence,window)):
                    raise ValueError('resolved window needs local and adjacent evidence')

        def validate_check(result):
            coverage(result.windows)
            for item in result.windows:
                check_evidence(item.evidence, segments)
                if item.verdict == 'confirmed' and not adjacent(item.evidence, targets[item.window_id][1]):
                    raise ValueError('confirmation needs adjacent evidence')
        try:
            proposal = call('relevance', prompts['context_resolve'], payload, model, validate_resolution)
            record['proposal'] = proposal.model_dump()
            review = call('relevance', prompts['context_check'],
                          payload | {'proposed_resolutions': proposal.model_dump()},
                          ContextResolutionReview, validate_check)
            record['check'] = review.model_dump()
        except FoundryError as exc:
            if exc.code != 'context_budget_exceeded':
                raise  # Model/network/validation failures are not negative results.
            record['reason'] = 'context_budget_exceeded'
            continue
        checked = {w.window_id: w for w in review.windows}
        for item in proposal.windows:
            if item.decision != 'review' and checked[item.window_id].verdict == 'confirmed':
                i, _, original = targets[item.window_id]
                effective[i] = type(original).model_validate(item.model_dump(exclude={'context_evidence'}))
                record['resolved_window_ids'].append(item.window_id)
    return effective, records

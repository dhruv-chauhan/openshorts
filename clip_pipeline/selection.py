"""Full-coverage relevance, context reconstruction, proposal and pre-render checks."""
import re
from .artifacts import digest
from .contracts import StoryPlan, StorySequence, StorySpec, TimeSpan
from .foundry import batch_payloads, FoundryError
from .selection_contracts import RelevanceResponse, ProposalResponse, DraftResponse, SelectionReview, RankingResponse
from . import selection_prompts as prompts
from .stories import validate_plan_transcript, validate_plan_config


def windows_for(transcript, settings):
    segments, windows, start = transcript.segments, [], 0
    while start < len(segments):
        end = start + 1
        while end < len(segments) and (segments[end-1].end - segments[start].start < settings.window_seconds
                                      or end - start <= settings.overlap_segments):
            end += 1
        windows.append({'window_id': f'w{len(windows):05d}', 'first': start, 'last': end-1,
                        'segments': [{'segment_id': s.segment_id, 'start': s.start, 'end': s.end,
                                      'text': s.text} for s in segments[start:end]]})
        if end == len(segments):
            break
        start = max(start + 1, end - settings.overlap_segments)
    return windows


def check_evidence(evidence, segments):
    normal = lambda text: re.sub(r'\s+', ' ', text).strip()
    by_id = {s.segment_id: s for s in segments}
    for item in evidence:
        s = by_id.get(item.segment_id)
        if s is None or item.start < s.start or item.end > s.end:
            raise ValueError('evidence bounds/ID mismatch')
        words = [w.word for w in s.words if w.start >= item.start and w.end <= item.end]
        # ASR may tokenize "OpenAI" as "Open AI". Preserve literal source text
        # while accepting only whitespace differences in the timed-word binding.
        compact = lambda text: re.sub(r'\s+', '', text).casefold()
        if (normal(item.quote) not in normal(s.text)
                or compact(item.quote) not in compact(' '.join(words))):
            raise ValueError('evidence quote mismatch')


def check_markets(ids, markets, path=None):
    enabled = {m.market_id for m in markets.markets if m.enabled}
    if len(ids) != len(set(ids)) or not set(ids).issubset(enabled):
        raise ValueError('unknown, disabled or duplicate market IDs')
    if path == 'allowed_market' and not ids:
        raise ValueError('missing allowed market')


def check_spoken_market_names(story, markets, transcript):
    """Reject invented ID links; lexical presence is necessary, not sufficient."""
    text = ' '.join(w.word for segment in transcript.segments for w in segment.words
                    if w.start >= story.span.start and w.end <= story.span.end)
    spoken=set(spoken_market_ids(text,markets))
    if not set(story.matched_market_ids).issubset(spoken):
        raise ValueError('market ID has no spoken name/alias in its story')


def spoken_market_ids(text, markets):
    """Lexical hints only; they do not establish relevance or disambiguation."""
    def names(market):
        for name in [market.underlying_name, *market.aliases]:
            yield name
            # Legal suffixes do not change identity; do not infer subsidiaries,
            # index membership, fuzzy ASR matches or arbitrary first-word aliases.
            stripped = re.sub(r'(?:[,\s]+(?:incorporated|inc|corporation|corp|limited|ltd|plc)\.?)+$',
                              '', name, flags=re.I).strip()
            if stripped:
                yield stripped
    def mentioned(market, name):
        # Ticker aliases can be ordinary speech (BE, NOW, ON). Case-folding those
        # manufactures company hints from verbs/adverbs. Full names remain case
        # insensitive; exact uppercase symbols are still only lexical hints.
        ticker = market.market_id.rsplit(':', 1)[-1]
        flags = 0 if name == ticker and name.isupper() else re.I
        return re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text, flags)
    return [market.market_id for market in markets.markets if market.enabled and any(
        mentioned(market, name) for name in names(market))]


def boundary_reviews(story, transcript):
    """ASR punctuation is a review signal, never proof of semantic completion."""
    words = [w for segment in transcript.segments for w in segment.words]
    positions = [i for i, w in enumerate(words)
                 if w.start >= story.span.start and w.end <= story.span.end]
    if not positions:
        return ['story_has_no_words']
    terminal = lambda word: word.rstrip('\"\'”’)]}').endswith(('.', '?', '!'))
    reasons = []
    if positions[0] > 0 and not terminal(words[positions[0]-1].word):
        reasons.append('start_inside_asr_sentence')
    if not terminal(words[positions[-1]].word):
        reasons.append('end_inside_asr_sentence')
    return reasons


def regions_for(windows, decisions):
    """Expand context across batch boundaries, then union overlapping regions."""
    ranges = []
    for i, decision in enumerate(decisions):
        if decision.decision == 'irrelevant':
            continue
        left, right = max(0, i-1), min(len(windows)-1, i+1)
        while left > 0 and decisions[left].continues_before:
            left -= 1
        while right < len(windows)-1 and decisions[right].continues_after:
            right += 1
        ranges.append((windows[left]['first'], windows[right]['last']))
    merged = []
    for left, right in sorted(ranges):
        if merged and left <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
        else:
            merged.append((left, right))
    return merged


def _story_spec(story):
    return StorySpec.model_validate({key: getattr(story, key) for key in StorySpec.model_fields})


def check_presentation(response):
    """Check delivered claims, not proposal-only freshness/routing labels."""
    from .audit import display_metadata
    proposals = response.model_dump()
    displayed = {}
    for source, target in zip(response.sequences, proposals['sequences']):
        specs = [_story_spec(story) for story in source.stories]
        # Keep rejected hypotheses and verifier history out of the public-copy check.
        # Supported exposure is supplied explicitly in displayed_metadata below.
        target['stories'] = [story.model_dump(exclude={'enrichment'}) for story in specs]
        displayed.update({story.story_id: display_metadata(story) for story in specs})
    return {'proposals': proposals, 'displayed_metadata': displayed}


def _plan(source, transcript, sequences):
    return StoryPlan(source=source.origin, source_sha256=source.media_sha256,
                     transcript_sha256=digest(transcript), sequences=sequences)


def resolve_drafts(response, segments):
    """Resolve IDs to observed word boundaries and exact evidence; never invent time."""
    by_id = {s.segment_id: (i, s) for i, s in enumerate(segments)}
    def evidence(ids):
        if len(ids) != len(set(ids)) or not set(ids).issubset(by_id):
            raise ValueError('unknown or duplicate evidence segment ID')
        return [{'segment_id': key, 'start': by_id[key][1].words[0].start,
                 'end': by_id[key][1].words[-1].end, 'quote': by_id[key][1].text} for key in ids]
    sequences = []
    for sequence in response.sequences:
        stories = []
        if len(sequence.omission_reasons) != len(sequence.stories) - 1:
            raise ValueError('one omission reason is required for each story pair')
        for story in sequence.stories:
            if story.first_segment_id not in by_id or story.last_segment_id not in by_id:
                raise ValueError('unknown story boundary segment ID')
            first, last = by_id[story.first_segment_id][0], by_id[story.last_segment_id][0]
            if last < first or not set(story.evidence_segment_ids).issubset(
                    {s.segment_id for s in segments[first:last+1]}):
                raise ValueError('evidence/boundaries do not belong to the story')
            if not set(story.context_segment_ids).issubset(
                    {s.segment_id for s in segments[first:last+1]}):
                raise ValueError('required context must be inside the story boundaries')
            # ASR segments are not sentence boundaries. Expand only within the
            # complete region, then run the same overlap, duration and semantic checks.
            terminal = lambda word: word.rstrip('"\'”’)]}').endswith(('.', '?', '!'))
            while first > 0 and not terminal(segments[first-1].words[-1].word):
                first -= 1
            while last + 1 < len(segments) and not terminal(segments[last].words[-1].word):
                last += 1
            data = story.model_dump(exclude={'first_segment_id', 'last_segment_id',
                'evidence_segment_ids', 'context_segment_ids', 'attribution'})
            attribution = story.attribution
            stories.append(data | {'span': {'start': segments[first].words[0].start,
                                            'end': segments[last].words[-1].end},
                'evidence': evidence(story.evidence_segment_ids), 'context': evidence(story.context_segment_ids),
                'stance': attribution.stance if attribution else 'unclear',
                'speaker': attribution.speaker if attribution else None,
                'timeframe': attribution.timeframe if attribution else None, 'playback_rate': 1.0})
        omissions = [{'start': a['span']['end'], 'end': b['span']['start'], 'reason': reason}
                     for a,b,reason in zip(stories, stories[1:], sequence.omission_reasons)
                     if b['span']['start'] > a['span']['end']]
        sequences.append(sequence.model_dump(exclude={'stories', 'omission_reasons'}) |
                         {'stories': stories, 'omissions': omissions})
    return ProposalResponse.model_validate({'sequences': sequences, 'review_reasons': response.review_reasons,
                                           'explanation': response.explanation})


def select_stories(config, source, transcript, policy, markets, call, *, knowledge=None, assessed_on=None):
    """call() is a durable, budgeted Foundry operation supplied by the controller."""
    from datetime import date
    from .enrichment import enrich_story
    from .selection_contracts import EnrichedRelevanceResponse, EnrichedDraftResponse
    enriched = config.enrichment.mode == 'snapshot'
    if enriched != (knowledge is not None):
        raise ValueError('enrichment mode/snapshot mismatch')
    assessed_on = assessed_on or date.today().isoformat()
    prompt_set = prompts.snapshot(enriched)
    relevance_model = EnrichedRelevanceResponse if enriched else RelevanceResponse
    draft_model = EnrichedDraftResponse if enriched else DraftResponse
    windows = windows_for(transcript, config.selection)
    shared = {'policy': policy.model_dump(mode='json'),
              'markets_columns': ['market_id', 'underlying_name', 'asset_type', 'aliases'],
              'markets': [[m.market_id, m.underlying_name, m.asset_type, m.aliases]
                          for m in markets.markets if m.enabled],
              'published_at': source.published_at.isoformat() if source.published_at else None}
    if enriched:
        shared['knowledge'] = knowledge.model_dump()
    for window in windows:
        segments = transcript.segments[window['first']:window['last']+1]
        window['spoken_market_ids'] = []
        for market in markets.markets:
            if not market.enabled:
                continue
            try:
                check_spoken_market_names(StorySpec(story_id='window', title=window['window_id'],
                    span=TimeSpan(start=segments[0].start, end=segments[-1].end),
                    matched_market_ids=[market.market_id]), markets, transcript)
            except ValueError:
                continue
            window['spoken_market_ids'].append(market.market_id)
    batches, decisions, coverage_complete = [], [], True
    try:
        for offset in range(0, len(windows), config.selection.relevance_batch_windows):
            batches.extend(batch_payloads(windows[offset:offset + config.selection.relevance_batch_windows],
                system=prompt_set['relevance'], response_model=relevance_model,
                context_budget=config.selection.context_budget_tokens, output_tokens=config.foundry.max_output_tokens,
                shared=shared))
    except FoundryError as exc:
        if exc.code != 'context_budget_exceeded':
            raise
        from .selection_contracts import WindowDecision
        batches, coverage_complete = [], False
        decisions = [WindowDecision(window_id=w['window_id'], decision='review', topic_path=None,
            matched_market_ids=[], connection='', evidence=[], continues_before=False, continues_after=False,
            reason='not_evaluated: complete window and knowledge exceed context budget') for w in windows]
    for batch in batches:
        expected = {w['window_id']: w for w in batch['items']}
        def validate(response):
            ids = [w.window_id for w in response.windows]
            if len(ids) != len(set(ids)) or set(ids) != set(expected):
                raise ValueError('incomplete window coverage')
            for w in response.windows:
                window = expected[w.window_id]
                check_markets(w.matched_market_ids, markets, w.topic_path)
                segments = transcript.segments[window['first']:window['last']+1]
                check_spoken_market_names(StorySpec(story_id='window', title=w.window_id,
                    span=TimeSpan(start=segments[0].start, end=segments[-1].end),
                    matched_market_ids=w.matched_market_ids), markets, transcript)
                check_evidence(w.evidence, segments)
        response = call('relevance', prompt_set['relevance'], batch, relevance_model, validate)
        by_id = {w.window_id: w for w in response.windows}
        decisions.extend(by_id[w['window_id']] for w in batch['items'])
    assert len(decisions) == len(windows)
    initial_decisions = list(decisions)
    context_resolutions = []
    if coverage_complete and any(w.decision == 'review' for w in decisions):
        from .context_resolution import resolve_context
        decisions, context_resolutions = resolve_context(windows, decisions, transcript, markets,
            shared, prompt_set, call, enriched=enriched)
    regions = regions_for(windows, decisions) if coverage_complete else []
    review_reasons = [f'{w.window_id}: {w.reason}' for w in decisions if w.decision == 'review']
    proposals, checks, accepted, withheld, boundary_flags = [], [], [], [], []
    for region_id, (first, last) in enumerate(regions):
        segments = transcript.segments[first:last+1]
        payload = {**shared, 'region_id': region_id, 'limits': config.stories.model_dump(),
            'source_starts_here': first == 0, 'source_ends_here': last == len(transcript.segments)-1,
            'window_map': [d.model_dump() for w, d in zip(windows, decisions)
                           if w['first'] <= last and w['last'] >= first],
            'segments': [{'segment_id': s.segment_id, 'start': s.start, 'end': s.end, 'text': s.text,
                          'spoken_market_ids':spoken_market_ids(' '.join(w.word for w in s.words),markets),
                          'boundary_hint': 'ASR segment; may start/end inside a sentence'} for s in segments],
            'timing_warnings': transcript.timing_warnings}
        def normalized(response):
            return resolve_drafts(response, segments)
        def validate_proposals(response):
            result = normalized(response)
            seqs = []
            for sequence in result.sequences:
                for story in sequence.stories:
                    if story.playback_rate != 1.0:
                        raise ValueError('automatic selection must retain normal speed')
                    check_markets(story.matched_market_ids, markets, story.topic_path)
                    check_spoken_market_names(story, markets, transcript)
                    check_evidence(story.evidence + story.context, segments)
                    if not story.subject:
                        raise ValueError('a story needs an explicit subject')
                gaps = [(a.span.end, b.span.start) for a, b in zip(sequence.stories, sequence.stories[1:])
                        if b.span.start > a.span.end]
                if [(o.start, o.end) for o in sequence.omissions] != gaps:
                    raise ValueError('omitted intervals must exactly cover gaps between stories')
                seqs.append(StorySequence(sequence_id=sequence.sequence_id, title=sequence.title,
                                         stories=[_story_spec(s) for s in sequence.stories]))
            validate_plan_transcript(_plan(source, transcript, seqs), source, transcript, config)
        try:
            response = call('selection', prompt_set['proposal'], payload, draft_model, validate_proposals)
        except FoundryError as exc:
            if exc.code != 'context_budget_exceeded':
                raise
            review_reasons.append(f'region {region_id}: complete context exceeds configured model budget')
            continue
        response = normalized(response)
        # IDs are run-global and stable regardless of the model's local naming.
        local = response.model_dump()
        for i, sequence in enumerate(local['sequences']):
            sequence['sequence_id'] = f'r{region_id}-q{i}'
            for j, story in enumerate(sequence['stories']):
                story['story_id'] = f'r{region_id}-q{i}-s{j}'
        response = ProposalResponse.model_validate(local)
        association_withheld = set()
        if enriched:
            enriched_sequences = []
            for sequence in response.sequences:
                stories = []
                for story in sequence.stories:
                    metadata = enrich_story(story, transcript, knowledge, markets, config.enrichment,
                                            call, assessed_on=assessed_on)
                    story = story.model_copy(update={'enrichment': metadata})
                    if not metadata.coverage_complete:
                        review_reasons.extend(f'{story.story_id}: {reason}' for reason in metadata.review_reasons)
                    if story.topic_path == 'market_exposure' and not any(
                            a.decision == 'supported' for a in metadata.associations):
                        association_withheld.add(sequence.sequence_id)
                        if not metadata.coverage_complete or any(a.decision == 'needs_review' for a in metadata.associations):
                            review_reasons.append(f'{story.story_id}: unresolved required exposure')
                    stories.append(story)
                enriched_sequences.append(sequence.model_copy(update={'stories': stories}))
            response = response.model_copy(update={'sequences': enriched_sequences})
        for sequence in response.sequences:
            for story in sequence.stories:
                for reason in boundary_reviews(story, transcript):
                    boundary_flags.append({'story_id': story.story_id, 'reason': reason})
                    review_reasons.append(f'{story.story_id}: {reason}; inspect ASR punctuation and local completion')
        proposals.append(response.model_dump())
        review_reasons.extend(response.review_reasons)
        if not response.sequences:
            continue
        rule_ids = {r.rule_id for r in policy.rules}
        def validate_checks(result):
            expected = {s.sequence_id: s for s in response.sequences}
            if len(result.sequences) != len(expected) or {s.sequence_id for s in result.sequences} != set(expected):
                raise ValueError('incomplete sequence checks')
            for checked in result.sequences:
                ids = {s.story_id for s in expected[checked.sequence_id].stories}
                if len(checked.stories) != len(ids) or {s.story_id for s in checked.stories} != ids:
                    raise ValueError('incomplete story checks')
                for rules in [checked.rules, *[s.rules for s in checked.stories]]:
                    if len(rules) != len(rule_ids) or {r.rule_id for r in rules} != rule_ids:
                        raise ValueError('incomplete policy rule checks')
        try:
            checked = call('selection', prompt_set['check'], {**payload, **check_presentation(response)},
                           SelectionReview, validate_checks)
        except FoundryError as exc:
            if exc.code != 'context_budget_exceeded':
                raise
            review_reasons.append(f'region {region_id}: complete selection check exceeds model budget')
            continue
        checks.append(checked.model_dump())
        by_id = {s.sequence_id: s for s in checked.sequences}
        for sequence in response.sequences:
            rules = by_id[sequence.sequence_id].rules + [r for s in by_id[sequence.sequence_id].stories for r in s.rules]
            if any(r.result != 'pass' for r in rules):
                withheld.append({'sequence_id': sequence.sequence_id, 'reason': 'policy_check',
                                 'rules': [r.model_dump() for r in rules if r.result != 'pass']})
                review_rules = {r.rule_id for r in policy.rules if r.severity == 'review'}
                if any(r.result == 'review' or (r.result == 'reject' and r.rule_id in review_rules) for r in rules):
                    review_reasons.append(f'{sequence.sequence_id}: policy uncertainty')
                continue
            if sequence.sequence_id in association_withheld:
                withheld.append({'sequence_id': sequence.sequence_id, 'reason': 'no_supported_exposure'})
                continue
            seq = StorySequence(sequence_id=sequence.sequence_id, title=sequence.title,
                                stories=[_story_spec(s) for s in sequence.stories])
            try:
                validate_plan_config(_plan(source, transcript, [seq]), config)
            except ValueError:
                withheld.append({'sequence_id': sequence.sequence_id, 'reason': 'hard_sequence_budget'})
                review_reasons.append(f'{sequence.sequence_id}: complete sequence exceeds a hard limit')
                continue
            accepted.append((sequence, seq))
    ranking = None
    if accepted:
        ids = {seq.sequence_id for _, seq in accepted}
        def validate_ranking(result):
            seen = set()
            for item in result.ordered:
                if item.sequence_id not in ids or item.sequence_id in seen or (item.duplicate_of and item.duplicate_of not in seen):
                    raise ValueError('invalid global ranking/deduplication')
                seen.add(item.sequence_id)
            if seen != ids:
                raise ValueError('incomplete global ranking')
        ranking = call('selection', prompt_set['rank'], {**shared, 'sequences': [p.model_dump() for p, _ in accepted]},
                       RankingResponse, validate_ranking)
        by_id = {s.sequence_id: s for _, s in accepted}
        accepted = []
        for item in ranking.ordered:
            if item.duplicate_of:
                withheld.append({'sequence_id': item.sequence_id, 'reason': 'duplicate',
                                 'duplicate_of': item.duplicate_of, 'explanation': item.reason})
            elif len(accepted) >= config.stories.max_sequences:
                withheld.append({'sequence_id': item.sequence_id, 'reason': 'sequence_count_budget'})
            else:
                accepted.append(by_id[item.sequence_id])
    accepted.sort(key=lambda s: s.stories[0].span.start)
    plan = _plan(source, transcript, accepted)
    validate_plan_config(plan, config)
    validate_plan_transcript(plan, source, transcript, config)
    report = {'schema_version': '1', 'prompt_version': prompt_set['version'], 'approved': False,
              'audit_performed': False, 'windows': [d.model_dump() for d in decisions],
              'initial_windows': [d.model_dump() for d in initial_decisions],
              'context_resolutions': context_resolutions,
              'coverage_complete': coverage_complete, 'regions': regions, 'proposals': proposals, 'checks': checks,
              'ranking': ranking.model_dump() if ranking else None, 'withheld': withheld,
              'boundary_reviews': boundary_flags,
              'review_reasons': review_reasons,
              'outcome': 'needs_review' if review_reasons else 'selected' if accepted else 'completed_no_matches'}
    if enriched:
        report['knowledge'] = knowledge.model_dump()
        report['assessed_on'] = assessed_on
    binding = {'markets': digest(markets), 'policy': digest(policy),
               'prompts': prompts.binding(prompt_set),
               'settings': digest(config), 'report': digest(report)}
    if enriched:
        binding['enrichment'] = digest(knowledge)
    # Ambiguous coverage is explicit; retain proposals for review, withhold rendering.
    data = plan.model_dump() | {'selection_method': 'foundry', 'selection_binding': binding}
    if review_reasons:
        data['sequences'] = []
    return StoryPlan.model_validate(data), report


def validate_selection_checkpoint(manifest, directory, config):
    """Completed selection plans remain bound to their pinned decision inputs."""
    import json
    from .artifacts import ArtifactError, read_json_model
    from .contracts import SourceArtifact, TranscriptArtifact, MarketSnapshot, TopicPolicy
    if manifest.stage != 'selected':
        return
    def path(key):
        if key not in manifest.artifacts:
            raise ArtifactError('incomplete selection checkpoint')
        return directory / manifest.artifacts[key].path
    plan = read_json_model(path('story_plan'), StoryPlan)
    report = json.loads(path('selection_report').read_text())
    markets = read_json_model(path('markets'), MarketSnapshot)
    policy = read_json_model(path('policy'), TopicPolicy)
    saved_prompts = json.loads(path('prompts').read_text())
    expected = {'markets': digest(markets), 'policy': digest(policy), 'settings': digest(config),
                'report': digest(report), 'prompts': prompts.binding(saved_prompts)}
    if config.enrichment.mode == 'snapshot':
        from .enrichment_contracts import KnowledgeSnapshot
        expected['enrichment'] = digest(read_json_model(path('enrichment'), KnowledgeSnapshot))
    if plan.selection_method != 'foundry' or plan.selection_binding != expected:
        raise ArtifactError('selection provenance mismatch')
    source = read_json_model(path('source'), SourceArtifact)
    transcript = read_json_model(path('transcript'), TranscriptArtifact)
    validate_plan_transcript(plan, source, transcript, config)
    validate_plan_config(plan, config)
    expected_outcome = 'selected' if manifest.state == 'checkpointed' else manifest.outcome
    if report['outcome'] != expected_outcome or (expected_outcome == 'selected') != bool(plan.sequences):
        raise ArtifactError('selection outcome/plan mismatch')
    expected_ids = [w['window_id'] for w in windows_for(transcript, config.selection)]
    if (not report['coverage_complete'] and expected_outcome != 'needs_review') or [w['window_id'] for w in report['windows']] != expected_ids:
        raise ArtifactError('selection coverage mismatch')

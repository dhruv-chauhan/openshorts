"""Durable text-only selection over Phase C source/transcript artifacts."""
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from . import __version__, selection_prompts as prompts
from .artifacts import ArtifactError, ArtifactStore, atomic_write, digest, json_bytes, read_json_model
from .config import load_config, load_inputs, PipelineConfig
from .contracts import RunManifest, SourceArtifact, TranscriptArtifact, TopicPolicy, MarketSnapshot
from .foundry import FoundryBackend
from .markets import fetch_markets
from .runner import inspect_run
from .selection import select_stories
from .transcript import validate_transcript


def prompt_snapshot(enriched=False, asset_policy=None):
    if asset_policy is not None:
        from .asset_selection import snapshot
        return snapshot()
    return prompts.snapshot(enriched)


def prepare_selection(config_path, source_path, transcript_path, *, reuse_run_id=None):
    config = load_config(config_path)
    policy, markets = load_inputs(config)
    source = read_json_model(source_path, SourceArtifact)
    transcript = read_json_model(transcript_path, TranscriptArtifact)
    if source.origin.kind == 'fixture':
        raise ArtifactError('selection requires a real source artifact')
    validate_transcript(transcript, source, config.media)
    inputs = {'config': config, 'policy': policy, 'source': source, 'submission': source.origin,
              'transcript': transcript, 'prompts': prompt_snapshot(config.enrichment.mode == 'snapshot', config.asset_policy)}
    if config.enrichment.mode == 'snapshot':
        from .enrichment import load_knowledge
        inputs['enrichment'] = load_knowledge(config.enrichment)
    # Pin explicit development data before media or inference work.
    if config.markets.mode == 'mock_api':
        markets = read_json_model(Path(config.markets.path), MarketSnapshot)
    if markets is not None:
        inputs['market_input'] = markets
    store = ArtifactStore(Path(config.workspace))
    with store.lock():
        if reuse_run_id is not None:
            parent = inspect_run(store.workspace, reuse_run_id)
            if parent.execution_kind != 'topic_selection' or parent.state not in {'finished','checkpointed'}:
                raise ArtifactError('reuse requires a completed selection checkpoint')
            parent_dir = store.run_dir(reuse_run_id)
            parent_config = read_json_model(parent_dir / parent.artifacts['config'].path, PipelineConfig)
            if parent_config.foundry != config.foundry:
                raise ArtifactError('reuse requires identical provider and model settings')
            from .media_common import file_hash
            inputs['reuse_selection'] = {'run_id': reuse_run_id,
                'manifest_sha256': file_hash(parent_dir/'manifest.json')}
        run_id = uuid4().hex
        refs = {key: store.write_artifact(run_id, key + '.json', value) for key, value in inputs.items()}
        now = datetime.now(timezone.utc)
        manifest = RunManifest(implementation_version=__version__, run_id=run_id, created_at=now, updated_at=now,
            execution_kind='topic_selection', source=source.origin, artifacts=refs, input_artifact_keys=list(refs),
            input_fingerprint=digest({k: v.sha256 for k, v in refs.items()}), reason_code='selection_prepared')
        atomic_write(store.run_dir(run_id) / 'manifest.json', json_bytes(manifest))
        return manifest, store.run_dir(run_id)


def execute_selection(workspace, run_id, *, backend_factory=FoundryBackend):
    store = ArtifactStore(Path(workspace))
    with store.lock():
        manifest = inspect_run(store.workspace, run_id)
        if manifest.execution_kind != 'topic_selection':
            raise ArtifactError('not a topic-selection run')
        if manifest.state in {'finished', 'checkpointed'}:
            return inspect_run(store.workspace, run_id)
        directory = store.run_dir(run_id)
        def path(key):
            return directory / manifest.artifacts[key].path
        def save(**changes):
            nonlocal manifest
            manifest = RunManifest.model_validate(manifest.model_dump() | changes | {'updated_at': datetime.now(timezone.utc)})
            atomic_write(directory / 'manifest.json', json_bytes(manifest))
        def artifact(key, value):
            ref = store.write_artifact(run_id, key + '-' + uuid4().hex + '.json', value)
            save(artifacts=manifest.artifacts | {key: ref})
        config = read_json_model(path('config'), PipelineConfig)
        source = read_json_model(path('source'), SourceArtifact)
        transcript = read_json_model(path('transcript'), TranscriptArtifact)
        policy = read_json_model(path('policy'), TopicPolicy)
        if json.loads(path('prompts').read_text()) != prompt_snapshot(config.enrichment.mode == 'snapshot', config.asset_policy):
            raise ArtifactError('selection implementation changed; prepare a new run')
        save(state='running', stage='transcribed', reason_code='selection_started')
        try:
            reuse = None
            if 'reuse_selection' in manifest.artifacts:
                from .media_common import file_hash
                lineage = json.loads(path('reuse_selection').read_text())
                parent_dir = store.run_dir(lineage['run_id'])
                if file_hash(parent_dir/'manifest.json') != lineage['manifest_sha256']:
                    raise ArtifactError('reuse parent changed after preparation')
                reuse = inspect_run(store.workspace,lineage['run_id'])
            if 'markets' not in manifest.artifacts:
                settings = config.markets
                if settings.mode in {'mock_api', 'snapshot'}:
                    settings = settings.model_copy(update={'path': str(path('market_input'))})
                markets, provenance = fetch_markets(settings, store.workspace / 'cache' / 'markets')
                refs = {key: store.write_artifact(run_id, key + '-' + uuid4().hex + '.json', value)
                        for key, value in [('markets', markets), ('market_fetch', provenance)]}
                save(artifacts=manifest.artifacts | refs)
            markets = read_json_model(path('markets'), MarketSnapshot)
            # If interruption split the market commits, do not invent provenance.
            if 'market_fetch' not in manifest.artifacts:
                raise ArtifactError('incomplete market fetch checkpoint; prepare a new run')
            ledger = json.loads(path('usage').read_text()) if 'usage' in manifest.artifacts else {
                'reserved_calls': 0, 'reserved_tokens': 0, 'attempts': []}
            prior_attempts = ledger['attempts']
            def budget_sink(calls, tokens, attempts):
                artifact('usage', {'reserved_calls': calls, 'reserved_tokens': tokens,
                                   'attempts': prior_attempts + attempts})
            with backend_factory(config, prior_calls=ledger['reserved_calls'], prior_tokens=ledger['reserved_tokens'],
                                 budget_sink=budget_sink) as backend:
                def call(stage, system, payload, model, validate):
                    key = 'call.' + digest({'stage': stage, 'system': system, 'payload': payload,
                                            'schema': model.model_json_schema()})
                    if key in manifest.artifacts:
                        response = read_json_model(path(key), model)
                        validate(response)
                        return response
                    if reuse is not None and key in reuse.artifacts:
                        response = read_json_model(parent_dir/reuse.artifacts[key].path,model)
                        validate(response)  # Current checks apply to every cached response.
                        artifact(key,response)
                        reused = json.loads(path('reused_calls').read_text()) if 'reused_calls' in manifest.artifacts else []
                        artifact('reused_calls',reused+[{'key':key,'parent_run_id':reuse.run_id}])
                        return response
                    response = backend.generate(stage=stage, system=system, payload=payload,
                                                response_model=model, validate=validate)
                    artifact(key, response)
                    return response
                from .enrichment_contracts import KnowledgeSnapshot
                knowledge = read_json_model(path('enrichment'), KnowledgeSnapshot) if config.enrichment.mode == 'snapshot' else None
                if config.asset_policy is not None:
                    from .asset_selection import select
                    plan, report = select(config, source, transcript, policy, markets, call)
                else:
                    plan, report = select_stories(config, source, transcript, policy, markets, call,
                        knowledge=knowledge, assessed_on=manifest.created_at.date().isoformat())
            artifact('selection_report', report)
            artifact('story_plan', plan)
            from .report import selection_preview
            from .media_common import reference
            preview = directory / ('selection-preview-' + uuid4().hex + '.html')
            atomic_write(preview, selection_preview(plan, report, transcript).encode())
            save(artifacts=manifest.artifacts | {'preview': reference(preview, directory)})
            outcome = report['outcome']
            save(stage='selected', state='checkpointed' if outcome == 'selected' else 'finished',
                 outcome=None if outcome == 'selected' else outcome, reason_code='topic_' + outcome)
            return inspect_run(store.workspace, run_id)
        except KeyboardInterrupt:
            save(state='interrupted', stage='transcribed', outcome=None, reason_code='operator_interrupted')
            raise
        except Exception:
            save(state='retryable', stage='transcribed', outcome=None, reason_code='selection_failed')
            raise

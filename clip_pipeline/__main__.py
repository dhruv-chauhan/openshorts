"""Clip-only selection, rendering, independent audit, human review and publication."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from pydantic import ValidationError

from .artifacts import ArtifactError, WorkspaceBusy
from .config import load_config, load_inputs
from .contracts import ManualRenderRequest, SourceInput, TimeSpan
from .media_common import MediaError
from .runner import inspect_run, prepare_run, resume_preparation

# Public CLI codes; preparation succeeds without claiming clip completion.
OK = 0
INVALID = 2
NEEDS_REVIEW = 3
FAILED = 4
BUSY = 5
UNAVAILABLE = 6
OUTCOME_EXIT_CODES = {"completed": OK, "completed_no_matches": OK,
                      "needs_review": NEEDS_REVIEW, "partial_failure": FAILED,
                      "failed": FAILED}


def parser():
    cli = argparse.ArgumentParser(description="Clip-only pipeline: select, render, audit, review and publish")
    commands = cli.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate config and local policy/snapshot without IO to services")
    validate.add_argument("--config", type=Path, required=True)
    preflight = commands.add_parser("preflight", help="Make a small paid Foundry request per deployment")
    preflight.add_argument("--config", type=Path, required=True)
    run = commands.add_parser("run", help="Prepare inputs; automatic topic selection is not implemented yet")
    run.add_argument("--config", type=Path, required=True)
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--url")
    source.add_argument("--input", type=Path)
    source.add_argument("--fixture", type=Path, help="Explicitly synthetic contract fixture; never rendered")
    run.add_argument("--transcript", type=Path)
    run.add_argument("--prepare-only", action="store_true", help="Required for run; stops at configured")
    render = commands.add_parser("render", help="Render a manual span; output remains unaudited and unapproved")
    render.add_argument("--config", type=Path, required=True)
    source = render.add_mutually_exclusive_group(required=True)
    source.add_argument("--url")
    source.add_argument("--input", type=Path)
    render.add_argument("--transcript", type=Path)
    render.add_argument("--start", type=float, required=True)
    render.add_argument("--end", type=float, required=True)
    stories = commands.add_parser("render-stories", help="Render a pinned manual plan as separate ordered stories")
    stories.add_argument("--config", type=Path, required=True)
    stories.add_argument("--plan", type=Path, required=True)
    stories.add_argument("--transcript", type=Path)
    stories.add_argument("--prepare-only", action="store_true")
    stories.add_argument("--stop-after-stories", type=int, help="Pause after N newly completed stories")
    select = commands.add_parser("select", help="Select unaudited stories from reusable source/transcript artifacts")
    select.add_argument("--config", type=Path, required=True)
    select.add_argument("--source-artifact", type=Path, required=True)
    select.add_argument("--transcript", type=Path, required=True)
    select.add_argument("--prepare-only", action="store_true")
    audit = commands.add_parser("audit", help="Audit final story media independently and prepare human review")
    audit.add_argument("--config", type=Path, required=True)
    audit.add_argument("--render-workspace", type=Path, required=True)
    audit.add_argument("--render-run-id", required=True)
    audit.add_argument("--prepare-only", action="store_true")
    audit.add_argument("--media-only", action="store_true")
    for name in ("review", "promote"):
        command = commands.add_parser(name, help="Record human decisions" if name == "review" else "Publish approved sequences to private Blob storage")
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--run-id", required=True)
        if name == "review": command.add_argument("--decisions", type=Path, required=True)
    for name in ("status", "resume"):
        command = commands.add_parser(name, help="Verify a saved run and its artifact checksums")
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--run-id", required=True)
        if name == "resume":
            modes = command.add_mutually_exclusive_group()
            modes.add_argument("--prepare-only", action="store_true")
            modes.add_argument("--render-only", action="store_true", help="Resume an explicit manual render")
            modes.add_argument("--stories-only", action="store_true", help="Resume a pinned manual story plan")
            modes.add_argument("--selection-only", action="store_true", help="Resume topic selection with pinned inputs")
            modes.add_argument("--audit-only", action="store_true", help="Resume final-media auditing")
            command.add_argument("--media-only", action="store_true")
            command.add_argument("--stop-after-stories", type=int)
    return cli


def emit(value, *, error=False):
    print(json.dumps(value, sort_keys=True), file=sys.stderr if error else sys.stdout)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        from .foundry import FoundryError
        from .markets import MarketError
        from .storage import StorageError
        if getattr(args, 'media_only', False) and (getattr(args, 'prepare_only', False) or
                not (args.command == 'audit' or (args.command == 'resume' and args.audit_only))):
            raise ArtifactError('media-only requires audit execution')
        stop_after = getattr(args, "stop_after_stories", None)
        if stop_after is not None and (stop_after < 1 or (
                args.command == "resume" and not args.stories_only)):
            raise ArtifactError("stop-after-stories requires a positive count and a story command")
        if args.command == "preflight":
            from .foundry import FoundryError, preflight
            config = load_config(args.config)
            load_inputs(config)
            try:
                result = preflight(config)
            except FoundryError as exc:
                result = {"ok": False, **exc.public()}
            emit(result, error=not result["ok"])
            return OK if result["ok"] else FAILED
        if args.command == "validate":
            config = load_config(args.config)
            policy, snapshot = load_inputs(config)
            emit({"valid": True, "policy_version": policy.version,
                  "market_mode": config.markets.mode,
                  "snapshot_version": snapshot.version if snapshot else None,
                  "remote_services_checked": False})
            return OK
        if (args.command in {"run", "resume"} and not args.prepare_only
                and not getattr(args, "render_only", False) and not getattr(args, "stories_only", False)
                and not getattr(args, "selection_only", False) and not getattr(args, "audit_only", False)):
            emit({"error": "execution_not_implemented",
                  "message": "Use --prepare-only, render for an explicit span, or resume --render-only for a manual run."}, error=True)
            return UNAVAILABLE
        if args.command == "audit":
            from .audit_runner import prepare_audit, execute_audit
            manifest, directory = prepare_audit(args.config, args.render_workspace, args.render_run_id)
            if not args.prepare_only:
                manifest = execute_audit(directory.parent.parent, manifest.run_id, media_only=args.media_only)
        elif args.command == "resume" and args.audit_only:
            from .audit_runner import execute_audit
            manifest = execute_audit(args.workspace, args.run_id, media_only=args.media_only)
            directory = args.workspace.resolve() / "runs" / manifest.run_id
        elif args.command in {"review", "promote"}:
            if args.command == "review":
                from .review import apply_review
                manifest = apply_review(args.workspace, args.run_id, args.decisions)
            else:
                from .storage import promote
                manifest = promote(args.workspace, args.run_id)
            directory = args.workspace.resolve() / "runs" / manifest.run_id
        elif args.command == "select":
            from .selection_runner import prepare_selection, execute_selection
            manifest, directory = prepare_selection(args.config, args.source_artifact, args.transcript)
            if not args.prepare_only:
                manifest = execute_selection(directory.parent.parent, manifest.run_id)
        elif args.command == "resume" and args.selection_only:
            from .selection_runner import execute_selection
            manifest = execute_selection(args.workspace, args.run_id)
            directory = args.workspace.resolve() / "runs" / manifest.run_id
        elif args.command == "render-stories":
            from .execution import execute_stories
            manifest, directory = prepare_run(args.config, transcript_path=args.transcript, story_plan_path=args.plan)
            if not args.prepare_only:
                manifest = execute_stories(directory.parent.parent, manifest.run_id, stop_after_stories=stop_after)
        elif args.command == "resume" and args.stories_only:
            from .execution import execute_stories
            manifest = execute_stories(args.workspace, args.run_id, stop_after_stories=stop_after)
            directory = args.workspace.resolve() / "runs" / manifest.run_id
        elif args.command == "render":
            from .execution import execute_manual
            source = (SourceInput(kind="youtube", locator=args.url) if args.url else
                      SourceInput(kind="local", locator=str(args.input)))
            request = ManualRenderRequest(span=TimeSpan(start=args.start, end=args.end))
            manifest, directory = prepare_run(args.config, source, transcript_path=args.transcript,
                                              render_request=request)
            manifest = execute_manual(directory.parent.parent, manifest.run_id)
        elif args.command == "resume" and args.render_only:
            from .execution import execute_manual
            manifest = execute_manual(args.workspace, args.run_id)
            directory = args.workspace.resolve() / "runs" / manifest.run_id
        elif args.command == "run":
            source = (SourceInput(kind="youtube", locator=args.url) if args.url else
                      SourceInput(kind="local", locator=str(args.input)) if args.input else None)
            manifest, directory = prepare_run(args.config, source, fixture_path=args.fixture,
                                              transcript_path=args.transcript)
        else:
            manifest = (resume_preparation(args.workspace, args.run_id) if args.command == "resume"
                        else inspect_run(args.workspace, args.run_id))
            directory = args.workspace.resolve() / "runs" / manifest.run_id
        emit({"run_id": manifest.run_id, "manifest": str(directory / "manifest.json"),
              "stage": manifest.stage, "state": manifest.state, "outcome": manifest.outcome,
              "execution_kind": manifest.execution_kind,
              "input_fingerprint": manifest.input_fingerprint,
              "approved": manifest.stage == "stored",
              "audit_bundle": str(directory / manifest.artifacts["audit_bundle"].path) if "audit_bundle" in manifest.artifacts else None,
              "review_template": str(directory / manifest.artifacts["review_template"].path) if "review_template" in manifest.artifacts else None,
              "story_plan": str(directory / manifest.artifacts["story_plan"].path) if "story_plan" in manifest.artifacts else None,
              "selection_report": str(directory / manifest.artifacts["selection_report"].path) if "selection_report" in manifest.artifacts else None,
              "story_progress": {key: value.model_dump() for key, value in manifest.story_progress.items()},
              "sequence_index": str(directory / manifest.artifacts["sequence_index"].path) if "sequence_index" in manifest.artifacts else None,
              "preview": str(directory / manifest.artifacts["preview"].path) if "preview" in manifest.artifacts else None,
              "final_media": str(directory / manifest.artifacts["final_media"].path) if "final_media" in manifest.artifacts else None,
              "message": ("Final-media audit/review run; only stored sequences are published." if manifest.execution_kind == "story_audit" else
                          "Topic selection only; no final-media audit or approval performed." if manifest.execution_kind == "topic_selection" else
                          "Supplied-plan render; no final-media audit or approval performed."
                          if manifest.execution_kind in {"manual_render", "manual_stories"} else
                          "Inputs prepared; no download, transcription, inference, render, or approval performed.")})
        if manifest.execution_kind == "story_audit" and manifest.stage == "audited":
            return NEEDS_REVIEW
        return OUTCOME_EXIT_CODES.get(manifest.outcome, OK)
    except FoundryError as exc:
        emit(exc.public(), error=True)
        return FAILED
    except StorageError as exc:
        emit({"error": str(exc)}, error=True)
        return FAILED
    except MarketError as exc:
        emit({"error": str(exc)}, error=True)
        return FAILED
    except ValidationError as exc:
        # Never log input values or validator contexts: they could contain a
        # mistakenly pasted API key. Field paths and error types are sufficient.
        emit({"error": "invalid_input", "fields": [
            {"path": ".".join(map(str, error["loc"])), "type": error["type"]}
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        ]}, error=True)
        return INVALID
    except WorkspaceBusy:
        emit({"error": "workspace_busy", "message": "Another worker owns the workspace lock."}, error=True)
        return BUSY
    except MediaError as exc:
        emit({"error": str(exc), "message": "Media stage failed; inspect its local worker.log.",
              "run_id": manifest.run_id if "manifest" in locals() else getattr(args, "run_id", None)}, error=True)
        return FAILED
    except (ArtifactError, ValueError):
        emit({"error": "invalid_artifact", "message": "Invalid input or checkpoint; inspect local files."}, error=True)
        return INVALID
    except OSError:
        emit({"error": "local_io_failed", "message": "Could not read or write a required local file."}, error=True)
        return FAILED
    except KeyboardInterrupt:
        emit({"error": "interrupted", "message": "Interrupted; only stages committed in a valid manifest can be resumed."}, error=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())

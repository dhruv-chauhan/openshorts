"""Preparation, Foundry capability preflight, and unaudited manual rendering."""
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
    cli = argparse.ArgumentParser(description="Clip-only pipeline: prepare, preflight, and render manual spans")
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
    for name in ("status", "resume"):
        command = commands.add_parser(name, help="Verify a saved run and its artifact checksums")
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--run-id", required=True)
        if name == "resume":
            modes = command.add_mutually_exclusive_group()
            modes.add_argument("--prepare-only", action="store_true")
            modes.add_argument("--render-only", action="store_true", help="Resume an explicit manual render")
    return cli


def emit(value, *, error=False):
    print(json.dumps(value, sort_keys=True), file=sys.stderr if error else sys.stdout)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
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
        if args.command in {"run", "resume"} and not args.prepare_only and not getattr(args, "render_only", False):
            emit({"error": "execution_not_implemented",
                  "message": "Use --prepare-only, render for an explicit span, or resume --render-only for a manual run."}, error=True)
            return UNAVAILABLE
        if args.command == "render":
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
              "approved": False,
              "final_media": str(directory / manifest.artifacts["final_media"].path) if "final_media" in manifest.artifacts else None,
              "message": ("Manual render run; no topic selection, content audit, or approval performed."
                          if manifest.execution_kind == "manual_render" else
                          "Inputs prepared; no download, transcription, inference, render, or approval performed.")})
        return OUTCOME_EXIT_CODES.get(manifest.outcome, OK)
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

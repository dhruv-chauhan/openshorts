"""Run preparation and explicit Foundry capability preflight."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from pydantic import ValidationError

from .artifacts import ArtifactError, WorkspaceBusy
from .config import load_config, load_inputs
from .contracts import SourceInput
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
    cli = argparse.ArgumentParser(description="Clip-only pipeline: preparation and Foundry preflight")
    commands = cli.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate config and local policy/snapshot without IO to services")
    validate.add_argument("--config", type=Path, required=True)
    preflight = commands.add_parser("preflight", help="Make a small paid Foundry request per deployment")
    preflight.add_argument("--config", type=Path, required=True)
    run = commands.add_parser("run", help="Prepare a run; media/model execution is not implemented yet")
    run.add_argument("--config", type=Path, required=True)
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--url")
    source.add_argument("--input", type=Path)
    source.add_argument("--fixture", type=Path, help="Explicitly synthetic contract fixture; never rendered")
    run.add_argument("--transcript", type=Path)
    run.add_argument("--prepare-only", action="store_true", help="Required in Phase A; stops at configured")
    for name in ("status", "resume"):
        command = commands.add_parser(name, help="Verify a saved run and its artifact checksums")
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--run-id", required=True)
        if name == "resume":
            command.add_argument("--prepare-only", action="store_true")
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
        if args.command in {"run", "resume"} and not args.prepare_only:
            emit({"error": "execution_not_implemented",
                  "message": "Phase A supports --prepare-only. No media or model stages will run."}, error=True)
            return UNAVAILABLE
        if args.command == "run":
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
              "message": "Inputs prepared; no download, transcription, inference, render, or approval performed."})
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
    except (ArtifactError, ValueError):
        emit({"error": "invalid_artifact", "message": "Invalid input or checkpoint; inspect local files."}, error=True)
        return INVALID
    except OSError:
        emit({"error": "local_io_failed", "message": "Could not read or write a required local file."}, error=True)
        return FAILED
    except KeyboardInterrupt:
        emit({"error": "interrupted", "message": "Preparation interrupted; incomplete runs are not committed."}, error=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())

"""Process entry point with a DB-free fast path for the project adapter."""

import multiprocessing
import sys


def main():
    """Dispatch ``pipelinectl`` before importing the queue-backed CLI.

    Frozen workers spawn this adapter for capabilities probes and queue
    elections.  Importing :mod:`promptpilot.cli` first imports ``db`` and runs
    schema initialisation, turning a repository-only helper into a competing
    SQLite writer.  All ordinary commands retain their historical Click path.
    """
    multiprocessing.freeze_support()
    args = sys.argv[1:]
    if args[:1] == ["pipelinectl"]:
        from .project_pipeline import run

        raise SystemExit(run(args[1:]))

    if args[:2] == ["workflow", "verify-external"]:
        from .native_external_gate import main as verify_external

        raise SystemExit(verify_external(args[2:]))

    from .cli import cli

    cli()

"""Allow ``python -m tools.addons.parallel_orchestrator``."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())

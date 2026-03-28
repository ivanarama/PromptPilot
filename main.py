"""Entry point for PyInstaller build."""
import multiprocessing
multiprocessing.freeze_support()

from promptpilot.cli import cli
cli()

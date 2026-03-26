"""Allow running as: python -m promptpilot"""
import multiprocessing

multiprocessing.freeze_support()  # required for PyInstaller on Windows

from .cli import cli

cli()

"""Additive cascade-review controller for PromptPilot workflows.

The package intentionally lives outside ``promptpilot``.  It uses the existing
workflow state machine and queue, but owns the optional ``review_chain`` phase.
"""

from .controller import CascadeController, main

__all__ = ["CascadeController", "main"]

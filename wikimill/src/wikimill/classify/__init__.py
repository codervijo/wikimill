"""Stage 4 — classification.

A pure function over stored evidence, versioned and stored separately from the
observations it judges, so an improved classifier re-judges history offline.
"""

from .rules import Observation, Verdict, classify
from .runner import ReclassifyStats
from .runner import run as reclassify
from .state import is_terminal, recheck_seconds, record

__all__ = [
    "Observation",
    "ReclassifyStats",
    "Verdict",
    "classify",
    "is_terminal",
    "recheck_seconds",
    "reclassify",
    "record",
]

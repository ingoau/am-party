"""
Smart fades for am-party: beat/key/vocal-aware DJ-style transitions.

Vendored and adapted from Music Assistant 2.10.8 "smart fades" (Apache-2.0),
https://github.com/music-assistant/server - see NOTICE.md.

Layout:
  mixing/    planner + ffmpeg/rubberband renderer (numpy only; runs in the server process)
  analysis/  torch models (Beat This!, S-KEY, FireRed AED); only imported by the worker child
  worker.py  ``AnalysisWorker`` - out-of-process analysis
  transition.py  ``build_transition`` - smart -> standard -> cut

Importing this package does not import torch (nor numpy until a transition is built).
"""

from __future__ import annotations

from .transition import (
    TAIL_SECONDS_SMART,
    TransitionResult,
    build_transition,
    head_seconds,
    tail_seconds,
)
from .worker import AnalysisWorker

__all__ = [
    "TAIL_SECONDS_SMART",
    "AnalysisWorker",
    "TransitionResult",
    "build_transition",
    "head_seconds",
    "tail_seconds",
]

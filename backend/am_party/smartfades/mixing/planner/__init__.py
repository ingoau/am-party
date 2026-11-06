"""Smart Fades - candidate/policy transition planner package.

Adapted from Music Assistant (Apache-2.0), https://github.com/music-assistant/server
(music_assistant/controllers/streams/smart_fades/planner/__init__.py, v2.10.8). Modified for am-party: import paths only.
"""

from __future__ import annotations

from .planner import SmartCrossFadePlanner, TransitionPlanner

__all__ = ["SmartCrossFadePlanner", "TransitionPlanner"]

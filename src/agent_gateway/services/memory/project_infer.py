"""Infer a memory's project from the file paths it was recorded from."""

from __future__ import annotations

import re

KNOWN_PROJECTS = (
    "utility",
    "agent-gateway",
    "yjzt-wms-app",
    "yjzt-tms-app",
    "yjzt-app-framework",
    "agent-vibe",
)

_SEGMENT_SPLIT = re.compile(r"[\\/]+")


def _segment_project(segment: str) -> str:
    """A segment names the project when it equals or extends it by a non-ASCII-alnum."""
    lowered = segment.lower()
    for name in KNOWN_PROJECTS:
        if not lowered.startswith(name):
            continue
        rest = lowered[len(name) :]
        if rest and rest[0].isascii() and rest[0].isalnum():
            continue
        return name
    return ""


def infer_project(source_files: list[str]) -> str:
    """First known project name found in the paths; empty when nothing matches."""
    for raw in source_files or []:
        if not raw:
            continue
        for segment in _SEGMENT_SPLIT.split(str(raw)):
            project = _segment_project(segment)
            if project:
                return project
    return ""

"""Shared presentation helpers for bugyi-chops reports."""

from __future__ import annotations

from collections.abc import Mapping

from rich.cells import cell_len
from sase.chops import ChopReport, Tone

SEVERITY_TONES: Mapping[str, Tone] = {
    "violation": "error",
    "warning": "warn",
    "fyi": "info",
    "neutral": "muted",
    "unknown": "muted",
}


def start_report(title: str) -> ChopReport:
    """Start one report using the package-wide title convention."""

    return ChopReport(title=title.upper())


def severity_tone(severity: str) -> Tone:
    """Map a package severity to the shared semantic tone vocabulary."""

    return SEVERITY_TONES.get(severity, "neutral")


def elide_path(path: str, max_cells: int) -> str:
    """Elide a path from the left while respecting rendered cell width."""

    if max_cells <= 0:
        return ""
    if cell_len(path) <= max_cells:
        return path

    prefix = "…/"
    if max_cells <= cell_len(prefix):
        return prefix[:max_cells]
    for start, character in enumerate(path):
        if character != "/":
            continue
        candidate = prefix + path[start + 1 :]
        if cell_len(candidate) <= max_cells:
            return candidate
    for start in range(1, len(path) + 1):
        candidate = prefix + path[start:].lstrip("/")
        if cell_len(candidate) <= max_cells:
            return candidate
    return prefix


def add_facts_footer(
    report: ChopReport,
    facts: Mapping[str, str],
    *,
    tone: Tone = "muted",
) -> ChopReport:
    """Finish a report with the shared divider and factual key/value footer."""

    return report.divider().kv(facts, tone=tone)


__all__ = [
    "SEVERITY_TONES",
    "add_facts_footer",
    "elide_path",
    "severity_tone",
    "start_report",
]

"""Findings out: JSON for diffing two runs, Markdown for reading one."""

from bearhug.report.applied import AppliedEntry, append_entry, parse_applied, validate_applied
from bearhug.report.emit import render_markdown, write_findings
from bearhug.report.merge import ReportDiff, build_report, compare_reports, write_report
from bearhug.report.patches import (
    Patch,
    PatchOutcome,
    Skipped,
    emit_patches,
    emit_report_patches,
    generate_patch,
)

__all__ = [
    "AppliedEntry",
    "Patch",
    "PatchOutcome",
    "ReportDiff",
    "Skipped",
    "append_entry",
    "build_report",
    "compare_reports",
    "emit_patches",
    "emit_report_patches",
    "generate_patch",
    "parse_applied",
    "render_markdown",
    "validate_applied",
    "write_findings",
    "write_report",
]

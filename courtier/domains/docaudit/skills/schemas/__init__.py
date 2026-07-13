"""Pydantic input models for skills (sub-agent typed inputs)."""

from .format_audit import FormatAuditorInput
from .plagiarism import PlagiarismAuditorInput

__all__ = [
    "FormatAuditorInput",
    "PlagiarismAuditorInput",
]

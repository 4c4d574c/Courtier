"""Tests for skill input models living in skills/schemas/."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from skills.schemas import FormatAuditorInput, PlagiarismAuditorInput

from courtier.agent.agents.input_models import FormatAuditorInput as BackwardCompatFormatInput
from courtier.agent.agents.input_models import (
    PlagiarismAuditorInput as BackwardCompatPlagiarismInput,
)
from courtier.agent.agents.subagent.base import SubAgentInput


@pytest.mark.parametrize(
    "model_cls, compat_cls",
    [
        (FormatAuditorInput, BackwardCompatFormatInput),
        (PlagiarismAuditorInput, BackwardCompatPlagiarismInput),
    ],
)
def test_models_are_re_exported_from_input_models(model_cls, compat_cls):
    """The lazy re-exports in src.agent.agents.input_models must resolve to skills.schemas types."""
    assert compat_cls is model_cls


@pytest.mark.parametrize(
    "model_cls",
    [
        FormatAuditorInput,
        PlagiarismAuditorInput,
    ],
)
def test_models_inherit_sub_agent_input(model_cls):
    assert issubclass(model_cls, SubAgentInput)


def test_format_auditor_input_requires_document():
    with pytest.raises(ValidationError):
        FormatAuditorInput(task="audit")

    input_obj = FormatAuditorInput(task="audit", document="$ref:parse_document:1")
    assert input_obj.task == "audit"
    assert input_obj.document == "$ref:parse_document:1"
    assert input_obj.doc_type == "通知"


def test_plagiarism_auditor_input_defaults():
    input_obj = PlagiarismAuditorInput(task="check", document="doc")
    assert input_obj.top_k == 5
    assert input_obj.library_docs is None


def test_plagiarism_auditor_input_custom_top_k():
    input_obj = PlagiarismAuditorInput(task="check", document="doc", top_k=10)
    assert input_obj.top_k == 10

"""Run-scoped context for cross-cutting host-side model consumers.

The search rerank post-processor is registered once at app startup
(``make_search_rerank_post_processor``) but executes at a per-run tool
boundary, and the model-pool selection is per-run.  A ContextVar set by
the run runner carries the resolved :class:`~courtier.agent.api.services.agent_service.ModelProfile`
to that boundary without widening the ToolRegistry post-processor
contract.  Empty value = no pool selection (scalar settings apply).
"""

from __future__ import annotations

from contextvars import ContextVar

#: The current run's resolved model profile (or None).
run_model_profile: ContextVar = ContextVar("run_model_profile", default=None)

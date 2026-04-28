"""Pipeline runner: trigger CI/CD pipelines, watch run status, integrate with
the agent action loop.
"""
from dosm.pipelines.adapters import (
    PipelineAdapter,
    PipelineProviderError,
    PipelineUnreachable,
    PollResult,
    TriggerResult,
    get_adapter,
    list_providers,
)
from dosm.pipelines.poller import pipeline_poll_loop, poll_tick
from dosm.pipelines.routes import router as pipelines_router

__all__ = [
    "PipelineAdapter",
    "PipelineProviderError",
    "PipelineUnreachable",
    "PollResult",
    "TriggerResult",
    "get_adapter",
    "list_providers",
    "pipeline_poll_loop",
    "poll_tick",
    "pipelines_router",
]

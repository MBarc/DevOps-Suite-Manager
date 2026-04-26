"""Pipeline runner: trigger CI/CD pipelines, watch run status, integrate with
the agent action loop. v1 ships GitHub Actions; Azure DevOps / Octopus /
AWX / Terraform Cloud are planned as adapters in 11b/c/d.
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
from dosm.pipelines.routes import router as pipelines_router

__all__ = [
    "PipelineAdapter",
    "PipelineProviderError",
    "PipelineUnreachable",
    "PollResult",
    "TriggerResult",
    "get_adapter",
    "list_providers",
    "pipelines_router",
]

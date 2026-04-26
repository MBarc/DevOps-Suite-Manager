"""Pipeline provider adapters."""
from dosm.pipelines.adapters.base import (
    PipelineAdapter,
    PipelineProviderError,
    PipelineUnreachable,
    PollResult,
    TriggerResult,
)
from dosm.pipelines.adapters.github import GitHubActionsAdapter

_REGISTRY: dict[str, PipelineAdapter] = {
    "github_actions": GitHubActionsAdapter(),
}


def get_adapter(provider: str) -> PipelineAdapter:
    a = _REGISTRY.get(provider)
    if a is None:
        raise PipelineProviderError(f"unknown pipeline provider {provider!r}")
    return a


def list_providers() -> list[str]:
    return list(_REGISTRY.keys())


__all__ = [
    "PipelineAdapter",
    "PipelineProviderError",
    "PipelineUnreachable",
    "PollResult",
    "TriggerResult",
    "get_adapter",
    "list_providers",
]

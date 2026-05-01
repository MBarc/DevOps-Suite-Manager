"""Pipeline provider adapters."""
from dosm.pipelines.adapters.awx import AWXAdapter
from dosm.pipelines.adapters.azure_devops import AzureDevOpsAdapter
from dosm.pipelines.adapters.base import (
    FieldSpec,
    PipelineAdapter,
    PipelineProviderError,
    PipelineUnreachable,
    PollResult,
    TriggerResult,
)
from dosm.pipelines.adapters.github import GitHubActionsAdapter
from dosm.pipelines.adapters.octopus import OctopusDeployAdapter
from dosm.pipelines.adapters.terraform_cloud import TerraformCloudAdapter

_REGISTRY: dict[str, PipelineAdapter] = {
    "github_actions": GitHubActionsAdapter(),
    "azure_devops": AzureDevOpsAdapter(),
    "octopus_deploy": OctopusDeployAdapter(),
    "awx": AWXAdapter(),
    "terraform_cloud": TerraformCloudAdapter(),
}


def get_adapter(provider: str) -> PipelineAdapter:
    a = _REGISTRY.get(provider)
    if a is None:
        raise PipelineProviderError(f"unknown pipeline provider {provider!r}")
    return a


def list_providers() -> list[str]:
    return list(_REGISTRY.keys())


__all__ = [
    "FieldSpec",
    "PipelineAdapter",
    "PipelineProviderError",
    "PipelineUnreachable",
    "PollResult",
    "TriggerResult",
    "get_adapter",
    "list_providers",
]

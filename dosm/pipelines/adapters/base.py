"""Pipeline adapter contract.

Every provider (GitHub Actions, Azure DevOps, Octopus, AWX, TFC, ...)
implements this same minimal surface so the rest of DOSM (UI, agent
actions, audit log) is provider-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class FieldSpec:
    """Describes one config field that a provider exposes in the form UI."""
    name: str          # HTML <input name="...">
    config_key: str    # key in the normalized config dict
    label: str
    placeholder: str = ""
    hint: str = ""
    default: str = ""


class PipelineProviderError(RuntimeError):
    """Provider rejected the call (auth, missing entity, malformed config)."""


class PipelineUnreachable(PipelineProviderError):
    """Could not reach the provider's API at all (network, DNS, timeout)."""


# Statuses DOSM stores. Each adapter maps its native status into this set.
RUN_STATUSES = (
    "queued",      # accepted, not yet running
    "running",     # in progress
    "success",     # completed OK
    "failed",      # completed with non-OK conclusion
    "cancelled",   # cancelled by user or system
    "skipped",     # provider skipped this run
    "unknown",     # we couldn't determine — keep polling
)


@dataclass
class TriggerResult:
    external_id: str | None
    status: str
    html_url: str | None
    raw: dict


@dataclass
class PollResult:
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    html_url: str | None
    raw: dict


class PipelineAdapter(ABC):
    """Stateless contract — instances are reused across calls."""

    provider: str
    display_name: str = ""
    credential_hint: str = "API credential for this provider."

    @classmethod
    def field_schema(cls) -> list[FieldSpec]:
        """Ordered config fields rendered in the pipeline form."""
        return []

    def target_summary(self, config: dict) -> str:
        """Short human-readable identifier for list/detail views."""
        return ""

    @abstractmethod
    def validate_config(self, config: dict) -> dict:
        """Return a normalized config dict or raise PipelineProviderError."""

    @abstractmethod
    async def trigger(
        self, *, config: dict, secret: str | None, inputs: dict
    ) -> TriggerResult: ...

    @abstractmethod
    async def poll(
        self,
        *,
        config: dict,
        secret: str | None,
        external_id: str | None,
    ) -> PollResult: ...

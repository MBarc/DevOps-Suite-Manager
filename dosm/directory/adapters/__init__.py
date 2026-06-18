"""AD directory source ABC + factory.

Implementations:
- ``WinRMJumpboxSource`` (winrm_jumpbox.py) - production
- ``MockSource`` (mock.py) - fixture-backed for tests/dev
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from dosm.config import Config

# ---- Errors --------------------------------------------------------------


class AdDirectoryError(Exception):
    """Base error for any AD directory failure."""


class AdDirectoryUnreachable(AdDirectoryError):
    """Jumpbox not reachable, WinRM auth failed, or PowerShell errored.

    Distinct from "not found" so the UI can show a "last good data" banner
    rather than wiping the cached members.
    """


class AdGroupNotFound(AdDirectoryError):
    """The group name does not exist in AD."""


class AdUserNotFound(AdDirectoryError):
    """The user (manager) does not exist in AD."""


# ---- Records (plain dataclasses, no SQLAlchemy coupling) ----------------


@dataclass
class UserRecord:
    """A single AD user resolved from the directory."""

    distinguished_name: str
    display_name: str
    email: str | None = None
    title: str | None = None
    phone: str | None = None
    enabled: bool = True
    manager_dn: str | None = None
    sam_account_name: str | None = None


@dataclass
class GroupRecord:
    distinguished_name: str
    name: str  # cn
    description: str | None = None
    managed_by_dn: str | None = None


@dataclass
class MemberRecord:
    """A user record in the context of a group sync."""

    user_dn: str
    display_name: str
    email: str | None = None
    title: str | None = None
    phone: str | None = None
    enabled: bool = True
    # AD ``manager`` attribute (DN), plus the resolved display name. The
    # adapter is responsible for resolving DN to name in a single round trip;
    # the orchestrator just stores both.
    manager_dn: str | None = None
    manager_name: str | None = None


@dataclass
class GroupSyncResult:
    """Everything one group-sync round trip returns to the orchestrator."""

    group: GroupRecord
    manager: UserRecord | None
    members: list[MemberRecord] = field(default_factory=list)
    # The chain of manager DNs walking *up* from this dept's manager.
    # The first element is `manager.manager_dn`, and so on.
    manager_chain: list[str] = field(default_factory=list)


# ---- ABC -----------------------------------------------------------------


class AdDirectorySource(ABC):
    """A pluggable AD reader.

    Implementations connect to whatever backend (a Windows jumpbox over
    WinRM, a mock fixture set, etc.) and translate AD into our ``*Record``
    dataclasses. The orchestrator (``dosm.directory.sync``) doesn't know or
    care which adapter it has.
    """

    @abstractmethod
    def test_connection(self) -> str:
        """Quick smoke: returns the AD domain DNS root on success.

        Raises ``AdDirectoryUnreachable`` if the connection fails. Used by
        ``dosm org test-ad`` and by the configure page's "Test" button.
        """

    @abstractmethod
    def resolve_group(self, group_name: str) -> GroupRecord:
        """Find an AD group by sAMAccountName / cn / displayName.

        Raises ``AdGroupNotFound`` if no match.
        """

    @abstractmethod
    def resolve_user(self, identifier: str) -> UserRecord:
        """Find an AD user by sAMAccountName / displayName / mail.

        Raises ``AdUserNotFound`` if no match.
        """

    @abstractmethod
    def sync_group(self, group_dn: str, manager_dn: str | None) -> GroupSyncResult:
        """One round trip that returns everything a single department needs.

        Includes the group meta, all direct members with their attributes,
        and the manager's manager-chain (capped) so the orchestrator can
        infer the parent department locally.
        """


# ---- Factory -------------------------------------------------------------


def get_directory_source(cfg: Config) -> AdDirectorySource:
    """Pick an adapter based on ``cfg.directory.adapter``.

    Only constructed when the caller actually needs to talk to AD; the
    configure page and the empty-state list view never call this.
    """
    adapter = cfg.directory.adapter
    if adapter == "mock":
        from dosm.directory.adapters.mock import MockSource
        return MockSource()
    if adapter == "winrm_jumpbox":
        from dosm.directory.adapters.winrm_jumpbox import WinRMJumpboxSource
        return WinRMJumpboxSource.from_config(cfg)
    raise AdDirectoryError(f"unknown directory adapter: {adapter!r}")

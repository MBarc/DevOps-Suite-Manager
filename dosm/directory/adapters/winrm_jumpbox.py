"""Production AD source: PowerShell ActiveDirectory cmdlets via WinRM.

Reads the configured jumpbox host id from ``cfg.directory.ad_jumpbox_host_id``,
opens a fresh DOSM DB session to look up the host + its credential, then runs
short PowerShell scripts on the jumpbox. Every call is a single round trip
that emits one JSON blob; we parse it and translate into the ABC's record
types.

This adapter is deliberately stateless - each call opens a new ``winrm.Session``
and discards it. WinRM has no persistent socket to reuse.
"""
from __future__ import annotations

import json

from dosm.config import Config
from dosm.db import session_scope
from dosm.directory.adapters import (
    AdDirectoryError,
    AdDirectorySource,
    AdDirectoryUnreachable,
    AdGroupNotFound,
    AdUserNotFound,
    GroupRecord,
    GroupSyncResult,
    MemberRecord,
    UserRecord,
)
from dosm.models import Host
from dosm.secrets import SecretNotFound, get_backend

# ---- PowerShell scripts -----------------------------------------------------
#
# All scripts share the same shape: they emit either a single JSON object,
# or a JSON object with an ``error`` key when the cmdlet didn't find what it
# was looking for. This makes the parser one branch instead of N.

_TEST_PS = r"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory -ErrorAction Stop
$d = Get-ADDomain
@{ ok = $true; domain = $d.DNSRoot } | ConvertTo-Json -Depth 3 -Compress
"""


_RESOLVE_GROUP_PS = r"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory -ErrorAction Stop
try {
  $g = Get-ADGroup -Identity $($env:GROUP_NAME) -Properties description, managedBy
} catch {
  @{ error = 'not_found'; message = $_.Exception.Message } | ConvertTo-Json -Compress
  exit 0
}
@{
  ok = $true
  distinguishedName = $g.DistinguishedName
  name = $g.Name
  description = $g.description
  managedBy = $g.managedBy
} | ConvertTo-Json -Depth 3 -Compress
"""


_RESOLVE_USER_PS = r"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory -ErrorAction Stop
try {
  # Try SAM first (most common), then fall back to a broader search.
  $u = Get-ADUser -Identity $($env:USER_ID) -Properties displayName, mail, title, telephoneNumber, manager, enabled -ErrorAction Stop
} catch {
  $candidates = @(Get-ADUser -Filter "displayName -eq '$($env:USER_ID)' -or mail -eq '$($env:USER_ID)'" -Properties displayName, mail, title, telephoneNumber, manager, enabled)
  if ($candidates.Count -eq 0) {
    @{ error = 'not_found' } | ConvertTo-Json -Compress
    exit 0
  }
  $u = $candidates[0]
}
@{
  ok = $true
  distinguishedName = $u.DistinguishedName
  displayName = $u.DisplayName
  sAMAccountName = $u.SamAccountName
  mail = $u.mail
  title = $u.title
  telephoneNumber = $u.telephoneNumber
  manager = $u.manager
  enabled = [bool]$u.Enabled
} | ConvertTo-Json -Depth 3 -Compress
"""


# Single round trip: group meta, direct members with attrs, manager chain
# (capped at 20). $env:GROUP_DN and $env:MANAGER_DN are passed as env so the
# values can contain quotes, commas, equals signs, etc., without escaping.
_SYNC_GROUP_PS = r"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory -ErrorAction Stop

$groupDn = $env:GROUP_DN
$managerDn = $env:MANAGER_DN

$g = Get-ADGroup -Identity $groupDn -Properties description, managedBy

# Direct members only (matches user's spec - no recursion).
$memberDns = @(Get-ADGroupMember -Identity $g | Where-Object { $_.objectClass -eq 'user' } | Select-Object -ExpandProperty distinguishedName)
$members = @()
$memberManagers = @{}
foreach ($dn in $memberDns) {
  try {
    $u = Get-ADUser -Identity $dn -Properties displayName, mail, title, telephoneNumber, enabled, manager -ErrorAction Stop
    $members += @{
      distinguishedName = $u.DistinguishedName
      displayName = $u.DisplayName
      mail = $u.mail
      title = $u.title
      telephoneNumber = $u.telephoneNumber
      enabled = [bool]$u.Enabled
      managerDn = $u.manager
    }
    if ($u.manager) { $memberManagers[$u.manager] = $true }
  } catch {
    # Skip stale references silently - sync should be tolerant.
  }
}

# Resolve each unique member-manager DN to a display name, in the same
# round trip. ~one extra Get-ADUser per distinct manager - bounded by the
# number of leads above this group, not member count.
$managerNames = @{}
foreach ($mDn in $memberManagers.Keys) {
  try {
    $mu = Get-ADUser -Identity $mDn -Properties displayName -ErrorAction Stop
    $managerNames[$mDn] = $mu.DisplayName
  } catch {
    $managerNames[$mDn] = $null
  }
}
foreach ($m in $members) {
  if ($m.managerDn -and $managerNames.ContainsKey($m.managerDn)) {
    $m.managerName = $managerNames[$m.managerDn]
  } else {
    $m.managerName = $null
  }
}

$manager = $null
$chain = @()
if ($managerDn) {
  try {
    $m = Get-ADUser -Identity $managerDn -Properties displayName, mail, title, telephoneNumber, manager, enabled
    $manager = @{
      distinguishedName = $m.DistinguishedName
      displayName = $m.DisplayName
      mail = $m.mail
      title = $m.title
      telephoneNumber = $m.telephoneNumber
      manager = $m.manager
      enabled = [bool]$m.Enabled
    }
    $cur = $m
    $depth = 0
    while ($cur -and $cur.manager -and $depth -lt 20) {
      $chain += $cur.manager
      try { $cur = Get-ADUser -Identity $cur.manager -Properties manager -ErrorAction Stop }
      catch { break }
      $depth += 1
    }
  } catch {
    # Manager DN no longer resolves - leave $manager null; orchestrator handles.
  }
}

@{
  ok = $true
  group = @{
    distinguishedName = $g.DistinguishedName
    name = $g.Name
    description = $g.description
    managedBy = $g.managedBy
  }
  manager = $manager
  managerChain = $chain
  members = $members
} | ConvertTo-Json -Depth 6 -Compress
"""


def _parse_or_raise(out: bytes | str, *, on_not_found: type[AdDirectoryError]) -> dict:
    """Decode a script's stdout to dict. Translate ``error: not_found`` to
    the caller's expected exception so the route can render a friendly
    message without sniffing PowerShell text."""
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    out = out.strip()
    if not out:
        raise AdDirectoryUnreachable("PowerShell returned empty output")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as e:
        raise AdDirectoryUnreachable(f"PowerShell returned non-JSON: {out[:200]!r}") from e
    if isinstance(payload, dict) and payload.get("error") == "not_found":
        raise on_not_found(payload.get("message") or "not found in AD")
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise AdDirectoryUnreachable(f"unexpected payload: {out[:200]!r}")
    return payload


class WinRMJumpboxSource(AdDirectorySource):
    """Runs PowerShell on a configured Windows jumpbox over WinRM."""

    def __init__(
        self,
        *,
        endpoint: str,
        username: str,
        password: str,
        transport: str,
        cert_validation: str,
        timeout_seconds: float,
        host_label: str,
    ):
        self._endpoint = endpoint
        self._username = username
        self._password = password
        self._transport = transport
        self._cert_validation = cert_validation
        self._timeout = timeout_seconds
        self._host_label = host_label

    # ---- Construction ---------------------------------------------------

    @classmethod
    def from_config(cls, cfg: Config) -> WinRMJumpboxSource:
        host_id = cfg.directory.ad_jumpbox_host_id
        if not host_id:
            raise AdDirectoryError("AD jumpbox is not configured. Set one at /org/configure.")
        # Open a short DB session - adapter has no other DB needs.
        with session_scope() as db:
            host = db.get(Host, host_id)
            if host is None:
                raise AdDirectoryError(f"AD jumpbox host id {host_id} no longer exists")
            cred = host.credential
            if cred is None:
                raise AdDirectoryError(
                    f"host {host.name!r} has no credential profile; attach one before syncing"
                )
            try:
                secret_text = get_backend(cfg).get_str(cred.secret_ref)
            except SecretNotFound as e:
                raise AdDirectoryError(
                    f"credential {cred.name!r} secret missing in backend"
                ) from e
            if not cred.username:
                raise AdDirectoryError(
                    f"credential {cred.name!r} has no username; WinRM needs one"
                )
            # Compose the WinRM endpoint from the host. Defaults match the
            # WinRM source in dosm/metrics/sources.py for consistency.
            mcfg = cfg.metrics
            scheme = "https" if mcfg.winrm_use_https else "http"
            endpoint = f"{scheme}://{host.hostname}:{mcfg.winrm_port}/wsman"
            username = cred.username
            if cred.domain:
                username = f"{cred.domain}\\{username}"
            return cls(
                endpoint=endpoint,
                username=username,
                password=secret_text,
                transport=mcfg.winrm_transport,
                cert_validation="ignore" if mcfg.winrm_use_https else "validate",
                timeout_seconds=cfg.directory.powershell_timeout_seconds,
                host_label=host.name,
            )

    # ---- Internals ------------------------------------------------------

    def _run_ps(self, script: str, env: dict[str, str] | None = None) -> dict:
        """Open a Session, run a script with optional env vars, parse JSON."""
        try:
            import winrm  # type: ignore
        except ImportError as e:
            raise AdDirectoryError("pywinrm is not installed") from e
        try:
            session = winrm.Session(
                self._endpoint,
                auth=(self._username, self._password),
                transport=self._transport,
                server_cert_validation=self._cert_validation,
            )
        except Exception as e:
            raise AdDirectoryUnreachable(
                f"{self._host_label}: WinRM session init failed: {e}"
            ) from e
        # Prepend env-var assignments if any. PowerShell's $env:NAME = "value"
        # is the cleanest way to inject opaque strings without quoting hell.
        prelude = ""
        if env:
            prelude = "\n".join(
                f"$env:{k} = '{v.replace(chr(39), chr(39) * 2)}'" for k, v in env.items()
            ) + "\n"
        try:
            result = session.run_ps(prelude + script)
        except Exception as e:
            raise AdDirectoryUnreachable(
                f"{self._host_label}: WinRM call failed: {e}"
            ) from e
        if result.status_code != 0:
            err = (result.std_err or b"").decode("utf-8", errors="replace")[:500]
            raise AdDirectoryUnreachable(
                f"{self._host_label}: PowerShell exit {result.status_code}: {err}"
            )
        return _parse_or_raise(result.std_out, on_not_found=AdDirectoryUnreachable)

    # ---- ABC impls ------------------------------------------------------

    def test_connection(self) -> str:
        payload = self._run_ps(_TEST_PS)
        return payload.get("domain") or "(unknown)"

    def resolve_group(self, group_name: str) -> GroupRecord:
        try:
            payload = self._run_ps(_RESOLVE_GROUP_PS, env={"GROUP_NAME": group_name})
        except AdDirectoryUnreachable as e:
            # _parse_or_raise inside _run_ps maps "not_found" payloads to
            # AdDirectoryUnreachable by default - re-route to the right type
            # only when the upstream message looks like our marker.
            if "not_found" in str(e):
                raise AdGroupNotFound(group_name) from e
            raise
        return GroupRecord(
            distinguished_name=payload["distinguishedName"],
            name=payload["name"],
            description=payload.get("description"),
            managed_by_dn=payload.get("managedBy"),
        )

    def resolve_user(self, identifier: str) -> UserRecord:
        try:
            payload = self._run_ps(_RESOLVE_USER_PS, env={"USER_ID": identifier})
        except AdDirectoryUnreachable as e:
            if "not_found" in str(e):
                raise AdUserNotFound(identifier) from e
            raise
        return UserRecord(
            distinguished_name=payload["distinguishedName"],
            display_name=payload.get("displayName") or identifier,
            email=payload.get("mail"),
            title=payload.get("title"),
            phone=payload.get("telephoneNumber"),
            enabled=bool(payload.get("enabled", True)),
            manager_dn=payload.get("manager"),
            sam_account_name=payload.get("sAMAccountName"),
        )

    def sync_group(self, group_dn: str, manager_dn: str | None) -> GroupSyncResult:
        env = {"GROUP_DN": group_dn, "MANAGER_DN": manager_dn or ""}
        payload = self._run_ps(_SYNC_GROUP_PS, env=env)
        g_raw = payload["group"]
        group = GroupRecord(
            distinguished_name=g_raw["distinguishedName"],
            name=g_raw["name"],
            description=g_raw.get("description"),
            managed_by_dn=g_raw.get("managedBy"),
        )
        manager: UserRecord | None = None
        m_raw = payload.get("manager")
        if m_raw:
            manager = UserRecord(
                distinguished_name=m_raw["distinguishedName"],
                display_name=m_raw.get("displayName") or "",
                email=m_raw.get("mail"),
                title=m_raw.get("title"),
                phone=m_raw.get("telephoneNumber"),
                enabled=bool(m_raw.get("enabled", True)),
                manager_dn=m_raw.get("manager"),
            )
        members_raw = payload.get("members") or []
        # PowerShell collapses single-item arrays to a scalar via ConvertTo-Json
        # unless -AsArray is specified (PS 7+). Defend both shapes.
        if isinstance(members_raw, dict):
            members_raw = [members_raw]
        members = [
            MemberRecord(
                user_dn=m["distinguishedName"],
                display_name=m.get("displayName") or m["distinguishedName"],
                email=m.get("mail"),
                title=m.get("title"),
                phone=m.get("telephoneNumber"),
                enabled=bool(m.get("enabled", True)),
                manager_dn=m.get("managerDn"),
                manager_name=m.get("managerName"),
            )
            for m in members_raw
            if m.get("distinguishedName")
        ]
        chain_raw = payload.get("managerChain") or []
        if isinstance(chain_raw, str):
            chain_raw = [chain_raw]
        return GroupSyncResult(
            group=group, manager=manager, members=members, manager_chain=list(chain_raw)
        )

"""Fixture-backed AD source.

Returns canned data so the UI and route logic can be exercised without a
real Windows jumpbox. Activated by setting ``cfg.directory.adapter = "mock"``
in config.yaml. Tests instantiate it directly.

The fixture mimics a small org:

    Acme (CEO)
    ├── Engineering (Alice, reports to CEO)
    │   ├── Platform (Bob, reports to Alice)
    │   └── Frontend (Carol, reports to Alice)
    └── Operations (Dave, reports to CEO)
        └── Helpdesk (Eve, reports to Dave; one disabled member)

Each "department" is a group + a manager. The helper methods below
synthesize sync responses on demand so tests can exercise hierarchy
inference without a database round trip.
"""
from __future__ import annotations

from dosm.directory.adapters import (
    AdDirectorySource,
    AdGroupNotFound,
    AdUserNotFound,
    GroupRecord,
    GroupSyncResult,
    MemberRecord,
    UserRecord,
)

_USERS: dict[str, UserRecord] = {
    "CN=Eric CEO,OU=People,DC=acme,DC=local": UserRecord(
        distinguished_name="CN=Eric CEO,OU=People,DC=acme,DC=local",
        display_name="Eric CEO",
        email="eric@acme.local",
        title="Chief Executive",
        enabled=True,
        manager_dn=None,
        sam_account_name="eric",
    ),
    "CN=Alice Eng,OU=People,DC=acme,DC=local": UserRecord(
        distinguished_name="CN=Alice Eng,OU=People,DC=acme,DC=local",
        display_name="Alice Eng",
        email="alice@acme.local",
        title="VP Engineering",
        enabled=True,
        manager_dn="CN=Eric CEO,OU=People,DC=acme,DC=local",
        sam_account_name="alice",
    ),
    "CN=Bob Plat,OU=People,DC=acme,DC=local": UserRecord(
        distinguished_name="CN=Bob Plat,OU=People,DC=acme,DC=local",
        display_name="Bob Plat",
        email="bob@acme.local",
        title="Platform Lead",
        enabled=True,
        manager_dn="CN=Alice Eng,OU=People,DC=acme,DC=local",
        sam_account_name="bob",
    ),
    "CN=Carol Front,OU=People,DC=acme,DC=local": UserRecord(
        distinguished_name="CN=Carol Front,OU=People,DC=acme,DC=local",
        display_name="Carol Front",
        email="carol@acme.local",
        title="Frontend Lead",
        enabled=True,
        manager_dn="CN=Alice Eng,OU=People,DC=acme,DC=local",
        sam_account_name="carol",
    ),
    "CN=Dave Ops,OU=People,DC=acme,DC=local": UserRecord(
        distinguished_name="CN=Dave Ops,OU=People,DC=acme,DC=local",
        display_name="Dave Ops",
        email="dave@acme.local",
        title="Director of Operations",
        enabled=True,
        manager_dn="CN=Eric CEO,OU=People,DC=acme,DC=local",
        sam_account_name="dave",
    ),
    "CN=Eve Help,OU=People,DC=acme,DC=local": UserRecord(
        distinguished_name="CN=Eve Help,OU=People,DC=acme,DC=local",
        display_name="Eve Help",
        email="eve@acme.local",
        title="Helpdesk Lead",
        enabled=True,
        manager_dn="CN=Dave Ops,OU=People,DC=acme,DC=local",
        sam_account_name="eve",
    ),
    "CN=Frank Junior,OU=People,DC=acme,DC=local": UserRecord(
        distinguished_name="CN=Frank Junior,OU=People,DC=acme,DC=local",
        display_name="Frank Junior",
        email="frank@acme.local",
        title="Helpdesk",
        enabled=True,
        manager_dn="CN=Eve Help,OU=People,DC=acme,DC=local",
        sam_account_name="frank",
    ),
    "CN=Grace Disabled,OU=People,DC=acme,DC=local": UserRecord(
        distinguished_name="CN=Grace Disabled,OU=People,DC=acme,DC=local",
        display_name="Grace Disabled",
        email="grace@acme.local",
        title="Helpdesk (former)",
        enabled=False,  # demonstrates strikethrough rendering
        manager_dn="CN=Eve Help,OU=People,DC=acme,DC=local",
        sam_account_name="grace",
    ),
}


_GROUPS: dict[str, tuple[GroupRecord, list[str]]] = {
    # name to (group, list of member DNs)
    "engineering": (
        GroupRecord(
            distinguished_name="CN=Engineering,OU=Groups,DC=acme,DC=local",
            name="Engineering",
            description="All engineers",
            managed_by_dn="CN=Alice Eng,OU=People,DC=acme,DC=local",
        ),
        [
            "CN=Alice Eng,OU=People,DC=acme,DC=local",
            "CN=Bob Plat,OU=People,DC=acme,DC=local",
            "CN=Carol Front,OU=People,DC=acme,DC=local",
        ],
    ),
    "platform": (
        GroupRecord(
            distinguished_name="CN=Platform,OU=Groups,DC=acme,DC=local",
            name="Platform",
            description="Infra & SRE",
            managed_by_dn="CN=Bob Plat,OU=People,DC=acme,DC=local",
        ),
        ["CN=Bob Plat,OU=People,DC=acme,DC=local"],
    ),
    "frontend": (
        GroupRecord(
            distinguished_name="CN=Frontend,OU=Groups,DC=acme,DC=local",
            name="Frontend",
            description="Web app team",
            managed_by_dn="CN=Carol Front,OU=People,DC=acme,DC=local",
        ),
        ["CN=Carol Front,OU=People,DC=acme,DC=local"],
    ),
    "operations": (
        GroupRecord(
            distinguished_name="CN=Operations,OU=Groups,DC=acme,DC=local",
            name="Operations",
            description="Ops org",
            managed_by_dn="CN=Dave Ops,OU=People,DC=acme,DC=local",
        ),
        ["CN=Dave Ops,OU=People,DC=acme,DC=local"],
    ),
    "helpdesk": (
        GroupRecord(
            distinguished_name="CN=Helpdesk,OU=Groups,DC=acme,DC=local",
            name="Helpdesk",
            description="L1 support",
            managed_by_dn="CN=Eve Help,OU=People,DC=acme,DC=local",
        ),
        [
            "CN=Eve Help,OU=People,DC=acme,DC=local",
            "CN=Frank Junior,OU=People,DC=acme,DC=local",
            "CN=Grace Disabled,OU=People,DC=acme,DC=local",
        ],
    ),
}


def _user_to_member(u: UserRecord) -> MemberRecord:
    mgr_name = None
    if u.manager_dn and u.manager_dn in _USERS:
        mgr_name = _USERS[u.manager_dn].display_name
    return MemberRecord(
        user_dn=u.distinguished_name,
        display_name=u.display_name,
        email=u.email,
        title=u.title,
        phone=u.phone,
        enabled=u.enabled,
        manager_dn=u.manager_dn,
        manager_name=mgr_name,
    )


class MockSource(AdDirectorySource):
    """Returns the fixture data above. No I/O, no errors except the
    deliberate "not found" path used by tests."""

    def test_connection(self) -> str:
        return "acme.local"

    def resolve_group(self, group_name: str) -> GroupRecord:
        key = group_name.strip().lower()
        if key in _GROUPS:
            return _GROUPS[key][0]
        # also accept "CN=Engineering" or full DN
        for g, _ in _GROUPS.values():
            if group_name in (g.name, g.distinguished_name):
                return g
        raise AdGroupNotFound(group_name)

    def resolve_user(self, identifier: str) -> UserRecord:
        ident = identifier.strip()
        for u in _USERS.values():
            if ident in (
                u.distinguished_name,
                u.display_name,
                u.sam_account_name,
                u.email,
            ):
                return u
        raise AdUserNotFound(identifier)

    def sync_group(self, group_dn: str, manager_dn: str | None) -> GroupSyncResult:
        # Find the group by DN.
        group: GroupRecord | None = None
        member_dns: list[str] = []
        for g, dns in _GROUPS.values():
            if g.distinguished_name == group_dn:
                group = g
                member_dns = dns
                break
        if group is None:
            raise AdGroupNotFound(group_dn)

        # Materialize members; skip stale DNs silently like the real adapter.
        members = [_user_to_member(_USERS[d]) for d in member_dns if d in _USERS]

        manager: UserRecord | None = None
        chain: list[str] = []
        if manager_dn and manager_dn in _USERS:
            manager = _USERS[manager_dn]
            cur = manager
            depth = 0
            while cur and cur.manager_dn and depth < 20:
                chain.append(cur.manager_dn)
                cur = _USERS.get(cur.manager_dn)
                if cur is None:
                    break
                depth += 1
        return GroupSyncResult(
            group=group, manager=manager, members=members, manager_chain=chain
        )

"""Active Directory directory source.

DOSM never binds LDAP directly; it goes through a Windows jumpbox and runs
PowerShell ActiveDirectory cmdlets there over WinRM. The adapter pattern
matches the rest of the codebase (monitoring, pipelines, metrics).
"""
from dosm.directory.adapters import (
    AdDirectoryError,
    AdDirectoryUnreachable,
    AdGroupNotFound,
    AdUserNotFound,
    GroupSyncResult,
    MemberRecord,
    UserRecord,
    get_directory_source,
)

__all__ = [
    "AdDirectoryError",
    "AdDirectoryUnreachable",
    "AdGroupNotFound",
    "AdUserNotFound",
    "GroupSyncResult",
    "MemberRecord",
    "UserRecord",
    "get_directory_source",
]

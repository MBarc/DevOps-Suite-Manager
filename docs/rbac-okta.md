# DOSM Access Control — Roles, AD Groups, and Okta

This document explains DOSM's role-based access control (RBAC): what each
permission type can do, how an Active Directory group is correlated to a
permission type, and how Okta single sign-on fits in (including exactly what
information DOSM receives and stores).

It reflects the implementation on the `feature/rbac-okta-ad` branch
(`dosm/auth/deps.py`, `dosm/auth/okta.py`, `dosm/credentials/access.py`,
`dosm/config.py`).

## 1. The model in one paragraph

DOSM separates **authentication** (who you are) from **authorization** (what you
can do). Okta proves identity and tells us which **AD groups** you belong to;
DOSM maps those groups to exactly one **role**; the role determines which actions
you can take. AD and Okta know nothing about DOSM's hosts, pipelines, or
credentials — all permission logic lives in DOSM, keyed off a single role string
on the user record.

```
Active Directory ──(federates groups)──> Okta ──(groups claim in ID token)──> DOSM
                                                                                │
                                          group_role_map + highest-wins ────────┘
                                                                                ▼
                                                              role: viewer | operator | admin
                                                                                ▼
                                                       require_role() gates on every action
```

## 2. Permission types (roles)

There are three roles on a strict ladder — each one inherits everything below it.
Enforced by `require_role(minimum)` in `dosm/auth/deps.py`, which compares a
numeric rank:

```python
ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}
```

### `viewer` (rank 0) — read-only

Can **see** the operational picture but change nothing.

- View Hosts, Pipelines, Monitoring, Certificates, Docs, Org directory.
- View the credential **list** (names only — secret values are never rendered to
  anyone) limited to shared + their own.
- Use **LLM chat** (grounded Q&A over the docs index) and stream host **metrics**.
- Cannot create/edit/delete anything; cannot open terminals, transfer files,
  connect to hosts, run pipelines, or execute agent actions. Write buttons are
  hidden in the UI, and the server returns **403** if they try anyway.

### `operator` (rank 1) — day-to-day ops

Everything a viewer can do, **plus** the actual work:

- Create / edit / delete **hosts**; ping hosts.
- Create / edit / delete **credentials** (and choose private vs shared — see §3).
- Create / edit / delete and **run pipelines**; refresh runs.
- **Connect** to hosts through Apache Guacamole (SSH/RDP/VNC).
- Drive **agent mode** — approve / reject / group-approve the plan cards the AI
  proposes (the actual `ssh_exec` / `run_pipeline` execution).
- Cannot touch global configuration, manage users, or use the most sensitive
  surfaces (see admin).

### `admin` (rank 2) — full control

Everything an operator can do, **plus** the privileged surfaces:

- In-app **Terminals** (raw PowerShell/cmd/bash on the DOSM host, with session
  recording).
- **File transfer** browser (FTP/FTPS/SFTP).
- Global **Settings** and the CLI-tool catalog.
- **Certificate source** management (Azure KV / AWS ACM / GCP) and **Org
  directory** configuration.
- **User & role management** (`dosm user set-role`).
- Sees **all** credentials regardless of private/shared, for audit.

### Quick reference

| Capability | viewer | operator | admin |
|---|:--:|:--:|:--:|
| View hosts / pipelines / monitoring / certs / docs / org | ✅ | ✅ | ✅ |
| LLM chat, live metrics | ✅ | ✅ | ✅ |
| Create/edit/delete hosts · run pipelines · connect (Guacamole) | ❌ | ✅ | ✅ |
| Manage credentials (incl. private/shared flag) | ❌ | ✅ | ✅ |
| Approve/execute AI agent plan cards | ❌ | ✅ | ✅ |
| Terminals · file transfer · Settings · cert sources · org config | ❌ | ❌ | ✅ |
| Manage users & roles · see all private credentials | ❌ | ❌ | ✅ |

> **Why these lines?** The boundaries preserve DOSM's existing security posture —
> terminals and file transfer were already admin-only — while closing real gaps
> where host/credential/pipeline mutations used to be allowed for *anyone logged
> in*.

## 3. The one per-record exception: private vs shared credentials

Roles gate **actions**; they don't carve up the inventory — hosts and pipelines
are a single shared fleet. The deliberate exception is **credentials**, which
each carry a visibility flag (the "share with everyone / keep to myself" choice):

- `shared` (default) — any operator/admin can see and use it.
- `private` — visible and usable only by its **owner** and **admins**.

The rule lives in one place (`dosm/credentials/access.py`) and is applied to the
list, the host-form credential picker, the detail/edit/delete routes (which
return **404**, not 403, so a private credential's existence doesn't leak), and a
**use-time guard**: if a shared host is pinned to someone else's private
credential, connecting returns a clear 403 instead of failing opaquely.

## 4. Correlating an AD group to a permission type

The mapping is explicit, declarative configuration — DOSM never guesses.

**Where it's defined** — `$DOSM_HOME/config.yaml`:

```yaml
rbac:
  default_role: viewer            # used when no group matches
  group_role_map:
    DOSM-Admins:     admin
    DOSM-Operators:  operator
    DOSM-ReadOnly:   viewer
```

The **keys** are group names exactly as they appear in the token's `groups`
claim (with AD-federated Okta, those are your AD security groups). The **values**
are DOSM roles.

**How a role is chosen** (`map_groups_to_role` in `dosm/auth/okta.py`):

1. Look at every group the user is in.
2. Keep only the ones present in `group_role_map`.
3. **Highest role wins** — someone in both `DOSM-Operators` and `DOSM-Admins`
   becomes `admin`.
4. If the user is in **no** mapped group, they get `default_role` (`viewer`).

**When it's applied:** on **every login**, not just the first. So moving a user
between AD groups takes effect the next time they sign in — no manual DOSM
change, no de-provisioning step. The role lands on the user row and an audit
entry (`rbac.role_assigned`) records any change.

Inspect the live mapping any time with `dosm rbac show-mapping`.

## 5. How Okta integrates, and what we get from it

DOSM is a standard **OIDC Authorization Code client with PKCE** — it isn't
Okta-specific, so any compliant provider works, but the intended deployment is
Okta federating AD.

**The flow:**

1. User clicks **"Sign in with Okta"** → `GET /auth/okta/login`. DOSM fetches
   Okta's discovery document, generates `state` + `nonce` + a PKCE challenge
   (stashed in the session), and redirects to Okta.
2. User authenticates with Okta (password, MFA, whatever your org enforces — DOSM
   never sees credentials).
3. Okta redirects back to `GET /auth/okta/callback` with a one-time code. DOSM
   validates `state`, exchanges the code (+ PKCE verifier + client secret) for
   tokens, then **validates the ID token**: signature against Okta's JWKS, plus
   `issuer`, `audience`, `expiry`, and `nonce`.
4. DOSM reads the claims, maps groups → role, provisions/updates the local user,
   and sets its own session cookie.

**What information we extract from the ID token:**

| Claim | Used for | Stored as |
|---|---|---|
| `sub` | Stable identity key (survives email/name changes) | `User.okta_sub` (unique) |
| `email` | Contact / display | `User.email` |
| `preferred_username` (→ email → sub) | Login name shown in UI | `User.username` |
| `name` | Friendly display name | `User.display_name` |
| `groups` | **Authorization** — mapped to role | drives `User.role` (not stored raw) |

We also record `auth_provider = "okta"` and `last_login`, and write an
unverifiable sentinel password hash (`!okta`) so SSO accounts can never log in
via the local form.

**What we deliberately do *not* take or keep:**

- No password — ever.
- No access/refresh tokens are persisted; the ID token is used transiently during
  the handshake and discarded. DOSM's own session cookie carries the logged-in
  state thereafter.
- No AD attributes beyond group membership (no OU, manager, phone, etc. — that's
  the separate Org-directory feature, via a WinRM jumpbox).
- The Okta **client secret** lives only in the secrets backend
  (`okta/client_secret`), never in `config.yaml`.

**Trust boundary:** Okta is authoritative for *identity and group membership*;
DOSM is authoritative for *what a role can do*. The two are bridged only by
`group_role_map`. This means you administer access centrally in AD/Okta (add a
person to `DOSM-Operators`) without touching DOSM, and the **local break-glass
admin** still works if Okta is ever unreachable.

## 6. End-to-end example

> Priya is in AD groups `DOSM-Operators` and `Finance`. Okta federates these and
> includes them in her ID token's `groups` claim. She signs in. DOSM validates
> the token, sees `["DOSM-Operators", "Finance"]`, finds only `DOSM-Operators` in
> the map → role **operator** (`Finance` is ignored). She can manage hosts, run
> pipelines, and connect to servers — but the Settings and Terminals pages aren't
> even in her sidebar. Later she's promoted and added to `DOSM-Admins` in AD;
> next sign-in, DOSM recomputes her role to **admin** and audit-logs the change.
> No DOSM-side edit was needed.

## 7. Configuration & operations cheat-sheet

```yaml
# $DOSM_HOME/config.yaml
okta:
  enabled: true
  issuer: "https://your-org.okta.com/oauth2/default"
  client_id: "<okta-app-client-id>"
  # redirect_path defaults to /auth/okta/callback
  # scopes default to [openid, profile, email, groups]
  # groups_claim defaults to "groups"
rbac:
  default_role: viewer
  group_role_map:
    DOSM-Admins: admin
    DOSM-Operators: operator
    DOSM-ReadOnly: viewer
```

```bash
dosm secret set okta/client_secret   # store the Okta client secret (never in YAML)
dosm okta test                       # check discovery + JWKS + secret presence
dosm rbac show-mapping               # print the group -> role mapping
dosm user set-role <user> <role>     # local break-glass role change (audited)
```

See `docs/ROADMAP.md` (Phases 21 / 21b) for the design rationale and the current
known limitation: the Okta flow is exercised offline with a self-signed token but
not yet validated against a live Okta tenant.

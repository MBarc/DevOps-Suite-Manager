"""Typed pipeline inputs.

The pipeline `inputs_schema` is a JSON list of rows describing each input
the workflow accepts: name, type (string/boolean/choice/number), options
(for choice), default, required, description.

This module owns:
  - parsing the row-based pipeline-edit form into a normalized schema list
  - rendering normalized values back from a typed run form, with validation
  - per-provider wire-format coercion (GitHub wants all-strings)

Schemas missing the `type` key are treated as `string` so older pipelines
that pre-date typed inputs keep working unchanged.
"""
from __future__ import annotations

from typing import Any

SUPPORTED_TYPES = ("string", "boolean", "choice", "number")


class InputValidationError(ValueError):
    """Raised when a typed run form fails server-side validation."""


def _norm_type(t: str | None) -> str:
    t = (t or "string").strip().lower()
    return t if t in SUPPORTED_TYPES else "string"


def _split_options(raw: str | None) -> list[str]:
    if not raw:
        return []
    # Accept both newline- and comma-separated for forgiveness; the form
    # serializes as comma-separated but a paste from elsewhere shouldn't break.
    parts: list[str] = []
    for chunk in str(raw).replace("\n", ",").split(","):
        s = chunk.strip()
        if s:
            parts.append(s)
    return parts


def parse_schema_form(form: dict) -> list[dict]:
    """Read the row-based schema editor out of a posted form.

    Field names are `schema_<field>__<index>` where <field> is name, type,
    options, default, required, desc and <index> is an arbitrary token
    (the JS just uses incrementing integers). Rows with an empty name are
    dropped silently so the user can leave a blank trailing row.
    """
    indices: set[str] = set()
    prefix = "schema_name__"
    for key in form.keys():
        if key.startswith(prefix):
            indices.add(key[len(prefix):])

    rows: list[dict] = []
    for idx in sorted(indices, key=lambda s: (len(s), s)):
        name = (form.get(f"schema_name__{idx}") or "").strip()
        if not name:
            continue
        t = _norm_type(form.get(f"schema_type__{idx}"))
        row: dict = {"name": name, "type": t}
        opts = _split_options(form.get(f"schema_options__{idx}"))
        if t == "choice" and opts:
            row["options"] = opts
        default = (form.get(f"schema_default__{idx}") or "").strip()
        if default:
            row["default"] = default
        if (form.get(f"schema_required__{idx}") or "").strip():
            row["required"] = True
        desc = (form.get(f"schema_desc__{idx}") or "").strip()
        if desc:
            row["description"] = desc
        rows.append(row)
    return rows


def normalize_schema(schema: list[dict] | None) -> list[dict]:
    """Defensive read of a stored schema - fills in defaults so templates
    and coerce_run_inputs can rely on `type` being present and valid."""
    out: list[dict] = []
    for row in schema or []:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or "").strip()
        if not name:
            continue
        t = _norm_type(row.get("type"))
        norm: dict = {"name": name, "type": t}
        if t == "choice":
            opts = row.get("options") or []
            if isinstance(opts, str):
                opts = _split_options(opts)
            norm["options"] = [str(o) for o in opts if str(o).strip()]
        if "default" in row and row.get("default") not in (None, ""):
            norm["default"] = row["default"]
        if row.get("required"):
            norm["required"] = True
        if row.get("description"):
            norm["description"] = row["description"]
        out.append(norm)
    return out


def coerce_run_inputs(schema: list[dict], form: dict) -> dict[str, Any]:
    """Walk the schema and pull each input out of the run form, coercing to
    its declared type and validating required-ness / option membership.

    Form field names are `input__<input_name>`. Booleans use the standard
    HTML checkbox convention: present (any value) -> True, absent -> False.

    Raises InputValidationError on the first failure with a user-readable
    message naming the input.
    """
    schema = normalize_schema(schema)
    out: dict[str, Any] = {}
    for row in schema:
        name = row["name"]
        t = row["type"]
        field = f"input__{name}"
        raw = form.get(field)

        if t == "boolean":
            out[name] = bool(raw)
            continue

        s = (raw or "").strip() if isinstance(raw, str) else ""
        if not s:
            if row.get("required"):
                raise InputValidationError(f"{name!r} is required")
            # Skip unset optional inputs entirely so the provider sees only
            # the keys the user actually filled in (matters for GitHub:
            # workflow defaults kick in when the key is absent).
            continue

        if t == "choice":
            opts = row.get("options") or []
            if opts and s not in opts:
                raise InputValidationError(
                    f"{name!r} must be one of {opts!r} (got {s!r})"
                )
            out[name] = s
        elif t == "number":
            try:
                # Prefer int for clean integer strings so the agent + UI
                # don't end up displaying "1.0" for "1".
                out[name] = int(s) if s.lstrip("-").isdigit() else float(s)
            except ValueError as e:
                raise InputValidationError(f"{name!r} must be a number (got {s!r})") from e
        else:  # string
            out[name] = s
    return out


def validate_payload_values(schema: list[dict], values: dict[str, Any]) -> list[str]:
    """Check already-typed payload ``values`` against the current ``schema``.

    Unlike :func:`coerce_run_inputs` (which reads string form fields), this
    validates a stored dict directly - used to detect *drift* when a pipeline's
    schema changed after a payload was saved. Returns a list of human-readable
    problems (empty list = the payload still matches the schema)."""
    schema = normalize_schema(schema)
    errors: list[str] = []
    known = {row["name"] for row in schema}

    for row in schema:
        name, t = row["name"], row["type"]
        present = name in values and values[name] not in (None, "")
        if not present:
            if row.get("required"):
                errors.append(f"missing required input {name!r}")
            continue
        v = values[name]
        if t == "choice":
            opts = row.get("options") or []
            if opts and str(v) not in opts:
                errors.append(f"{name!r}={v!r} is not one of {opts!r}")
        elif t == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                try:
                    float(str(v))
                except (TypeError, ValueError):
                    errors.append(f"{name!r}={v!r} is not a number")

    # Inputs the payload carries that the schema no longer declares.
    for key in values:
        if key != "__raw__" and key not in known:
            errors.append(f"input {key!r} is no longer in the pipeline schema")
    return errors


def _stringify_for_wire(inputs: dict[str, Any]) -> dict[str, str]:
    """Generic string-coercion: bool"true"/"false", None dropped, everything
    else to str(v). Lowercase booleans are the common-denominator convention
    (GitHub / Octopus / shells all expect them)."""
    out: dict[str, str] = {}
    for k, v in (inputs or {}).items():
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif v is None:
            continue
        else:
            out[k] = str(v)
    return out


def coerce_for_github(inputs: dict[str, Any]) -> dict[str, str]:
    """GitHub workflow_dispatch wants string values on the wire - booleans
    become the literal strings "true"/"false", numbers become str(n)."""
    return _stringify_for_wire(inputs)


def coerce_for_octopus(inputs: dict[str, Any]) -> dict[str, str]:
    """Octopus FormValues is a flat dict of strings (deployment prompts).
    Same rules as GitHub: booleans lowercase, None dropped, everything else
    str()-coerced."""
    return _stringify_for_wire(inputs)


def split_azure_devops_inputs(
    inputs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Split inputs for the Azure DevOps Pipelines API.

    Plain keys to templateParameters with **native types preserved** (ADO's
    runtime parameters are typed: string / boolean / number).

    Keys prefixed with ``var.`` to runtime variables. ADO's variable wire
    format wraps each as ``{"value": "..."}`` and the value is documented
    as a string, so we string-coerce that bucket the same way as GitHub.

    Returns ``(template_params, variables)``. The caller wraps the
    variables dict into the JSON:API shape ADO expects.
    """
    template_params: dict[str, Any] = {}
    variables: dict[str, str] = {}
    for k, v in (inputs or {}).items():
        if v is None:
            continue
        if k.startswith("var."):
            name = k[4:]
            if isinstance(v, bool):
                variables[name] = "true" if v else "false"
            else:
                variables[name] = str(v)
        else:
            template_params[k] = v
    return template_params, variables

"""Generate Markdown reference docs for the dosm CLI.

Reads Typer/Click introspection from ``dosm.cli.app`` and AST-parses
``dosm/cli.py`` for ``raise typer.Exit(N)`` calls to extract per-command
exit codes. Splices hand-written prose from ``docs/cli/prose/<group>.md``
into the generated pages.

Outputs one file per command group plus ``top-level.md`` into
``docs/cli/_generated/``. Idempotent — committed output is the source of
truth checked by CI.

Run:
    python scripts/gen_cli_docs.py

CI fails if running this leaves ``docs/cli/_generated/`` dirty.
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import click
import typer

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dosm.cli import app  # noqa: E402

OUT_DIR = REPO_ROOT / "docs" / "cli" / "_generated"
PROSE_DIR = REPO_ROOT / "docs" / "cli" / "prose"
CLI_PY = REPO_ROOT / "dosm" / "cli.py"

# Order groups appear in. Matches the order they're registered in cli.py.
GROUP_ORDER = [
    "db",
    "user",
    "secret",
    "credential",
    "docs",
    "guacamole",
    "pipelines",
    "folder",
    "org",
]

PROSE_SECTIONS = ("When to use", "Examples", "Gotchas")

# Slug of the Folder row that all generated pages associate to. The
# runtime helper (`dosm/docs_index/cli_reference.py`) creates the row
# with this slug.
FOLDER_SLUG = "dosm-cli"


# ---------------------------------------------------------------------------
# AST: extract typer.Exit codes per function name
# ---------------------------------------------------------------------------


@dataclass
class ExitInfo:
    codes: set[int] = field(default_factory=set)
    has_dynamic: bool = False  # `raise typer.Exit(some_var)` or non-literal


def parse_exit_codes(path: Path) -> dict[str, ExitInfo]:
    """Map function name -> ExitInfo by scanning ``raise typer.Exit(...)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, ExitInfo] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        info = ExitInfo()
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Raise) or sub.exc is None:
                continue
            call = sub.exc if isinstance(sub.exc, ast.Call) else None
            if call is None:
                continue
            # Match typer.Exit(...) — the func is an Attribute "Exit" on "typer".
            func = call.func
            is_typer_exit = (
                isinstance(func, ast.Attribute)
                and func.attr == "Exit"
                and isinstance(func.value, ast.Name)
                and func.value.id == "typer"
            )
            if not is_typer_exit:
                continue
            # Pull literal int from positional or `code=` keyword.
            literal: int | None = None
            if call.args:
                arg = call.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                    literal = arg.value
            for kw in call.keywords:
                if kw.arg == "code" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                    literal = kw.value.value
            if literal is None:
                info.has_dynamic = True
            else:
                info.codes.add(literal)
        if info.codes or info.has_dynamic:
            out[node.name] = info
    return out


# ---------------------------------------------------------------------------
# Prose loading
# ---------------------------------------------------------------------------


def load_prose(group: str) -> dict[str, str]:
    """Read prose/<group>.md and slice it by ``## <Section>`` headings.

    Returns {section_name: body}. Missing sections come back as empty strings.
    """
    path = PROSE_DIR / f"{group}.md"
    sections = {name: "" for name in PROSE_SECTIONS}
    if not path.exists():
        return sections
    text = path.read_text(encoding="utf-8")
    # Split on level-2 headings.
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    # parts[0] is preamble (ignored), then alternating heading, body, heading, body...
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if heading in sections:
            sections[heading] = body
    return sections


# ---------------------------------------------------------------------------
# Click introspection
# ---------------------------------------------------------------------------


@dataclass
class ParamRow:
    name: str
    kind: str  # "argument" | "option"
    type_name: str
    required: bool
    default: str
    help: str
    flag: str  # rendered flag string e.g. "--force"


def _type_name(p: click.Parameter) -> str:
    t = p.type
    name = getattr(t, "name", None) or t.__class__.__name__
    if isinstance(t, click.Choice):
        return f"choice ({'|'.join(t.choices)})"
    return str(name)


def _default(p: click.Parameter) -> str:
    if p.default in (None, ()):
        return "—"
    if isinstance(p.default, bool):
        return str(p.default).lower()
    return repr(p.default)


def _flag_string(p: click.Parameter) -> str:
    if isinstance(p, click.Argument):
        return f"`<{p.name.upper()}>`"
    opts = list(getattr(p, "opts", []) or [])
    secondary = list(getattr(p, "secondary_opts", []) or [])
    return ", ".join(f"`{o}`" for o in opts + secondary) or f"`--{p.name}`"


def _params(cmd: click.Command) -> tuple[list[ParamRow], list[ParamRow]]:
    args, opts = [], []
    for p in cmd.params:
        if p.name == "help":
            continue
        row = ParamRow(
            name=p.name or "",
            kind="argument" if isinstance(p, click.Argument) else "option",
            type_name=_type_name(p),
            required=bool(getattr(p, "required", False)),
            default=_default(p),
            help=(getattr(p, "help", None) or "").strip(),
            flag=_flag_string(p),
        )
        (args if row.kind == "argument" else opts).append(row)
    return args, opts


def _synopsis(cmd_path: list[str], cmd: click.Command) -> str:
    parts = ["dosm", *cmd_path]
    args, opts = _params(cmd)
    if opts:
        parts.append("[OPTIONS]")
    for a in args:
        token = f"<{a.name.upper()}>"
        parts.append(token if a.required else f"[{token}]")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_param_table(rows: list[ParamRow]) -> str:
    if not rows:
        return "_None._"
    lines = ["| Name | Type | Required | Default | Description |", "| --- | --- | --- | --- | --- |"]
    for r in rows:
        help_text = r.help.replace("|", "\\|") or "—"
        flag = r.flag.replace("|", "\\|")
        lines.append(
            f"| {flag} | {r.type_name} | {'yes' if r.required else 'no'} | `{r.default}` | {help_text} |"
        )
    return "\n".join(lines)


def _render_exit_codes(info: ExitInfo | None) -> str:
    lines = ["- `0` — success."]
    if info is None:
        return "\n".join(lines)
    for code in sorted(info.codes):
        if code == 0:
            continue
        lines.append(f"- `{code}` — see command description above for the conditions that trigger this.")
    if info.has_dynamic:
        lines.append("- Other non-zero codes may propagate from underlying integrations.")
    return "\n".join(lines)


def _render_command(
    cmd_path: list[str],
    cmd: click.Command,
    exit_codes: dict[str, ExitInfo],
) -> str:
    name = " ".join(["dosm", *cmd_path])
    args, opts = _params(cmd)
    desc = (cmd.help or cmd.short_help or "").strip() or "_(no description)_"
    callback = cmd.callback
    fn_name = getattr(callback, "__name__", "") if callback else ""
    info = exit_codes.get(fn_name)
    blocks = [
        f"### `{name}`",
        "",
        f"**Synopsis:** `{_synopsis(cmd_path, cmd)}`",
        "",
        desc,
        "",
        "**Arguments:**",
        "",
        _render_param_table(args),
        "",
        "**Options:**",
        "",
        _render_param_table(opts),
        "",
        "**Exit codes:**",
        "",
        _render_exit_codes(info),
        "",
    ]
    return "\n".join(blocks)


def _render_page(
    title: str,
    summary: str,
    prose: dict[str, str],
    cmd_blocks: list[str],
) -> str:
    parts = [
        "---",
        f"folder: {FOLDER_SLUG}",
        f"title: {title}",
        "---",
        "",
        f"# `{title}`",
        "",
        "<!-- DO NOT EDIT — generated by scripts/gen_cli_docs.py -->",
        "",
        f"> {summary}" if summary else "",
        "",
    ]
    if prose.get("When to use"):
        parts += ["## When to use", "", prose["When to use"], ""]
    parts += ["## Commands", "", *cmd_blocks]
    if prose.get("Examples"):
        parts += ["## Examples", "", prose["Examples"], ""]
    if prose.get("Gotchas"):
        parts += ["## Gotchas", "", prose["Gotchas"], ""]
    # Collapse stray blank lines.
    text = "\n".join(p for p in parts if p is not None)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text


# ---------------------------------------------------------------------------
# Top-level + group page builders
# ---------------------------------------------------------------------------


def _click_obj() -> click.Group:
    obj = typer.main.get_command(app)
    if not isinstance(obj, click.Group):
        raise SystemExit("dosm.cli.app is not a click.Group — generator needs updating")
    return obj


def build_top_level(root: click.Group, exit_codes: dict[str, ExitInfo]) -> str:
    prose = load_prose("top-level")
    blocks = []
    for cmd_name in sorted(root.commands):
        cmd = root.commands[cmd_name]
        if isinstance(cmd, click.Group):
            continue
        blocks.append(_render_command([cmd_name], cmd, exit_codes))
    return _render_page(
        title="dosm",
        summary=(root.help or "").strip(),
        prose=prose,
        cmd_blocks=blocks,
    )


def build_group(name: str, group: click.Group, exit_codes: dict[str, ExitInfo]) -> str:
    prose = load_prose(name)
    blocks = []
    for cmd_name in sorted(group.commands):
        cmd = group.commands[cmd_name]
        if isinstance(cmd, click.Group):
            continue
        blocks.append(_render_command([name, cmd_name], cmd, exit_codes))
    return _render_page(
        title=f"dosm {name}",
        summary=(group.help or "").strip(),
        prose=prose,
        cmd_blocks=blocks,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    root = _click_obj()
    exit_codes = parse_exit_codes(CLI_PY)

    written: list[Path] = []

    top_md = build_top_level(root, exit_codes)
    top_path = OUT_DIR / "top-level.md"
    top_path.write_text(top_md, encoding="utf-8", newline="\n")
    written.append(top_path)

    seen: set[str] = set()
    for name in GROUP_ORDER:
        sub = root.commands.get(name)
        if not isinstance(sub, click.Group):
            print(f"warning: group {name!r} not found in CLI", file=sys.stderr)
            continue
        seen.add(name)
        out_path = OUT_DIR / f"{name}.md"
        out_path.write_text(build_group(name, sub, exit_codes), encoding="utf-8", newline="\n")
        written.append(out_path)

    for cmd_name, cmd in root.commands.items():
        if isinstance(cmd, click.Group) and cmd_name not in seen:
            out_path = OUT_DIR / f"{cmd_name}.md"
            out_path.write_text(build_group(cmd_name, cmd, exit_codes), encoding="utf-8", newline="\n")
            written.append(out_path)
            print(f"note: emitted {cmd_name}.md (not in GROUP_ORDER — add it to scripts/gen_cli_docs.py)")

    # Drop generated files for groups that no longer exist.
    valid_names = {p.name for p in written}
    for stale in OUT_DIR.glob("*.md"):
        if stale.name not in valid_names:
            stale.unlink()
            print(f"removed stale page: {stale.name}")

    print(f"wrote {len(written)} file(s) to {OUT_DIR.relative_to(REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

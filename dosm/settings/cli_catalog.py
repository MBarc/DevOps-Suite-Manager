"""Known DevOps CLI tools that DOSM can wire as quick-launch shells.

Each entry knows how to:
- Find an installed binary (a list of candidate executable names — pwsh
  before powershell.exe, awscli before aws, etc.).
- Probe a version string (the arg list passed once at detection).
- Render a stable id used in config.yaml's `cli_tools.<id>: bool`.

Enabled entries are surfaced on the Terminals page as additional shells in
addition to the auto-detected raw shells (bash/cmd/pwsh).
"""
from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class CLITool:
    id: str
    name: str
    description: str
    exe_candidates: list[str]
    version_args: list[str]
    install_url: str | None = None
    # When the user opens a launcher for this tool, we wrap it in the host
    # shell so they get a usable prompt (e.g. ``pwsh -NoLogo``). For tools
    # that have their own REPL (psql, mongosh) leave shell_wrapper=None and
    # we'll launch the tool directly.
    shell_wrapper: bool = True


CATALOG: list[CLITool] = [
    # Shells / interpreters
    CLITool("pwsh", "PowerShell 7", "Cross-platform PowerShell.",
            ["pwsh", "pwsh.exe"], ["-Version"], "https://aka.ms/powershell"),
    CLITool("powershell", "Windows PowerShell", "Built-in Windows PowerShell 5.x.",
            ["powershell.exe"], ["-Command", "$PSVersionTable.PSVersion.ToString()"]),
    CLITool("cmd", "Command Prompt", "Windows cmd.exe.",
            ["cmd.exe"], ["/C", "ver"]),
    CLITool("bash", "Bash", "GNU bash.",
            ["bash"], ["--version"]),

    # Cloud
    CLITool("az", "Azure CLI", "Microsoft Azure command-line.",
            ["az"], ["version"], "https://learn.microsoft.com/cli/azure/install-azure-cli"),
    CLITool("aws", "AWS CLI", "Amazon Web Services command-line.",
            ["aws"], ["--version"], "https://aws.amazon.com/cli/"),
    CLITool("gcloud", "Google Cloud SDK", "gcloud CLI for GCP.",
            ["gcloud"], ["--version"], "https://cloud.google.com/sdk/docs/install"),

    # Source control / forges
    CLITool("git", "Git", "Source control.",
            ["git"], ["--version"], "https://git-scm.com/downloads"),
    CLITool("gh", "GitHub CLI", "github.com from the terminal.",
            ["gh"], ["--version"], "https://cli.github.com/"),

    # IaC + orchestration
    CLITool("terraform", "Terraform", "HashiCorp infrastructure as code.",
            ["terraform"], ["version"], "https://developer.hashicorp.com/terraform/install"),
    CLITool("ansible", "Ansible", "Configuration management.",
            ["ansible"], ["--version"], "https://docs.ansible.com/ansible/latest/installation_guide/"),
    CLITool("kubectl", "kubectl", "Kubernetes CLI.",
            ["kubectl"], ["version", "--client", "--output=yaml"], "https://kubernetes.io/docs/tasks/tools/"),
    CLITool("helm", "Helm", "Kubernetes package manager.",
            ["helm"], ["version", "--short"], "https://helm.sh/docs/intro/install/"),
    CLITool("docker", "Docker", "Container CLI.",
            ["docker"], ["version", "--format", "{{.Client.Version}}"], "https://docs.docker.com/engine/install/"),

    # Service Fabric
    CLITool("sfctl", "sfctl", "Service Fabric command-line.",
            ["sfctl"], ["--version"], "https://learn.microsoft.com/azure/service-fabric/service-fabric-cli"),
]


@dataclass
class DetectedTool:
    spec: CLITool
    installed: bool
    path: str | None
    version: str | None
    error: str | None = None


def _detect_one(spec: CLITool, *, with_version: bool = True) -> DetectedTool:
    found: str | None = None
    for cand in spec.exe_candidates:
        path = shutil.which(cand)
        if path:
            found = path
            break
    if found is None:
        return DetectedTool(spec=spec, installed=False, path=None, version=None)
    version: str | None = None
    err: str | None = None
    if with_version:
        try:
            res = subprocess.run(
                [found, *spec.version_args],
                capture_output=True,
                text=True,
                timeout=4,
            )
            text = (res.stdout or res.stderr or "").strip()
            version = text.splitlines()[0][:120] if text else None
            if res.returncode != 0 and not version:
                err = f"exit {res.returncode}"
        except Exception as e:  # pragma: no cover
            err = f"{type(e).__name__}: {e}"
    return DetectedTool(spec=spec, installed=True, path=found, version=version, error=err)


def detect_all(*, with_version: bool = True) -> list[DetectedTool]:
    return [_detect_one(s, with_version=with_version) for s in CATALOG]


def get_spec(tool_id: str) -> CLITool | None:
    for s in CATALOG:
        if s.id == tool_id:
            return s
    return None


def shell_argv_for(spec: CLITool, exe_path: str) -> list[str]:
    """Argv DOSM uses to spawn the tool from the Terminals page.

    Shells get launched directly (with their no-logo / no-profile flags
    where appropriate). Other CLIs are wrapped in the host's preferred
    shell so the user lands at an interactive prompt with the tool on PATH.
    """
    if spec.id == "pwsh":
        return [exe_path, "-NoLogo"]
    if spec.id == "powershell":
        return [exe_path, "-NoLogo", "-NoProfile"]
    if spec.id == "cmd":
        return [exe_path]
    if spec.id == "bash":
        return [exe_path]
    # All other tools: drop into the platform's default shell with the tool
    # available on PATH (already the case by virtue of detection).
    if platform.system() == "Windows":
        return [shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "cmd.exe", "-NoLogo"]
    return [shutil.which("bash") or "/bin/sh"]

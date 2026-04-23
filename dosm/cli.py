from __future__ import annotations

import os
from pathlib import Path

import typer
import uvicorn
from rich.console import Console

from dosm import __version__
from dosm.bootstrap import initialize_home
from dosm.config import load_config

app = typer.Typer(
    help="DevOps Operations Suite Manager.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def version() -> None:
    """Print the installed DOSM version."""
    console.print(f"dosm {__version__}")


@app.command()
def init(
    home: Path = typer.Argument(..., help="Path to create as $DOSM_HOME."),
    force: bool = typer.Option(False, "--force", help="Overwrite config.yaml and README."),
) -> None:
    """Create a new DOSM_HOME directory with the standard layout and default config."""
    created = initialize_home(home, force=force)
    if created:
        console.print(f"[green]Initialized[/green] {home.resolve()}")
        for p in created:
            console.print(f"  + {p.relative_to(home.resolve()) if p != home.resolve() else p}")
    else:
        console.print(f"[yellow]Nothing to do[/yellow] at {home.resolve()} (already initialized)")
    console.print(
        "\nNext: export DOSM_HOME="
        f"{home.resolve()} and run `dosm serve`."
    )


@app.command()
def serve(
    home: Path | None = typer.Option(None, "--home", help="Override $DOSM_HOME."),
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload (dev only)."),
) -> None:
    """Start the DOSM web app."""
    if home is not None:
        os.environ["DOSM_HOME"] = str(home.expanduser().resolve())
    cfg = load_config()
    bind_host = host or cfg.server.host
    bind_port = port or cfg.server.port
    console.print(f"[green]Starting DOSM[/green] on http://{bind_host}:{bind_port}")
    console.print(f"  DOSM_HOME = {cfg.home}")
    uvicorn.run(
        "dosm.main:create_app",
        factory=True,
        host=bind_host,
        port=bind_port,
        reload=reload,
    )


if __name__ == "__main__":
    app()

"""The complete user interface: explicit commands, no frontend or watcher."""

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from rich.console import Console
from rich.table import Table

from stacktrail import __version__
from stacktrail.architecture import diagram
from stacktrail.config import load_config
from stacktrail.graph import GraphStore
from stacktrail.llm import ModelError, Ollama
from stacktrail.query import answer_question
from stacktrail.scanner import scan
from stacktrail.sync import synchronize

app = typer.Typer(
    no_args_is_help=True,
    invoke_without_command=True,
    help="Discover architecture from local repositories.",
)
console = Console(highlight=False, markup=False)
CONFIG = Annotated[Path, typer.Option("--config", "-c", help="Local configuration file.")]


@contextmanager
def errors():
    try:
        yield
    except (ValueError, OSError, Neo4jError, ServiceUnavailable, ModelError) as error:
        if isinstance(error, (Neo4jError, ServiceUnavailable)):
            message = "Neo4j request failed. Check Compose health, credentials, and database configuration."
        elif isinstance(error, ModelError):
            message = str(error)
        elif isinstance(error, OSError):
            message = (
                f"File operation failed ({type(error).__name__}). Check paths and permissions."
            )
        else:
            message = str(error)
        console.print(f"Error: {message}")
        raise typer.Exit(1) from error


@app.callback()
def main(version: Annotated[bool, typer.Option("--version", is_eager=True)] = False):
    if version:
        console.print(f"StackTrail {__version__}")
        raise typer.Exit()


@app.command()
def init(
    source: Annotated[
        list[Path] | None,
        typer.Option("--source", help="Source folder; repeat for multiple roots."),
    ] = None,
    config: CONFIG = Path("stacktrail.toml"),
):
    """Create local configuration. Existing configuration is never overwritten."""
    with errors():
        roots = source or [Path(typer.prompt("Source folder", default=str(Path.cwd())))]
        for root in roots:
            if not root.expanduser().is_dir():
                raise ValueError(f"Source folder does not exist: {root}")
        content = (
            "# Local configuration: do not commit private source paths.\n"
            '[project]\nname = "default"\nroots = '
            + json.dumps([str(root.expanduser().resolve()) for root in roots])
            + "\nexclude = []\nmax_file_bytes = 262144\n\n"
            '[neo4j]\nuri = "bolt://localhost:7687"\nuser = "neo4j"\ndatabase = "neo4j"\n\n'
            '[ollama]\nurl = "http://localhost:11434"\nmodel = "qwen3:4b"\nenrichment = true\ncontext_chars = 12000\n'
        )
        with config.open("x") as handle:
            handle.write(content)
        console.print(
            f"Created {config}. Set STACKTRAIL_NEO4J_PASSWORD, start Neo4j, then run stacktrail sync."
        )


@app.command()
def sync(
    config: CONFIG = Path("stacktrail.toml"),
    full: Annotated[bool, typer.Option(help="Re-extract all eligible files.")] = False,
):
    """Apply new, modified, and removed source files to the graph."""
    with errors():
        settings = load_config(config)
        with GraphStore(settings) as store:
            with console.status("Synchronizing local repositories..."):
                report = synchronize(settings, store, Ollama(settings), full=full)
        console.print(json.dumps(report.as_dict(), indent=2))
        if report.pending:
            console.print("Model enrichment is pending. Check Ollama and run sync again to retry.")
        if report.architecture_pending:
            console.print(
                "Architecture synthesis is incomplete. Structured views remain available; run sync again to retry."
            )
        if report.errors:
            raise typer.Exit(1)


@app.command()
def status(config: CONFIG = Path("stacktrail.toml")):
    """Show synchronization state and pending local changes without model calls."""
    with errors():
        settings = load_config(config)
        with GraphStore(settings) as store:
            previous = store.inventory()
            summary = store.summary()
            architectures = store.architectures()
            summary["architecture_repositories"] = len(architectures)
            summary["architecture_pending"] = sum(a["status"] != "complete" for a in architectures)
        current = {source.id: source for source in scan(settings)}
        from stacktrail.models import digest

        changed = sum(
            source_id not in previous
            or previous[source_id].get("content_hash") != source.content_hash
            or previous[source_id].get("fingerprint")
            != digest(settings.fingerprint, source.repository, source.component)
            for source_id, source in current.items()
        )
        console.print(
            json.dumps(
                {
                    **summary,
                    "source_files": len(previous),
                    "new_or_modified": changed,
                    "removed": len(previous.keys() - current.keys()),
                    "stale_or_failed": sum(bool(p.get("error")) for p in previous.values()),
                    "enrichment_pending": sum(bool(p.get("pending")) for p in previous.values()),
                },
                indent=2,
            )
        )
        failures = [p for p in previous.values() if p.get("error")]
        if failures:
            table = Table("File", "Status")
            for failure in failures:
                table.add_row(failure["path"], failure["error"])
            console.print(table)


def show_snapshot(store: GraphStore) -> None:
    inventory = store.inventory()
    stale = sum(bool(source.get("error")) for source in inventory.values())
    pending = sum(bool(source.get("pending")) for source in inventory.values())
    console.print(
        f"Last sync: {store.summary().get('last_sync') or 'never'} | stale/failed: {stale} | enrichment pending: {pending}"
    )
    console.print("Answers describe synchronized sources. Run sync to apply local changes.")


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Architecture question.")],
    config: CONFIG = Path("stacktrail.toml"),
):
    """Answer a question using graph paths and cited source evidence."""
    with errors():
        settings = load_config(config)
        with GraphStore(settings) as store:
            show_snapshot(store)
            console.print(answer_question(settings, store, Ollama(settings), question))


@app.command()
def chat(config: CONFIG = Path("stacktrail.toml")):
    """Ask independent architecture questions until /exit. No conversation is stored."""
    with errors():
        settings = load_config(config)
        model = Ollama(settings)
        with GraphStore(settings) as store:
            show_snapshot(store)
            console.print(
                "Enter a question, or /exit to leave. Each question is retrieved independently."
            )
            while True:
                try:
                    question = typer.prompt("You")
                except (EOFError, KeyboardInterrupt):
                    break
                if question.strip().lower() in {"/exit", "/quit", "exit", "quit"}:
                    break
                if question.strip():
                    console.print(answer_question(settings, store, model, question))


@app.command(name="export")
def export_command(
    view: Annotated[str, typer.Option(help="solution or infrastructure")] = "solution",
    config: CONFIG = Path("stacktrail.toml"),
    format_: Annotated[str, typer.Option("--format")] = "mermaid",
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("architecture.md"),
    repository: Annotated[
        str | None, typer.Option(help="Exact repository path or folder name.")
    ] = None,
):
    """Export the synchronized architecture as a Mermaid Markdown document."""
    with errors():
        if format_ != "mermaid":
            raise ValueError("Only --format mermaid is supported.")
        settings = load_config(config)
        with GraphStore(settings) as store:
            show_snapshot(store)
            import json

            projections = [
                json.loads(row["payload"])
                for row in store.architectures()
                if repository is None
                or row["repository"] == repository
                or row["repository"].endswith("/" + repository)
            ]
            if not projections:
                raise ValueError("No architectural model found. Run stacktrail sync first.")
            text = diagram(
                [e for p in projections for e in p["edges"]],
                [n for p in projections for n in p["nodes"]],
                view,
            )
            text += (
                "\n## Synthesis status\n\n"
                + "\n".join(
                    f"- {p['status']}: {len(p['nodes'])} architectural elements"
                    for p in projections
                )
                + "\n"
            )
        # Do not silently overwrite source files or an existing diagram.
        with output.open("x") as handle:
            handle.write(text)
        console.print(
            f"Exported {output}. It may contain private architecture; keep it outside public commits."
        )


if __name__ == "__main__":
    app()

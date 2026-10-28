"""Local, git-ignored configuration. Database passwords come from the environment."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

from stacktrail import EXTRACTOR_VERSION
from stacktrail.models import digest

DEFAULT_EXCLUDES = [
    ".git/",
    "node_modules/",
    "vendor/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "dist/",
    "build/",
    ".next/",
    "coverage/",
    ".stacktrail/",
    "private/",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.crt",
    "*credentials*",
    "*secret*",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "poetry.lock",
    "*.min.js",
    "*.map",
    "*.log",
    "stacktrail.toml",
]


@dataclass
class Config:
    roots: list[Path]
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    database: str = "neo4j"
    ollama_url: str = "http://localhost:11434"
    model: str = "qwen3:4b"
    enrichment: bool = True
    max_file_bytes: int = 262144
    context_chars: int = 12000
    excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    path: Path = Path("stacktrail.toml")
    workspace_name: str = "default"

    @property
    def password(self) -> str:
        values = dotenv_values(self.path.resolve().parent / ".env", interpolate=False)
        value = (
            os.environ.get("STACKTRAIL_NEO4J_PASSWORD")
            or os.environ.get("NEO4J_PASSWORD")
            or values.get("STACKTRAIL_NEO4J_PASSWORD")
            or values.get("NEO4J_PASSWORD")
        )
        if not value:
            raise ValueError(
                "Set NEO4J_PASSWORD or STACKTRAIL_NEO4J_PASSWORD in the environment or in .env beside stacktrail.toml."
            )
        return value

    @property
    def workspace(self) -> str:
        # Stable when roots change, so removed roots can be reconciled by sync.
        return digest(str(self.path.resolve()), self.workspace_name)

    @property
    def fingerprint(self) -> str:
        return digest(
            EXTRACTOR_VERSION,
            self.model,
            self.ollama_url,
            str(self.enrichment),
            str(self.context_chars),
        )


def load_config(path: Path) -> Config:
    path = path.resolve()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    project = raw.get("project", {})
    neo4j = raw.get("neo4j", {})
    ollama = raw.get("ollama", {})
    roots = [(path.parent / Path(p).expanduser()).resolve() for p in project.get("roots", [])]
    if not roots:
        raise ValueError("Configure at least one source folder in [project].roots.")
    if len(roots) != len(set(roots)):
        raise ValueError("Source folders must be unique.")
    if any(a in b.parents for a in roots for b in roots if a != b):
        raise ValueError("Source folders must not overlap.")
    url = ollama.get("url", "http://localhost:11434").rstrip("/")
    if urlparse(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("The MVP requires a local Ollama endpoint.")
    config = Config(
        roots=roots,
        path=path,
        workspace_name=project.get("name", "default"),
        uri=neo4j.get("uri", "bolt://localhost:7687"),
        user=neo4j.get("user", "neo4j"),
        database=neo4j.get("database", "neo4j"),
        ollama_url=url,
        model=ollama.get("model", "qwen3:4b"),
        enrichment=ollama.get("enrichment", True),
        context_chars=ollama.get("context_chars", 12000),
        max_file_bytes=project.get("max_file_bytes", 262144),
        excludes=[*DEFAULT_EXCLUDES, *project.get("exclude", [])],
    )
    if config.max_file_bytes < 1 or not 1000 <= config.context_chars <= 24000:
        raise ValueError("max_file_bytes must be positive; context_chars must be 1000–24000.")
    return config

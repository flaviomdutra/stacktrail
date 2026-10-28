"""Read eligible source files without following links or collecting raw secrets."""

import json
import os
import re
from pathlib import Path

import pathspec

from stacktrail.config import Config
from stacktrail.models import SourceFile, digest

SUFFIXES = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".md",
    ".rst",
    ".tf",
    ".example",
}
SECRET_NAME = (
    r"(?:[\w.-]*(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|credential)[\w.-]*)"
)
ASSIGNMENT = re.compile(
    rf"""(?im)(["']?{SECRET_NAME}["']?[ \t]*[:=][ \t]*)"""
    r"""(![A-Za-z][^\r\n]*|"(?:\\.|[^"\r\n])*"|'(?:\\.|[^'\r\n])*'|\$\{\{[^\r\n]*?\}\}|\$\{[^}\r\n]*\}|[^\s{}\[\],"'|>][^\r\n,}\]]*)"""
)
URL_AUTH = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", re.I)


def redact(text: str) -> str:
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        lambda match: "[REDACTED PRIVATE KEY]" + "\n" * match[0].count("\n"),
        text,
        flags=re.S,
    )
    text = ASSIGNMENT.sub(lambda m: m[1] + '"[REDACTED]"', text)
    text = URL_AUTH.sub(r"\1[REDACTED]@", text)
    text = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[REDACTED]", text)
    text = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{20,})\b",
        "[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    # Opaque signed URLs and query parameters are unnecessary for architecture discovery.
    text = re.sub(r"(\b[a-z][a-z0-9+.-]*://[^\s\"'<>?]+)\?[^\s\"'<>]*", r"\1", text)
    return text


def eligible(path: Path) -> bool:
    return path.suffix.lower() in SUFFIXES or path.name.startswith("Dockerfile")


def repository_for(path: Path, root: Path) -> Path:
    ancestors = [path.parent, *path.parent.parents]
    candidates = []
    for parent in ancestors:
        if parent != root and root not in parent.parents:
            continue
        if (parent / ".git").exists():
            return parent
        if any((parent / name).exists() for name in ("package.json", "pyproject.toml", "go.mod")):
            candidates.append(parent)
        if parent == root:
            break
    if candidates:
        return candidates[-1]  # Root package, rather than every nested package, is the repository.
    relative = path.relative_to(root)
    return root / relative.parts[0] if len(relative.parts) > 1 else root


def component_name(repository: Path) -> str:
    manifest = repository / "package.json"
    if manifest.is_file() and not manifest.is_symlink():
        try:
            return str(json.loads(manifest.read_text()).get("name") or repository.name)
        except (OSError, ValueError):
            pass
    return repository.name


def scan(config: Config) -> list[SourceFile]:
    """Fail closed on unreadable roots/directories, preventing accidental graph deletions."""
    sources = []
    excludes = pathspec.PathSpec.from_lines("gitwildmatch", config.excludes)
    for root in config.roots:
        if not root.is_dir():
            raise ValueError(
                f"Source folder is missing or inaccessible: {root}. No changes applied."
            )
        ignores: dict[Path, pathspec.PathSpec] = {}

        def onerror(error: OSError) -> None:
            raise error

        def ignored(path: Path, directory: bool = False, root=root, ignores=ignores) -> bool:
            relative = path.relative_to(root).as_posix() + ("/" if directory else "")
            if excludes.match_file(relative):
                return True
            return any(
                base in path.parents
                and spec.match_file(path.relative_to(base).as_posix() + ("/" if directory else ""))
                for base, spec in ignores.items()
            )

        for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
            base = Path(directory)
            gitignore = base / ".gitignore"
            if gitignore.is_file() and not gitignore.is_symlink():
                ignores[base] = pathspec.PathSpec.from_lines(
                    "gitwildmatch", gitignore.read_text().splitlines()
                )
            dirs[:] = sorted(
                d
                for d in dirs
                if not (base / d).is_symlink() and not ignored(base / d, directory=True)
            )
            for name in sorted(files):
                path = base / name
                if path.is_symlink() or ignored(path) or not eligible(path):
                    continue
                if path.stat().st_size > config.max_file_bytes:
                    continue
                raw = path.read_bytes()
                if b"\x00" in raw:
                    continue
                try:
                    content = redact(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    continue
                repository = repository_for(path, root)
                repository_id = str(repository)
                sources.append(
                    SourceFile(
                        id=digest(config.workspace, str(path)),
                        path=str(path),
                        repository=repository_id,
                        component=component_name(repository),
                        content_hash=digest(raw.hex()),
                        content=content,
                    )
                )
    return sources

# StackTrail

**Trace your architecture from local repositories.**

StackTrail builds an evidence-backed architecture graph from source code, infrastructure definitions,
and repository documentation. Run `sync` when you want to apply changes, ask questions in your terminal,
and export Mermaid diagrams. Your source code and graph stay local.

Early MVP: extraction is deliberately conservative and incomplete. The graph describes indexed sources,
not a verified inventory of deployed infrastructure. Inferred connections are labeled explicitly.

## Requirements

- Python 3.11 or newer, Git, and Docker Compose.
- Ollama for local model enrichment and generated answers; structured extraction and diagrams also work offline.
- Linux or WSL for the current local locking implementation.

## Quick start

```bash
git clone https://github.com/flaviomdutra/stacktrail.git
cd stacktrail
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env
# Edit .env and replace NEO4J_PASSWORD with your own local password.
docker compose up -d --wait

# Set this to the same password, without storing it in your shell history:
read -rsp 'Neo4j password: ' STACKTRAIL_NEO4J_PASSWORD
export STACKTRAIL_NEO4J_PASSWORD
echo

stacktrail init --source ./examples/workspace
stacktrail sync
stacktrail status
stacktrail export --format mermaid --output architecture.md
```

To enable model-assisted analysis, install [Ollama](https://ollama.com), start it, and pull the default model:

```bash
ollama pull qwen3:4b
stacktrail sync
stacktrail ask "Which components depend on PostgreSQL?"
stacktrail chat
```

The model is configurable. Its download size is not its runtime memory requirement: benchmark it on your
machine. Neo4j is limited to 1.5 GiB by Compose. Inference is serial, with a bounded context and timeout.

## Commands

| Command | Behavior |
| --- | --- |
| `init --source PATH` | Create local configuration; repeat `--source` for multiple folders. |
| `sync` | Apply added, changed, and removed files, and retry pending enrichment. |
| `sync --full` | Deliberately re-extract all eligible files. |
| `status` | Show indexed state, pending changes, errors, and incomplete enrichment. |
| `ask "QUESTION"` | Retrieve source evidence and graph paths, then answer locally. |
| `chat` | Ask independent questions until `/exit`; no conversation history is stored. |
| `export --format mermaid --output FILE` | Export architecture and evidence to Markdown. |

Commands accept `--config PATH`. Export accepts `--repository NAME_OR_PATH` and refuses to overwrite files.
There is no background watcher. Unchanged, fully enriched files make no model calls. Pending enrichment
is retried by the next `sync`; disable `[ollama].enrichment` for structured extraction only.

## Configure your repositories

`stacktrail.toml` is local and ignored by Git. Change `[project].roots` to point at your repository folder.
Roots must exist, must not overlap, and are never modified by StackTrail. A workspace is identified by its
configuration file location and project name; moving that file creates a separate workspace.

```toml
[project]
name = "my-project"
roots = ["/absolute/path/to/repositories"]
exclude = ["archives/", "internal-notes.md"]
max_file_bytes = 262144

[neo4j]
uri = "bolt://localhost:7687"
user = "neo4j"
database = "neo4j"

[ollama]
url = "http://localhost:11434"
model = "qwen3:4b"
enrichment = true
context_chars = 12000
```

Passwords are read exclusively from `STACKTRAIL_NEO4J_PASSWORD`. Compose reads its own password from
`.env`; StackTrail does not load `.env`. Ollama must use a loopback endpoint in this MVP.

## What is discovered

- npm and Python dependency manifests: library dependencies, not assumed runtime infrastructure.
- Docker Compose: declared services, database/cache/queue images, explicit service dependencies and URLs.
- AWS SAM/CloudFormation: supported resources and intrinsic references.
- JavaScript, TypeScript, Python and configuration: literal endpoints and AWS ARNs.
- Documentation: searchable context; hyperlinks alone are not treated as runtime connections.
- Exact npm package references: resolved to a local component only when a unique package declaration exists.
- Optional model enrichment: additional line-validated hypotheses, always marked as inferred.

Resource names and `localhost` URLs are scoped to their repository and configuration context.
Names alone do not prove cross-repository identity. Generated code, installed dependencies, symlinks,
oversized files, real `.env` files, key material, and git-ignored files are excluded.

Common credential assignments, URL user information, signed URL queries, and known token formats are
redacted before persistence or inference. This is best-effort filtering, not a universal secret detector.
Add exclusions for sensitive documents and keep the database private.

## Development

```bash
pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest
```

Alternatively, use `uv sync --extra dev --locked` to install the committed dependency lockfile.

Integration tests require a local Neo4j instance and an explicit opt-in. Each run uses an isolated
workspace and removes only that workspace's records:

```bash
STACKTRAIL_TEST_NEO4J=1 pytest -m integration
```

See [architecture](docs/architecture.md), [roadmap](docs/roadmap.md), and [contribution guidelines](CONTRIBUTING.md).

## Public code, private analysis

Only StackTrail and synthetic examples belong in this repository. Never commit analyzed repositories,
source excerpts, real infrastructure identifiers, graph dumps, local configuration, or private diagrams.
The default `architecture.md` output is ignored; exports to other paths must also stay outside public commits.

## License

StackTrail's code is licensed under [MIT](LICENSE). Neo4j and other dependencies retain their own licenses.

## Architecture reconstruction

`sync` also reconstructs a separate architectural model per repository in Neo4j. It identifies
executable units, explicit infrastructure definitions, and selected client usage patterns, then asks
Ollama to summarize component responsibilities using targeted source passages. Multiple executable
units may be found in one repository. Installed packages alone do not establish runtime connections.

Generate the two complementary views:

```bash
stacktrail export --view solution --output .stacktrail/solution.md
stacktrail export --view infrastructure --output .stacktrail/infrastructure.md
```

The default view is `solution`. There is no evidence export view. Source evidence remains in the
internal graph, cited answers, and references below each diagram. Solution views hide deployment-only
containers; infrastructure views retain them. Both describe logical architecture, not verified
production deployment. Unsupported resource identities remain unresolved rather than being merged
by name. Mermaid appearance is intentionally simpler than a manually illustrated architecture chart.

Synthesis is independent of optional per-file `[ollama].enrichment`: it analyzes selected passages per
repository, even when file enrichment is disabled. Successful summaries are cached by source paths,
hashes, model settings, and synthesis version. Failed or offline synthesis retains structured
results and marks reused summaries stale. `sync` reports `architecture_pending`; export reports
synthesis status. A subsequent `sync` retries incomplete synthesis. Repository changes invalidate the
corresponding summary. This initial reconstruction supports package entrypoints, Docker definitions,
SAM/CloudFormation resources, and selected database, storage, and messaging clients; arbitrary business
flows and deployment platforms are not exhaustively understood.

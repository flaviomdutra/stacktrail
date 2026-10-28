# Contributing

Use English for code, comments, documentation, commits, issues, and pull requests.

Install the development dependencies and run `ruff check .`, `ruff format --check .`, and `pytest`.
Use synthetic fixtures only. Never include private repositories, infrastructure data, credentials,
generated diagrams of real systems, or source excerpts in issues or commits.

Extraction changes should include evidence-focused tests, especially for ambiguous identifiers,
incorrect connections, deletion, and retry behavior. Update `EXTRACTOR_VERSION` in
`src/stacktrail/__init__.py` when extraction semantics change so existing workspaces can be refreshed.

Describe the user-visible behavior, limitations, and validation in your pull request.

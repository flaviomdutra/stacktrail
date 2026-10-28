from pathlib import Path

from typer.testing import CliRunner

from stacktrail.cli import app
from stacktrail.config import load_config
from stacktrail.export import mermaid
from stacktrail.query import answer_question, search_terms, source_snippet


def test_init_and_help(tmp_path):
    path = tmp_path / "stacktrail.toml"
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--source", str(tmp_path), "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert load_config(path).roots == [tmp_path]
    before = path.read_text()
    assert (
        runner.invoke(app, ["init", "--source", str(tmp_path), "--config", str(path)]).exit_code
        == 1
    )
    assert path.read_text() == before
    help_text = runner.invoke(app, ["--help"]).output
    assert "sync" in help_text and "watch" not in help_text and "analyze" not in help_text
    assert runner.invoke(app, ["--version"]).exit_code == 0


def test_search_has_no_user_controlled_lucene_syntax():
    query = search_terms('Which services depend on PostgreSQL? "+* OR password:*')
    assert "postgresql" in query and "postgres" in query
    assert "*" not in query and "+" not in query
    assert '"s3"' in search_terms("Who uses S3?")


def test_source_excerpt_finds_matches_beyond_the_file_prefix():
    content = "unrelated = True\n" * 300 + 'url = "postgres://db/app"\n'
    snippet, first_line = source_snippet(content, ["postgres"], 1000)
    assert '301: url = "postgres://db/app"' in snippet
    assert first_line == 300


def test_mermaid_deduplicates_edges_and_escapes_labels():
    row = {
        "source": {"id": "a", "name": 'api"<script>', "kind": "component"},
        "target": {"id": "b", "name": "db", "kind": "database"},
        "evidence": {
            "kind": "connects-to",
            "certainty": "inferred",
            "file": "/example/app.py",
            "line": 2,
            "content_hash": "abc",
        },
    }
    output = mermaid([row, row])
    assert output.count('-.->|"connects-to"|') == 1
    assert "&lt;script&gt;" in output and 'api"<script>' not in output
    assert "/example/app.py:2" in output


def test_mermaid_includes_components_without_detected_dependencies():
    output = mermaid([], [{"id": "a", "name": "website", "kind": "component"}])
    assert "website (component)" in output


class RetrievalStore:
    def retrieve(self, search):
        return [
            {"id": "1", "path": "/example/app.py", "content": "db = 'postgres'", "pending": False}
        ]

    def neighborhood(self, source_ids, terms=None):
        return []


def test_answers_reject_fabricated_citations(config):
    class Model:
        def available(self):
            return True

        def answer(self, question, context):
            return "Depends on database [E999]."

    answer = answer_question(config, RetrievalStore(), Model(), "postgres")
    assert "without valid evidence" in answer
    assert "E999" not in answer


def test_offline_answers_show_evidence(config):
    class Model:
        def available(self):
            return False

    answer = answer_question(config, RetrievalStore(), Model(), "postgres")
    assert "unavailable" in answer and "/example/app.py" in answer


def test_config_rejects_remote_model_endpoint(tmp_path):
    path = tmp_path / "stacktrail.toml"
    path.write_text('[project]\nroots = ["."]\n[ollama]\nurl = "https://remote.example.test"')
    import pytest

    with pytest.raises(ValueError, match="local Ollama"):
        load_config(Path(path))

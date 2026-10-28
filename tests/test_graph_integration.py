"""Opt-in tests against a real Neo4j database, isolated by a random workspace name."""

import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from stacktrail.graph import GraphStore
from stacktrail.query import answer_question
from stacktrail.sync import synchronize

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("STACKTRAIL_TEST_NEO4J") != "1",
        reason="Set STACKTRAIL_TEST_NEO4J=1 to use a local test database",
    ),
]


class OfflineModel:
    def available(self):
        return False


@pytest.fixture
def graph(config):
    config.workspace_name = "integration-" + uuid4().hex
    config.uri = os.environ.get("STACKTRAIL_TEST_NEO4J_URI", "bolt://localhost:7687")
    with GraphStore(config) as store:
        store.initialize()
        try:
            yield store
        finally:
            store.purge()


def test_real_graph_sync_resolution_query_and_deletion(config, graph):
    example = Path(__file__).resolve().parents[1] / "examples/workspace"
    shutil.copytree(example, config.roots[0], dirs_exist_ok=True)
    model = OfflineModel()
    report = synchronize(config, graph, model)
    assert not report.errors
    rows = graph.edges()
    resolved = [row for row in rows if row["evidence"]["kind"] == "uses-local-package"]
    assert len(resolved) == 1
    assert resolved[0]["target"]["name"] == "@stacktrail-example/notifications"
    duplicate = config.roots[0] / "duplicate"
    duplicate.mkdir()
    duplicate_manifest = duplicate / "package.json"
    duplicate_manifest.write_text('{"name":"@stacktrail-example/notifications"}')
    synchronize(config, graph, model)
    assert not [row for row in graph.edges() if row["evidence"]["kind"] == "uses-local-package"]
    duplicate_manifest.unlink()
    synchronize(config, graph, model)
    assert (
        len([row for row in graph.edges() if row["evidence"]["kind"] == "uses-local-package"]) == 1
    )
    assert any(row["target"]["kind"] == "database" for row in rows)
    # Full-text indexes are populated asynchronously when initially created.
    graph.query("CALL db.awaitIndexes(30)")
    answer = answer_question(config, graph, model, "Which services depend on PostgreSQL?")
    assert "postgres" in answer.lower() and "Sources:" in answer
    before = len(rows)
    report = synchronize(config, graph, model)
    assert report.updated == 0 and len(graph.edges()) == before
    manifest = config.roots[0] / "notifications/package.json"
    manifest.unlink()
    synchronize(config, graph, model)
    assert not [row for row in graph.edges() if row["evidence"]["kind"] == "uses-local-package"]
    for path in list(config.roots[0].rglob("*")):
        if path.is_file():
            path.unlink()
    synchronize(config, graph, model)
    assert graph.edges() == []
    assert graph.summary()["entities"] == 0


def test_real_graph_shared_evidence_and_atomic_failure(config, graph):
    repo = config.roots[0] / "api"
    repo.mkdir()
    (repo / "package.json").write_text('{"name":"api"}')
    a, b = repo / "a.ts", repo / "b.ts"
    a.write_text('const url = "https://billing.example.test";')
    b.write_text('const url = "https://billing.example.test";')
    model = OfflineModel()
    synchronize(config, graph, model)
    assert len(graph.edges()) == 2
    assert len({row["target"]["id"] for row in graph.edges()}) == 1
    a.unlink()
    synchronize(config, graph, model)
    assert len(graph.edges()) == 1
    # A malformed source keeps its previous relationships and is reported stale.
    template = repo / "compose.yaml"
    template.write_text("services:\n  db:\n    image: postgres:16\n")
    synchronize(config, graph, model)
    before = graph.edges()
    template.write_text("services: [invalid")
    report = synchronize(config, graph, model)
    assert report.errors
    assert graph.edges() == before
    assert any(source.get("error") for source in graph.inventory().values())


def test_workspaces_are_isolated(config, graph):
    (config.roots[0] / "app.py").write_text('url = "https://example.test"')
    synchronize(config, graph, OfflineModel())
    original = config.workspace_name
    config.workspace_name = "other-" + uuid4().hex
    with GraphStore(config) as other:
        other.initialize()
        try:
            assert other.edges() == [] and other.inventory() == {}
        finally:
            other.purge()
    config.workspace_name = original
    assert graph.edges()

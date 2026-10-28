from dataclasses import asdict

import pytest

from stacktrail.llm import ModelError
from stacktrail.sync import synchronize


class MemoryStore:
    def __init__(self):
        self.sources = {}
        self.results = {}

    def initialize(self):
        pass

    def inventory(self):
        return {key: dict(value) for key, value in self.sources.items()}

    def replace(self, source, result, fingerprint, pending, enrichment_error=""):
        self.sources[source.id] = {
            **asdict(source),
            "fingerprint": fingerprint,
            "pending": pending,
            "error": "",
            "enrichment_error": enrichment_error,
        }
        self.results[source.id] = result

    def mark_error(self, source, error):
        self.sources.setdefault(
            source.id, {**asdict(source), "content_hash": "", "fingerprint": ""}
        )["error"] = error

    def delete(self, source_id):
        del self.sources[source_id]
        self.results.pop(source_id, None)

    def reconcile(self):
        pass

    def finish(self, errors):
        pass


class FakeModel:
    def __init__(self):
        self.online = True
        self.probes = 0
        self.calls = 0
        self.fail = False

    def available(self):
        self.probes += 1
        return self.online

    def enrich(self, source, result):
        self.calls += 1
        if self.fail:
            raise ModelError("Invalid model output")


def test_sync_lifecycle_and_no_model_calls_on_unchanged(config):
    config.enrichment = True
    repo = config.roots[0] / "api"
    repo.mkdir()
    manifest = repo / "package.json"
    manifest.write_text('{"name":"api","dependencies":{"pg":"8"}}')
    store, model = MemoryStore(), FakeModel()
    assert synchronize(config, store, model).updated == 1
    assert model.calls == 1
    model.probes = 0
    report = synchronize(config, store, model)
    assert report.unchanged == 1
    assert model.calls == 1 and model.probes == 0
    manifest.write_text('{"name":"api","dependencies":{"redis":"4"}}')
    assert synchronize(config, store, model).updated == 1
    assert "pg" not in {
        entity.name for result in store.results.values() for entity in result.entities.values()
    }
    manifest.unlink()
    assert synchronize(config, store, model).deleted == 1
    assert not store.sources


def test_offline_enrichment_is_retried(config):
    config.enrichment = True
    (config.roots[0] / "app.py").write_text('url = "https://example.test"')
    store, model = MemoryStore(), FakeModel()
    model.online = False
    report = synchronize(config, store, model)
    assert report.updated == 1 and report.pending == 1
    assert model.calls == 0
    model.online = True
    report = synchronize(config, store, model)
    assert report.updated == 1 and report.pending == 0
    assert model.calls == 1


def test_failure_preserves_previous_evidence_and_recovers(config):
    path = config.roots[0] / "package.json"
    path.write_text('{"name":"api","dependencies":{"pg":"8"}}')
    store, model = MemoryStore(), FakeModel()
    synchronize(config, store, model)
    before = dict(store.results)
    path.write_text("{invalid")
    assert synchronize(config, store, model).errors
    assert store.results == before
    assert next(iter(store.sources.values()))["error"]
    path.write_text('{"name":"api","dependencies":{"pg":"8"}}')
    assert synchronize(config, store, model).updated == 1
    assert not next(iter(store.sources.values()))["error"]


def test_configuration_change_invalidates_cache(config):
    (config.roots[0] / "client.py").write_text("pass")
    store, model = MemoryStore(), FakeModel()
    synchronize(config, store, model)
    config.model = "another-local-model"
    assert synchronize(config, store, model).updated == 1


def test_full_sync_rebuilds_unchanged_files(config):
    (config.roots[0] / "client.py").write_text("pass")
    store, model = MemoryStore(), FakeModel()
    synchronize(config, store, model)
    assert synchronize(config, store, model, full=True).updated == 1


def test_missing_root_does_not_delete_previous_data(config):
    path = config.roots[0] / "app.py"
    path.write_text("pass")
    store, model = MemoryStore(), FakeModel()
    synchronize(config, store, model)
    path.unlink()
    config.roots[0].rmdir()
    with pytest.raises(ValueError):
        synchronize(config, store, model)
    assert len(store.sources) == 1


def test_concurrent_sync_is_rejected(config):
    import os
    import tempfile
    from pathlib import Path

    from filelock import FileLock

    from stacktrail.models import digest

    key = digest(config.uri, config.database, config.workspace)
    directory = Path(tempfile.gettempdir()) / f"stacktrail-{os.getuid()}"
    directory.mkdir(exist_ok=True)
    with FileLock(directory / f"{key}.lock"):
        with pytest.raises(ValueError, match="already running"):
            synchronize(config, MemoryStore(), FakeModel())

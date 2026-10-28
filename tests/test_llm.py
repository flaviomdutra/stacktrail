import json

import pytest

from stacktrail.extract import extract
from stacktrail.llm import ModelError, Ollama


def test_model_hypotheses_require_literal_line_evidence(config, source, monkeypatch):
    model = Ollama(config)
    text = 'bucket = "reports-bucket"\n'
    file = source("worker.py", text)
    result = extract(file)
    payload = {
        "dependencies": [
            {"name": "reports-bucket", "kind": "bucket", "line": 1},
            {"name": "invented-database", "kind": "database", "line": 1},
            {"name": "reports-bucket", "kind": "bucket", "line": 999},
            {"name": "reports-bucket", "kind": "bucket", "line": True},
        ]
    }
    monkeypatch.setattr(model, "complete", lambda *args, **kwargs: json.dumps(payload))
    model.enrich(file, result)
    assert len(result.relations) == 1
    assert result.relations[0].certainty == "inferred"
    assert next(e for e in result.entities.values() if e.kind == "bucket").name == "reports-bucket"


def test_invalid_model_output_preserves_structured_extraction(config, source, monkeypatch):
    model = Ollama(config)
    file = source("client.py", 'url = "https://example.test"')
    result = extract(file)
    before = list(result.relations)
    monkeypatch.setattr(model, "complete", lambda *args, **kwargs: '{"dependencies": "invalid"}')
    with pytest.raises(ModelError):
        model.enrich(file, result)
    assert result.relations == before

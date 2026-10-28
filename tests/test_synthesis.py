import json

from stacktrail.models import digest
from stacktrail.synthesis import reconstruct, snippets, update_architecture


def records(tmp_path):
    root = str(tmp_path)
    return root, [
        dict(
            id="1",
            path=root + "/package.json",
            repository=root,
            content=json.dumps({"name": "api", "scripts": {"start": "node server.js"}}),
            content_hash="1",
        ),
        dict(
            id="2",
            path=root + "/server.js",
            repository=root,
            content="const pool = new Pool(config);\n",
            content_hash="2",
        ),
    ]


def test_clients_need_usage_not_imports(tmp_path):
    root, sources = records(tmp_path)
    p = reconstruct(root, sources)
    assert any(n["kind"] == "database" for n in p["nodes"])
    sources[1]["content"] = "import pg from 'pg';"
    assert not any(n["kind"] == "database" for n in reconstruct(root, sources)["nodes"])


def test_snippets_find_late_evidence(tmp_path):
    _, sources = records(tmp_path)
    sources[1]["content"] = "unrelated\n" * 400 + "const pool = new Pool(config);"
    assert ":401:" in snippets(sources, 2000)


def test_synthesis_is_cached_and_invalidated(config, tmp_path):
    root, sources = records(tmp_path)

    class Store:
        saved = {}

        def architectures(self):
            return list(self.saved.values())

        def save_architecture(self, repo, fingerprint, p):
            self.saved[repo] = dict(
                repository=repo, fingerprint=fingerprint, status=p["status"], payload=json.dumps(p)
            )

        def delete_architectures(self, repos):
            self.saved = {k: v for k, v in self.saved.items() if k in repos}

    class Model:
        calls = 0

        def available(self):
            return True

        def complete(self, *args, **kwargs):
            self.calls += 1
            return json.dumps(
                {
                    "components": [
                        dict(
                            id=digest(root, "unit", root),
                            responsibility="Serves application requests",
                            layer="Application services",
                            evidence=[root + "/server.js:1"],
                        )
                    ]
                }
            )

    store, model = Store(), Model()
    inventory = {s["id"]: s for s in sources}
    update_architecture(config, store, model, inventory)
    update_architecture(config, store, model, inventory)
    assert model.calls == 1
    sources[1]["content_hash"] = "changed"
    update_architecture(config, store, model, inventory)
    assert model.calls == 2
    update_architecture(config, store, model, {})
    assert not store.saved


def test_http_clients_and_docker_are_architectural_units(tmp_path):
    root, sources = records(tmp_path)
    sources.append(
        dict(
            id="3",
            path=root + "/Dockerfile",
            repository=root,
            content="FROM python:3.12\n",
            content_hash="3",
        )
    )
    sources[1]["content"] = 'fetch("https://billing.example.test/v1/invoices");'
    result = reconstruct(root, sources)
    assert any(n["technology"] == "Docker" for n in result["nodes"])
    assert any(n["name"] == "billing.example.test" for n in result["nodes"])
    assert not any("/invoices" in n["name"] for n in result["nodes"])

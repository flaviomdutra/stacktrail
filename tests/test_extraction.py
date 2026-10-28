import json

import pytest

from stacktrail.extract import extract
from stacktrail.scanner import redact, scan


def test_package_presence_is_only_a_library_dependency(source):
    result = extract(source("package.json", '{"dependencies":{"pg":"8","twilio":"5"}}'))
    assert {r.kind for r in result.relations} == {"uses-library"}
    assert {e.kind for e in result.entities.values()} == {"component", "library"}


def test_compose_connections_are_scoped_and_evidenced(source):
    content = """services:
  api:
    build: .
    depends_on: [db]
    environment:
      DATABASE_URL: postgres://db:5432/app
  db:
    image: postgres:16
"""
    result = extract(source("compose.yaml", content))
    db = next(e for e in result.entities.values() if e.name == "db")
    assert db.kind == "database"
    connections = [r for r in result.relations if r.target == db.id and r.kind == "connects-to"]
    assert len(connections) == 1
    assert connections[0].line == 6
    assert connections[0].certainty == "source-backed"
    other = extract(source("compose.prod.yaml", content))
    assert db.id != next(e.id for e in other.entities.values() if e.name == "db")


def test_cloudformation_intrinsics(source):
    result = extract(
        source(
            "template.yaml",
            """Resources:
  Uploads:
    Type: AWS::S3::Bucket
  Worker:
    Type: AWS::Serverless::Function
    Properties:
      Environment:
        Variables:
          BUCKET: !Ref Uploads
      Policies:
        - Resource: !GetAtt Uploads.Arn
""",
        )
    )
    entities = result.entities
    assert any(
        entities[r.source].name == "Worker" and entities[r.target].name == "Uploads"
        for r in result.relations
        if r.kind == "references"
    )
    assert {r.line for r in result.relations if r.kind == "references"} == {9, 11}


def test_compose_reference_lines_do_not_point_to_prior_declarations(source):
    result = extract(
        source(
            "compose.yaml",
            """services:
  db:
    image: postgres:16
  api:
    depends_on:
      db:
        condition: service_started
    environment:
      - DATABASE_URL=postgres://db/app
""",
        )
    )
    assert {r.line for r in result.relations if r.kind == "depends-on"} == {6}
    assert {
        r.line
        for r in result.relations
        if r.kind == "connects-to" and result.entities[r.target].name == "db"
    } == {9}


def test_documentation_links_are_not_connections_and_endpoints_are_scoped(source):
    docs = extract(source("README.md", "Reference: https://billing.example.test/v1"))
    assert not docs.relations
    a = source("client.ts", 'fetch("https://billing.example.test/v1")')
    first = extract(a)
    from dataclasses import replace

    second = extract(replace(a, repository="/synthetic/other"))
    assert first.relations[0].target != second.relations[0].target


def test_secret_redaction_handles_assignments_auth_and_signed_urls():
    raw = '{"password": "sensitive-value", "host": "example.test"}'
    clean = redact(raw)
    assert "sensitive-value" not in clean
    assert json.loads(clean)["host"] == "example.test"
    clean = redact(
        "DATABASE_URL=postgres://alice:very-private@db:5432/app\n"
        "API_TOKEN=also-private\nhttps://example.test/path?signature=private"
    )
    assert all(
        value not in clean
        for value in ["alice", "very-private", "also-private", "signature=private"]
    )
    assert "db:5432/app" in clean


def test_redaction_preserves_template_syntax_and_line_numbers(source):
    import yaml

    content = """env:
  API_TOKEN: ${{ secrets.API_TOKEN }}
  PASSWORD: ${PASSWORD}
  EMPTY_SECRET:
  DATABASE_URL: postgres://user:private@db/app
  NESTED_TOKEN: '${{ secrets.OTHER_TOKEN }}'
  INTERNAL_TOKEN: !Sub '{{resolve:secretsmanager:${SecretArn}:SecretString:token}}'
"""
    clean = redact(content)
    assert clean.count("\n") == content.count("\n")
    from stacktrail.extract import TemplateLoader

    parsed = yaml.load(clean, Loader=TemplateLoader)
    assert parsed["env"]["API_TOKEN"] == "[REDACTED]"
    assert parsed["env"]["EMPTY_SECRET"] is None
    assert parsed["env"]["NESTED_TOKEN"] == "[REDACTED]"
    assert parsed["env"]["INTERNAL_TOKEN"] == "[REDACTED]"
    assert "private" not in clean


def test_scanner_excludes_secrets_ignored_files_and_symlinks(config, tmp_path):
    repo = config.roots[0] / "api"
    repo.mkdir()
    (repo / "package.json").write_text('{"name":"example-api"}')
    (repo / "client.ts").write_text('const token = "hidden-value";')
    (repo / ".env").write_text("PASSWORD=hidden-value")
    (repo / ".gitignore").write_text("ignored/\n")
    ignored = repo / "ignored"
    ignored.mkdir()
    (ignored / "private.ts").write_text("private")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "lib.js").write_text("generated")
    outside = tmp_path / "outside.py"
    outside.write_text("private")
    (repo / "linked.py").symlink_to(outside)
    sources = scan(config)
    assert len(sources) == 2
    assert {s.component for s in sources} == {"example-api"}
    assert all("hidden-value" not in s.content for s in sources)


def test_missing_root_aborts_inventory(config):
    config.roots[0].rmdir()
    with pytest.raises(ValueError, match="No changes applied"):
        scan(config)

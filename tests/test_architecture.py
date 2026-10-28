import pytest

from stacktrail.architecture import diagram, project


def test_architecture_excludes_libraries_and_groups_layers():
    nodes = [
        {"id": "a", "name": "API", "kind": "component", "repository": "api"},
        {"id": "b", "name": "Database", "kind": "database", "repository": "api"},
        {"id": "c", "name": "pg", "kind": "library", "repository": "npm"},
    ]
    text = diagram([], nodes)
    assert "Applications" in text and "Data and messaging" in text
    assert "pg (library)" not in text
    assert "Evidence architecture" not in text
    with pytest.raises(ValueError):
        project([], nodes, "evidence")


def test_endpoint_paths_are_consolidated_but_repositories_are_distinct():
    nodes = [
        {"id": str(i), "name": name, "kind": "external-api", "repository": repo}
        for i, (name, repo) in enumerate(
            [
                ("https://billing.example.test/v1", "api"),
                ("https://billing.example.test/v2", "api"),
                ("https://billing.example.test/v1", "worker"),
            ]
        )
    ]
    _, projected, _ = project([], nodes)
    assert len(projected) == 2
    assert {n["name"] for n in projected} == {"billing.example.test"}

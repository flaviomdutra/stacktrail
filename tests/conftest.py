from pathlib import Path

import pytest

from stacktrail.config import Config
from stacktrail.models import SourceFile, digest


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "repos"
    root.mkdir()
    return Config(roots=[root], path=tmp_path / "stacktrail.toml", enrichment=False)


@pytest.fixture
def source():
    def make(name, content):
        return SourceFile(
            digest(name),
            str(Path("/synthetic/api") / name),
            "/synthetic/api",
            "synthetic-api",
            digest(content),
            content,
        )

    return make

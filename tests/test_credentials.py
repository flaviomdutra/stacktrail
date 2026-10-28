import pytest

from stacktrail.config import Config


def test_dotenv_next_to_config_with_literal_password(tmp_path, monkeypatch):
    monkeypatch.delenv("STACKTRAIL_NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    (tmp_path / ".env").write_text('NEO4J_PASSWORD="literal-${VALUE}-password"\n')
    config = Config(roots=[tmp_path], path=tmp_path / "stacktrail.toml")
    assert config.password == "literal-${VALUE}-password"
    monkeypatch.setenv("NEO4J_PASSWORD", "exported-docker-password")
    assert config.password == "exported-docker-password"
    monkeypatch.setenv("STACKTRAIL_NEO4J_PASSWORD", "exported-specific-password")
    assert config.password == "exported-specific-password"


def test_missing_password_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.delenv("STACKTRAIL_NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    config = Config(roots=[tmp_path], path=tmp_path / "stacktrail.toml")
    with pytest.raises(ValueError, match="beside stacktrail.toml"):
        _ = config.password

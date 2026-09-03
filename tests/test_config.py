import pytest

from vibe_harness.config import load_settings


def test_load_settings_reads_dotenv_from_current_directory(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    (tmp_path / ".env").write_text(
        "LLM_API_KEY=test-api-key\n"
        "LLM_BASE_URL=https://example.invalid/v1\n"
        "LLM_MODEL=test-model\n",
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.api_key == "test-api-key"
    assert settings.base_url == "https://example.invalid/v1"
    assert settings.model == "test-model"


def test_load_settings_missing_api_key(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    with pytest.raises(RuntimeError, match="LLM_API_KEY 未设置"):
        load_settings()


def test_load_settings_missing_base_url(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-api-key")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "test-model")

    with pytest.raises(RuntimeError, match="LLM_BASE_URL 未设置"):
        load_settings()


def test_load_settings_missing_model(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-api-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="LLM_MODEL 未设置"):
        load_settings()


@pytest.mark.parametrize("key", ["LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"])
@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_load_settings_rejects_blank_value(
    tmp_path,
    monkeypatch,
    key: str,
    value: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-api-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match=rf"{key} 未设置"):
        load_settings()


def test_load_settings_error_does_not_leak_values(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "SUPER_SECRET_API_KEY")
    monkeypatch.setenv("LLM_BASE_URL", "https://super-secret.example.invalid/v1")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "SUPER_SECRET_API_KEY" not in message
    assert "super-secret" not in message

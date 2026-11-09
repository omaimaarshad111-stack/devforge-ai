"""Configuration and LLM factory checks that do not make API requests."""

import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.llm import MissingGroqApiKeyError, create_groq_chat_model


def test_settings_read_groq_values_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-for-real-use")
    monkeypatch.setenv("GROQ_MODEL", "test-model")

    config = Settings(_env_file=None)

    assert config.groq_api_key == SecretStr("test-key-not-for-real-use")
    assert config.groq_model == "test-model"
    assert "test-key-not-for-real-use" not in str(config.groq_api_key)


def test_llm_factory_explains_missing_api_key() -> None:
    config = Settings(_env_file=None, groq_api_key=None)

    with pytest.raises(MissingGroqApiKeyError, match="GROQ_API_KEY"):
        create_groq_chat_model(config)


def test_llm_factory_creates_model_without_making_api_call() -> None:
    config = Settings(
        _env_file=None,
        groq_api_key=SecretStr("test-key-not-for-real-use"),
        groq_model="test-model",
    )

    model = create_groq_chat_model(config)

    assert model.model_name == "test-model"

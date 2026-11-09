"""Centralized factory for creating Groq-backed LangChain chat models."""

from langchain_groq import ChatGroq

from app.config.settings import Settings, settings


class MissingGroqApiKeyError(RuntimeError):
    """Raised when an LLM is requested without a configured Groq API key."""


def create_groq_chat_model(config: Settings | None = None) -> ChatGroq:
    """Create a Groq chat model, reporting missing configuration without secrets."""
    active_settings = config or settings
    if active_settings.groq_api_key is None:
        raise MissingGroqApiKeyError(
            "GROQ_API_KEY is not configured. Set it in your environment or .env "
            "before requesting a Groq chat model."
        )

    return ChatGroq(
        model=active_settings.groq_model,
        api_key=active_settings.groq_api_key.get_secret_value(),
    )

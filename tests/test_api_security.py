"""API access control for loopback and remote clients."""

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config.settings import settings
from app.main import app


def test_remote_api_access_requires_configured_token() -> None:
    original_token = settings.api_access_token
    remote_client = TestClient(app, client=("203.0.113.8", 12345))
    try:
        settings.api_access_token = None
        denied = remote_client.get("/api/projects")
        assert denied.status_code == 403

        settings.api_access_token = SecretStr("test-only-api-access-token")
        missing_token = remote_client.get("/api/projects")
        assert missing_token.status_code == 401

        invalid_token = remote_client.get(
            "/api/projects",
            headers={"Authorization": "Bearer incorrect"},
        )
        assert invalid_token.status_code == 401

        accepted = remote_client.post(
            "/api/projects",
            headers={"Authorization": "Bearer test-only-api-access-token"},
        )
        assert accepted.status_code == 422
        assert remote_client.get("/health").status_code == 200
    finally:
        settings.api_access_token = original_token
        remote_client.close()

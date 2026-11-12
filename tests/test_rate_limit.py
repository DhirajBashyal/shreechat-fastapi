from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

client = TestClient(app)


def test_rate_limit():
    url = f"{settings.API_V1_PREFIX}/health"
    for i in range(5):
        response = client.get(url)
        assert response.status_code == 200, f"Failed at {i + 1}"
    response = client.get(url)
    assert response.status_code == 429

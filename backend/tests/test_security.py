from fastapi.testclient import TestClient

from app_whatsapp import server
from tests.test_agent import temp_db  # noqa: F401  (fixture)


def test_data_endpoints_need_the_admin_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret-key")
    client = TestClient(server.app)
    for method, path in [("get", "/leads"), ("get", "/appointments"), ("get", "/approvals"), ("get", "/routines"),
                         ("get", "/leads/wa-1/notes"), ("post", "/chat")]:
        assert getattr(client, method)(path).status_code == 401, path
    assert client.get("/leads", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/leads", headers={"Authorization": "Bearer s3cret-key"}).status_code == 200
    assert client.get("/leads", headers={"X-API-Key": "s3cret-key"}).status_code == 200
    assert client.get("/").status_code == 200  # health check stays open for the host


def test_webhook_refuses_unsigned_calls_once_whatsapp_is_live(monkeypatch):
    monkeypatch.setenv("WHATSAPP_TOKEN", "live-token")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    client = TestClient(server.app)
    assert client.post("/webhook", json={"entry": []}).status_code == 503
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    assert client.post("/webhook", json={"entry": []}).status_code == 401  # no signature

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app_whatsapp import server
from src.graph import create_graph
from src.graph_nodes.search_criteria_agent import Extraction
from tests.test_agent import ScriptedModel, call, temp_db  # noqa: F401  (fixture)

DEMO = "https://property-demo-rho.vercel.app"


def test_demo_site_may_call_api_other_sites_may_not():
    client = TestClient(server.app)
    pre = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-api-key,content-type"}
    ok = client.options("/chat", headers={"Origin": DEMO, **pre})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == DEMO
    bad = client.options("/chat", headers={"Origin": "https://evil.example", **pre})
    assert "access-control-allow-origin" not in bad.headers


def test_chat_returns_matches_and_requirement(monkeypatch):
    model = ScriptedModel(
        replies=[call("ToSearchAgent", {"request": "3BHK Whitefield 1.5-1.8 Cr"}), AIMessage(content="Here are 3 options.")],
        extractions=[Extraction(city="Bengaluru", locality="Whitefield", bhk=3, min_budget="1.5 Cr", max_budget="1.8 Cr", timeline_months=2)],
    )
    monkeypatch.setattr(server, "_graph", create_graph(llm=model))
    res = TestClient(server.app).post("/chat", headers={"Origin": DEMO},
                                      json={"thread_id": "demo-1", "message": "3BHK Whitefield 1.5 to 1.8 Cr", "name": "Rohan"}).json()
    assert res["reply"] == "Here are 3 options."
    assert res["criteria"]["locality"] == "Whitefield" and res["criteria"]["max_price"] == 18_000_000
    assert res["matches"][0]["project"] == "Aurum Greens" and "rera_id" in res["matches"][0]
    assert res["lead_score"]["band"] in ("Hot", "Warm", "Cold") and res["approval_code"] is None

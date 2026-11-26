"""WhatsApp Business Cloud API webhook + simple REST API.

Run from the backend folder:
    uvicorn app_whatsapp.server:app --port 8000

Customer messages -> agent reply on WhatsApp.
Booking requests -> broker gets "Reply YES 4F2A or NO 4F2A <reason>" on WhatsApp.
REST (for testing, a website widget, or a dashboard):
    POST /chat {"thread_id","message","name","phone","source"}
    POST /approvals/{code} {"approve": true|false, "reason": ""}
    GET  /leads, GET /appointments, GET /approvals
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.graph import create_graph
from src.runner import config_for, describe_pending, resume, run_turn
from src import campaigns
from src.util import db, memory
from src.util.llm import firm_name
from src.util.notify import list_templates, notify_broker, send_whatsapp

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp")

def require_admin(authorization: str = Header(default=""), x_api_key: str = Header(default="")):
    """Protects every endpoint that reads or changes customer data.

    Send the key as "Authorization: Bearer <ADMIN_API_KEY>" or "X-API-Key: <ADMIN_API_KEY>".
    Without ADMIN_API_KEY set (local development) the API stays open and a warning is logged at startup.
    """
    expected = os.getenv("ADMIN_API_KEY")
    if not expected:
        return
    given = x_api_key or (authorization[7:] if authorization.lower().startswith("bearer ") else "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "Missing or wrong API key")


protected = APIRouter(dependencies=[Depends(require_admin)])


@asynccontextmanager
async def lifespan(_app):
    if not os.getenv("ADMIN_API_KEY"):
        logger.warning("ADMIN_API_KEY is not set: lead and appointment endpoints are open. Set it before going public.")
    if os.getenv("WHATSAPP_TOKEN") and not os.getenv("WHATSAPP_APP_SECRET"):
        logger.warning("WHATSAPP_APP_SECRET is not set: incoming webhooks will be refused until it is.")
    db.ensure_listings()
    stops = []
    if os.getenv("ROUTINES_ENABLED", "1") != "0":
        from src.routines import start_background
        stops.append(start_background())
    if os.getenv("CAMPAIGNS_ENABLED", "1") != "0":
        stops.append(campaigns.start_worker())
    yield
    for stop in stops:
        stop.set()


app = FastAPI(title="Real estate AI agent (India)", lifespan=lifespan)
# Browsers only let a web page call this server from the sites listed here (the pitch demo).
# Every data endpoint still needs ADMIN_API_KEY; this only lets the browser send the request.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv(
        "CORS_ORIGINS", "https://property-demo-rho.vercel.app,http://localhost:8765").split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type"],
)
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_graph = None


def graph():
    global _graph
    if _graph is None:
        from langgraph.checkpoint.sqlite import SqliteSaver

        try:
            from src.util.llm import get_llm
            llm = get_llm()
        except Exception as exc:
            logger.error("AI model is not configured: %s", exc)
            raise HTTPException(503, "The AI model is not configured. Set OPENAI_API_KEY (or LLM_MODEL and that provider's key).")
        conn = sqlite3.connect(os.getenv("CHECKPOINT_DB", "data/checkpoints.db"), check_same_thread=False)
        _graph = create_graph(llm=llm, checkpointer=SqliteSaver(conn))
        db.ensure_listings()
    return _graph


def _digits(phone: Optional[str]) -> str:
    return re.sub(r"\D", "", phone or "")


def handle_customer(thread_id: str, text: str, lead: dict, reply_to: Optional[str]) -> dict:
    """Run one turn; message the customer and, if needed, ask the broker for approval."""
    with _locks[thread_id]:
        result = run_turn(graph(), thread_id, text, lead)
        approval_code = None
        if result.pending:
            approval_code = request_approval(thread_id)
            if not result.reply:
                result.reply = "Let me confirm this slot with our advisor. I'll message you here in a few minutes."
        if result.reply and reply_to:
            send_whatsapp(reply_to, result.reply)
        state = graph().get_state(config_for(thread_id)).values
        return {"reply": result.reply, "approval_code": approval_code, "lead_score": result.lead_score,
                "approval_summary": (db.get_approval(approval_code) or {}).get("summary") if approval_code else None,
                "criteria": state.get("search_criteria") or {}, "lead": state.get("lead") or {},
                "matches": [{k: m.get(k) for k in MATCH_FIELDS} for m in state.get("matches") or []]}


MATCH_FIELDS = ("listing_id", "project", "builder", "locality", "city", "bhk", "carpet_sqft", "price_inr",
                "ready_to_move", "possession", "rera_id", "highlight")


def request_approval(thread_id: str) -> str:
    snap = graph().get_state(config_for(thread_id))
    summary = describe_pending(snap.values["messages"][-1].tool_calls, snap.values)
    code = secrets.token_hex(2).upper()
    db.add_approval(code, thread_id, summary)
    notify_broker(f"Approval needed:\n{summary}\n\nReply YES {code} to confirm, or NO {code} <reason>.")
    return code


def handle_decision(code: str, approve: bool, reason: str = "") -> dict:
    approval = db.get_approval(code)
    if not approval or approval["status"] != "pending":
        raise HTTPException(404, f"No pending approval {code}")
    thread_id = approval["thread_id"]
    with _locks[thread_id]:
        db.decide_approval(code, "approved" if approve else "declined")
        result = resume(graph(), thread_id, approve, reason)
        if result.pending:  # agent immediately proposed another booking
            request_approval(thread_id)
    customer = thread_id[3:] if thread_id.startswith("wa-") else None
    if result.reply and customer:
        send_whatsapp(customer, result.reply)
    return {"thread_id": thread_id, "reply": result.reply}


# ---------- WhatsApp webhook ----------
@app.get("/webhook")
def verify(mode: str = Query(alias="hub.mode", default=""), token: str = Query(alias="hub.verify_token", default=""),
           challenge: str = Query(alias="hub.challenge", default="")):
    if mode == "subscribe" and token and token == os.getenv("WHATSAPP_VERIFY_TOKEN"):
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(403, "Verification failed")


@app.post("/webhook")
async def receive(request: Request, background: BackgroundTasks):
    body = await request.body()
    secret = os.getenv("WHATSAPP_APP_SECRET")
    if os.getenv("WHATSAPP_TOKEN") and not secret:
        # With a real WhatsApp number connected, unsigned webhooks could be forged to spend AI credits.
        raise HTTPException(503, "Set WHATSAPP_APP_SECRET to accept WhatsApp webhooks")
    if secret:
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", "")):
            raise HTTPException(401, "Bad signature")
    data = await request.json()
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for status in value.get("statuses", []):  # delivery receipts for messages we sent
                campaigns.apply_status(status)
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                # Meta re-delivers when it doesn't get a fast 200; handle each message id once.
                if msg.get("id") and not db.claim_message(msg["id"]):
                    logger.info("Skipping repeat delivery of %s", msg["id"])
                    continue
                sender = msg.get("from", "")
                if msg.get("type") == "text":
                    text = msg["text"]["body"]
                elif msg.get("type") == "button":  # quick-reply button on a template, e.g. "Interested" / "Stop promotions"
                    text = msg["button"].get("text", "")
                elif msg.get("type") == "interactive":
                    reply = msg["interactive"].get("button_reply") or msg["interactive"].get("list_reply") or {}
                    text = reply.get("title", "")
                else:
                    send_whatsapp(sender, "Sorry, I can read text messages only for now. Please type your question.")
                    continue
                background.add_task(route_inbound, sender, text, names.get(sender))
    return {"status": "ok"}  # Meta needs a fast 200; work happens in the background


def route_inbound(sender: str, text: str, profile_name: Optional[str]) -> None:
    try:
        broker = _digits(os.getenv("BROKER_WHATSAPP"))
        if broker and _digits(sender) == broker:
            m = re.match(r"\s*(yes|no|haan|nahi)\s+([0-9a-f]{4})\b\s*(.*)", text, re.I | re.S)
            if not m:
                send_whatsapp(sender, "To approve a visit reply YES <code>, to decline reply NO <code> <reason>.")
                return
            approve = m.group(1).lower() in ("yes", "haan")
            out = handle_decision(m.group(2), approve, m.group(3).strip())
            send_whatsapp(sender, f"{'Confirmed' if approve else 'Declined'}. Customer told: {out['reply'][:300]}")
            return
        phone = _digits(sender)
        check = campaigns.on_inbound(phone, text)
        if check["action"] == "opted_out":
            send_whatsapp(sender, f"Done. You won't get offers from {firm_name()} on WhatsApp any more. Reply START if you change your mind.")
            return
        if check["action"] == "opted_in":
            send_whatsapp(sender, f"Welcome back! You'll get property updates from {firm_name()} again. Reply STOP any time.")
            return
        lead = {"phone": "+" + phone, "source": "WhatsApp"}
        if profile_name:
            lead["name"] = profile_name
        if check["action"] == "campaign_reply":
            lead["source"] = f"Campaign: {check['campaign']}"
            memory.add_notes(f"wa-{phone}", [f"Replied to the '{check['campaign']}' WhatsApp campaign"], by="agent")
        handle_customer(f"wa-{phone}", text, lead, reply_to=sender)
    except HTTPException as exc:
        send_whatsapp(sender, str(exc.detail))
    except Exception:
        logger.exception("Failed to handle message from %s", sender)


# ---------- REST API ----------
class ChatIn(BaseModel):
    thread_id: str
    message: str
    name: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = "Website chat"


class DecisionIn(BaseModel):
    approve: bool
    reason: str = ""


@app.get("/")
def health():
    return {"status": "ok", "service": "real-estate-agent-india"}


@protected.post("/chat")
def chat(body: ChatIn):
    lead = {k: v for k, v in {"name": body.name, "phone": body.phone, "source": body.source}.items() if v}
    return handle_customer(body.thread_id, body.message, lead, reply_to=None)


@protected.post("/approvals/{code}")
def decide(code: str, body: DecisionIn):
    return handle_decision(code, body.approve, body.reason)


@protected.get("/approvals")
def approvals():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM approvals ORDER BY created_at DESC")]


@protected.get("/leads")
def leads():
    return db.list_leads()


@protected.get("/appointments")
def appointments():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM appointments ORDER BY start_ist")]


class NoteIn(BaseModel):
    note: str


@protected.get("/leads/{thread_id}/notes")
def lead_notes(thread_id: str):
    return memory.read_notes(thread_id)


@protected.post("/leads/{thread_id}/notes")
def add_lead_note(thread_id: str, body: NoteIn):
    """The broker's own notes ("prefers calls after 7 pm") go into the same memory the agent reads."""
    return {"added": memory.add_notes(thread_id, [body.note], by="broker")}


@protected.get("/routines")
def routines_history():
    return db.routine_history()


@protected.post("/routines/{name}/run")
def run_routine(name: str):
    """Run one routine now, ignoring its schedule (for demos and testing)."""
    from src.routines import ROUTINES, run_due
    if name not in {r.name for r in ROUTINES}:
        raise HTTPException(404, f"No routine {name}")
    return run_due(force=name)


# ---------- campaigns ----------
class CampaignIn(BaseModel):
    name: str
    template_name: str
    language: str = "en"
    params: list[str] = []  # one entry per {{n}} in the template body, e.g. ["{first_name}", "{locality|Pune}"]
    csv: str  # the contact file's text
    header_image_url: Optional[str] = None
    source_file: str = ""


class TestIn(BaseModel):
    phone: str


def _campaign_or_404(cid: int) -> dict:
    c = campaigns.get_campaign(cid)
    if not c:
        raise HTTPException(404, f"No campaign {cid}")
    return c


@protected.get("/whatsapp/templates")
def whatsapp_templates():
    """Approved templates from WhatsApp Manager (needs WHATSAPP_BUSINESS_ACCOUNT_ID)."""
    try:
        return list_templates()
    except Exception as exc:
        raise HTTPException(502, f"Could not load templates from Meta: {exc}")


@protected.post("/campaigns")
def create_campaign(body: CampaignIn):
    """Create a draft campaign from CSV text. Returns counts, sample variables and row problems to review."""
    try:
        return campaigns.create_campaign(body.name, body.template_name, body.language, body.params, body.csv,
                                         body.header_image_url, body.source_file)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@protected.get("/campaigns")
def list_all_campaigns():
    return {"campaigns": campaigns.list_campaigns(), "daily_limit": campaigns.daily_limit(),
            "sending_hours": "%d:00-%d:00 IST" % campaigns.sending_hours()}


@protected.get("/campaigns/{cid}")
def campaign_detail(cid: int):
    return {**_campaign_or_404(cid), "stats": campaigns.stats(cid)}


@protected.get("/campaigns/{cid}/recipients")
def campaign_recipients(cid: int, status: Optional[str] = None):
    _campaign_or_404(cid)
    return campaigns.recipients(cid, status, limit=500)


@protected.get("/campaigns/{cid}/export.csv")
def campaign_export(cid: int):
    _campaign_or_404(cid)
    return Response(campaigns.export_csv(cid), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="campaign-{cid}.csv"'})


@protected.post("/campaigns/{cid}/test")
def campaign_test(cid: int, body: TestIn):
    _campaign_or_404(cid)
    try:
        return campaigns.send_test(cid, body.phone)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@protected.post("/campaigns/{cid}/{action}")
def campaign_action(cid: int, action: str):
    target = {"start": "running", "resume": "running", "pause": "paused", "cancel": "cancelled"}.get(action)
    if not target:
        raise HTTPException(404, f"Unknown action {action}")
    _campaign_or_404(cid)
    try:
        return campaigns.set_status(cid, target)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@protected.get("/opt-outs")
def opt_outs():
    return campaigns.list_opt_outs()


@protected.post("/opt-outs")
def add_opt_out(body: TestIn):
    phone = campaigns.normalize_phone(body.phone)
    if not phone:
        raise HTTPException(400, "Invalid phone number")
    campaigns.opt_out(phone, reason="added by broker")
    return {"opted_out": "+" + phone}


@app.get("/admin", include_in_schema=False)
def admin_page():
    """Campaign dashboard. The page holds no data itself; it asks for ADMIN_API_KEY and calls the API."""
    return FileResponse(Path(__file__).with_name("admin.html"), media_type="text/html")


app.include_router(protected)

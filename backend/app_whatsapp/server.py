"""WhatsApp Business Cloud API webhook + REST API, shared by many brokers (tenants).

Run from the backend folder:
    uvicorn app_whatsapp.server:app --port 8000

Who is who:
  - The platform owner holds ADMIN_API_KEY. It manages brokers (/platform/...) and can act for any
    broker by sending "X-Tenant: <broker id>" (default: the "default" broker).
  - Each broker gets their own API key (bm_<id>_...). It only ever sees that broker's data.
  - Incoming WhatsApp messages are routed to a broker by the business number they were sent to.

Broker endpoints (broker key or platform key): /chat, /leads, /appointments, /approvals, /campaigns,
/opt-outs, /listings, /routines, /me.  Platform endpoints: /platform/tenants...
Pages: /admin (broker campaign dashboard), /platform (add and manage brokers).
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

from src import campaigns, tenancy
from src.graph import create_graph
from src.runner import config_for, describe_pending, resume, run_turn
from src.tenancy import Tenant
from src.util import db, memory
from src.util.notify import list_templates, notify_broker, send_whatsapp

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp")


# ---------- auth ----------
def _given_key(authorization: str, x_api_key: str) -> str:
    return x_api_key or (authorization[7:] if authorization.lower().startswith("bearer ") else "")


def _is_platform_key(key: str) -> bool:
    expected = os.getenv("ADMIN_API_KEY")
    return bool(expected) and hmac.compare_digest(key.encode(), expected.encode())


def current_tenant(authorization: str = Header(default=""), x_api_key: str = Header(default=""),
                   x_tenant: str = Header(default="")) -> Tenant:
    """The broker this request is for. Broker keys map to their broker; the platform key may pick one with X-Tenant."""
    key = _given_key(authorization, x_api_key)
    if _is_platform_key(key) or (not os.getenv("ADMIN_API_KEY") and not key):  # no key configured = local development
        t = tenancy.get(x_tenant or tenancy.DEFAULT)
        if not t:
            raise HTTPException(404, f"No broker '{x_tenant}'")
        return t
    t = tenancy.by_api_key(key)
    if not t:
        raise HTTPException(401, "Missing or wrong API key")
    if t.status != "active":
        raise HTTPException(403, "This account is paused. Contact your provider.")
    return t


def require_platform(authorization: str = Header(default=""), x_api_key: str = Header(default="")) -> None:
    if not os.getenv("ADMIN_API_KEY"):
        raise HTTPException(503, "Set ADMIN_API_KEY on the server to manage brokers")
    if not _is_platform_key(_given_key(authorization, x_api_key)):
        raise HTTPException(401, "This needs the platform key (ADMIN_API_KEY)")


protected = APIRouter(dependencies=[Depends(current_tenant)])
platform = APIRouter(prefix="/platform", dependencies=[Depends(require_platform)])


@asynccontextmanager
async def lifespan(_app):
    if not os.getenv("ADMIN_API_KEY"):
        logger.warning("ADMIN_API_KEY is not set: the API is open. Set it before going public.")
    tenancy.ensure_default()
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
# Every data endpoint still needs a key; this only lets the browser send the request.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv(
        "CORS_ORIGINS", "https://property-demo-rho.vercel.app,http://localhost:8765").split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "X-API-Key", "X-Tenant", "Content-Type"],
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


def _thread(t: Tenant, local_id: str) -> str:
    """Conversation ids are namespaced by broker, so the same customer can talk to two brokers."""
    return f"{t.id}:{local_id}"


def _owned_thread(t: Tenant, thread_id: str) -> str:
    if not thread_id.startswith(f"{t.id}:"):
        raise HTTPException(404, "No such lead")
    return thread_id


MATCH_FIELDS = ("listing_id", "project", "builder", "locality", "city", "bhk", "carpet_sqft", "price_inr",
                "ready_to_move", "possession", "rera_id", "highlight")
LIMIT_REPLY = "Thanks for your message! Our team will get back to you shortly."


def handle_customer(t: Tenant, thread_id: str, text: str, lead: dict, reply_to: Optional[str]) -> dict:
    """Run one turn for one broker; message the customer and, if needed, ask the broker for approval."""
    usage = db.get_usage(t.id)
    if t.ai_monthly_limit and usage["ai_turns"] >= t.ai_monthly_limit:
        # Over the plan's AI allowance: answer politely, alert the broker, skip the AI call.
        if reply_to:
            send_whatsapp(reply_to, LIMIT_REPLY, t)
        notify_broker(f"Your monthly AI reply limit ({t.ai_monthly_limit}) is used up. New message from "
                      f"{lead.get('name') or lead.get('phone') or 'a customer'}: {text[:200]}", t)
        return {"reply": LIMIT_REPLY, "approval_code": None, "lead_score": {}, "approval_summary": None,
                "criteria": {}, "lead": lead, "matches": [], "limit_reached": True}
    with _locks[thread_id]:
        result = run_turn(graph(), thread_id, text, lead, tenant=t.id)
        db.add_usage(t.id, "ai_turns")
        approval_code = None
        if result.pending:
            approval_code = request_approval(t, thread_id)
            if not result.reply:
                result.reply = "Let me confirm this slot with our advisor. I'll message you here in a few minutes."
        if result.reply and reply_to:
            send_whatsapp(reply_to, result.reply, t)
        state = graph().get_state(config_for(thread_id, t.id)).values
        return {"reply": result.reply, "approval_code": approval_code, "lead_score": result.lead_score,
                "approval_summary": (db.get_approval(approval_code) or {}).get("summary") if approval_code else None,
                "criteria": state.get("search_criteria") or {}, "lead": state.get("lead") or {},
                "matches": [{k: m.get(k) for k in MATCH_FIELDS} for m in state.get("matches") or []]}


def request_approval(t: Tenant, thread_id: str) -> str:
    snap = graph().get_state(config_for(thread_id, t.id))
    summary = describe_pending(snap.values["messages"][-1].tool_calls, snap.values, t.id)
    code = secrets.token_hex(2).upper()
    while db.approval_code_taken(code):
        code = secrets.token_hex(2).upper()
    db.add_approval(code, thread_id, summary, t.id)
    notify_broker(f"Approval needed:\n{summary}\n\nReply YES {code} to confirm, or NO {code} <reason>.", t)
    return code


def handle_decision(t: Tenant, code: str, approve: bool, reason: str = "") -> dict:
    approval = db.get_approval(code, t.id)
    if not approval or approval["status"] != "pending":
        raise HTTPException(404, f"No pending approval {code}")
    thread_id = approval["thread_id"]
    with _locks[thread_id]:
        db.decide_approval(code, "approved" if approve else "declined")
        result = resume(graph(), thread_id, approve, reason, tenant=t.id)
        if result.pending:  # agent immediately proposed another booking
            request_approval(t, thread_id)
    local = thread_id.split(":", 1)[-1]
    customer = local[3:] if local.startswith("wa-") else None
    if result.reply and customer:
        send_whatsapp(customer, result.reply, t)
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
    app_secrets = tenancy.webhook_secrets()
    if tenancy.any_whatsapp_live() and not app_secrets:
        # With a real WhatsApp number connected, unsigned webhooks could be forged to spend AI credits.
        raise HTTPException(503, "Set WHATSAPP_APP_SECRET to accept WhatsApp webhooks")
    if app_secrets:
        given = request.headers.get("X-Hub-Signature-256", "")
        if not any(hmac.compare_digest("sha256=" + hmac.new(s.encode(), body, hashlib.sha256).hexdigest(), given) for s in app_secrets):
            raise HTTPException(401, "Bad signature")
    data = await request.json()
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for status in value.get("statuses", []):  # delivery receipts for messages we sent
                campaigns.apply_status(status)
            if not value.get("messages"):
                continue
            t = tenancy.by_phone_number_id((value.get("metadata") or {}).get("phone_number_id"))
            if not t or t.status != "active":
                logger.warning("Message for an unknown or paused WhatsApp number %s ignored", (value.get("metadata") or {}).get("phone_number_id"))
                continue
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for msg in value["messages"]:
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
                    send_whatsapp(sender, "Sorry, I can read text messages only for now. Please type your question.", t)
                    continue
                background.add_task(route_inbound, t.id, sender, text, names.get(sender))
    return {"status": "ok"}  # Meta needs a fast 200; work happens in the background


def route_inbound(tenant_id: str, sender: str, text: str, profile_name: Optional[str]) -> None:
    t = tenancy.get(tenant_id)
    if not t:
        return
    try:
        broker = _digits(t.broker_number())
        if broker and _digits(sender) == broker:
            m = re.match(r"\s*(yes|no|haan|nahi)\s+([0-9a-f]{4})\b\s*(.*)", text, re.I | re.S)
            if not m:
                send_whatsapp(sender, "To approve a visit reply YES <code>, to decline reply NO <code> <reason>.", t)
                return
            approve = m.group(1).lower() in ("yes", "haan")
            out = handle_decision(t, m.group(2), approve, m.group(3).strip())
            send_whatsapp(sender, f"{'Confirmed' if approve else 'Declined'}. Customer told: {out['reply'][:300]}", t)
            return
        phone = _digits(sender)
        check = campaigns.on_inbound(phone, text, t.id)
        if check["action"] == "opted_out":
            send_whatsapp(sender, f"Done. You won't get offers from {t.name} on WhatsApp any more. Reply START if you change your mind.", t)
            return
        if check["action"] == "opted_in":
            send_whatsapp(sender, f"Welcome back! You'll get property updates from {t.name} again. Reply STOP any time.", t)
            return
        lead = {"phone": "+" + phone, "source": "WhatsApp"}
        if profile_name:
            lead["name"] = profile_name
        thread_id = _thread(t, f"wa-{phone}")
        if check["action"] == "campaign_reply":
            lead["source"] = f"Campaign: {check['campaign']}"
            memory.add_notes(thread_id, [f"Replied to the '{check['campaign']}' WhatsApp campaign"], by="agent")
        handle_customer(t, thread_id, text, lead, reply_to=sender)
    except HTTPException as exc:
        send_whatsapp(sender, str(exc.detail), t)
    except Exception:
        logger.exception("Failed to handle message for %s from %s", t.id, sender)


# ---------- broker API ----------
class ChatIn(BaseModel):
    thread_id: str
    message: str
    name: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = "Website chat"


class DecisionIn(BaseModel):
    approve: bool
    reason: str = ""


class NoteIn(BaseModel):
    note: str


class CsvIn(BaseModel):
    csv: str


@app.get("/")
def health():
    return {"status": "ok", "service": "real-estate-agent-india"}


@protected.get("/me")
def me(t: Tenant = Depends(current_tenant)):
    """This broker's profile, WhatsApp status and this month's usage."""
    return tenancy.summary(t)


@protected.post("/chat")
def chat(body: ChatIn, t: Tenant = Depends(current_tenant)):
    lead = {k: v for k, v in {"name": body.name, "phone": body.phone, "source": body.source}.items() if v}
    return handle_customer(t, _thread(t, body.thread_id), body.message, lead, reply_to=None)


@protected.post("/approvals/{code}")
def decide(code: str, body: DecisionIn, t: Tenant = Depends(current_tenant)):
    return handle_decision(t, code, body.approve, body.reason)


@protected.get("/approvals")
def approvals(t: Tenant = Depends(current_tenant)):
    return db.list_approvals(t.id)


@protected.get("/leads")
def leads(t: Tenant = Depends(current_tenant)):
    return db.list_leads(t.id)


@protected.get("/appointments")
def appointments(t: Tenant = Depends(current_tenant)):
    return db.list_appointments(t.id)


@protected.get("/leads/{thread_id}/notes")
def lead_notes(thread_id: str, t: Tenant = Depends(current_tenant)):
    return memory.read_notes(_owned_thread(t, thread_id))


@protected.post("/leads/{thread_id}/notes")
def add_lead_note(thread_id: str, body: NoteIn, t: Tenant = Depends(current_tenant)):
    """The broker's own notes ("prefers calls after 7 pm") go into the same memory the agent reads."""
    return {"added": memory.add_notes(_owned_thread(t, thread_id), [body.note], by="broker")}


@protected.get("/listings")
def listings(t: Tenant = Depends(current_tenant)):
    return db.list_listings(t.id)


@protected.post("/listings")
def upload_listings(body: CsvIn, t: Tenant = Depends(current_tenant)):
    """Replace this broker's inventory with a CSV (same columns as data/india_listings.csv)."""
    try:
        return {"loaded": db.replace_listings(t.id, body.csv)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@protected.get("/routines")
def routines_history(t: Tenant = Depends(current_tenant)):
    return db.routine_history(t.id)


@protected.post("/routines/{name}/run")
def run_routine(name: str, t: Tenant = Depends(current_tenant)):
    """Run one routine now for this broker, ignoring its schedule (for demos and testing)."""
    from src.routines import ROUTINES, run_due
    if name not in {r.name for r in ROUTINES}:
        raise HTTPException(404, f"No routine {name}")
    return run_due(force=name, tenant=t.id)


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


def _campaign_or_404(cid: int, t: Tenant) -> dict:
    c = campaigns.get_campaign(cid, t.id)
    if not c:
        raise HTTPException(404, f"No campaign {cid}")
    return c


@protected.get("/whatsapp/templates")
def whatsapp_templates(t: Tenant = Depends(current_tenant)):
    """Approved templates from this broker's WhatsApp Manager."""
    try:
        return list_templates(t)
    except Exception as exc:
        raise HTTPException(502, f"Could not load templates from Meta: {exc}")


@protected.post("/campaigns")
def create_campaign(body: CampaignIn, t: Tenant = Depends(current_tenant)):
    """Create a draft campaign from CSV text. Returns counts, sample variables and row problems to review."""
    try:
        return campaigns.create_campaign(body.name, body.template_name, body.language, body.params, body.csv,
                                         body.header_image_url, body.source_file, tenant_id=t.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@protected.get("/campaigns")
def list_all_campaigns(t: Tenant = Depends(current_tenant)):
    return {"campaigns": campaigns.list_campaigns(t.id), "daily_limit": t.limit_per_day(),
            "sending_hours": "%d:00-%d:00 IST" % t.hours(), "firm": t.name}


@protected.get("/campaigns/{cid}")
def campaign_detail(cid: int, t: Tenant = Depends(current_tenant)):
    return {**_campaign_or_404(cid, t), "stats": campaigns.stats(cid)}


@protected.get("/campaigns/{cid}/recipients")
def campaign_recipients(cid: int, status: Optional[str] = None, t: Tenant = Depends(current_tenant)):
    _campaign_or_404(cid, t)
    return campaigns.recipients(cid, status, limit=500)


@protected.get("/campaigns/{cid}/export.csv")
def campaign_export(cid: int, t: Tenant = Depends(current_tenant)):
    _campaign_or_404(cid, t)
    return Response(campaigns.export_csv(cid), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="campaign-{cid}.csv"'})


@protected.post("/campaigns/{cid}/test")
def campaign_test(cid: int, body: TestIn, t: Tenant = Depends(current_tenant)):
    _campaign_or_404(cid, t)
    try:
        return campaigns.send_test(cid, body.phone, tenant_id=t.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@protected.post("/campaigns/{cid}/{action}")
def campaign_action(cid: int, action: str, t: Tenant = Depends(current_tenant)):
    target = {"start": "running", "resume": "running", "pause": "paused", "cancel": "cancelled"}.get(action)
    if not target:
        raise HTTPException(404, f"Unknown action {action}")
    _campaign_or_404(cid, t)
    try:
        return campaigns.set_status(cid, target, tenant_id=t.id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@protected.get("/opt-outs")
def opt_outs(t: Tenant = Depends(current_tenant)):
    return campaigns.list_opt_outs(t.id)


@protected.post("/opt-outs")
def add_opt_out(body: TestIn, t: Tenant = Depends(current_tenant)):
    phone = campaigns.normalize_phone(body.phone)
    if not phone:
        raise HTTPException(400, "Invalid phone number")
    campaigns.opt_out(phone, reason="added by broker", tenant_id=t.id)
    return {"opted_out": "+" + phone}


# ---------- platform (owner) API ----------
class TenantIn(BaseModel):
    name: str
    id: Optional[str] = None
    assistant_name: Optional[str] = "Riya"
    broker_whatsapp: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_token: Optional[str] = None
    whatsapp_business_account_id: Optional[str] = None
    whatsapp_app_secret: Optional[str] = None
    daily_limit: Optional[int] = None
    ai_monthly_limit: Optional[int] = 300
    campaign_hours: Optional[str] = None
    notes: Optional[str] = None
    sample_listings: bool = False


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    assistant_name: Optional[str] = None
    broker_whatsapp: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_token: Optional[str] = None
    whatsapp_business_account_id: Optional[str] = None
    whatsapp_app_secret: Optional[str] = None
    daily_limit: Optional[int] = None
    ai_monthly_limit: Optional[int] = None
    campaign_hours: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


@platform.get("/tenants")
def platform_tenants():
    return [tenancy.summary(t) for t in tenancy.list_all()]


@platform.post("/tenants")
def platform_create_tenant(body: TenantIn):
    """Add a broker. The response holds their API key once; it can't be shown again (rotate to get a new one)."""
    fields = body.model_dump(exclude={"name", "id", "sample_listings"})
    try:
        t, key = tenancy.create(body.name, body.id, **fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if body.sample_listings:
        db.load_listings(tenant_id=t.id)
    return {**tenancy.summary(t), "api_key": key}


@platform.get("/tenants/{tenant_id}")
def platform_tenant(tenant_id: str):
    t = tenancy.get(tenant_id)
    if not t:
        raise HTTPException(404, "No such broker")
    return tenancy.summary(t)


@platform.post("/tenants/{tenant_id}")
def platform_update_tenant(tenant_id: str, body: TenantUpdate):
    try:
        return tenancy.summary(tenancy.update(tenant_id, **body.model_dump(exclude_none=True)))
    except KeyError:
        raise HTTPException(404, "No such broker")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@platform.post("/tenants/{tenant_id}/rotate-key")
def platform_rotate_key(tenant_id: str):
    """Issue a new key for a broker. Their old key stops working at once."""
    try:
        return {"id": tenant_id, "api_key": tenancy.rotate_key(tenant_id)}
    except KeyError:
        raise HTTPException(404, "No such broker")


@platform.post("/tenants/{tenant_id}/{action}")
def platform_tenant_status(tenant_id: str, action: str):
    status = {"pause": "paused", "activate": "active"}.get(action)
    if not status:
        raise HTTPException(404, f"Unknown action {action}")
    if tenant_id == tenancy.DEFAULT and status == "paused":
        raise HTTPException(400, "The default broker can't be paused")
    try:
        return tenancy.summary(tenancy.update(tenant_id, status=status))
    except KeyError:
        raise HTTPException(404, "No such broker")


# ---------- pages ----------
@app.get("/admin", include_in_schema=False)
def admin_page():
    """Broker campaign dashboard. The page holds no data itself; it asks for the broker's key."""
    return FileResponse(Path(__file__).with_name("admin.html"), media_type="text/html")


@app.get("/platform", include_in_schema=False)
def platform_page():
    """Owner page to add and manage brokers. Needs the platform key."""
    return FileResponse(Path(__file__).with_name("platform.html"), media_type="text/html")


app.include_router(protected)
app.include_router(platform)

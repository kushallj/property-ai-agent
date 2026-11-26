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
from typing import Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel

from src.graph import create_graph
from src.runner import config_for, describe_pending, resume, run_turn
from src.util import db, memory
from src.util.notify import notify_broker, send_whatsapp

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp")

@asynccontextmanager
async def lifespan(_app):
    stop = None
    if os.getenv("ROUTINES_ENABLED", "1") != "0":
        from src.routines import start_background
        stop = start_background()
    yield
    if stop:
        stop.set()


app = FastAPI(title="Real estate AI agent (India)", lifespan=lifespan)
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_graph = None


def graph():
    global _graph
    if _graph is None:
        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(os.getenv("CHECKPOINT_DB", "data/checkpoints.db"), check_same_thread=False)
        _graph = create_graph(checkpointer=SqliteSaver(conn))
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
        return {"reply": result.reply, "approval_code": approval_code, "lead_score": result.lead_score}


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
    if secret:
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", "")):
            raise HTTPException(401, "Bad signature")
    data = await request.json()
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                # Meta re-delivers when it doesn't get a fast 200; handle each message id once.
                if msg.get("id") and not db.claim_message(msg["id"]):
                    logger.info("Skipping repeat delivery of %s", msg["id"])
                    continue
                sender = msg.get("from", "")
                if msg.get("type") == "text":
                    text = msg["text"]["body"]
                elif msg.get("type") == "button":
                    text = msg["button"].get("text", "")
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
        lead = {"phone": "+" + _digits(sender), "source": "WhatsApp"}
        if profile_name:
            lead["name"] = profile_name
        handle_customer(f"wa-{_digits(sender)}", text, lead, reply_to=sender)
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


@app.post("/chat")
def chat(body: ChatIn):
    lead = {k: v for k, v in {"name": body.name, "phone": body.phone, "source": body.source}.items() if v}
    return handle_customer(body.thread_id, body.message, lead, reply_to=None)


@app.post("/approvals/{code}")
def decide(code: str, body: DecisionIn):
    return handle_decision(code, body.approve, body.reason)


@app.get("/approvals")
def approvals():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM approvals ORDER BY created_at DESC")]


@app.get("/leads")
def leads():
    return db.list_leads()


@app.get("/appointments")
def appointments():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM appointments ORDER BY start_ist")]


class NoteIn(BaseModel):
    note: str


@app.get("/leads/{thread_id}/notes")
def lead_notes(thread_id: str):
    return memory.read_notes(thread_id)


@app.post("/leads/{thread_id}/notes")
def add_lead_note(thread_id: str, body: NoteIn):
    """The broker's own notes ("prefers calls after 7 pm") go into the same memory the agent reads."""
    return {"added": memory.add_notes(thread_id, [body.note], by="broker")}


@app.get("/routines")
def routines_history():
    return db.routine_history()


@app.post("/routines/{name}/run")
def run_routine(name: str):
    """Run one routine now, ignoring its schedule (for demos and testing)."""
    from src.routines import ROUTINES, run_due
    if name not in {r.name for r in ROUTINES}:
        raise HTTPException(404, f"No routine {name}")
    return run_due(force=name)

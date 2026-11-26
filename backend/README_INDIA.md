# Real estate AI agent: India edition

A WhatsApp-first sales agent for Indian brokers, adapted from the LangGraph agent in this repo.
It qualifies buyers, searches your inventory, scores each lead, books site visits or video
walkthroughs, and asks the broker to approve every booking on WhatsApp before it is confirmed.

## What changed from the original

| Area | Original (US) | India edition |
|---|---|---|
| Data | Kaggle US listings, dollars | Your inventory CSV: locality, BHK, carpet area, ₹ price, possession, RERA ID (`data/india_listings.csv`, sample data) |
| Understanding | City/state, beds/baths | City + locality, BHK, budgets in lakhs/crores ("80 L", "1.2 Cr"), timeline, home loan status, purpose, NRI, Hinglish |
| Replies | Voice-call style | Short WhatsApp messages in the buyer's language, prices as ₹95 L / ₹1.62 Cr, EMI estimates |
| Lead scoring | none | New `lead_scorer` node: score out of 100 with reasons; saved to a `leads` table; broker alerted on Hot leads |
| Appointments | Google Calendar only, email required | Local calendar in IST (Google Calendar optional), phone number instead of email, site or video mode |
| Approval | Terminal prompt | Broker replies `YES 4F2A` / `NO 4F2A reason` on WhatsApp, or via REST |
| Channel | Retell voice, Twilio SMS, web socket | WhatsApp Business Cloud API webhook + `/chat` REST endpoint (Retell server kept in `app-retell/`) |
| Stack | LangGraph 0.1, pinned 2024 deps | LangGraph 1.2, LangChain 1.5, any model via `LLM_MODEL` |

Also fixed: a missing tool reply when handing over to the appointment agent (caused API errors on the
next turn), search-criteria merge using wrong field names, and a calendar tool that could never be called.

## How a conversation flows

```
customer message
  -> main_agent (qualifies, one question at a time)
       -> search_criteria_agent (extracts requirement + profile)
       -> query_database (SQL search, widens locality -> city -> nearest price)
       -> lead_scorer (score, CRM update, hot-lead alert to broker)
       -> main_agent (replies with up to 3 listings)
  -> enter_appointment_agent -> appointment_agent
       -> check_availability / list_my_appointments (run immediately)
       -> book / reschedule / cancel  ** paused until the broker approves **
```

Score rubric: Timeline 25 · Budget fit 20 · Financing 15 · Buying intent 20 · Contact info 10 · Engagement 10.
Hot ≥ 75, Warm 50–74, Cold < 50. Edit `src/util/lead_scoring.py` to tune it for a client.

## Ideas borrowed from OpenMausBot

Three features follow patterns from [OpenMausBot](https://github.com/milind-soni/OpenMausBot) (Apache-2.0).
The ideas were reimplemented here; no code was copied.

**1. Lead memory** (`src/util/memory.py`). One Markdown file per customer in `data/memory/`, e.g.
`wa-919800004521.md`. The search step saves lasting personal details ("Wife works in Hinjewadi",
"Wants east-facing, not ground floor") and every reply reads them, so a buyer returning weeks later
is remembered. The broker can open and edit the file, or add notes over the API:
`POST /leads/{thread_id}/notes {"note": "Prefers calls after 7 pm"}`.

**2. Routines** (`src/routines.py`). The server checks every minute; each routine has a schedule, a
cheap check in plain code, and an action that runs only when the check finds work. A ledger table
makes each run happen once per period, even after a restart.

| Routine | When (IST) | What it does |
|---|---|---|
| `morning_briefing` | daily 08:30 | Broker's WhatsApp summary: new leads, hot leads to call, today's visits, pending approvals |
| `visit_reminders` | hourly 18:00–21:00 | Reminds each customer of tomorrow's visit, once |
| `weekly_shortlist` | Monday 10:00 | Sends quiet Warm/Cold leads up to 2 listings they haven't been sent; skips anyone with nothing new |

Run by hand: `python -m src.routines` (what's due now) or `python -m src.routines --force morning_briefing`.
Over the API: `POST /routines/morning_briefing/run`, history at `GET /routines`. Set `ROUTINES_ENABLED=0` to turn off the background loop.

**3. Duplicate-safe webhook.** Meta re-delivers a webhook if it doesn't get a fast reply. Every
incoming WhatsApp message id is recorded once (`processed_messages` table); repeats are skipped, so a
customer never gets two answers to one message.

## Run it

From `backend/`:

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; use source .venv/bin/activate on Mac/Linux
pip install -r requirements.txt
copy .env.example .env          # then add OPENAI_API_KEY (or another provider, see LLM_MODEL)
python -m src.util.db           # load listings from data/india_listings.csv
python main.py --demo           # sample Whitefield buyer; you approve the visit as the broker
python main.py                  # chat as any customer
```

Tests run without any API key (a scripted model stands in for the LLM):

```bash
python -m pytest tests
```

## Use the client's own inventory

Export their listings to a CSV with the same columns as `data/india_listings.csv`
(`listing_id, project, builder, locality, city, state, bhk, carpet_sqft, price_inr, ready_to_move, possession, rera_id, amenities, highlight`),
then run `python -m src.util.db` again. `price_inr` is in rupees (1.62 Cr = 16200000).

## Host it on Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/kushallj/property-ai-agent)

The repo has a Render Blueprint (`render.yaml`) and a `backend/Dockerfile`. It creates one web service in
Singapore with a 1 GB disk at `/var/data` for the database, conversation history and lead memory.
Expect about $7/month for the Starter instance plus about $0.25/month for the disk. A paid instance is
needed because free ones sleep, miss WhatsApp messages and routines, and lose their data.

1. Sign in at render.com with GitHub and allow access to `kushallj/property-ai-agent`.
2. Click the button above (or **New → Blueprint** and pick the repo).
3. Fill in the secrets Render asks for. Only `OPENAI_API_KEY` is needed to start; leave the WhatsApp ones
   blank until you connect a number. `ADMIN_API_KEY` and `WHATSAPP_VERIFY_TOKEN` are generated for you.
4. **Apply**. The first build takes a few minutes. Your URL looks like `https://property-ai-agent.onrender.com`.
5. Check it: open the URL (shows `{"status":"ok"}`), then copy `ADMIN_API_KEY` from **Environment** and try:
   ```bash
   curl -X POST https://<your-url>/chat -H "X-API-Key: <ADMIN_API_KEY>" -H "Content-Type: application/json" \
     -d "{\"thread_id\":\"test1\",\"message\":\"3BHK in Whitefield around 1.6 Cr\",\"name\":\"Test\"}"
   ```

Every push to `main` redeploys automatically. Security built in:
- Every lead, appointment, chat, approval and routine endpoint needs `ADMIN_API_KEY`
  (`Authorization: Bearer <key>` or `X-API-Key: <key>`). Only `/` (health) and `/webhook` are open.
- Once `WHATSAPP_TOKEN` is set, `/webhook` refuses anything not signed with `WHATSAPP_APP_SECRET`.
- Run one instance only: the routine scheduler and conversation locks live in the process.

## Connect WhatsApp

1. In Meta for Developers, create an app with WhatsApp, and note the phone number ID and a permanent access token.
2. Fill `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`, `BROKER_WHATSAPP` in `.env`.
3. Start the server and expose it over HTTPS (for a pilot, ngrok or a small cloud VM):
   ```bash
   uvicorn app_whatsapp.server:app --port 8000
   ```
4. Set the webhook URL to `https://<your-host>/webhook` with the same verify token, and subscribe to `messages`.

Without WhatsApp credentials the server still runs. Outgoing messages are logged, and you can test with:

```bash
curl -X POST localhost:8000/chat -H "Content-Type: application/json" -d "{\"thread_id\":\"t1\",\"message\":\"2BHK in Baner under 1.1 Cr\",\"name\":\"Asha\"}"
curl localhost:8000/approvals
curl -X POST localhost:8000/approvals/4F2A -H "Content-Type: application/json" -d "{\"approve\":true}"
curl localhost:8000/leads
```

Notes for going live:
- WhatsApp only allows free-form messages within 24 hours of the person's last message. The morning
  briefing, visit reminders and weekly shortlists are usually outside that window, so in production
  they need message templates approved in Meta's WhatsApp Manager (the code sends plain text today).
  Only send shortlists to customers who agreed to receive updates.
- Buyers' phone numbers and requirements are personal data under India's DPDP Act 2023: tell customers
  an AI assistant is replying, keep the database private, and delete data on request.

## Files

- `src/graph.py`: the agent graph
- `src/graph_nodes/`: main agent, search extraction, DB query, lead scorer, appointment agent
- `src/util/india.py`: lakhs/crores parsing and formatting, EMI, IST helpers
- `src/util/db.py`: SQLite listings, leads, appointments, approvals
- `src/util/appointment_tools.py`: availability and booking tools
- `src/runner.py`: one turn / approve / decline, shared by CLI and server
- `app_whatsapp/server.py`: WhatsApp webhook and REST API
- `main.py`: terminal chat

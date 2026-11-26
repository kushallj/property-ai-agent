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

## Many brokers on one server

One deployment serves every broker you sell to. Each broker has their own firm name, assistant name,
WhatsApp number, inventory, leads, campaigns, opt-out list, daily sending limit and AI reply allowance, and
an API key that only ever sees their own data. Hosting is paid once and shared.

**Who holds which key**
- **You (platform owner):** `ADMIN_API_KEY`. Manages brokers at `/platform`, and can act for any broker by
  adding the header `X-Tenant: <broker id>` (without it you act as the `default` broker).
- **Each broker:** their own key (`bm_<id>_…`), created when you add them. They paste it at `/admin`
  (campaign dashboard) or in the demo's "Connect live backend". It can't see other brokers or manage anything.

**Add a broker (2 minutes):** open `https://<your-server>/platform`, paste `ADMIN_API_KEY`, fill in the firm
name, the broker's own WhatsApp number (for hot-lead alerts and visit approvals) and their plan limits, then
**Add broker**. Copy the key shown (it is shown once) and send it to them privately. Tick "Start with sample
listings" for a trial, or have them upload their inventory: `POST /listings {"csv": "..."}` with the columns of
`data/india_listings.csv`.

**Connect their WhatsApp:** add their Phone number ID (and WhatsApp Business Account ID) on the broker row.
Incoming messages are routed to the right broker by the business number they were sent to. If all numbers sit
under your own Meta app (Meta Tech Provider), one platform `WHATSAPP_TOKEN` and `WHATSAPP_APP_SECRET` serve
everyone; if a broker uses their own Meta app, store their token and app secret on their row.

**Protecting your margin**
- `ai_monthly_limit` (default 300 on new brokers): once a broker uses their AI replies for the month, customers
  get a polite "our team will get back to you" and the broker is alerted; no AI call is made.
- Usage per broker (AI replies, campaign messages) is shown on `/platform` and at `GET /me` for billing.
- Campaign messages are sent from the broker's own WhatsApp account, so Meta bills them, not you.
- **Pause** a broker who stops paying: their key and WhatsApp number stop working until you activate them.
  **New key** replaces a leaked key at once.

**Platform API** (needs `ADMIN_API_KEY`): `GET|POST /platform/tenants`, `GET|POST /platform/tenants/{id}`,
`POST /platform/tenants/{id}/rotate-key|pause|activate`. **Broker API** (broker key): everything below, plus
`GET /me`, `GET|POST /listings`.

The broker that existed before this change is `default`: its settings still come from the environment
(`FIRM_NAME`, `WHATSAPP_*`, `BROKER_WHATSAPP`), and its data was kept as is.

## WhatsApp campaigns

Send an approved WhatsApp template to a contact list, and let the AI agent handle every reply.

**Easiest way:** open `https://<your-server>/admin`, paste `ADMIN_API_KEY`, then:
1. Upload a CSV with a phone column (`phone`, `mobile`, `whatsapp`…) and any other columns (`name`, `locality`, `project`…).
2. Pick an approved template (listed automatically when `WHATSAPP_BUSINESS_ACCOUNT_ID` is set) and fill its
   variables from columns, one per line: `{first_name}`, `{locality|your area}` (text after `|` is used when the
   cell is empty), or fixed text.
3. **Check list** shows how many will receive it, duplicates merged, invalid numbers, people who opted out,
   and three filled-in samples. Send a test to your own number, then **Start campaign**.
4. Watch waiting → sent → delivered → read → replied update live. Pause, resume, cancel, or export results as CSV.

**What happens automatically**
- Numbers are cleaned: `98000 04521`, `09800004521` and `+91-9800004521` all become `+919800004521`. NRI numbers with a country code are kept.
- Sending runs only between `CAMPAIGN_HOURS` (default 9:00 to 21:00 IST) and stops at `WHATSAPP_DAILY_LIMIT`
  people per day across all campaigns. It carries on the next morning.
- Meta rate limits are retried with backoff. A template or token problem pauses the campaign with Meta's error
  so nothing is lost; fix it and press Resume. A bad number fails on its own without stopping the rest.
- Anyone who replies **STOP** (or taps a "Stop promotions" button) is opted out of every future campaign and
  the weekly shortlist, and gets a confirmation. **START** opts them back in.
- A reply goes to the AI agent like any WhatsApp chat. The lead's source becomes `Campaign: <name>`, and the
  agent's notes record which campaign they answered.

**Before your first real campaign**
- Create the template in WhatsApp Manager (category Marketing) and wait for approval. Example body:
  `Hi {{1}}, new 2 and 3 BHK homes are launching in {{2}}, starting ₹85 L. Reply YES for the price list and site visit slots.`
  Add a quick-reply button "Stop promotions" so people can opt out in one tap.
- Only message people who agreed to hear from the business on WhatsApp. Messaging people without consent gets
  numbers reported, lowers your quality rating, and can get the number blocked.
- Meta charges per marketing message delivered. Check the current India rate in WhatsApp Manager.
- Set `WHATSAPP_DAILY_LIMIT` to your number's messaging limit (shown in WhatsApp Manager). New numbers start
  around 250 people a day and Meta raises the limit as you send good-quality messages.

**API** (all need `ADMIN_API_KEY`): `POST /campaigns` (name, template_name, language, params, csv),
`POST /campaigns/{id}/test|start|pause|resume|cancel`, `GET /campaigns`, `GET /campaigns/{id}`,
`GET /campaigns/{id}/recipients`, `GET /campaigns/{id}/export.csv`, `GET /whatsapp/templates`, `GET|POST /opt-outs`.

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

## Host it on AWS Lightsail

Step-by-step guide with a paste-in launch script: [deploy/lightsail/README.md](../deploy/lightsail/README.md).
About $12/month in Mumbai, with automatic HTTPS.

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

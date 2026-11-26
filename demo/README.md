# Pitch demo

A single-page, no-backend demo of the agent for broker meetings. Live at https://property-demo-rho.vercel.app

Everything runs in the browser with sample data; nothing is sent and uploaded files never leave the page.

| Tab | What it shows |
|---|---|
| Live agent | WhatsApp chat scenarios (ready buyer, NRI investor, Hinglish browser), live lead score, matched listings, visit booking with broker approval |
| Lead manager | 500 sample leads (or an uploaded Excel/CSV) cleaned, de-duplicated, scored Hot/Warm/Nurture/Dead, call list, auto-assign, re-engagement |
| Campaigns | WhatsApp template campaign to 1,000 contacts: list check, daily limit, live delivered/read/replied, replies flowing into the Lead manager |
| Lead sources | Where leads come from and the live lead from the chat |
| Broker briefing | The 8:30 AM WhatsApp summary to the owner |
| How it works | Agent steps, integrations, rollout plan, ROI calculator |

Type the client's firm name in the "Demo for" box before a meeting.

**Open locally:** open `index.html` in a browser.

**Deploy to Vercel:** from this folder run `npx vercel deploy --prod`, or import the repo in Vercel and set the
root directory to `demo` (no build step).

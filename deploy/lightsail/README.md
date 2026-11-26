# Deploy the backend on AWS Lightsail

One small server runs the backend in Docker, behind Caddy, which gets a free HTTPS certificate automatically.
Data (database, conversations, lead memory) lives on the server's disk and survives restarts and updates.
Cost: the 2 GB Lightsail plan, about $12/month. The static IP is free while attached.

## 1. Create the instance

In the Lightsail console, choose **Create instance**:

| Setting | Choose |
|---|---|
| Region | **Mumbai (ap-south-1)** |
| Platform / blueprint | **Linux/Unix → OS Only → Ubuntu 24.04 LTS** |
| Launch script | Click **+ Add launch script** and paste the script below |
| Plan | **2 GB RAM** (1 GB can run out of memory while installing) |
| Name | `property-ai-agent` |

Launch script:

```bash
#!/bin/bash
set -euxo pipefail
apt-get update -y
apt-get install -y git
git clone https://github.com/kushallj/property-ai-agent.git /opt/property-ai-agent
bash /opt/property-ai-agent/deploy/lightsail/install.sh > /var/log/property-ai-agent-install.log 2>&1
```

Click **Create instance**.

## 2. Static IP and firewall

1. **Networking → Create static IP**, attach it to `property-ai-agent`. Note the address.
2. Open the instance → **Networking → IPv4 Firewall → Add rule → HTTPS (TCP 443)**. HTTP (80) is open by default; keep it, it is needed to get the certificate.

## 3. Finish setup in the browser terminal

Open the instance → **Connect using SSH** (opens a terminal in your browser). The first install takes 5 to 10 minutes. Check progress:

```bash
sudo tail -f /var/log/property-ai-agent-install.log
```

When it shows the summary with the backend URL, press Ctrl+C, then:

```bash
sudo /opt/property-ai-agent/deploy/lightsail/start.sh
```

This picks up the static IP you attached and gets the HTTPS certificate for it, at an address like
`https://13-234-56-78.sslip.io`.

Add your OpenAI key (it's typed hidden):

```bash
sudo /opt/property-ai-agent/deploy/lightsail/set-env.sh OPENAI_API_KEY
```

Show the address, admin key and health:

```bash
sudo /opt/property-ai-agent/deploy/lightsail/info.sh
```

Open `https://<your-address>/admin` and paste the admin key to use the campaign dashboard.

## Everyday commands

| What | Command |
|---|---|
| Show address, admin key, health | `sudo /opt/property-ai-agent/deploy/lightsail/info.sh` |
| Change a setting (WhatsApp token, firm name…) | `sudo /opt/property-ai-agent/deploy/lightsail/set-env.sh NAME` |
| Update to the latest code from GitHub | `sudo /opt/property-ai-agent/deploy/lightsail/update.sh` |
| See the backend's logs | `cd /opt/property-ai-agent && sudo docker compose -f deploy/lightsail/compose.yaml logs -f app` |
| Restart | `sudo /opt/property-ai-agent/deploy/lightsail/start.sh` |

Settings live in `/opt/property-ai-agent/backend/.env` (readable by root only).

## Your own domain (optional)

Point a DNS A record (for example `agent.yourfirm.in`) at the static IP, then:

```bash
sudo /opt/property-ai-agent/deploy/lightsail/set-env.sh DOMAIN
```

and enter `agent.yourfirm.in`. Caddy gets a certificate for it automatically.

## Connect WhatsApp later

Set `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_BUSINESS_ACCOUNT_ID` and
`BROKER_WHATSAPP` with `set-env.sh`, then in Meta's app settings use the webhook URL and verify token shown by `info.sh`.

## Backups

In Lightsail, open the instance → **Snapshots** → turn on **automatic snapshots** (daily, a few cents per GB a month).
That backs up the whole disk, including the lead database.

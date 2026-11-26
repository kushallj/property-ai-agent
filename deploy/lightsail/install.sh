#!/bin/bash
# One-time setup on a fresh Ubuntu 24.04 Lightsail (or EC2) server. Run as root.
# Installs Docker, adds swap, writes the settings file, and starts the backend on every boot.
set -euo pipefail

APP=/opt/property-ai-agent
ENV_FILE="$APP/backend/.env"
export DEBIAN_FRONTEND=noninteractive

echo "==> Installing Docker"
apt-get update -y
apt-get install -y docker.io docker-compose-v2 git curl openssl
systemctl enable --now docker

echo "==> Adding 2 GB swap (helps while installing the AI libraries)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Writing settings to $ENV_FILE"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
# Backend settings. Change a value with: sudo $APP/deploy/lightsail/set-env.sh NAME
LLM_MODEL=openai:gpt-4o-mini
OPENAI_API_KEY=
ADMIN_API_KEY=$(openssl rand -hex 24)
FIRM_NAME=Apna Ghar Realty
ASSISTANT_NAME=Riya
BROKER_WHATSAPP=
WHATSAPP_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_APP_SECRET=
WHATSAPP_VERIFY_TOKEN=$(openssl rand -hex 16)
WHATSAPP_BUSINESS_ACCOUNT_ID=
WHATSAPP_DAILY_LIMIT=250
CAMPAIGN_HOURS=9-21
ROUTINES_ENABLED=1
CAMPAIGNS_ENABLED=1
# Optional: your own domain (pointed at this server's static IP). Empty = <ip>.sslip.io
DOMAIN=
EOF
  chmod 600 "$ENV_FILE"
fi

echo "==> Starting the backend on every boot"
chmod +x "$APP"/deploy/lightsail/*.sh
cat > /etc/systemd/system/property-ai-agent.service <<EOF
[Unit]
Description=Property AI agent backend
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$APP/deploy/lightsail/start.sh
TimeoutStartSec=1800

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable property-ai-agent.service
systemctl start property-ai-agent.service

"$APP/deploy/lightsail/info.sh"

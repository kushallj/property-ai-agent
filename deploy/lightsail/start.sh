#!/bin/bash
# Builds and (re)starts the backend behind HTTPS. Safe to run any time; runs at every boot.
# The web address is DOMAIN from the settings file, or <public-ip>.sslip.io when DOMAIN is empty,
# so after attaching a static IP, run this again (or reboot) to get a certificate for the new address.
set -euo pipefail

APP=/opt/property-ai-agent
cd "$APP"

DOMAIN=$(grep -E '^DOMAIN=' backend/.env | cut -d= -f2- | tr -d '[:space:]' || true)
if [ -z "$DOMAIN" ]; then
  TOKEN=$(curl -s -m 3 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' || true)
  IP=$(curl -s -m 3 -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4 || true)
  if ! [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    IP=$(curl -s -m 5 https://checkip.amazonaws.com | tr -d '[:space:]')
  fi
  DOMAIN="${IP//./-}.sslip.io"
fi

export SITE="$DOMAIN"
echo "$SITE" > deploy/lightsail/.site
docker compose -f deploy/lightsail/compose.yaml up -d --build --remove-orphans
echo "Backend running at https://$SITE  (campaign dashboard: https://$SITE/admin)"

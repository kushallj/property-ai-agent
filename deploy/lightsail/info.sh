#!/bin/bash
# Show the backend's address, admin key and whether it is healthy.
#   sudo /opt/property-ai-agent/deploy/lightsail/info.sh
set -uo pipefail
APP=/opt/property-ai-agent
SITE=$(cat "$APP/deploy/lightsail/.site" 2>/dev/null || echo "not started yet")
KEY=$(grep -E '^ADMIN_API_KEY=' "$APP/backend/.env" | cut -d= -f2-)
VERIFY=$(grep -E '^WHATSAPP_VERIFY_TOKEN=' "$APP/backend/.env" | cut -d= -f2-)
OPENAI=$(grep -E '^OPENAI_API_KEY=.+' "$APP/backend/.env" >/dev/null && echo "set" || echo "NOT SET (run set-env.sh OPENAI_API_KEY)")
HEALTH=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "https://$SITE/" 2>/dev/null || echo "-")
cat <<EOF

  Backend URL ........ https://$SITE
  Health check ....... $HEALTH (200 means running)
  Campaign dashboard . https://$SITE/admin
  Admin key .......... $KEY
  WhatsApp webhook ... https://$SITE/webhook   (verify token: $VERIFY)
  OpenAI key ......... $OPENAI

EOF

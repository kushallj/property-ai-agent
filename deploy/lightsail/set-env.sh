#!/bin/bash
# Change one setting and restart the backend. The value is typed hidden, so keys don't land in shell history.
#   sudo /opt/property-ai-agent/deploy/lightsail/set-env.sh OPENAI_API_KEY
set -euo pipefail

NAME=${1:?Usage: set-env.sh NAME   (for example OPENAI_API_KEY)}
ENV_FILE=/opt/property-ai-agent/backend/.env
read -r -s -p "Value for $NAME (hidden): " VALUE
echo

NAME="$NAME" VALUE="$VALUE" awk '
  BEGIN { n = ENVIRON["NAME"]; v = ENVIRON["VALUE"]; done = 0 }
  index($0, n "=") == 1 { print n "=" v; done = 1; next }
  { print }
  END { if (!done) print n "=" v }
' "$ENV_FILE" > "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"

/opt/property-ai-agent/deploy/lightsail/start.sh
echo "Saved $NAME and restarted the backend."

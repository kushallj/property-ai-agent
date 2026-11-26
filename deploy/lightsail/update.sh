#!/bin/bash
# Pull the latest code from GitHub and restart. Settings and data are kept.
#   sudo /opt/property-ai-agent/deploy/lightsail/update.sh
set -euo pipefail
git -C /opt/property-ai-agent pull --ff-only
chmod +x /opt/property-ai-agent/deploy/lightsail/*.sh
/opt/property-ai-agent/deploy/lightsail/start.sh

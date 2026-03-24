#!/bin/sh
# contAIned in-container entrypoint.
# Reads agent.model from the workspace manifest and execs claude directly.
MODEL=$(/opt/contained-venv/bin/python3 -c "
import yaml, sys
try:
    m = yaml.safe_load(open('/workspace/.contAIned/manifest.yaml'))
    print(m.get('agent', {}).get('model', ''))
except Exception:
    pass
" 2>/dev/null)
CMD="claude --plugin-dir /etc/contained/plugin"
[ -n "$MODEL" ] && CMD="$CMD --model $MODEL"
exec $CMD "$@"

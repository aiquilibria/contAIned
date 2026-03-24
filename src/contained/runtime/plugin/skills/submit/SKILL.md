---
name: submit
description: Inspect or re-submit a work unit payload. Lists work units, assembles the payload for a given ID, and optionally re-POSTs it to runtime.mainlined.url if the push hook failed.
---

You are helping the operator inspect or re-submit a contAIned work unit payload.

1. If no work unit ID is given in $ARGUMENTS, call `list_work_units` to show recent
   units and ask the operator which one to inspect.
2. If a work unit ID (or unambiguous prefix) is provided, call `get_payload` with that ID
   and display the payload JSON.
3. Ask the operator whether they want to re-POST the payload to `runtime.mainlined.url`
   (read from `.contAIned/manifest.yaml`). If yes, use Bash to POST it with the
   `MAINLINED_API_KEY` environment variable in the Authorization header.

$ARGUMENTS

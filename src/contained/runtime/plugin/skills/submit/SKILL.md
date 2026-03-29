---
name: submit
description: Inspect or re-submit a work unit payload. Lists work units, assembles the payload for a given ID, and optionally re-POSTs it to the proof/submit endpoint if the push hook failed.
---

You are helping the operator inspect or re-submit a contAIned work unit payload.

1. If no work unit ID is given in $ARGUMENTS, call `list_work_units` to show recent
   units and ask the operator which one to inspect.
2. If a work unit ID (or unambiguous prefix) is provided, call `get_payload` with that ID
   and display the payload JSON.
3. Ask the operator whether they want to re-POST the payload. If yes:
   - Read `.contAIned/manifest.yaml` and resolve the submission URL as follows:
     - Prefer the in-container Docker network URL from `mainlined.policy_yaml`
       (parsed as YAML; `policy.mAInlined.url`, e.g. `http://mainlined:8080`) as the
       base — this avoids localhost resolution issues inside the container.
     - Graft the path from `mainlined.url` (the host-side bootstrap URL) onto that base
       so the org/workspace path (e.g. `/aiquilibria/default`) is preserved.
     - If only one source is available, use whichever is present.
   - Append `/proof/submit` to the resolved base URL.
   - POST the payload JSON to that endpoint with the `mAInlined_API_KEY` environment
     variable in the `Authorization: Bearer` header.

$ARGUMENTS

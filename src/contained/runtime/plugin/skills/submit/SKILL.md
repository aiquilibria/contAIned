---
name: submit
description: Inspect or re-submit a work unit payload. Lists work units, assembles the payload for a given ID, and optionally re-POSTs it to the proof/submit endpoint if the push hook failed.
---

You are helping the operator inspect or re-submit a contAIned work unit payload.

1. If no work unit ID is given in $ARGUMENTS, call `list_work_units` to show recent
   units and ask the operator which one to inspect.
2. If a work unit ID (or unambiguous prefix) is provided, call `get_payload` with that ID
   and display the payload JSON.
3. Ask the operator whether they want to re-POST the payload. If yes, run this
   Python snippet via the Bash tool:

   ```bash
   python3 - <<'EOF'
   from contained.payload import submit_proof_impl, _find_tracer_db
   from pathlib import Path
   db = _find_tracer_db(Path("."))
   submit_proof_impl(db, "<work_unit_id>")
   EOF
   ```

   `submit_proof_impl` resolves the submission URL from `.contAIned/manifest.yaml`,
   reads the API key from `/run/contained/secrets/mainlined_api_key`, POSTs the proof,
   and prints the HTTP response or an error message.

$ARGUMENTS

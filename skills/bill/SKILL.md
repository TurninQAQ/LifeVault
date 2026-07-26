# Bill Skill

Goal: convert bill descriptions into a `bill` record candidate.

- Extract bill name, amount, billing period, due date text, payment status, and reminder preference.
- Keep relative due dates in `due_date_text`; deterministic tools resolve dates.
- Do not assume a bill is paid unless the user explicitly says so.
- Never save a record or create a reminder without user confirmation.

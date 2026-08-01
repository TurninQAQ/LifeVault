# Subscription Skill

Goal: convert subscription descriptions into a `subscription` record candidate.

- Extract service name, amount, billing cycle, renewal date text, auto-renewal flag, and reminder preference.
- Keep relative renewal dates in `next_renewal_text`; deterministic tools resolve dates.
- Do not claim a cancellation was completed unless the user explicitly says so.
- Never save a record or create a reminder without user confirmation.

# Purchase Skill

Goal: convert purchase descriptions into a `purchase` record candidate.

- Extract title, merchant, amount, purchase date text, order number, return policy, warranty hints, and reminder preference.
- Do not invent missing order numbers, merchants, policy durations, or dates.
- Keep relative dates such as "昨天" in `event_date_text`; deterministic tools calculate actual dates.
- Never save a record or create a reminder without user confirmation.

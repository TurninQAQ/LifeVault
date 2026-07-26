# LifeVault

LifeVault v0.3 is a local-first life record and reminder assistant. It uses local Qwen for language understanding, LangGraph for human-in-the-loop create-record workflows, MCP for the personal vault data boundary, deterministic Python tools for dates and validation, SQLite for durable records, and a reminder worker for local notifications.

## Current MVP

Implemented chain:

```text
natural language -> Qwen/fallback extraction -> LangGraph interrupts
-> deterministic tools -> user confirmation -> SQLite record
-> reminder confirmation -> SQLite reminder -> worker notification
```

The model does not write the database or send notifications. It only produces a candidate record and a tool plan. The Agent validates fields, calculates dates, checks duplicates, and requires confirmation before saving. LangGraph persists interrupted create-record workflows in `data/langgraph_checkpoints.sqlite`.

Create-record interrupts:

- `missing_fields`: resume with natural language supplement.
- `duplicate_review`: choose `continue` or `cancel`.
- `record_confirmation`: choose `confirm` or `cancel`.
- `reminder_confirmation`: choose `confirm` or `skip`.

## Local Qwen

Defaults match the local vLLM service found on this machine:

```text
LIFEVAULT_QWEN_BASE_URL=http://127.0.0.1:8008/v1
LIFEVAULT_QWEN_MODEL=qwen-enterprise-agent
```

If Qwen is unavailable or returns invalid JSON, the app falls back to a small rules-based extractor so the demo still runs.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m lifevault.cli init-db
```

On this machine, `python3-venv` is not installed and `HTTP_PROXY/HTTPS_PROXY` can point at an unavailable local proxy. The verified fallback is:

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy python3 -m pip install --user -r requirements.txt
python3 -m lifevault.cli init-db
```

## CLI Demo

```bash
python -m lifevault.cli add "我昨天在京东买了一个耳机，3499 元，订单号 123456，七天无理由，退货前两天提醒我。" --yes
python -m lifevault.cli list
python -m lifevault.cli reminders
python -m lifevault.cli worker --once
```

Resume an interrupted graph thread:

```bash
python -m lifevault.cli state THREAD_ID
python -m lifevault.cli resume THREAD_ID --text "金额是 3499 元，订单号是 123456"
python -m lifevault.cli resume THREAD_ID --action confirm
python -m lifevault.cli resume THREAD_ID --action skip
```

## MCP Server

Run the Personal Vault MCP server over stdio:

```bash
python3 -m lifevault.mcp_server.server
```

The CLI wrapper is equivalent:

```bash
python3 -m lifevault.cli mcp-server
```

Run a real stdio MCP smoke test:

```bash
python3 -m lifevault.cli mcp-smoke
```

MCP tools:

```text
save_record
search_records
get_record
find_duplicate
update_record_status
create_reminder
list_reminders
snooze_reminder
cancel_reminder
```

All tools use the configured local user from `LIFEVAULT_USER_ID`; `user_id` is not exposed to the model or client. Tool responses use JSON objects with either `ok: true` and data fields or `ok: false` with an error object. `save_record`, `create_reminder`, and `cancel_reminder` require `user_confirmed=true`.

## Streamlit

```bash
python3 -m streamlit run lifevault/app/main.py
```

Open the URL printed by Streamlit. The app has four tabs: add record, records, reminders, and settings.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Project Shape

- `lifevault/models`: Pydantic schemas and Qwen adapter.
- `lifevault/tools`: deterministic tools for dates, idempotency, and notifications.
- `lifevault/storage`: SQLite schema and repository.
- `lifevault/agent`: LangGraph create-record workflow plus the v0.1 service fallback.
- `lifevault/mcp_server`: FastMCP stdio server and smoke client.
- `lifevault/app`: Streamlit UI.
- `lifevault/worker`: reminder scanner and notification sender.
- `skills`: task-specific extraction instructions for purchase, subscription, and bill records.

# CareCircle: family care, by voice (Alexa+ MCP server)

Built for the **Build, Ship, Shape: Amazon Developer Hackathon**. Track: **Alexa+**.

Millions of families share the care of an ageing parent across siblings, spouses and
paid helpers, and the shared state lives in group chats and memory. Two people give the
same pill. Nobody knows who checked in last. CareCircle is a self-hosted **MCP server**
(Streamable HTTP, MCP spec 2025-11-25 or later) that gives Alexa+ a shared, persistent
memory of a loved one's care, so anyone in the family can just ask.

> "Alexa, how is Mom doing today?"
> "Alexa, I'm James, did Mom take her Metformin?"
> "Alexa, I'm Sara, handing over to James."

## What it does

| Tool | Purpose |
|---|---|
| `daily_briefing` | One-shot status: meds taken/overdue, last check-in, open concerns, suggested next action |
| `todays_schedule` | Live medication status (taken / overdue / upcoming) |
| `log_dose` | Records a dose and **blocks duplicate doses across family members**, telling you who already gave it |
| `check_in` | Mood 1-5 + note; low scores **auto-escalate** to a concern |
| `flag_concern` | Falls, confusion, symptoms. High severity tells the caller to contact a doctor/emergency services (no diagnosing) |
| `caregiver_handoff` | Generates and stores a handoff note (done, still due, mood trend, concerns) |
| `add_medication`, `resolve_concern` | Setup and follow-up |

Also exposes an MCP **resource** (`carecircle://family/{person}/history`) and an MCP **prompt**
(`morning_routine`, an agent-skill style routine). State is stored in SQLite and persists across sessions.

## Run it

```bash
pip install -r requirements.txt
python server.py            # MCP endpoint: http://127.0.0.1:8765/mcp
```

- Open **http://127.0.0.1:8765/** for the simulated Alexa+ demo UI.
- In another terminal run `python mcp_client_demo.py` to call the tools over real MCP
  Streamable HTTP (list tools, call tools).
- Point any MCP client (or Alexa+ once your endpoint is public, e.g. via a tunnel) at `/mcp`.

Python 3.10+. Tested with `mcp` 2.3.0.

## Honest notes on the demo UI

`/mcp` is the real MCP server. The chat page at `/` is a **simulated Alexa+ front end**: a small
keyword router (`_route` in `server.py`) stands in for Alexa+'s planner so the demo runs with no
API keys. It calls the same tool functions the MCP server exposes.

## Safety

CareCircle never gives medical advice or changes doses. It tracks, alerts and escalates.
Demo data is fictional.

## License

MIT

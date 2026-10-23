"""CareCircle - an Alexa+ MCP server for family caregiving.

Runs a self-hosted MCP server over Streamable HTTP at /mcp (MCP spec 2025-11-25+),
and serves a simulated Alexa+ demo UI at /.

Run:  python server.py   ->  http://127.0.0.1:8765  (MCP endpoint: /mcp)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

DB_PATH = os.environ.get("CARECIRCLE_DB", str(Path(__file__).parent / "carecircle.db"))
_lock = threading.Lock()

INSTRUCTIONS = (
    "CareCircle helps families look after a loved one together. Use it when someone asks about "
    "a family member's medications, whether a dose was taken, how the day went, or needs to hand "
    "care over to another family member. State persists across sessions. Prefer "
    "`daily_briefing` for open-ended questions like 'how is Mom doing?'. Never give medical "
    "advice or change doses; escalate concerns with `flag_concern`."
)

mcp = MCPServer(
    name="CareCircle",
    title="CareCircle: family care, by voice",
    description="Shared medication tracking, check-ins and caregiver handoffs for families.",
    instructions=INSTRUCTIONS,
    version="1.0.0",
)


# ---------------------------------------------------------------- storage
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS meds(
          id INTEGER PRIMARY KEY, person TEXT, name TEXT, dose TEXT, time TEXT, notes TEXT);
        CREATE TABLE IF NOT EXISTS doses(
          id INTEGER PRIMARY KEY, med_id INTEGER, day TEXT, taken_at TEXT, by_whom TEXT);
        CREATE TABLE IF NOT EXISTS checkins(
          id INTEGER PRIMARY KEY, person TEXT, ts TEXT, mood INTEGER, note TEXT, by_whom TEXT);
        CREATE TABLE IF NOT EXISTS concerns(
          id INTEGER PRIMARY KEY, person TEXT, ts TEXT, severity TEXT, text TEXT, by_whom TEXT, resolved INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS handoffs(
          id INTEGER PRIMARY KEY, person TEXT, ts TEXT, from_who TEXT, to_who TEXT, summary TEXT);
        """
    )
    return con


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def seed_demo() -> None:
    with _lock, db() as con:
        if con.execute("SELECT COUNT(*) FROM meds").fetchone()[0]:
            return
        con.executemany(
            "INSERT INTO meds(person,name,dose,time,notes) VALUES(?,?,?,?,?)",
            [
                ("Mom", "Metformin", "500 mg", "08:00", "with breakfast"),
                ("Mom", "Lisinopril", "10 mg", "08:00", "for blood pressure"),
                ("Mom", "Vitamin D", "1000 IU", "12:30", "with lunch"),
                ("Mom", "Atorvastatin", "20 mg", "20:00", "evening"),
            ],
        )
        con.execute(
            "INSERT INTO checkins(person,ts,mood,note,by_whom) VALUES(?,?,?,?,?)",
            ("Mom", (datetime.now() - timedelta(days=1)).isoformat(timespec="minutes"), 4,
             "Walked in the garden, ate well, a bit tired in the evening.", "Sara"),
        )


def _norm(person: str) -> str:
    return (person or "Mom").strip().title()


def _schedule(con: sqlite3.Connection, person: str) -> list[dict]:
    rows = con.execute("SELECT * FROM meds WHERE person=? ORDER BY time,name", (person,)).fetchall()
    out = []
    for m in rows:
        d = con.execute("SELECT * FROM doses WHERE med_id=? AND day=?", (m["id"], today())).fetchone()
        if d:
            status = "taken"
        elif m["time"] <= now_hm():
            status = "overdue"
        else:
            status = "upcoming"
        out.append({
            "medication": m["name"], "dose": m["dose"], "scheduled": m["time"], "notes": m["notes"],
            "status": status, "taken_at": d["taken_at"] if d else None, "taken_confirmed_by": d["by_whom"] if d else None,
        })
    return out


# ---------------------------------------------------------------- tools
@mcp.tool()
def add_medication(person: str, name: str, dose: str, time: str, notes: str = "") -> dict:
    """Add a daily medication for a family member. `time` is 24h HH:MM, e.g. '08:00'."""
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time):
        return {"ok": False, "error": "time must be 24h HH:MM, e.g. 08:00"}
    person = _norm(person)
    with _lock, db() as con:
        con.execute("INSERT INTO meds(person,name,dose,time,notes) VALUES(?,?,?,?,?)",
                    (person, name.strip().title(), dose, time, notes))
    return {"ok": True, "message": f"Added {name} {dose} for {person} at {time}."}


@mcp.tool()
def todays_schedule(person: str = "Mom") -> dict:
    """List today's medications for a person with live status: taken, overdue or upcoming."""
    person = _norm(person)
    with _lock, db() as con:
        sched = _schedule(con, person)
    return {"person": person, "date": today(), "now": now_hm(), "schedule": sched,
            "overdue_count": sum(s["status"] == "overdue" for s in sched)}


@mcp.tool()
def log_dose(person: str, medication: str, taken_by_whom: str = "someone") -> dict:
    """Record that a medication was taken today. Prevents double-dosing across family members:
    if it was already logged, say who confirmed it instead of logging again."""
    person = _norm(person)
    with _lock, db() as con:
        m = con.execute("SELECT * FROM meds WHERE person=? AND lower(name) LIKE ?",
                        (person, f"%{medication.strip().lower()}%")).fetchone()
        if not m:
            return {"ok": False, "error": f"No medication matching '{medication}' for {person}."}
        prior = con.execute("SELECT * FROM doses WHERE med_id=? AND day=?", (m["id"], today())).fetchone()
        if prior:
            return {"ok": False, "duplicate": True,
                    "message": f"Hold on - {m['name']} was already marked taken at {prior['taken_at']} "
                               f"(confirmed by {prior['by_whom']}). Do not give a second dose."}
        con.execute("INSERT INTO doses(med_id,day,taken_at,by_whom) VALUES(?,?,?,?)",
                    (m["id"], today(), now_hm(), taken_by_whom.title()))
    return {"ok": True, "message": f"Logged {m['name']} {m['dose']} for {person} at {now_hm()}."}


@mcp.tool()
def check_in(person: str, mood_1_to_5: int, note: str = "", by_whom: str = "someone") -> dict:
    """Record how a family member is doing (mood 1=struggling .. 5=great). Low scores
    automatically raise a concern so the family is alerted."""
    person = _norm(person)
    mood = max(1, min(5, int(mood_1_to_5)))
    with _lock, db() as con:
        con.execute("INSERT INTO checkins(person,ts,mood,note,by_whom) VALUES(?,?,?,?,?)",
                    (person, datetime.now().isoformat(timespec="minutes"), mood, note, by_whom.title()))
        escalated = mood <= 2
        if escalated:
            con.execute("INSERT INTO concerns(person,ts,severity,text,by_whom) VALUES(?,?,?,?,?)",
                        (person, datetime.now().isoformat(timespec="minutes"), "high",
                         f"Low mood check-in ({mood}/5): {note}", by_whom.title()))
    return {"ok": True, "escalated_to_family": escalated,
            "message": f"Check-in saved for {person}." + (" I flagged this for the family." if escalated else "")}


@mcp.tool()
def flag_concern(person: str, text: str, severity: str = "medium", reported_by: str = "someone") -> dict:
    """Flag a worry (fall, missed meals, confusion, new symptom). severity: low | medium | high.
    High severity: tell the caller to contact a doctor or emergency services; do not diagnose."""
    sev = severity.lower() if severity.lower() in {"low", "medium", "high"} else "medium"
    person = _norm(person)
    with _lock, db() as con:
        con.execute("INSERT INTO concerns(person,ts,severity,text,by_whom) VALUES(?,?,?,?,?)",
                    (person, datetime.now().isoformat(timespec="minutes"), sev, text, reported_by.title()))
    advice = ("This sounds urgent. Please call their doctor or local emergency number now."
              if sev == "high" else "Noted and shared with the family.")
    return {"ok": True, "severity": sev, "message": advice}


@mcp.tool()
def daily_briefing(person: str = "Mom") -> dict:
    """One-shot status for 'how is Mom doing?': meds taken/overdue, last check-in, open concerns,
    and a plain-language summary plus suggested next action."""
    person = _norm(person)
    with _lock, db() as con:
        sched = _schedule(con, person)
        last = con.execute("SELECT * FROM checkins WHERE person=? ORDER BY ts DESC LIMIT 1", (person,)).fetchone()
        concerns = con.execute("SELECT * FROM concerns WHERE person=? AND resolved=0 ORDER BY ts DESC", (person,)).fetchall()
    taken = [s for s in sched if s["status"] == "taken"]
    overdue = [s for s in sched if s["status"] == "overdue"]
    upcoming = [s for s in sched if s["status"] == "upcoming"]
    parts = [f"{person} has taken {len(taken)} of {len(sched)} medications today."]
    if overdue:
        parts.append("Overdue: " + ", ".join(f"{s['medication']} (due {s['scheduled']})" for s in overdue) + ".")
    if upcoming:
        parts.append(f"Next up: {upcoming[0]['medication']} at {upcoming[0]['scheduled']}.")
    if last:
        parts.append(f"Last check-in by {last['by_whom']}: mood {last['mood']}/5. {last['note']}")
    if concerns:
        parts.append(f"{len(concerns)} open concern(s): " + "; ".join(c["text"] for c in concerns[:2]))
    next_action = (f"Remind {person} to take {overdue[0]['medication']}." if overdue
                   else "Everything is on track." if not concerns else "Review the open concerns.")
    return {"person": person, "summary": " ".join(parts), "suggested_next_action": next_action,
            "medications": sched, "open_concerns": [dict(c) for c in concerns]}


@mcp.tool()
def caregiver_handoff(person: str, from_who: str, to_who: str) -> dict:
    """Generate and save a handoff note when care passes between family members: what was taken,
    what is still due, mood trend and open concerns. Stored so the next caregiver can ask for it."""
    person = _norm(person)
    briefing = daily_briefing(person)
    with _lock, db() as con:
        moods = [r["mood"] for r in con.execute(
            "SELECT mood FROM checkins WHERE person=? ORDER BY ts DESC LIMIT 5", (person,))]
        trend = "no data"
        if len(moods) >= 2:
            trend = "improving" if moods[0] > moods[-1] else "declining" if moods[0] < moods[-1] else "steady"
        due = [s for s in briefing["medications"] if s["status"] != "taken"]
        note = (f"Handoff for {person}, {from_who.title()} -> {to_who.title()} at {now_hm()}. "
                f"{briefing['summary']} Mood trend: {trend}. "
                + ("Still due: " + ", ".join(f"{s['medication']} {s['scheduled']}" for s in due) + "." if due else "Nothing else due today."))
        con.execute("INSERT INTO handoffs(person,ts,from_who,to_who,summary) VALUES(?,?,?,?,?)",
                    (person, datetime.now().isoformat(timespec="minutes"), from_who.title(), to_who.title(), note))
    return {"ok": True, "handoff_note": note}


@mcp.tool()
def resolve_concern(person: str) -> dict:
    """Mark all open concerns for a person as resolved (after the family has followed up)."""
    with _lock, db() as con:
        n = con.execute("UPDATE concerns SET resolved=1 WHERE person=? AND resolved=0", (_norm(person),)).rowcount
    return {"ok": True, "resolved": n}


@mcp.resource("carecircle://family/{person}/history")
def history(person: str) -> str:
    """Recent check-ins and handoffs for a person (read-only context for the assistant)."""
    person = _norm(person)
    with _lock, db() as con:
        c = [dict(r) for r in con.execute("SELECT ts,mood,note,by_whom FROM checkins WHERE person=? ORDER BY ts DESC LIMIT 10", (person,))]
        h = [dict(r) for r in con.execute("SELECT ts,from_who,to_who,summary FROM handoffs WHERE person=? ORDER BY ts DESC LIMIT 5", (person,))]
    return json.dumps({"checkins": c, "handoffs": h}, indent=2)


@mcp.prompt()
def morning_routine(person: str = "Mom") -> str:
    """Agent skill-style routine: brief, remind on overdue meds, ask for a check-in."""
    return (f"Run the morning routine for {person}: call daily_briefing, read the summary aloud, "
            f"if anything is overdue remind them, then ask how they feel and call check_in.")


# ---------------------------------------------------------------- simulated Alexa+ UI
# Lightweight intent router so the demo page works without an LLM key.
# (Real Alexa+ calls the MCP tools above itself; /api/say just mimics that planner.)
def _route(utterance: str) -> tuple[str, dict, str]:
    u = utterance.lower()
    who = "Sara"
    m = re.search(r"\b(i am|i'm|this is)\s+(\w+)", u)
    if m:
        who = m.group(2).title()
    person = "Dad" if "dad" in u else "Mom"
    if re.search(r"hand ?off|handing over|passing|taking over", u):
        to = re.search(r"(?:to|for)\s+(\w+)\s*$", u)
        return "caregiver_handoff", {"person": person, "from_who": who, "to_who": (to.group(1) if to else "James")}, ""
    if re.search(r"took|taken|gave|had (her|his|their)", u) and not re.search(r"\b(did|has|have)\b.*\?", u):
        for med in ["metformin", "lisinopril", "vitamin d", "atorvastatin"]:
            if med in u:
                return "log_dose", {"person": person, "medication": med, "taken_by_whom": who}, ""
    if re.search(r"(did|has|have).*(take|taken)|schedule|medicat|pills", u):
        return "todays_schedule", {"person": person}, ""
    mood = re.search(r"(?:mood|feeling|feels?).*?(\d)", u)
    if "check in" in u or "check-in" in u or mood:
        return "check_in", {"person": person, "mood_1_to_5": int(mood.group(1)) if mood else 3, "note": utterance, "by_whom": who}, ""
    if re.search(r"fell|fall|dizzy|confus|worr|concern|chest|pain", u):
        sev = "high" if re.search(r"fell|fall|chest|confus", u) else "medium"
        return "flag_concern", {"person": person, "text": utterance, "severity": sev, "reported_by": who}, ""
    return "daily_briefing", {"person": person}, ""


@mcp.custom_route("/api/say", methods=["POST"])
async def api_say(request: Request) -> JSONResponse:
    body = await request.json()
    tool, args, _ = _route(body.get("text", ""))
    fn = {t.__name__: t for t in [add_medication, todays_schedule, log_dose, check_in, flag_concern,
                                   daily_briefing, caregiver_handoff, resolve_concern]}[tool]
    result = fn(**args)
    if tool in ("daily_briefing",):
        reply = result["summary"] + " " + result["suggested_next_action"]
    elif tool == "todays_schedule":
        reply = f"{result['person']}'s medications: " + "; ".join(
            f"{s['medication']} at {s['scheduled']} is {s['status']}" for s in result["schedule"]) + "."
    elif tool == "caregiver_handoff":
        reply = result["handoff_note"]
    else:
        reply = result.get("message") or result.get("error", "Done.")
    return JSONResponse({"tool": tool, "args": args, "result": result, "reply": reply})


@mcp.custom_route("/api/reset", methods=["POST"])
async def api_reset(request: Request) -> JSONResponse:
    with _lock:
        Path(DB_PATH).unlink(missing_ok=True)
    seed_demo()
    return JSONResponse({"ok": True})


@mcp.custom_route("/", methods=["GET"])
async def index(request: Request) -> FileResponse:
    return FileResponse(Path(__file__).parent / "demo.html")


def main() -> None:
    seed_demo()
    app = mcp.streamable_http_app()
    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()

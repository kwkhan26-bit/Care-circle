"""Calls the CareCircle MCP server over Streamable HTTP, like Alexa+ would.

Usage: python server.py   (in one terminal)
       python mcp_client_demo.py [http://127.0.0.1:8765/mcp]
"""
import asyncio
import sys

from mcp import Client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/mcp"


async def main() -> None:
    async with Client(URL) as c:
        tools = await c.list_tools()
        print("tools:", [t.name for t in tools.tools])
        for name, args in [
            ("log_dose", {"person": "Mom", "medication": "metformin", "taken_by_whom": "Sara"}),
            ("log_dose", {"person": "Mom", "medication": "metformin", "taken_by_whom": "James"}),
            ("flag_concern", {"person": "Mom", "text": "Fell in the kitchen", "severity": "high", "reported_by": "Sara"}),
            ("caregiver_handoff", {"person": "Mom", "from_who": "Sara", "to_who": "James"}),
        ]:
            r = await c.call_tool(name, args)
            print(f"\n{name} ->", r.content[0].text[:500])


asyncio.run(main())

"""End-to-end check against a running server: auth, tool list, and each tool.

Run inside the container:  docker exec garmin-mcp python tests/smoke_test.py
"""

import asyncio
import json
import os
import urllib.error
import urllib.request

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("MCP_URL", "http://127.0.0.1:8000/mcp")


def check_rejects_bad_token() -> None:
    for header in (None, "Bearer wrong-token"):
        req = urllib.request.Request(URL, data=b"{}", method="POST")
        if header:
            req.add_header("Authorization", header)
        try:
            urllib.request.urlopen(req, timeout=5)
            raise SystemExit(f"FAIL: request with {header!r} was accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 401, e.code
    print("auth: rejects missing and wrong tokens")


async def main() -> None:
    check_rejects_bad_token()
    headers = {"Authorization": f"Bearer {os.environ['MCP_TOKEN']}"}
    async with streamablehttp_client(URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            print("tools:", sorted(t.name for t in (await s.list_tools()).tools))

            async def call(name, **args):
                r = await s.call_tool(name, args)
                if r.isError:
                    raise SystemExit(f"FAIL {name}: {r.content[0].text}")
                data = r.structuredContent
                if data is None:  # tool sent text only
                    return json.loads(r.content[0].text)
                # list results come wrapped as {"result": [...]}
                return data["result"] if set(data) == {"result"} else data

            acts = await call("list_activities", limit=3)
            print("list_activities:", json.dumps(acts[0]), f"(+{len(acts) - 1} more)")
            one = await call("get_activity", activity_id=acts[0]["id"])
            print("get_activity: raw keys", sorted(one["raw"])[:5], "...")
            health = await call("get_health", kinds=["sleep", "hrv"])
            print("get_health:", len(health), "rows, e.g.", json.dumps(health[-1]))
            print("training_totals:", json.dumps(await call("training_totals")))
            print("sync_now:", json.dumps(await call("sync_now", days_back=2)))
            print("sync_status:", json.dumps((await call("sync_status"))["stored"]))

            bad = await s.call_tool("get_health", {"start_date": "yesterday"})
            assert bad.isError, "bad date should be an error"
            print("bad date ->", bad.content[0].text)
    print("ALL OK")


asyncio.run(main())

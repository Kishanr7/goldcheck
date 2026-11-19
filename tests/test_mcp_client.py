"""Talk to `goldcheck mcp` with the official MCP Python SDK, the client library real agents use.

Skipped when the `mcp` package is not installed (pip install mcp).
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:  # pragma: no cover
    ClientSession = None

from goldcheck import demo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _f(obj, snake, camel):
    """SDK 2.x uses snake_case fields, 1.x camelCase."""
    return getattr(obj, snake) if hasattr(obj, snake) else getattr(obj, camel)


@unittest.skipUnless(ClientSession, "the official mcp SDK is not installed")
class OfficialClient(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db, _, _ = demo.build(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    async def session_run(self, args, steps):
        env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
        params = StdioServerParameters(command=sys.executable, args=["-m", "goldcheck", "mcp", *args], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                return init, await steps(session)

    def run_async(self, coro):
        return asyncio.run(asyncio.wait_for(coro, 120))

    def test_handshake_listing_and_all_three_tools(self):
        async def steps(s):
            tools = await s.list_tools()
            check = await s.call_tool("check_sql", {"sql": "SELECT COUNT(*) FROM orders WHERE status = 'Completed'"})
            desc = await s.call_tool("describe_values", {"table": "orders", "column": "status"})
            comp = await s.call_tool("compare_queries", {
                "trusted_sql": "SELECT COUNT(DISTINCT customer_id) FROM orders",
                "sql": "SELECT COUNT(customer_id) FROM orders"})
            bad = await s.call_tool("check_sql", {})
            return tools, check, desc, comp, bad

        init, (tools, check, desc, comp, bad) = self.run_async(self.session_run(["--db", f"shop={self.db}"], steps))
        self.assertEqual(_f(init, "server_info", "serverInfo").name, "goldcheck")
        names = sorted(t.name for t in tools.tools)
        self.assertEqual(names, ["check_sql", "compare_queries", "describe_values"])
        self.assertNotIn("demo.sqlite", json.dumps([t.model_dump() for t in tools.tools]))
        finding = json.loads(check.content[0].text)["findings"][0]
        self.assertEqual((_f(check, "is_error", "isError"), finding["suggestions"][0]), (False, "completed"))
        self.assertEqual(json.loads(desc.content[0].text)["distinct"], 3)
        cmp = json.loads(comp.content[0].text)
        self.assertEqual((cmp["same_result"], cmp["cause"]["clause"]), (False, "SELECT"))
        self.assertTrue(_f(bad, "is_error", "isError"))
        self.assertIn("missing `sql`", bad.content[0].text)

    def test_local_mode_with_paths(self):
        async def steps(s):
            return await s.call_tool("check_sql", {"db_path": self.db, "sql": "SELECT 1 FROM orders WHERE status = 'pending'"})

        _, res = self.run_async(self.session_run([], steps))
        self.assertFalse(_f(res, "is_error", "isError"))
        self.assertTrue(json.loads(res.content[0].text)["ok"])


if __name__ == "__main__":
    unittest.main()

"""Start the server over stdio exactly as an MCP client would, list its tools and call a few.

Needs Windows + LabVIEW. usage: python tests/smoke_mcp.py
"""
import asyncio
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {"lv_status", "vi_info", "vi_text", "vi_snapshot", "vi_hierarchy", "vi_run", "vi_dump", "vi_edit",
            "vi_create", "lv_functions", "function_terminals", "lv_class_properties",
            "project_info", "project_create", "project_add", "project_build", "lv_mass_compile", "vi_diff", "vi_test"}


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "labview_mcp"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            missing = EXPECTED - names
            print("tools:", sorted(names))
            if missing:
                print("MISSING", missing)
                return 1
            r = await s.call_tool("lv_class_properties", {"class_name": "Boolean", "query": "visible"})
            print("lv_class_properties:", "error" if r.is_error else " ".join(r.content[0].text[:120].split()))
            if r.is_error:
                return 1
            ex = (r"C:\Program Files\National Instruments\LabVIEW 2026\vi.lib\AppBuilder\AB_API"
                  r"\examples\example_project\API - Example Project.lvproj")
            if os.path.exists(ex):
                r = await s.call_tool("project_info", {"path": ex})
                print("project_info:", "error" if r.is_error else " ".join(r.content[0].text[:160].split()))
                if r.is_error:
                    return 1
            r = await s.call_tool("function_terminals", {"name": "Add"})
            print("function_terminals:", r.content[0].text[:120])
            path = os.path.join(tempfile.mkdtemp(prefix="lvmcp-smoke-"), "smoke.vi")
            r = await s.call_tool("vi_create", {"path": path, "spec": {
                "controls": [{"name": "x", "type": "dbl", "default": 2}],
                "indicators": [{"name": "y", "type": "dbl"}],
                "functions": [{"name": "sq", "function": "Square"}],
                "wires": [["x", "sq.x"], ["sq.x^2", "y"]]}})
            print("vi_create:", "error" if r.is_error else "ok", [c.type for c in r.content])
            if r.is_error:
                print(r.content[0].text)
                return 1
            r = await s.call_tool("vi_run", {"path": path, "inputs": {"x": 3}})
            text = r.content[0].text
            print("vi_run:", text.replace("\n", " ")[:160])
            return 0 if '"y": 9.0' in text else 1


sys.exit(asyncio.run(main()))

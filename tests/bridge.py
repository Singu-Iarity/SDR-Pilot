"""Test bridge: keeps one MCP client session to the SDR Pilot server open and exposes it on a local
socket so tools can be called one at a time (python tests/call.py TOOL '{json args}')."""
import asyncio, base64, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

OUT = os.environ.get("BRIDGE_OUT", "/tmp/claude-0/bridge")
os.makedirs(OUT, exist_ok=True)
N = [0]

async def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(root, "run_server.py")],
                                   env={**os.environ}, cwd=root)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = await s.list_tools()
            print("tools:", len(tools.tools), flush=True)
            json.dump([{"name": t.name, "description": t.description, "schema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)} for t in tools.tools],
                      open(os.path.join(OUT, "tools.json"), "w"), indent=1)
            json.dump({"instructions": getattr(init, "instructions", None)}, open(os.path.join(OUT, "init.json"), "w"))

            async def handle(reader, writer):
                req = json.loads((await reader.readline()).decode())
                try:
                    res = await s.call_tool(req["tool"], req.get("args") or {}, read_timeout_seconds=600)
                    out = {"isError": getattr(res, "is_error", None) if hasattr(res, "is_error") else res.isError, "content": []}
                    for c in res.content:
                        if c.type == "text":
                            out["content"].append({"type": "text", "text": c.text})
                        elif c.type == "image":
                            N[0] += 1
                            p = os.path.join(OUT, f"img{N[0]:03d}_{req['tool']}.png")
                            open(p, "wb").write(base64.b64decode(c.data))
                            out["content"].append({"type": "image", "path": p})
                except Exception as e:
                    out = {"isError": True, "content": [{"type": "text", "text": f"bridge error: {e!r}"}]}
                writer.write((json.dumps(out) + "\n").encode())
                await writer.drain()
                writer.close()

            srv = await asyncio.start_server(handle, "127.0.0.1", int(os.environ.get("BRIDGE_PORT", "47000")))
            print("bridge ready", flush=True)
            async with srv:
                await srv.serve_forever()

asyncio.run(main())

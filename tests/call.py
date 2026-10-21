import json, socket, sys
tool = sys.argv[1]
a = sys.argv[2] if len(sys.argv) > 2 else "{}"
args = json.load(open(a[1:])) if a.startswith("@") else json.loads(a)
s = socket.create_connection(("127.0.0.1", 47000))
s.sendall((json.dumps({"tool": tool, "args": args}) + "\n").encode())
buf = b""
while not buf.endswith(b"\n"):
    d = s.recv(1 << 20)
    if not d: break
    buf += d
res = json.loads(buf)
if res["isError"]: print("ERROR:")
for c in res["content"]:
    print(c["text"] if c["type"] == "text" else f"[image] {c['path']}")

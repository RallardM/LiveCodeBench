import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

f = r"B:\repos\MyProjects\_LiveCodeBench\dsh-session-session\dsh-session-session-75c49f98-b4a3-4ee4-b531-07b744e9a7e6\session.v4.jsonl"
lines = open(f, "r", encoding="utf-8").read().splitlines()

# Map: for each line print type + short digest
def digest(rec):
    t = rec.get("type")
    d = rec.get("data") or {}
    out = [t]
    if t == "turn/start":
        out.append(f"turn={d.get('turn')}")
    if t == "assistant/message":
        msg = d.get("message") or {}
        content = msg.get("content") or []
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append("TXT:" + c.get("text", "")[:300].replace("\n", " | "))
                elif c.get("type") == "tool_use":
                    parts.append("TOOLUSE:%s(%s)" % (c.get("name"), json.dumps(c.get("input", {}), ensure_ascii=False)[:200]))
        out.append(" ; ".join(parts))
    elif t == "tool/call":
        out.append(json.dumps(d, ensure_ascii=False)[:300])
    elif t == "tool/result":
        out.append(json.dumps(d, ensure_ascii=False)[:250])
    elif t == "todo/write":
        out.append(json.dumps(d, ensure_ascii=False)[:1500])
    return " | ".join(str(x) for x in out)

start, end = int(sys.argv[1]), int(sys.argv[2])
for i, ln in enumerate(lines, start=1):
    if i < start or i > end:
        continue
    rec = json.loads(ln)
    print(f"L{i}: {digest(rec)}")

import json
p = "bench/zz-mix-cg2025-harness/generations.jsonl"
n = f = 0
for line in open(p, encoding="utf-8"):
    d = json.loads(line); n += 1
    if d.get("failed"):
        f += 1
        print(d["qid"], str(d.get("error"))[:200])
print("records", n, "failed", f)

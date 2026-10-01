import json, os, shutil
p = "bench/zz-mix-cg2025-harness/generations.jsonl"
bak = p + ".with-failures.bak"
if not os.path.exists(bak):
    shutil.copy2(p, bak)
lines = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
keep = [l for l in lines if not l.get("failed")]
drop = [l["qid"] for l in lines if l.get("failed")]
with open(p, "w", encoding="utf-8") as f:
    for d in keep:
        f.write(json.dumps(d, ensure_ascii=False) + "\n")
print("dropped", len(drop), drop, "| kept", len(keep))

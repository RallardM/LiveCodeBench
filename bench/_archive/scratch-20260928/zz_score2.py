import json
raw = open("zz_run_cg.out", "rb").read()
txt = raw.decode("utf-16-le", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
for l in txt.splitlines():
    if l.startswith("Sample:") or l.startswith("Pool:") or "mix" in l.lower() and "got" in l:
        print(l)
s = json.load(open("bench/zz-mix-cg2025-harness/_summary.json", encoding="utf-8"))
print("score", s["score"])
print("config.hardest", s["config"].get("hardest"), "| config.mix", s["config"].get("mix"))
print("sample", s.get("sample_hash"), "| hardest_tier", s.get("hardest_tier"))
print("pool", json.dumps(s.get("pool")))
print("note", s["config"].get("sample_note"))

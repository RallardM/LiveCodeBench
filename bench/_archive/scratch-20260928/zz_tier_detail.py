import json
d = "bench/zz-mix-cg2025-harness/"
tier = set(json.load(open(d + "hardest_ids.json", encoding="utf-8")))
s = json.load(open(d + "_summary.json", encoding="utf-8"))
pp = s["per_problem_pass"]
recs = {}
for l in open(d + "generations.jsonl", encoding="utf-8"):
    r = json.loads(l)
    recs[r["qid"]] = r
def fin(q):
    r = recs[q]
    u = r.get("completion_tokens"); m = r.get("max_tokens")
    return "TRUNC" if (u and m and u >= m) else "done"
rows = []
for q, passed in pp.items():
    rows.append((q in tier, q, passed, fin(q), recs[q].get("completion_tokens")))
for tiered, q, passed, st, ct in sorted(rows, reverse=True):
    print(f"{'HARD' if tiered else '    '}  {q:10s} {'PASS' if passed else 'fail'}  {st:6s} {ct}")
t = [r for r in rows if r[0]]
print("tier:", len(t), "passed", sum(1 for r in t if r[2]),
      "truncated", sum(1 for r in t if r[3] == "TRUNC"))
nt = [r for r in rows if not r[0]]
print("rest:", len(nt), "passed", sum(1 for r in nt if r[2]),
      "truncated", sum(1 for r in nt if r[3] == "TRUNC"))

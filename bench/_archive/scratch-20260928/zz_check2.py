import json
p = "bench/zz-mix-cg2025-harness/generations.jsonl"
recs = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
ok = [r["qid"] for r in recs]
print("records", len(recs), "distinct", len(set(ok)))
for q in ("abc400_e", "abc400_g", "arc195_b"):
    print(q, "present:", q in ok)
s = json.load(open("bench/zz-mix-cg2025-harness/_summary.json", encoding="utf-8"))
pp = s["per_problem_pass"]
print("summary problems:", len(pp))
print({k: v for k, v in list(pp.items())})

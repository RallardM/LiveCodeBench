import json
import os
import statistics as st

# --- how long do the fast scenarios take per item (from the real runs) -------
for d in ("qwen38-flash-next-q2-exec-harness",
          "unsloth-Qwen3.8-Flash-Next-GGUF-UD-IQ4_XS-exec-harness"):
    p = os.path.join("bench", d, "generations.jsonl")
    rows = [json.loads(l) for l in open(p, encoding="utf-8")]
    sec = [r["seconds"] for r in rows if r.get("seconds") is not None]
    ct = [r.get("completion_tokens") or 0 for r in rows]
    pt = [r.get("prompt_tokens") or 0 for r in rows]
    print(d, "items", len(rows), "| seconds/item med", round(st.median(sec), 1),
          "max", round(max(sec), 1), "sum", round(sum(sec) / 60, 1), "min",
          "| completion_tokens med", round(st.median(ct), 1),
          "| prompt_tokens med", round(st.median(pt), 0))

# --- codegen pool: how much recent material is there -----------------------
meta = json.load(open("zz_codegen_meta.json", encoding="utf-8"))
by = {}
for m in meta:
    key = (m["date"][:7] >= "2025-01", m["difficulty"])
    by[key] = by.get(key, 0) + 1
print("\ncodegen pool: recent-2025 vs older, per label")
for k in sorted(by):
    print("   ", ("2025+" if k[0] else "pre-2025"), k[1], by[k])
q = [m for m in meta if m["date"] >= "2024-06-01"]
print("codegen pool >= 2024-06-01:", len(q), "labels",
      {d: sum(1 for m in q if m["difficulty"] == d) for d in ("easy", "medium", "hard")})
q2 = [m for m in meta if m["date"] >= "2025-01-01"]
print("codegen pool >= 2025-01-01:", len(q2), "labels",
      {d: sum(1 for m in q2 if m["difficulty"] == d) for d in ("easy", "medium", "hard")})

# --- codegen: recency x label, from the two real runs ----------------------
mmap = {m["qid"]: m for m in meta}
buckets = {}
for tag, d in (("harness", "qwen38-flash-next-q2-harness"),
               ("plain", "qwen38-flash-next-q2-noharness")):
    ppp = json.load(open(os.path.join("bench", d, "_summary.json"),
                         encoding="utf-8"))["per_problem_pass"]
    for qid, ok in ppp.items():
        m = mmap.get(qid)
        if not m:
            continue
        age = "2025" if m["date"][:4] == "2025" else m["date"][:4]
        buckets.setdefault((m["difficulty"], age), []).append(bool(ok))
print("\ncodegen pass@1 by label x year:")
for k in sorted(buckets):
    g = buckets[k]
    print(f"   {k[0]:6} {k[1]}  n={len(g):3}  pass={sum(g)/len(g)*100:5.1f}%")

# --- exec/top feature tables: which structural slices are hard? ------------
ex = json.load(open("zz_exec_features.json", encoding="utf-8"))
print("\nexec top-50 structural (score = code_len + output_len + 300*(rows-1)) mix:")
sc = sorted(ex, key=lambda r: -(r["code_len"] + r["output_len"] + 300 * (r["rows_per_question"] - 1)))
t50 = sc[:50]
print("   labels", {d: sum(1 for r in t50 if r["difficulty"] == d) for d in ("easy", "medium", "hard")},
      "| code_len>=", min(r["code_len"] for r in t50),
      "| q2 pass on the subset that was run:",
      sum(1 for r in t50 if r.get("pass_q2")), "of", sum(1 for r in t50 if "pass_q2" in r),
      "| iq4:", sum(1 for r in t50 if r.get("pass_iq4")),
      "of", sum(1 for r in t50 if "pass_iq4" in r))
for kind, rows in (("exec", ex), ("top", json.load(open("zz_top_features.json", encoding="utf-8")))):
    print(f"\n{kind}: biggest / longest 10 rows of the pool")
    key = "code_len" if kind == "exec" else "content_len"
    for r in sorted(rows, key=lambda r: -r[key])[:10]:
        print("   ", r["qid"], r["difficulty"], r["date"], key, r[key],
              "out_len", r.get("output_len", r.get("out_len")))

"""Why do the fast scenarios come out at 100% (and is code generation any
different)? Dump feature tables + join the finished runs, write JSON artifacts.

Throwaway: zz_easy_probe.py -> zz_exec_features.json / zz_top_features.json /
zz_codegen_meta.json
"""
import base64
import json
import statistics as st
import zlib

from datasets import load_dataset

import lcb_bench as lb

EXEC_RUNS = {"q2": "qwen38-flash-next-q2-exec-harness",
             "iq4": "unsloth-Qwen3.8-Flash-Next-GGUF-UD-IQ4_XS-exec-harness"}
CG_RUNS = {"q2_yes": "qwen38-flash-next-q2-harness",
           "q2_no": "qwen38-flash-next-q2-noharness"}


def out_kind(v):
    s = str(v)
    if s.startswith("[") or s.startswith("{"):
        depth = best = 0
        for ch in s:
            if ch in "[{":
                depth += 1
                best = max(best, depth)
            elif ch in "]}":
                depth -= 1
        return f"nested{best}"
    if s.lower() in ("true", "false"):
        return "bool"
    if s.startswith("Traceback") or "Error" in s[:40]:
        return "error"
    if "." in s and s.replace(".", "", 1).replace("-", "", 1).isdigit():
        return "float"
    if s.replace("-", "", 1).isdigit():
        return "int"
    if s in ("None", "null", ""):
        return "none"
    return "string"


def run_results(run_dir):
    import os
    got = {}
    p = os.path.join("bench", run_dir, "eval_results.jsonl")
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8"):
            r = json.loads(line)
            res = [x for sub in r["results"] for x in sub]
            got[str(r["qid"])] = bool(res) and all(res)
    return got


def quantiles(vals, n=5):
    vs = sorted(vals)
    if not vs:
        return []
    return [vs[min(int(len(vs) * (i + 1) / n), len(vs) - 1)] for i in range(n)]


def bucket(v, cuts):
    for i, c in enumerate(cuts):
        if v <= c:
            return i + 1
    return len(cuts) + 1


def table(groups, keyfun, label, tags):
    buckets = {}
    for r in groups:
        buckets.setdefault(keyfun(r), []).append(r)
    print(f"\n  by {label}:", flush=True)
    for k in sorted(buckets, key=lambda k: (-len(buckets[k]), str(k))):
        g = buckets[k]
        cells = []
        for tag in tags:
            yes = sum(1 for r in g if r.get("pass_" + tag))
            n = sum(1 for r in g if "pass_" + tag in r)
            if n:
                cells.append(f"{tag}={yes/n*100:5.1f}% (n={n})")
        print(f"    {str(k)[:16]:17} " + "  ".join(cells), flush=True)


# ------------------------------------------------------------------ exec
print("loading exec pool ...", flush=True)
ds = load_dataset(lb.EXEC_DATASET_NAME, split="test", trust_remote_code=True)
seen = {}
exec_rows = []
for row in ds:
    base = str(row["question_id"])
    idx = seen.get(base, 0)
    seen[base] = idx + 1
    code, inp, outp = row["code"], row["input"], row["output"]
    exec_rows.append({
        "qid": f"{base}#{idx}", "base": base,
        "difficulty": str(row["difficulty"]).lower(),
        "date": str(row["contest_date"])[:10],
        "code_len": len(code or ""), "input_len": len(str(inp or "")),
        "output_len": len(str(outp or "")), "out_kind": out_kind(outp),
    })
for r in exec_rows:
    r["rows_per_question"] = seen[r["base"]]
for tag, name in EXEC_RUNS.items():
    passed = run_results(name)
    for r in exec_rows:
        if r["qid"] in passed:
            r["pass_" + tag] = passed[r["qid"]]
json.dump(exec_rows, open("zz_exec_features.json", "w"), indent=0)
ran = [r for r in exec_rows if "pass_q2" in r]
print(f"exec rows={len(exec_rows)} questions={len(seen)} ran={len(ran)}", flush=True)
tags = ["q2", "iq4"]
table(ran, lambda r: r["difficulty"], "exec pass rate by label", tags)
cl = quantiles([r["code_len"] for r in ran])
table(ran, lambda r: f"L{bucket(r['code_len'], cl)}", "code length quintile", tags)
ol = quantiles([r["output_len"] for r in ran])
table(ran, lambda r: f"O{bucket(r['output_len'], ol)}", "output length quintile", tags)
sc = sorted(exec_rows, key=lambda r: -(r["code_len"] + r["output_len"]
                                       + 300 * (r["rows_per_question"] - 1)))
top10 = sc[:len(sc) // 10]
mix = {}
for r in top10:
    mix[r["difficulty"]] = mix.get(r["difficulty"], 0) + 1
print(f"\nexec score-ranked top 10% ({len(top10)} rows): labels {mix}"
      f" code_len>={min(r['code_len'] for r in top10)} dates"
      f" {min(r['date'] for r in top10)}..{max(r['date'] for r in top10)}", flush=True)

# ------------------------------------------------------------------ top
print("\nloading top pool ...", flush=True)
ds2 = load_dataset(lb.TOP_DATASET_NAME, split="test", trust_remote_code=True)
top_rows = []
for row in ds2:
    t = json.loads(row["test"])[0]
    top_rows.append({
        "qid": f"{row['question_id']}#{row['test_id']}",
        "difficulty": str(row["difficulty"]).lower(),
        "date": str(row["contest_date"])[:10],
        "content_len": len(row["question_content"] or ""),
        "starter_len": len(row["starter_code"] or ""),
        "input_len": len(str(t["input"] or "")),
        "out_len": len(str(t["output"] or "")), "out_kind": out_kind(t["output"]),
    })
json.dump(top_rows, open("zz_top_features.json", "w"), indent=0)
print(f"top rows={len(top_rows)} questions="
      f"{len({r['qid'].split('#')[0] for r in top_rows})}", flush=True)
for d in ("easy", "medium", "hard"):
    g = [r for r in top_rows if r["difficulty"] == d]
    if not g:
        continue
    print(f"  {d:6} n={len(g):4} content med={int(st.median(x['content_len'] for x in g))}"
          f" starter med={int(st.median(x['starter_len'] for x in g))}"
          f" out med={int(st.median(x['out_len'] for x in g))}"
          f" out max={max(x['out_len'] for x in g)} dates"
          f" {min(x['date'] for x in g)}..{max(x['date'] for x in g)}", flush=True)

# ------------------------------------------------------------- codegen meta
print("\nloading code_generation metadata ...", flush=True)
ds3 = load_dataset(lb.DATASET_NAME, split="test", trust_remote_code=True)
meta = {}
for row in ds3:
    qid = str(row["question_id"])
    n_tests = -1
    try:
        pub = json.loads(row["public_test_cases"])
        try:
            priv = json.loads(row["private_test_cases"])
        except Exception:
            priv = json.loads(zlib.decompress(base64.b64decode(
                row["private_test_cases"].encode("utf-8"))))
        n_tests = len(pub) + len(priv)
    except Exception:
        pass
    d = str(row["contest_date"])[:10]
    meta[qid] = {"qid": qid, "difficulty": str(row["difficulty"]).lower(),
                 "date": d, "n_tests": n_tests,
                 "content_len": len(row["question_content"] or "")}
json.dump(list(meta.values()), open("zz_codegen_meta.json", "w"), indent=0)
print(f"codegen rows={len(meta)} dates {min(v['date'] for v in meta.values())}.."
      f"{max(v['date'] for v in meta.values())}", flush=True)

cg_runs = {t: run_results(n) for t, n in CG_RUNS.items()}
cg = [dict(meta[k], **{f"pass_{t}": v.get(k) for t, v in cg_runs.items()
                       if k in v}) for k in meta if any(k in v for v in cg_runs.values())]
print(f"codegen rows with results: {len(cg)}", flush=True)
ctags = list(cg_runs.keys())
table(cg, lambda r: r["difficulty"], "codegen pass rate by label", ctags)
table(cg, lambda r: r["date"][:4], "codegen pass rate by year", ctags)
nt = quantiles([r["n_tests"] for r in cg if r["n_tests"] > 0], 4)
table([r for r in cg if r["n_tests"] > 0],
      lambda r: f"T{bucket(r['n_tests'], nt)}", "codegen pass rate by test-case count",
      ctags)
yr = quantiles([int(r["date"][:4]) for r in cg if r["date"]], 4)
table(cg, lambda r: f"Y{bucket(int(r['date'][:4]), yr)}", "codegen quartile by date",
      ctags)
print("ALL DONE", flush=True)

"""Probe 2: exec rows grouped by identical code; top rows grouped by question."""
import json
import sys
from collections import Counter, defaultdict

sys.path.insert(0, ".")
import lcb_bench as lb

exec_probs = lb.load_exec_problems("release_latest", None, None, None, 0)
by_code = defaultdict(list)
for p in exec_probs:
    d = json.loads(p["eval_payload"])
    by_code[d["code"]].append((d["input"], d["output"]))
sizes = Counter(len(v) for v in by_code.values())
print(f"exec: distinct code blocks = {len(by_code)}")
print(f"  rows per identical-code group: {dict(sorted(sizes.items()))}")
for K in (2, 3, 4):
    items = sum(len(v) // K for v in by_code.values())
    ok = sum(1 for v in by_code.values() if len(v) >= K)
    # distinct inputs inside groups
    print(f"  K={K}: {ok} code blocks with >= K rows; {items} disjoint bundles")
dup_in = 0
for code, rows in by_code.items():
    if len({i for i, _ in rows}) != len(rows):
        dup_in += 1
print(f"  code blocks with duplicate inputs: {dup_in}")
lens = sorted(len(code) for code in by_code)
print(f"  code block sizes: median {lens[len(lens)//2]}, max {lens[-1]}")
sample = max(by_code.items(), key=lambda kv: len(kv[1]))
print("  biggest group input list:", [i for i, _ in sample[1]])

top_probs = lb.load_top_problems("release_latest", None, None, None, 0)
by_q = defaultdict(set)
for p in top_probs:
    base = str(p["question_id"]).split("#")[0]
    by_q[base].add(p["messages"][1]["content"][:200])
multi = sum(1 for v in by_q.values() if len(v) > 1)
print(f"top: {len(by_q)} questions; {multi} have differing prompt heads per base id")
print("DONE")

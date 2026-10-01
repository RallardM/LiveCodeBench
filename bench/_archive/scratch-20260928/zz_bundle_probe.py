"""Probe: how many same-question bundles can exec/top pools supply? (no model)"""
import json
import sys

sys.path.insert(0, ".")
import lcb_bench as lb


def dist(rows_per_q):
    from collections import Counter
    c = Counter(rows_per_q)
    return dict(sorted(c.items()))


print("exec pool ...")
exec_probs = lb.load_exec_problems("release_latest", None, None, None, 0)
by_q = {}
for p in exec_probs:
    base = str(p["question_id"]).split("#")[0]
    by_q.setdefault(base, []).append(p)
print(f"  rows={len(exec_probs)} questions={len(by_q)}")
print(f"  rows/question dist: {dist([len(v) for v in by_q.values()])}")
for K in (2, 3, 4, 5, 6):
    items = sum(len(v) // K for v in by_q.values())
    q_ok = sum(1 for v in by_q.values() if len(v) >= K)
    print(f"  K={K}: {q_ok} questions have >= K rows; {items} disjoint bundle(s)")
# verify identical code within a question
bad = 0
for base, rows in by_q.items():
    codes = {json.loads(r["eval_payload"])["code"] for r in rows}
    if len(codes) != 1:
        bad += 1
print(f"  questions whose rows do NOT share one code block: {bad}")
# distinct inputs within a question?
bad_in = 0
for base, rows in by_q.items():
    ins = [json.loads(r["eval_payload"])["input"] for r in rows]
    if len(set(ins)) != len(ins):
        bad_in += 1
print(f"  questions with duplicate inputs among rows: {bad_in}")

print("top pool ...")
top_probs = lb.load_top_problems("release_latest", None, None, None, 0)
by_qt = {}
for p in top_probs:
    base = str(p["question_id"]).split("#")[0]
    by_qt.setdefault(base, []).append(p)
print(f"  rows={len(top_probs)} questions={len(by_qt)}")
print(f"  rows/question dist: {dist([len(v) for v in by_qt.values()])}")
for K in (2, 3, 4):
    items = sum(len(v) // K for v in by_qt.values())
    q_ok = sum(1 for v in by_qt.values() if len(v) >= K)
    print(f"  K={K}: {q_ok} questions have >= K rows; {items} disjoint bundle(s)")
print("DONE")

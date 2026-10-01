"""Dry-check bundle loading: prompts, ids, payloads, mix interaction. No server."""
import json
import sys

sys.path.insert(0, ".")
import lcb_bench as lb

print("== exec bundle K=3, mix 50/25/15/10, sample 12 ==")
ps = lb.load_exec_problems("release_latest", None, None, None, 0,
                           random_sample=12,
                           mix=lb.parse_mix("50/25/15/10"), bundle=3)
print("ids:", [p["question_id"] for p in ps])
p = ps[0]
print("kind:", p.get("kind"), "bundle:", p.get("bundle"), "weight:", p["weight"])
print("inputs:", p["inputs"])
msg = p["messages"][1]["content"]
print("---- prompt head (first 900 chars) ----")
print(msg[:900])
print("---- prompt tail (last 700 chars) ----")
print(msg[-700:])
items = json.loads(p["eval_payload"])["items"]
print("payload items:", len(items), "codes distinct:",
      len({it["code"] for it in items}))

print()
print("== top bundle K=2, sample 6 ==")
ts = lb.load_top_problems("release_latest", None, None, None, 0,
                          random_sample=6, bundle=2)
print("ids:", [p["question_id"] for p in ts])
t = ts[0]
print("calls:", t["calls"])
msg = t["messages"][1]["content"]
print("---- top prompt tail (last 900 chars) ----")
print(msg[-900:])
print("payload:", t["eval_payload"][:200])
print("DONE")

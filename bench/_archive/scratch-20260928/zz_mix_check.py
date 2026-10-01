"""Offline check of parse_mix / mix_pick / pool_facts with fake problems.
No datasets, no server - pure logic, run in seconds."""
import datetime as dt
import lcb_bench as lb

def mk(qid, label, year, weight):
    return {"question_id": qid, "difficulty": label,
            "contest_date": dt.datetime(year, 6, 1), "weight": weight}

def show(tag, picked):
    rows = [(p["question_id"], p["difficulty"], p["contest_date"].year,
            p["weight"]) for p in picked]
    print(f"--- {tag}: {len(rows)} picked")
    for r in rows:
        print("     ", r)

# pool: 4 hard (two from 2025, two from 2023), 6 medium, 20 easy
hard_rows = [(2025, 90), (2025, 60), (2023, 40), (2023, 20)]
pool = ([mk(f"h{i}", "hard", y, w) for i, (y, w) in enumerate(hard_rows)]
        + [mk(f"m{i}", "medium", 2024, 30) for i in range(6)]
        + [mk(f"e{i}", "easy", 2024, 10) for i in range(20)])

for spec_text in ["50/25/15/10", "hardest=50,hard=25,medium=15,easy=10",
                  "100", "hard=60,medium=40", "50/25/25"]:
    spec = lb.parse_mix(spec_text)
    picked, tier, note = lb.mix_pick(pool, 20, spec, spec_text)
    print(f"== spec {spec_text!r} -> shares {spec}")
    print("   note:", note)
    show("picked (id,label,year,weight)", picked)
    print("   hardest tier ids:", sorted(tier))

# a pool that cannot fill any of it: 2 hard, rest easy, ask 10
small = ([mk("h0", "hard", 2025, 50), mk("h1", "hard", 2024, 30)]
         + [mk(f"e{i}", "easy", 2023, 10) for i in range(30)])
spec = lb.parse_mix("50/25/15/10")
picked, tier, note = lb.mix_pick(small, 20, spec, "50/25/15/10")
print("== short pool note:", note)
show("picked", picked)

# hardest_picks ordering: newest before biggest inside a label
hp = lb.hardest_picks(pool, 5)
print("== hardest_picks(5):",
      [(p["question_id"], p["difficulty"], p["contest_date"].year,
        p["weight"]) for p in hp])

# parse_mix rejects the nonsense
for bad in ["50/25/15/10/5", "hardest=50,impossible=10", "-5/105",
            "0/0/0/0", "hard=50"]:
    try:
        print("== bad spec accepted:", bad, lb.parse_mix(bad))
    except ValueError as e:
        print("== bad spec rejected:", repr(bad), "->", e)

print("== pool_facts:", lb.pool_facts(pool))

# --- realistic pools, same shapes the real datasets have -----------------------
def fake(n_by_label, years, w_lo, w_hi):
    """fake pool: n per label, contests spread over `years`, weights spread."""
    rows = []
    for label, n in n_by_label.items():
        for i in range(n):
            y = years[i % len(years)]
            w = w_lo + (i * 7) % (w_hi - w_lo)
            rows.append(mk(f"{label}{i}", label, y, w))
    return rows

CODEGEN = fake({"easy": 45, "medium": 55, "hard": 82},
               [2025, 2025, 2024, 2025, 2023, 2025], 100, 900)
EXEC = fake({"easy": 216, "medium": 254, "hard": 9},
            [2023, 2023, 2023, 2023, 2024, 2023], 60, 500)
TOP = fake({"easy": 147, "medium": 223, "hard": 72},
           [2023, 2024, 2023, 2024, 2023, 2024], 300, 1250)

for name, big in (("codegen 2025+", CODEGEN), ("exec (all)", EXEC),
                  ("top (all)", TOP)):
    print(f"\n#### {name}: {len(big)} rows, labels",
          lb.pool_facts(big)["labels"])
    for spec_text in ("50/25/15/10", "hardest=100"):
        spec = lb.parse_mix(spec_text)
        picked, tier, note = lb.mix_pick(big, 100, spec, spec_text)
        years = {}
        for p in picked:
            y = p["contest_date"].year
            years[y] = years.get(y, 0) + 1
        print(f"   --mix {spec_text} -> {note}")
        print(f"      per year: {dict(sorted(years.items()))},"
              f" weight median {sorted(lb.problem_weight(p) for p in picked)[50]}")

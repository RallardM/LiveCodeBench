import lcb_bench as lb
lb.POOL_FACTS = {"n": 442, "questions": 182, "labels": {"easy": 147, "hard": 72, "medium": 223},
                 "date_min": "2023-05-07", "date_max": "2024-03-02", "weight_min": 176,
                 "weight_median": 514, "weight_max": 1308, "biggest": "2791#1"}
print(lb.saturation_note("top", 95.0, 60, {"hard": [1]*60}, {"n": 60, "pass@1": 0.95}))
print("----")
print(lb.saturation_note("codegen", 100.0, 12, {"hard": [1]*12}, {"n": 12, "pass@1": 1.0}))

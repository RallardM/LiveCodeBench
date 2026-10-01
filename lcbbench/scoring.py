"""pass@k maths and the per-problem (n, c) count shared by every pipeline."""


def estimate_pass_at_k(n, c, k):
    if n - c < k:
        return 1.0
    import numpy as np

    return float(1.0 - np.prod(1.0 - k / np.arange(n - c + 1, n + 1)))


def compute_pass_metrics(per_problem):
    out = {}
    if not per_problem:
        return out
    for k in (1, 5, 10):
        if all(n >= k for n, _ in per_problem):
            vals = [estimate_pass_at_k(n, c, k) for n, c in per_problem]
            out[f"pass@{k}"] = sum(vals) / len(vals)
    return out


def count_pass(rec):
    """(n, c) of one graded record: n samples, c of them right. Skipped
    (input-echoing) exec samples leave n, like the official code; if every
    sample was skipped n stays the full count and c is 0."""
    kind = rec.get("kind", "codegen")
    if kind == "codegen":
        n = len(rec["results"])
        c = sum(1 for res in rec["results"] if res and all(x > 0 for x in res))
    elif kind in ("exec", "execb"):
        skips = rec.get("skipped", [False] * len(rec["results"]))
        scored = [r for r, s in zip(rec["results"], skips) if not s]
        c = sum(1 for r in scored if r and all(bool(x) for x in r))
        n = len(scored) if scored else len(rec["results"])
    else:  # top, topb, math, mcq
        n = len(rec["results"])
        c = sum(1 for r in rec["results"] if r and all(bool(x) for x in r))
    return n, c

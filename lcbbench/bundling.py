"""--bundle K: K single calls asked in ONE answer, all right or the item fails.
Same rows, same grader, same prompts; only the answer unit gets heavier."""

import json
from itertools import groupby as _groupby

from .prompts import (format_prompt_exec_bundle, format_prompt_top_bundle,
                      parse_function_name_from_starter_code)


def _bundle_chunks(rows, k):
    """Chunk rows (already sorted, grouped by base question id) into consecutive
    full groups of k; a leftover of 1..k-1 rows at the end of a question is
    dropped, so every item is exactly k calls wide. Deterministic."""

    def base_of(p):
        return str(p["question_id"]).split("#")[0]

    chunks = []
    for base, grp in _groupby(sorted(rows, key=lambda p: (
            str(p["question_id"]).split("#")[0], str(p["question_id"]))), key=base_of):
        grp = list(grp)
        n_full = len(grp) // k
        for ci in range(n_full):
            chunks.append((base, ci, grp[ci * k:(ci + 1) * k]))
    return chunks


def bundle_capacity(problems):
    """How many K-call bundle items this pool could supply, per K (2..6)."""
    caps = {}
    for k in range(2, 7):
        by_base = {}
        for p in problems:
            b = str(p["question_id"]).split("#")[0]
            by_base[b] = by_base.get(b, 0) + 1
        caps[str(k)] = sum(n // k for n in by_base.values())
    return caps


def bundle_exec_problems(rows, k):
    """K exec rows -> one all-or-nothing item (K small programs answered in one
    go; the same contest problem's rows stay together, their calls differ)."""
    out = []
    for base, ci, grp in _bundle_chunks(rows, k):
        items = []
        for p in grp:
            d = json.loads(p["eval_payload"])
            items.append({"code": d["code"], "input": d["input"],
                          "output": d["output"]})
        first = grp[0]
        out.append({
            "question_id": f"{base}#b{k}c{ci}",
            "difficulty": first["difficulty"],
            "contest_date": first["contest_date"],
            "kind": "exec",
            "bundle": k,
            "inputs": [it["input"] for it in items],
            "eval_payload": json.dumps({"items": items}, ensure_ascii=False),
            "weight": sum(int(p.get("weight") or 0) for p in grp),
            "messages": format_prompt_exec_bundle(items, cot=False),
        })
    return out


def bundle_exec_cot(rows, k, problems_bundle):
    """Rebuild the bundle prompts for --exec-cot."""
    for p in problems_bundle:
        items = json.loads(p["eval_payload"])["items"]
        p["messages"] = format_prompt_exec_bundle(items, cot=True)
    return problems_bundle


def bundle_top_problems(rows, k):
    """K tests of the SAME problem -> one all-or-nothing item: the model reads
    the problem once and must predict the output of K different inputs."""
    out = []
    for base, ci, grp in _bundle_chunks(rows, k):
        first = grp[0]
        items, calls = [], []
        fn = parse_function_name_from_starter_code(first.get("starter") or "") \
            or first.get("fname")
        for p in grp:
            d = json.loads(p["eval_payload"])
            items.append({"expected": d["expected"]})
            calls.append(f"{fn}({', '.join(str(p['test_input']).splitlines())})")
        out.append({
            "question_id": f"{base}#tb{k}c{ci}",
            "difficulty": first["difficulty"],
            "contest_date": first["contest_date"],
            "kind": "top",
            "bundle": k,
            "calls": calls,
            "eval_payload": json.dumps({"items": items}, ensure_ascii=False),
            "weight": sum(int(p.get("weight") or 0) for p in grp),
            "messages": format_prompt_top_bundle(
                first["q_content"], first["starter"], first["fname"],
                [p["test_input"] for p in grp]),
        })
    return out

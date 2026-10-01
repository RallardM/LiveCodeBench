"""How a problem pool becomes a sample: filters, stratified picks, the hardest
tier and the --mix shares."""

import datetime as dt
import json
import os
import sys

from . import state
from .bundling import bundle_capacity


def problem_weight(problem):
    """Rough hardness signal inside a difficulty band. For code generation it is
    how many test cases one answer has to satisfy at once. The fast scenarios
    store their own `weight` (how much code / problem text the call carries).
    Unknown: 0, the other sort keys decide there."""
    stored = problem.get("weight")
    if stored is not None:
        return stored
    try:
        data = json.loads(problem.get("eval_payload") or "")
    except Exception:
        return 0
    if isinstance(data, dict):
        for key in ("inputs", "testcases"):
            if isinstance(data.get(key), list):
                return len(data[key])
    return 0


HARDNESS_RANK = {"hard": 0, "medium": 1, "easy": 2}
MIX_TIERS = ("hardest", "hard", "medium", "easy")


def _big_new_first(p):
    """Sort key inside one difficulty band: newest contest first, then the
    biggest problem. Age goes first because a problem the model has already
    read is not a hard problem, it is a recall test."""
    return (-p["contest_date"].timestamp(), -problem_weight(p),
            str(p["question_id"]))


def _hard_key(p):
    """Full hardness key: dataset label band first, then newest and biggest."""
    return (HARDNESS_RANK.get(str(p["difficulty"]).lower(), 1),) + _big_new_first(p)


def hardest_picks(pool, count, exclude=()):
    """The `count` hardest problems of `pool` that are not already `exclude`d:
    hard before medium before easy, and inside a band the newest and biggest
    problems first."""
    skip = {str(q) for q in exclude}
    left = [p for p in pool if str(p["question_id"]) not in skip]
    left.sort(key=_hard_key)
    return left[:count]


def parse_mix(spec):
    """`50/25/15/10` (or `hardest=50,hard=25,medium=15,easy=10`) -> the share of
    one sample every tier should take. Positional numbers fill
    hardest/hard/medium/easy in that order; missing tiers get nothing."""
    if not spec:
        return None
    parts = [p.strip() for p in spec.replace(",", "/").split("/") if p.strip()]
    named, plain = {}, []
    for p in parts:
        if "=" in p:
            k, _, v = p.partition("=")
            named[k.strip().lower()] = float(v)
        else:
            plain.append(float(p))
    if named:
        unknown = sorted(set(named) - set(MIX_TIERS))
        if unknown:
            raise ValueError(f"--mix: unknown tier '{', '.join(unknown)}' - use"
                             " hardest/hard/medium/easy")
        shares = {t: float(named.get(t, 0)) for t in MIX_TIERS}
    else:
        if len(plain) > len(MIX_TIERS):
            raise ValueError("--mix: at most four numbers"
                             " (hardest/hard/medium/easy)")
        shares = {t: (plain[i] if i < len(plain) else 0.0)
                  for i, t in enumerate(MIX_TIERS)}
    if any(v < 0 for v in shares.values()):
        raise ValueError("--mix: a share cannot be negative")
    total = sum(shares.values())
    if total <= 0:
        raise ValueError("--mix: at least one share must be above zero")
    return {t: v / total for t, v in shares.items() if v > 0}


def mix_pick(pool, count, spec, spec_text=""):
    """Fill `count` problems from `pool` in the tier proportions of `spec`.
    `hardest` is the structural ranking of the whole pool (label, then newest,
    then biggest); the other tiers are the dataset's own labels, newest first.
    Scarce tiers are filled first, `hardest` last. A tier the pool cannot fill
    is said out loud and its empty slots go to the best problems still on the
    table. Returns (picked, ids of the hardest tier, note)."""
    want = {t: int(round(v * count)) for t, v in spec.items()}
    slack = count - sum(want.values())
    if slack and want:
        want[max(spec, key=lambda t: spec[t])] += slack
    supply = {}
    for p in pool:
        k = str(p["difficulty"]).lower()
        supply[k] = supply.get(k, 0) + 1
    named = [t for t in MIX_TIERS if t != "hardest" and want.get(t)]
    named.sort(key=lambda t: (supply.get(t, 0) / want[t], t))
    order = named + (["hardest"] if want.get("hardest") else [])
    picked, got, tier_ids = [], {}, set()
    for tier in order:
        k = want[tier]
        taken = {str(p["question_id"]) for p in picked}
        rows = [p for p in pool if str(p["question_id"]) not in taken]
        if tier == "hardest":
            rows.sort(key=_hard_key)
        else:
            rows = [p for p in rows if str(p["difficulty"]).lower() == tier]
            rows.sort(key=_big_new_first)
        chunk = rows[:k]
        if tier == "hardest":
            tier_ids = {str(p["question_id"]) for p in chunk}
        got[tier] = len(chunk)
        picked += chunk
    short = count - len(picked)
    if short > 0:
        taken = {str(p["question_id"]) for p in picked}
        rest = [p for p in pool if str(p["question_id"]) not in taken]
        rest.sort(key=_hard_key)
        picked += rest[:short]
    labels = {}
    for p in picked:
        k = str(p["difficulty"]).lower()
        labels[k] = labels.get(k, 0) + 1
    parts = [f"{got.get(t, 0)}" + (f"/{want[t]}" if want.get(t) != got.get(t) else "")
             + f" {t}" for t in MIX_TIERS if want.get(t)]
    note = ("mix " + (spec_text + ": " if spec_text else "") + "got "
            + ", ".join(parts) + "; labels "
            + ", ".join(f"{v} {k}" for k, v in sorted(labels.items())))
    if short > 0:
        note += (f" - {short} slot(s) refilled with the best that was left: this"
                 " pool cannot fill every tier asked for")
    return picked, tier_ids, note


def pool_facts(problems):
    """What a sample can be drawn from: rows, distinct questions, the dataset's
    own labels, the date range and the weight range."""
    if not problems:
        return {"n": 0}
    labels = {}
    for p in problems:
        k = str(p["difficulty"]).lower()
        labels[k] = labels.get(k, 0) + 1
    dates = [p["contest_date"] for p in problems]
    bases = {str(p["question_id"]).split("#")[0] for p in problems}
    ws = sorted(problem_weight(p) for p in problems)
    biggest = max(problems, key=problem_weight)
    return {
        "n": len(problems),
        "questions": len(bases),
        "labels": labels,
        "date_min": min(dates).strftime("%Y-%m-%d"),
        "date_max": max(dates).strftime("%Y-%m-%d"),
        "weight_min": ws[0], "weight_median": ws[len(ws) // 2], "weight_max": ws[-1],
        "biggest": str(biggest["question_id"]),
    }


def stratified_pick(pool, count, seed=1234, equal_tiers=False):
    """`count` problems taken from `pool`. By default the difficulties are spread
    in the proportion the pool holds them; with `equal_tiers` every difficulty
    present gets the same share instead. Fixed seed: same pool, same picks."""
    import random as _random

    rng = _random.Random(seed)
    groups = {}
    for p in pool:
        groups.setdefault(p["difficulty"].lower(), []).append(p)
    if equal_tiers:
        quotas = {d: count / len(groups) for d in groups}
    else:
        quotas = {d: count * len(g) / len(pool) for d, g in groups.items()}
    alloc = {d: int(q) for d, q in quotas.items()}
    remaining = count - sum(alloc.values())
    for d in sorted(quotas, key=lambda k: quotas[k] - int(quotas[k]), reverse=True):
        if remaining <= 0:
            break
        alloc[d] += 1
        remaining -= 1
    picked = []
    for d, g in groups.items():
        picked += rng.sample(g, min(alloc[d], len(g)))
    if len(picked) < count:
        # a tier ran out - top up from what is left so the sample is `count` wide
        taken = {str(p["question_id"]) for p in picked}
        left = [p for p in pool if str(p["question_id"]) not in taken]
        picked += rng.sample(left, min(count - len(picked), len(left)))
    return picked


def _write_sample_ids(path, problems):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(str(p["question_id"]) for p in problems), f)


def _write_tier(path, ids):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)


def filter_sample(problems, start_date, end_date, difficulty, limit,
                  random_sample=0, sample_ids_file=None, hardest=0, mix=None):
    state.SAMPLE_NOTE = ""
    state.HARDEST_IDS = set()
    state.POOL_FACTS = {}
    tier_file = (os.path.join(os.path.dirname(sample_ids_file) or ".",
                              "hardest_ids.json") if sample_ids_file else None)
    problems.sort(key=lambda p: str(p["question_id"]))
    if start_date:
        d = dt.datetime.strptime(start_date, "%Y-%m-%d")
        problems = [p for p in problems if p["contest_date"] >= d]
    if end_date:
        d = dt.datetime.strptime(end_date, "%Y-%m-%d")
        problems = [p for p in problems if p["contest_date"] <= d]
    if difficulty:
        problems = [
            p for p in problems if p["difficulty"].lower() == difficulty.lower()
        ]
    if limit:
        problems = problems[:limit]
    pool = problems
    state.POOL_FACTS = pool_facts(pool)
    # how many K-call bundle items this pool could supply (read by --pool-info)
    state.POOL_FACTS["bundle_cap"] = bundle_capacity(pool)
    reused_sample = False
    if random_sample:
        if sample_ids_file and os.path.exists(sample_ids_file):
            # resume: reuse the exact same subset chosen for this run
            reused_sample = True
            with open(sample_ids_file, encoding="utf-8") as f:
                keep = {str(x) for x in json.load(f)}
            problems = [p for p in problems if str(p["question_id"]) in keep]
            if not problems and keep:
                sys.exit("The saved sample list of this run holds ids that are"
                         " not in this pool - a --bundle run scores items, not"
                         " rows, so its sample is a different universe. Give the"
                         " bundled run its own --name (or delete sample_ids.json"
                         " in the run folder to pick fresh).")
        elif len(problems) > random_sample:
            picked = stratified_pick(problems, random_sample)
            picked.sort(key=lambda p: str(p["question_id"]))
            problems = picked
            if sample_ids_file:
                os.makedirs(os.path.dirname(sample_ids_file) or ".", exist_ok=True)
                with open(sample_ids_file, "w", encoding="utf-8") as f:
                    json.dump(sorted(str(p["question_id"]) for p in picked), f)
    if (hardest or mix) and reused_sample:
        if tier_file and os.path.exists(tier_file):
            with open(tier_file, encoding="utf-8") as f:
                state.HARDEST_IDS = {str(x) for x in json.load(f)}
            print("--hardest/--mix: this run already has its saved sample list,"
                  " so it is reused exactly as it was picked"
                  f" ({len(state.HARDEST_IDS)} of them are the hardest tier)")
        else:
            print("--hardest/--mix: this run already has its saved sample list"
                  " (the hardest problems are inside it), so it is reused exactly"
                  " as it was picked")
        hardest, mix = 0, None
    if mix:
        if limit:
            print("--mix: ignored together with --limit (the limit already says"
                  " which problems, mix has no room to pick any)")
        elif not random_sample:
            print("--mix: needs --random-sample N - it is a share *of a sample*,"
                  " so with no sample there is nothing to fill. Ignored.")
        elif len(pool) <= random_sample:
            print(f"--mix: the filter holds {len(pool)} problems, not more than the"
                  " sample size, so mix has nowhere to pick")
        else:
            chosen, tier_ids, note = mix_pick(pool, random_sample, mix,
                                              str(state.MIX_SPEC or mix))
            problems = sorted(chosen, key=lambda p: str(p["question_id"]))
            state.HARDEST_IDS = tier_ids
            state.SAMPLE_NOTE = note
            print(f"Sample: {note}")
            hardest = 0
            if sample_ids_file:
                _write_sample_ids(sample_ids_file, problems)
                if tier_ids:
                    _write_tier(tier_file, tier_ids)
    if hardest:
        if limit:
            print(f"--hardest {hardest}: ignored together with --limit (the limit"
                  " already says which problems, hardest has no room to pick any)")
        elif random_sample and len(pool) > random_sample:
            # One sample of exactly random_sample problems: the hardest N slots go
            # to the hardest problems the release has, the rest are spread evenly
            # over easy / medium / hard.
            chosen = hardest_picks(pool, min(hardest, random_sample))
            ids = {str(p["question_id"]) for p in chosen}
            room = random_sample - len(chosen)
            rest_pool = [p for p in pool if str(p["question_id"]) not in ids]
            rest = (rest_pool if room >= len(rest_pool)
                    else stratified_pick(rest_pool, room, equal_tiers=True))
            problems = sorted(chosen + rest, key=lambda p: str(p["question_id"]))
            state.HARDEST_IDS = set(ids)
            bd = {}
            for p in chosen:
                k = str(p["difficulty"]).lower()
                bd[k] = bd.get(k, 0) + 1
            state.SAMPLE_NOTE = (f"{len(chosen)} hardest ("
                                 + ", ".join(f"{v} {d}" for d, v in sorted(bd.items()))
                                 + f") + {len(rest)} spread (--hardest {hardest})")
            print(f"Hardest: {state.SAMPLE_NOTE}")
            if sample_ids_file:
                _write_sample_ids(sample_ids_file, problems)
                _write_tier(tier_file, state.HARDEST_IDS)
        elif random_sample:
            print(f"--hardest {hardest}: the filter holds {len(pool)} problems,"
                  " not more than the sample size, so hardest has nowhere to pick")
        else:
            # --hardest N on its own: the run IS those N hardest problems.
            problems = hardest_picks(pool, hardest)
            state.HARDEST_IDS = {str(p["question_id"]) for p in problems}
            state.SAMPLE_NOTE = f"{len(problems)} hardest (--hardest {hardest})"
            print(f"Hardest: {state.SAMPLE_NOTE}")
            if tier_file:
                _write_tier(tier_file, state.HARDEST_IDS)
    counts = {}
    for p in problems:
        k = str(p["difficulty"]).lower()
        counts[k] = counts.get(k, 0) + 1
    print(f"Loaded {len(problems)} problems"
          f" ({', '.join(f'{v} {d}' for d, v in sorted(counts.items()))})")
    return problems

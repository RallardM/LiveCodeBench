"""The item pools of the one-command suites. Every item is one prompt with
its own grader. Four skills x four tiers (easy, medium, hard, hardest):

  code     LiveCodeBench code generation; hardest = the newest, biggest hard ones
  math     GSM8K -> MATH-500 (L3-4) -> MATH-500 (L5) -> AIME 2026 + HMMT Feb 2026
  science  SuperGPQA easy -> middle -> hard -> hard calculation problems
  reading  code execution, K calls per answer: K=1, 2, 4, 6 (all K must be right)

Only public, ungated datasets. Each cell is shuffled once with a fixed seed, so
a run is the same fixed order of items for every model; a faster model just goes
further down the same list."""

import contextlib
import io
import json
import random
import zlib

from .prompts import format_prompt_math, format_prompt_mcq

TIERS = ("easy", "medium", "hard", "hardest")
SKILLS = ("code", "math", "science", "reading")
SEED = "lcb-suite-v1"
READING_K = {"easy": 1, "medium": 2, "hard": 4, "hardest": 6}
MAX_PER_CELL = 40          # items kept per cell (a run never gets near it)
HARDEST_CODE = 60          # size of the code hardest pool


def _shuffled(items, skill, tier):
    rng = random.Random(f"{SEED}/{skill}/{tier}")
    items = list(items)
    rng.shuffle(items)
    return items[:MAX_PER_CELL]


def _load_rows(options):
    """options: list of alternatives, each a list of (dataset_id, config, split).
    The first alternative that downloads completely wins. Returns
    (rows, label)."""
    from datasets import load_dataset

    errors = []
    for option in options:
        try:
            rows = []
            for ds_id, cfg, split in option:
                ds = (load_dataset(ds_id, cfg, split=split) if cfg
                      else load_dataset(ds_id, split=split))
                rows += [dict(r) for r in ds]
            if rows:
                return rows, "+".join(o[0] for o in option)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{option[0][0]} ({type(exc).__name__})")
    raise RuntimeError("could not load any of: " + ", ".join(errors)
                       + ". Check the internet connection (setting HF_TOKEN"
                         " lifts the download rate limit).")


def _math_item(tier, uid, problem, gold, source):
    return {"question_id": f"math-{tier}-{uid}", "skill": "math", "tier": tier,
            "difficulty": tier, "eval_kind": "math", "source": source,
            "eval_payload": json.dumps({"gold": str(gold).strip()}),
            "messages": format_prompt_math(problem)}


def _mcq_item(tier, uid, question, options, letter):
    return {"question_id": f"science-{tier}-{uid}", "skill": "science",
            "tier": tier, "difficulty": tier, "eval_kind": "mcq",
            "source": "SuperGPQA",
            "eval_payload": json.dumps({"gold": str(letter).strip().upper(),
                                        "n_options": len(options)}),
            "messages": format_prompt_mcq(question, options)}


def load_math(apex=False):
    pools = {t: [] for t in TIERS}
    notes = []
    rows, label = _load_rows([[("openai/gsm8k", "main", "test")]])
    notes.append(label)
    for i, r in enumerate(rows):
        gold = str(r["answer"]).split("####")[-1].strip().replace(",", "")
        pools["easy"].append(_math_item("easy", f"gsm8k{i}", r["question"], gold, "GSM8K"))
    rows, label = _load_rows([[("HuggingFaceH4/MATH-500", None, "test")]])
    notes.append(label)
    for i, r in enumerate(rows):
        lvl = int(r.get("level") or 0)
        tier = "medium" if lvl in (3, 4) else "hard" if lvl == 5 else None
        if tier:
            pools[tier].append(_math_item(tier, f"m500-{i}", r["problem"],
                                          r["answer"], "MATH-500"))
    if apex:
        options = [[("MathArena/apex-shortlist", None, "train"),
                    ("MathArena/apex_2025", None, "train")],
                   [("MathArena/apex-shortlist", None, "train")]]
    else:
        options = [[("MathArena/aime_2026", None, "train"),
                    ("MathArena/hmmt_feb_2026", None, "train")],
                   [("MathArena/aime_2026_I", None, "train"),
                    ("MathArena/aime_2026_II", None, "train"),
                    ("MathArena/hmmt_feb_2026", None, "train")],
                   [("MathArena/aime_2025", None, "train"),
                    ("MathArena/hmmt_feb_2025", None, "train")]]
    rows, label = _load_rows(options)
    notes.append(label)
    for i, r in enumerate(rows):
        pools["hardest"].append(_math_item("hardest", f"arena{i}", r["problem"],
                                           r["answer"], label))
    return {t: _shuffled(v, "math", t) for t, v in pools.items()}, notes


def load_science():
    rows, label = _load_rows([[("m-a-p/SuperGPQA", None, "train")]])
    pools = {t: [] for t in TIERS}
    hard = []
    for r in rows:
        opts = list(r.get("options") or [])
        letter = r.get("answer_letter")
        if len(opts) < 2 or not letter:
            continue
        diff = str(r.get("difficulty") or "").lower()
        uid = str(r.get("uuid") or len(pools["easy"]) + len(hard))
        if diff == "easy":
            pools["easy"].append(_mcq_item("easy", uid, r["question"], opts, letter))
        elif diff in ("middle", "medium"):
            pools["medium"].append(_mcq_item("medium", uid, r["question"], opts, letter))
        elif diff == "hard":
            hard.append((bool(r.get("is_calculation")),
                         _mcq_item("hard", uid, r["question"], opts, letter)))
    calc = [it for is_calc, it in hard if is_calc]
    plain = [it for is_calc, it in hard if not is_calc]
    if len(calc) < 20:  # no calculation flag in this copy: split the hard ones
        half = len(hard) // 2
        calc = [it for _, it in hard[:half]]
        plain = [it for _, it in hard[half:]]
    for it in calc:
        it["tier"] = it["difficulty"] = "hardest"
        it["question_id"] = it["question_id"].replace("science-hard-", "science-hardest-")
    pools["hard"], pools["hardest"] = plain, calc
    return {t: _shuffled(v, "science", t) for t, v in pools.items()}, [label]


def load_reading(release):
    from .loaders import load_exec_problems

    pools = {t: [] for t in TIERS}
    for gi, tier in enumerate(TIERS):
        k = READING_K[tier]
        with contextlib.redirect_stdout(io.StringIO()):
            rows = load_exec_problems(release, None, None, None, 0, bundle=k)
        for p in rows:
            base = str(p["question_id"]).split("#")[0]
            if zlib.crc32(base.encode("utf-8")) % len(TIERS) != gi:
                continue   # every tier reads its own quarter of the questions
            item = {"question_id": f"reading-{tier}-{p['question_id']}",
                    "skill": "reading", "tier": tier, "difficulty": tier,
                    "source": "execution-v2",
                    "eval_payload": p["eval_payload"], "messages": p["messages"]}
            if k > 1:
                item["eval_kind"] = "execb"
                item["inputs"] = p.get("inputs") or []
            else:
                item["eval_kind"] = "exec"
            pools[tier].append(item)
    return {t: _shuffled(v, "reading", t) for t, v in pools.items()}, ["execution-v2"]


def load_code(release, start_date):
    from .loaders import load_problems
    from .sampling import hardest_picks

    with contextlib.redirect_stdout(io.StringIO()):
        rows = load_problems(release, start_date, None, None, 0)
    top = hardest_picks(rows, HARDEST_CODE)
    top_ids = {str(p["question_id"]) for p in top}
    pools = {t: [] for t in TIERS}
    for p in rows:
        qid = str(p["question_id"])
        tier = "hardest" if qid in top_ids else str(p["difficulty"]).lower()
        if tier not in pools:
            continue
        pools[tier].append({
            "question_id": f"code-{qid}", "skill": "code", "tier": tier,
            "difficulty": tier, "eval_kind": "codegen",
            "source": "LiveCodeBench",
            "eval_payload": p["eval_payload"], "messages": p["messages"]})
    return {t: _shuffled(v, "code", t) for t, v in pools.items()}, ["LiveCodeBench"]


def build_pools(skills, release, start_date, apex=False):
    """{skill: {tier: [item, ...]}} for the skills asked for. A skill whose
    dataset cannot be downloaded is dropped with a warning (the rest still
    run); if nothing loads the run stops."""
    loaders = {"code": lambda: load_code(release, start_date),
               "math": lambda: load_math(apex),
               "science": load_science,
               "reading": lambda: load_reading(release)}
    out = {}
    for skill in skills:
        print(f"Loading {skill} items...", flush=True)
        try:
            pools, notes = loaders[skill]()
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: skill '{skill}' dropped: {exc}")
            continue
        sizes = ", ".join(f"{t} {len(pools[t])}" for t in TIERS)
        print(f"   {skill}: {sizes}   [{', '.join(notes)}]")
        out[skill] = pools
    if not out:
        raise SystemExit("No item pool could be loaded, so there is nothing to run.")
    return out


def item_sequence(pools, rounds):
    """The fixed order every run follows: round r holds item r of every cell,
    easy tier first, then medium, hard, hardest (skills in a fixed order)."""
    cells = [(s, t) for t in TIERS for s in SKILLS if s in pools]
    seq = []
    for r in range(rounds):
        for s, t in cells:
            items = pools[s][t]
            if r < len(items):
                seq.append(items[r])
    return seq

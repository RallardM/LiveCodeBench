#!/usr/bin/env python
"""
lcb_bench.py — Fast, resumable, Windows-friendly LiveCodeBench harness for local
OpenAI-compatible model servers (llama.cpp / llama-server, LM Studio, vLLM, Ollama...).

Why this exists
---------------
* lcb_llama.py generated answers but never scored them — no pass@1, no test runs.
* It used raw question text with no prompt / format instructions, which badly
  hurts scores compared to the official LiveCodeBench chat prompt used on the
  official leaderboard (the GPT/Claude-style dialog format).
* The official lcb_runner in this repo cannot talk to llama.cpp (its OpenAI
  runner hardcodes api.openai.com) AND its evaluator uses signal.SIGALRM, which
  does not exist on Windows. This harness ports the same grading logic
  (grade_stdio / grade_call_based from lcb_runner/evaluation/testing_util.py)
  to a subprocess + parent-watchdog design that works natively on Windows.

Pipeline: generate -> extract code -> run the LCB test cases in sandboxed child
processes -> pass@k per difficulty + generation speed metrics -> report.

Two things are asked out loud and never guessed:
* WHICH MODEL: the label defaults to the model id the server reports on
  --base-url, so a row in bench/report.md always says which model it is.
  Pass --name to use a shorter label you choose yourself.
* WHICH MODE: is an agent chat (a harness) sharing this model server? Answer
  when asked, or pass --harness yes|no for a detached run. The answer becomes
  part of the run folder (-harness / -noharness) so both modes are two
  comparable rows instead of overwriting each other.

Everything checkpoints under bench/<run>/ so you can Ctrl+C or crash and just
re-run the same command.

Usage examples
--------------
  # 1) Start your llama-server for the model you want to test. For real speed
  #    use slots, e.g.:
  #      llama-server -m model.gguf -c 16384 --parallel 8 --threads <cores>
  #    (with -ngl 99 / --n-gpu-layers for GPU offload) then:
  python lcb_bench.py --name "qwen3-8b-instruct-q5" --workers 8

  # 2) Fast smoke test on 10 problems (label optional):
  python lcb_bench.py --limit 10

  # 3) Thinking model, more samples:
  python lcb_bench.py --name "r1-distill-14b" --n 5 --max-tokens 32768 --workers 8 \
      --extra-body '{"chat_template_kwargs": {"enable_thinking": false}}'   # optional

  # 4) Re-run only evaluation of generations you already have:
  python lcb_bench.py --name "qwen3-8b-instruct-q5" --skip-generate

  # 5) Every run side by side. A run itself prints only its own row (or the two
  #    rows of a --both pair); this is the command that prints them all:
  python lcb_bench.py --report-only

  # 6) Sanity-check the Windows evaluator (no model server needed):
  python lcb_bench.py --self-test

  # 7) Detached / from an agent chat, where nothing can be answered:
  python lcb_bench.py --harness yes --harness-note "deepseek-harness" \
      --speed-probe --random-sample 100 --workers 1 --max-tokens 16384

  # 8) One command, both rows: pass 1 with nothing else on the model server,
  #    pass 2 while the agent chat is working. Same problems both times.
  #    --hardest keeps the run at 100 problems but spends 25 of them on the
  #    hardest the release has and spreads the rest evenly over easy/medium/hard,
  #    so one line covers every difficulty and no strong model sweeps it.
  python lcb_bench.py --both --random-sample 100 --hardest 25

  # 9) Tidy up scores: numbered list, delete one, bring it back. Same commands
  #    are available while a run is going - press a key in its window, type help.
  python lcb_bench.py --manage
  python lcb_bench.py --list-runs
  python lcb_bench.py --delete-run 2
  python lcb_bench.py --restore-run 2

Outputs per model: bench/<name>/generations.jsonl (raw outputs, append-only),
bench/<name>/generations.json (official LCB output format),
bench/<name>/results_official_format.json (custom_evaluator-compatible, with
extracted code), bench/<name>/eval_results.jsonl, bench/<name>/_summary.json.
Cross-model table: bench/report.md + bench/report.csv. Runs you delete move to
bench/_archive/<stamp>__<folder> instead of being erased, and drop out of the table.
"""

import argparse
import ast
import base64
import csv
import datetime as dt
import hashlib
import json
import multiprocessing
import os
import pickle
import re
import shutil
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import StringIO
from types import ModuleType
from unittest.mock import mock_open, patch
from urllib.parse import urlparse

import requests

try:  # some LCB test cases contain very large integers
    sys.set_int_max_str_digits(1_000_000)
except AttributeError:  # python < 3.11
    pass

DATASET_NAME = "livecodebench/code_generation_lite"
# Set by filter_sample when --hardest picks problems for the sample, so the run's
# _summary.json and its row can say what the sample really is.
SAMPLE_NOTE = ""
# ... and which question_ids the hardest tier is made of, so their pass rate can
# be reported on its own instead of blended into "hard".
HARDEST_IDS = set()
# ... and what the pool the sample came from looks like (rows, questions, labels,
# date range). That is what a 100% is measured against.
POOL_FACTS = {}
# The --mix spec as typed, so a run's note reads `mix 50/25/15/10: ...` and not a
# python dict.
MIX_SPEC = ""
# The --bundle size (fast scenarios): every scored item asks K single calls in
# one answer and is all-or-nothing. 1 = off, the classic one-call-per-row mode.
BUNDLE_K = 1
# The console answers commands typed while a run is going; see bench_command.
CONSOLE_LIVE = False

# -----------------------------------------------------------------------------
# Prompts (official LiveCodeBench OpenAIChat dialog style — verbatim, this is
# what scores on the public leaderboard are produced with)
# -----------------------------------------------------------------------------

SYSTEM_MESSAGE_GENERIC = (
    "You are an expert Python programmer. You will be given a question (problem "
    "specification) and will generate a correct Python program that matches the "
    "specification and passes all tests."
)
FORMATTING_MESSAGE_WITH_STARTER_CODE = (
    "You will use the following starter code to write the solution to the "
    "problem and enclose your code within delimiters."
)
FORMATTING_WITHOUT_STARTER_CODE = (
    "Read the inputs from stdin solve the problem and write the answer to stdout "
    "(do not directly test on the sample inputs). Enclose your code within "
    "delimiters as follows. Ensure that the python program runs, it reads the "
    "inputs, runs the algorithm and writes output to STDOUT."
)


def format_prompt(question_content: str, starter_code: str) -> list:
    prompt = f"### Question:\n{question_content}\n\n"
    if starter_code:
        prompt += f"### Format: {FORMATTING_MESSAGE_WITH_STARTER_CODE}\n"
        prompt += f"```python\n{starter_code}\n```\n\n"
    else:
        prompt += f"### Format: {FORMATTING_WITHOUT_STARTER_CODE}\n"
        prompt += "```python\n# YOUR CODE HERE\n```\n\n"
    prompt += "### Answer: (use the provided format with backticks)\n\n"
    return [
        {"role": "system", "content": SYSTEM_MESSAGE_GENERIC},
        {"role": "user", "content": prompt},
    ]


# -----------------------------------------------------------------------------
# Code extraction
# -----------------------------------------------------------------------------


def extract_code_official(model_output: str) -> str:
    """Exactly lcb_runner.utils.extraction_utils.extract_code for chat models."""
    outputlines = model_output.split("\n")
    indexlines = [i for i, line in enumerate(outputlines) if "```" in line]
    if len(indexlines) < 2:
        return ""
    return "\n".join(outputlines[indexlines[-2] + 1 : indexlines[-1]])


def extract_code_auto(model_output: str) -> str:
    """Official last-fence rule, with fallbacks for truncated / fence-less output."""
    if not model_output:
        return ""
    outputlines = model_output.split("\n")
    indexlines = [i for i, line in enumerate(outputlines) if "```" in line]
    if len(indexlines) >= 2:
        return "\n".join(outputlines[indexlines[-2] + 1 : indexlines[-1]])
    if len(indexlines) == 1:
        i = indexlines[0]
        before = "\n".join(outputlines[:i]).strip()
        after = "\n".join(outputlines[i + 1 :]).strip()
        head = outputlines[i].strip().lower()
        if head.startswith("```") and after:  # opening fence, closing truncated
            return after
        return before if before else after
    return model_output.strip()


def extract_code(model_output: str, extractor: str) -> str:
    if extractor == "official":
        return extract_code_official(model_output)
    return extract_code_auto(model_output)


# -----------------------------------------------------------------------------
# Dataset
# -----------------------------------------------------------------------------


def load_problems(release, start_date, end_date, difficulty, limit,
                  random_sample=0, sample_ids_file=None, hardest=0, mix=None):
    # Imported lazily so spawned eval children don't pay this cost.
    from datasets import load_dataset

    try:
        ds = load_dataset(
            DATASET_NAME, split="test", version_tag=release, trust_remote_code=True
        )
    except Exception:
        ds = load_dataset(DATASET_NAME, split="test", trust_remote_code=True)

    problems = []
    for row in ds:
        meta = json.loads(row["metadata"])
        pub = json.loads(row["public_test_cases"])
        try:
            priv = json.loads(row["private_test_cases"])
        except Exception:
            priv = json.loads(
                pickle.loads(
                    zlib.decompress(
                        base64.b64decode(row["private_test_cases"].encode("utf-8"))
                    )
                )
            )
        in_out = json.dumps(
            {
                "inputs": [t["input"] for t in pub + priv],
                "outputs": [t["output"] for t in pub + priv],
                "fn_name": meta.get("func_name", None),
            }
        )
        contest_date = row["contest_date"]
        if isinstance(contest_date, str):
            contest_date = dt.datetime.fromisoformat(contest_date)
        problems.append(
            {
                "question_id": row["question_id"],
                "question_content": row["question_content"],
                "starter_code": row["starter_code"],
                "difficulty": row["difficulty"],
                "contest_date": contest_date,
                "in_out": in_out,
                "eval_payload": in_out,
                "kind": "codegen",
                "messages": format_prompt(row["question_content"], row["starter_code"]),
            }
        )
    return filter_sample(
        problems, start_date, end_date, difficulty, limit,
        random_sample, sample_ids_file, hardest=hardest, mix=mix,
    )


def problem_weight(problem):
    """Rough hardness signal inside a difficulty band. For code generation it is
    how many test cases one answer has to satisfy at once. The fast scenarios
    ship one call per row, so their loaders store their own `weight` (how much
    code / problem text the single call carries) - see load_exec_problems and
    load_top_problems. Unknown: 0, the other sort keys decide there."""
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
    biggest problem. Age goes first because our own runs say it matters most:
    one model passed 50% of the hard problems published in 2023 and 12% of the
    hard ones from 2025. A problem the model has already read is not a hard
    problem, it is a recall test."""
    return (-p["contest_date"].timestamp(), -problem_weight(p),
            str(p["question_id"]))


def _hard_key(p):
    """Full hardness key of one problem: dataset label band first, then (inside a
    band) newest contest and biggest problem."""
    return (HARDNESS_RANK.get(str(p["difficulty"]).lower(), 1),) + _big_new_first(p)


def hardest_picks(pool, count, exclude=()):
    """The `count` hardest problems of `pool` that are not already `exclude`d:
    hard before medium before easy, and inside a band the newest (least
    memorised) and biggest problems first."""
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
    then biggest); the other tiers are the dataset's own labels, newest first
    inside each. The tiers are filled in the order of how little they have to
    give per share asked - a scarce tier must not starve because a bigger tier
    took its problems first - and `hardest` comes last, since it can take
    anything. A tier the pool cannot fill is said out loud and its empty slots
    go to the best problems still on the table, never quietly to a nicer
    difficulty. Returns (picked, ids of the hardest tier, note)."""
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
            # this bench's own ranking of the whole pool, not a dataset label
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
    own labels, the date range and the weight range. This is the tool's answer
    to "why did that scenario come out 100%": a pool of 479 tiny 2023 snippets
    has no hard to give."""
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
    present gets the same share instead (25 easy / 25 medium / 25 hard, ...).
    Fixed seed: the same pool always gives the same picks."""
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
        # a tier ran out (a release can hold very few hard problems) - top up
        # from what is left so the sample still comes out `count` wide.
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


def show_pool_info(args, scen):
    """--pool-info: what a scenario can actually be sampled from, with no model
    involved. This is the answer to "why did that come out 100%": a pool that
    holds no hard problems cannot hand out a hard sample."""
    label = {"codegen": DATASET_NAME, "exec": EXEC_DATASET_NAME,
             "top": TOP_DATASET_NAME}[scen]
    print(f"Pool for scenario {args.scenario} ({label}), release {args.release}:")
    if scen == "codegen":
        problems = load_problems(args.release, args.start_date, args.end_date,
                                 args.difficulty, 0)
    elif scen == "exec":
        problems = load_exec_problems(args.release, args.start_date, args.end_date,
                                      args.difficulty, 0, cot=args.exec_cot,
                                      bundle=args.bundle)
    else:
        problems = load_top_problems(args.release, args.start_date, args.end_date,
                                     args.difficulty, 0, bundle=args.bundle)
    facts = POOL_FACTS
    if not facts or not facts.get("n"):
        print("   nothing is left after the filters - no sample can be drawn at all")
        return
    labels = facts["labels"]
    print(f"   {facts['n']} problems from {facts['questions']} distinct questions"
          + ("" if facts["questions"] == facts["n"] else
             f"  ({facts['n'] / facts['questions']:.1f} rows per question:"
             " every row is ONE call, not the whole problem)"))
    print("   labels: " + ", ".join(f"{v} {k}" for k, v in sorted(labels.items())))
    hard_n = labels.get("hard", 0)
    print(f"   dates: {facts['date_min']} .. {facts['date_max']}"
          f"  -> {hard_n / facts['n'] * 100:.1f}% of the pool is labelled hard")
    print(f"   weight: median {facts['weight_median']}, biggest {facts['weight_max']}"
          f" (id {facts['biggest']}) - weight = test cases for code generation,"
          " characters of code / problem text for the fast scenarios")
    print("   what --mix can fill: hardest takes from all"
          f" {facts['n']}, then {hard_n} hard,"
          f" {labels.get('medium', 0)} medium, {labels.get('easy', 0)} easy - a"
          " share bigger than that gets refilled from the other labels")
    if args.random_sample:
        spec_text = args.mix or "50/25/15/10"
        spec = parse_mix(spec_text)
        got, _, note = mix_pick(problems, args.random_sample, spec, spec_text)
        print(f"   --random-sample {args.random_sample} --mix {spec_text} here:"
              f" {note}")
    if scen != "codegen":
        caps = facts.get("bundle_cap") or {}
        print("   what --bundle can do (K calls answered in ONE answer, all K"
              " right or the item fails - the only lever that bites on a pool"
              " this flat):")
        for k in sorted(caps, key=int):
            if caps[k] >= 10:
                print(f"      K={k}: {caps[k]} items  (up to {int(k) * caps[k]}"
                      " single calls in play)")
        if not any(v >= 10 for v in caps.values()):
            print("      no K holds even 10 items - this pool cannot bundle")
        bk = facts.get("bundle_k")
        if bk:
            print(f"   (running now: --bundle {bk}, every item = {bk} calls,"
                  " all-or-nothing)")
        print("   verdict: every problem in this scenario is one call to one small"
              " function with a short answer. It is a ceiling check - can the model"
              " run code in its head and answer in the asked format - not a"
              " discrimination test. --bundle K restores the slope by making the"
              " ANSWER unit heavier (a K-item passes at per-call-rate**K), but the"
              " ranking stays on code_generation: pin the difficulty there with"
              " --hardest / --mix plus --start-date (the newest contests are the"
              " least memorised ones).")


def _harden_ladder(scen, per_call, target, caps, cur_k=1, n_rows=0):
    """The harden ratchet: given the per-call pass rate just measured, which
    --bundle size would push the item score under `target` (p**k <= target), and
    does the pool actually hold that many items? Returns (suggestion_text_lines,
    None) or (None, why_no_more_text) - the tool never lets a saturated fast
    run end without printing the next command that has a chance at being hard."""
    if not caps:
        return None, ("this pool cannot bundle at all (nothing groups into"
                      " repeated calls) - rank on code_generation instead")
    scen_flag = "code_execution" if scen == "exec" else "test_output_prediction"
    lines = []
    k_order = sorted(int(k) for k, v in caps.items() if v >= 10)
    if cur_k:
        k_order = [k for k in k_order if k > cur_k]
    if per_call >= 0.99999:
        top_cov = max(k_order, key=lambda k: caps[str(k)] * k) if k_order else None
        if top_cov is None:
            return None, ("no bundle size of this pool holds >= 10 items"
                          " - rank on code_generation instead")
        cov = caps[str(top_cov)] * top_cov
        lines.append(
            f"every single call was right ({int(per_call * 100)}%): no"
            " difficulty mix can lower a number that has no misses yet - the"
            " answer unit has to carry more calls so any hidden miss fails a"
            f" whole item. The widest net this pool holds: --bundle {top_cov}"
            f" --random-sample {min(100, caps[str(top_cov)])} covers"
            f" {cov}/{n_rows} of every call in the release.")
        lines.append(
            f"  python lcb_bench.py --scenario {scen_flag} --bundle {top_cov}"
            f" --random-sample {min(100, caps[str(top_cov)])} --workers 1"
            " --max-tokens 16384 --eval-workers 8")
        return lines, None
    for k in k_order:
        if per_call ** k <= target:
            n_samp = min(100, caps[str(k)])
            lines.append(
                f"per-call rate {per_call * 100:.1f}% -> a K-call item passes at"
                f" about {per_call * 100:.1f}%**K, so K={k} lands near"
                f" {per_call ** k * 100:.0f}% (target {target * 100:.0f}%):"
                f" --bundle {k}, and the pool holds {caps[str(k)]} items of"
                f" {k} calls.")
            lines.append(
                f"  python lcb_bench.py --scenario {scen_flag} --bundle {k}"
                f" --random-sample {n_samp} --workers 1 --max-tokens 16384"
                " --eval-workers 8")
            return lines, None
    kmax = max(k_order) if k_order else None
    if kmax:
        lines.append(
            f"even K={kmax} items are expected at {per_call ** kmax * 100:.0f}% >"
            f" {target * 100:.0f}%: this pool is saturated for this model down"
            " to its last bundle - the number that ranks it is"
            " code_generation with --mix plus --start-date.")
        return lines, None
    return None, ("no bundle size of this pool holds >= 10 items"
                  " - rank on code_generation instead")


def saturation_note(scen, score, n_problems, per_diff, hardest_m,
                    bundle_m=None, harden_target=None):
    """Printed under a score of 99% or more (90% on the fast scenarios, which
    saturate that low): what that score was measured against, the exact next
    command that has a chance of being harder for THIS model (the harden
    ratchet), and the pool facts behind both. The fast scenarios get the
    uncomfortable version - their whole release is old one-call snippets, so
    no sample drawn from it can be hard; only a bigger answer unit can."""
    facts = POOL_FACTS or {}
    labels = facts.get("labels") or {}
    target = ((harden_target if harden_target is not None
               else (90.0 if scen != "codegen" else 99.0)) / 100.0)
    pool_line = None
    if facts.get("n"):
        if facts.get("bundle_k"):
            lead = (f"{facts.get('raw_rows', '?')} rows from"
                    f" {facts.get('raw_questions', '?')} distinct questions,"
                    f" bundled {facts['bundle_k']} calls to an item ->"
                    f" {facts['n']} items")
        else:
            lead = (f"{facts['n']} rows from {facts['questions']}"
                    " distinct questions")
        pool_line = (f"      the pool it came from: {lead},"
                     f" {facts['date_min']} to {facts['date_max']},"
                     " labelled " + ", ".join(f"{v} {k}"
                                              for k, v in sorted(labels.items()))
                     + f"; biggest single row {facts['weight_max']}"
                     f" (median {facts['weight_median']})")
    hard_n = len(per_diff.get("hard", []))
    lines = []
    if scen == "codegen":
        if hard_n == n_problems:
            lines.append(f"      {score:g}% with all {n_problems} problems flagged hard:"
                         " memorised problems, not easy ones. Take the newest"
                         " contests: --start-date 2025-01-01 --hardest N")
        else:
            lines.append(f"      {score:g}% on {n_problems} problems where only {hard_n}"
                         " were hard: the sample cannot tell models apart. Pin the"
                         " difficulty: --random-sample 100 --hardest 25, or --mix"
                         " 50/25/15/10, plus --start-date 2025-01-01")
        if pool_line:
            lines.append(pool_line)
    else:
        unit = (f" rows of {bundle_m['k']} calls" if bundle_m else " rows")
        lines.append(f"      {score:g}% on {n_problems}{unit} of the {scen} scenario:"
                     " read this as a ceiling check, not as a score. Every row here is"
                     " one or a few calls to small functions with short answers"
                     " printed back, and the whole release is old, so resampling it"
                     " cannot make it harder - we checked.")
        if pool_line:
            lines.append(pool_line)
        pc = (bundle_m["per_call_pass"] if bundle_m else score / 100.0)
        ladder, dead_end = _harden_ladder(
            scen, pc, target, facts.get("bundle_cap") or {},
            cur_k=(bundle_m["k"] if bundle_m else 1),
            n_rows=facts.get("raw_rows") or facts.get("n", 0))
        if ladder:
            lines.append("      harden it further (same rows, same grader - only the"
                         " answer unit gets heavier):")
            lines += [f"      {ln}" if ln.startswith("python") else f"      {ln}"
                      for ln in ladder]
        elif dead_end:
            lines.append(f"      {dead_end}")
        lines.append("      to rank models run code_generation instead:"
                     " --scenario code_generation --random-sample 100 --mix"
                     " 50/25/15/10 --start-date 2025-01-01 (or --hardest 50)."
                     " --pool-info prints the same facts about any pool with no run")
    if hardest_m.get("n") and hardest_m.get("pass@1") is not None \
            and hardest_m["pass@1"] >= 0.9:
        lines.append(f"      even the hardest tier ({hardest_m['n']} problems) came"
                     f" out at {hardest_m['pass@1'] * 100:.0f}% - the pool had no"
                     " headroom left for this model within these filters")
    return "\n".join(lines)


def filter_sample(problems, start_date, end_date, difficulty, limit,
                  random_sample=0, sample_ids_file=None, hardest=0, mix=None):
    global SAMPLE_NOTE, HARDEST_IDS, POOL_FACTS
    SAMPLE_NOTE = ""
    HARDEST_IDS = set()
    POOL_FACTS = {}
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
    POOL_FACTS = pool_facts(pool)
    # how many K-call bundle items this pool could supply (0 for pools where
    # nothing groups, e.g. code_generation): read by --pool-info and the ratchet
    POOL_FACTS["bundle_cap"] = bundle_capacity(pool)
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
                HARDEST_IDS = {str(x) for x in json.load(f)}
            print(f"--hardest/--mix: this run already has its saved sample list,"
                  f" so it is reused exactly as it was picked"
                  f" ({len(HARDEST_IDS)} of them are the hardest tier)")
        else:
            print(f"--hardest/--mix: this run already has its saved sample list"
                  " (the hardest problems are inside it), so it is reused exactly as"
                  " it was picked")
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
                                              str(MIX_SPEC or mix))
            problems = sorted(chosen, key=lambda p: str(p["question_id"]))
            HARDEST_IDS = tier_ids
            SAMPLE_NOTE = note
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
            # One command, one sample of exactly random_sample problems: the
            # hardest N slots go to the hardest problems the release has, the
            # rest are spread evenly over easy / medium / hard. --random-sample
            # 100 --hardest 25 is 25 hardest + 25 easy + 25 medium + 25 hard:
            # every difficulty in one total, no 100% surprise.
            chosen = hardest_picks(pool, min(hardest, random_sample))
            ids = {str(p["question_id"]) for p in chosen}
            room = random_sample - len(chosen)
            rest_pool = [p for p in pool if str(p["question_id"]) not in ids]
            rest = (rest_pool if room >= len(rest_pool)
                    else stratified_pick(rest_pool, room, equal_tiers=True))
            problems = sorted(chosen + rest, key=lambda p: str(p["question_id"]))
            HARDEST_IDS = set(ids)
            bd = {}
            for p in chosen:
                k = str(p["difficulty"]).lower()
                bd[k] = bd.get(k, 0) + 1
            SAMPLE_NOTE = (f"{len(chosen)} hardest ("
                           + ", ".join(f"{v} {d}" for d, v in sorted(bd.items()))
                           + f") + {len(rest)} spread (--hardest {hardest})")
            print(f"Hardest: {SAMPLE_NOTE}")
            if sample_ids_file:
                _write_sample_ids(sample_ids_file, problems)
                _write_tier(tier_file, HARDEST_IDS)
        elif random_sample:
            print(f"--hardest {hardest}: the filter holds {len(pool)} problems,"
                  " not more than the sample size, so hardest has nowhere to pick")
        else:
            # --hardest N on its own: the run IS those N hardest problems.
            problems = hardest_picks(pool, hardest)
            HARDEST_IDS = {str(p["question_id"]) for p in problems}
            SAMPLE_NOTE = f"{len(problems)} hardest (--hardest {hardest})"
            print(f"Hardest: {SAMPLE_NOTE}")
            if tier_file:
                _write_tier(tier_file, HARDEST_IDS)
    counts = {}
    for p in problems:
        counts[str(p["difficulty"]).lower()] = counts.get(str(p["difficulty"]).lower(), 0) + 1
    print(f"Loaded {len(problems)} problems ({', '.join(f'{v} {d}' for d, v in sorted(counts.items()))})")
    return problems


# -----------------------------------------------------------------------------
# Extra scenarios (official LiveCodeBench "code_execution" and
# "test_output_prediction"). Prompts/scoring ported verbatim from
# lcb_runner/prompts/*.py and evaluation/compute_*.py.
# -----------------------------------------------------------------------------

EXEC_DATASET_NAME = "livecodebench/execution-v2"
TOP_DATASET_NAME = "livecodebench/test_generation"

EXEC_SYSTEM_MESSAGE = (
    "You are an expert at Python programming, code execution, test case "
    "generation, and fuzzing."
)
TOP_SYSTEM_MESSAGE = (
    "You are a helpful programming assistant and an expert Python programmer."
    " You are helping a user to write a test case to help to check the"
    " correctness of the function."
    " The user has written a input for the testcase."
    " You will calculate the output of the testcase and"
    " write the whole assertion statement in the markdown code block with the"
    " correct output."
)

EXEC_COT_PROMPT = """You are given a Python function and an assertion containing an input to the function. Complete the assertion with a literal (no unsimplified expressions, no function calls) containing the output when executing the provided code on the given input, even if the function is incorrect or incomplete. Do NOT output any extra information. Execute the program step by step before arriving at an answer, and provide the full assertion with the correct output in [ANSWER] and [/ANSWER] tags, following the examples.

[PYTHON]
def performOperation(s):
    s = s + s
    return "b" + s + "a"
assert performOperation(s = "hi") == ??
[/PYTHON]
[THOUGHT]
Let's execute the code step by step:

1. The function performOperation is defined, which takes a single argument s.
2. The function is called with the argument "hi", so within the function, s is initially "hi".
3. Inside the function, s is concatenated with itself, so s becomes "hihi".
4. The function then returns a new string that starts with "b", followed by the value of s (which is now "hihi"), and ends with "a".
5. The return value of the function is therefore "bhihia".
[/THOUGHT]
[ANSWER]
assert performOperation(s = "hi") == "bhihia"
[/ANSWER]

[PYTHON]
{code}
assert {inp} == ??
[/PYTHON]
[THOUGHT]
"""

EXEC_DIRECT_PROMPT = """You are given a Python function and an assertion containing an input to the function. Complete the assertion with a literal (no unsimplified expressions, no function calls) containing the output when executing the provided code on the given input, even if the function is incorrect or incomplete. Do NOT output any extra information. Provide the full assertion with the correct output in [ANSWER] and [/ANSWER] tags, following the examples.

[PYTHON]
def repeatNumber(number : int) -> int:
    return number
assert repeatNumber(number = 17) == ??
[/PYTHON]
[ANSWER]
assert repeatNumber(number = 17) == 17
[/ANSWER]

[PYTHON]
def addCharacterA(string : str) -> str:
    return string + "a"
assert addCharacterA(string = "x9j") == ??
[/PYTHON]
[ANSWER]
assert addCharacterA(string = "x9j") == "x9ja"
[/ANSWER]

[PYTHON]
{code}
assert {inp} == ??
[/PYTHON]
[ANSWER]
"""


def format_prompt_exec(code, inp, cot=False):
    tmpl = EXEC_COT_PROMPT if cot else EXEC_DIRECT_PROMPT
    return [{"role": "system", "content": EXEC_SYSTEM_MESSAGE},
            {"role": "user", "content": tmpl.format(code=code, inp=inp)}]


def parse_function_name_from_starter_code(starter_code):
    try:
        tree = ast.parse(starter_code)
    except SyntaxError:
        return None
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if fn is not None:
                return None
            fn = node.name
    return fn


def format_testcase_func_name_input(function_name, testcase):
    input_str = ", ".join(testcase.split("\n"))
    return f"assert {function_name}({input_str}) == # TODO"


def format_prompt_top(question_content, starter_code, function_name, testcase_input):
    func_name = parse_function_name_from_starter_code(starter_code) or function_name
    prompt = f"Problem:\n{question_content}"
    prompt += f"Function:\n```\n{starter_code}\n```\n"
    prompt += "Please complete the following test case:\n\n"
    prompt += f"```\n{format_testcase_func_name_input(func_name, testcase_input)}\n```\n"
    return [{"role": "system", "content": TOP_SYSTEM_MESSAGE},
            {"role": "user", "content": prompt}]


# ---- --bundle K: the fast scenarios answered K calls per prompt, all or
# nothing. One call to one small function is a ceiling check for a strong model
# (the whole exec release came out 100% - see EXTRA_MINIMAL_BENCHMARK.md); K
# calls answered in ONE go, every one right or the item fails, turn that ceiling
# into a slope again: an item passes at roughly p**K when a single call passes
# at p. Same rows, same grader, same prompts - only the answer unit changes.

EXEC_BUNDLE_HEAD = (
    "You are given several short Python programs and, under each one, an "
    "assertion containing an input to that program. Complete EVERY assertion "
    "with a literal (no unsimplified expressions, no function calls) "
    "containing the output when executing that program on that input, even if "
    "a program is incorrect or incomplete. Do NOT output any extra "
    "information. Give one completed assertion per program, in the same order "
    "the programs appear, one per line, inside one pair of [ANSWER] and "
    "[/ANSWER] tags, following the examples."
)

EXEC_BUNDLE_HEAD_COT = (
    "You are given several short Python programs and, under each one, an "
    "assertion containing an input to that program. Complete EVERY assertion "
    "with a literal (no unsimplified expressions, no function calls) "
    "containing the output when executing that program on that input, even if "
    "a program is incorrect or incomplete. Do NOT output any extra "
    "information. Execute each program step by step before arriving at its "
    "answer, and give one completed assertion per program, in the same order "
    "the programs appear, one per line, inside one pair of [ANSWER] and "
    "[/ANSWER] tags, following the examples."
)

EXEC_BUNDLE_EXAMPLES = """[PYTHON]
def repeatNumber(number : int) -> int:
    return number
assert repeatNumber(number = 17) == ??
[/PYTHON]
[PYTHON]
def addCharacterA(string : str) -> str:
    return string + "a"
assert addCharacterA(string = "x9j") == ??
[/PYTHON]
[ANSWER]
assert repeatNumber(number = 17) == 17
assert addCharacterA(string = "x9j") == "x9ja"
[/ANSWER]"""


def format_prompt_exec_bundle(items, cot=False):
    """items: list of {"code", "input"} dicts, same order as the answers."""
    blocks = "".join(
        f"[PYTHON]\n{it['code']}\nassert {it['input']} == ??\n[/PYTHON]\n"
        for it in items)
    tmpl = (f"{EXEC_BUNDLE_HEAD_COT if cot else EXEC_BUNDLE_HEAD}\n\n"
            + EXEC_BUNDLE_EXAMPLES + "\n\n" + blocks
            + ("[THOUGHT]\n" if cot else "[ANSWER]\n"))
    return [{"role": "system", "content": EXEC_SYSTEM_MESSAGE},
            {"role": "user", "content": tmpl}]


def format_prompt_top_bundle(question_content, starter_code, function_name,
                             testcase_inputs):
    func_name = parse_function_name_from_starter_code(starter_code) or function_name
    prompt = f"Problem:\n{question_content}"
    prompt += f"Function:\n```\n{starter_code}\n```\n"
    prompt += ("Please complete ALL of the following test cases: one finished"
               " assert statement per line, in the order given, inside one"
               " python code block.\n\n```\n")
    for t in testcase_inputs:
        prompt += format_testcase_func_name_input(func_name, t) + "\n"
    prompt += "```\n"
    return [{"role": "system", "content": TOP_SYSTEM_MESSAGE},
            {"role": "user", "content": prompt}]


def _norm_nospace(s):
    return "".join(str(s).split())


def _exec_rhs(line):
    """Right-hand side of one answer assertion line, like extract_exec_answer
    does for a single-call answer."""
    if "==" not in line:
        return ""
    return extract_exec_answer(line, cot=False)


def extract_exec_bundle_answers(model_output, inputs, cot=False):
    """One answer string -> K per-call answers (missing ones as "").

    Matching is by the call text first (the line holding `assert f(5)` is the
    answer for the `f(5)` input), then by position among the leftovers, so a
    model that answers in order but without repeating the call text still
    gets counted. The grader stays strict: an answer that is not exactly the
    evaluated output fails."""
    text = model_output or ""
    block = ""
    if "[ANSWER]" in text:
        block = text.rsplit("[ANSWER]", 1)[-1]
        if "[/ANSWER]" in block:
            block = block.split("[/ANSWER]")[0]
    lines_src = block if any("==" in ln for ln in block.splitlines()) else text
    lines = [ln.strip() for ln in lines_src.splitlines() if ln.strip()]
    if not any("==" in ln for ln in lines):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    def unanswered(ln):
        rhs = ln.split("==")[-1].strip()
        return (not rhs or not rhs.rstrip(".?") or
                (rhs.startswith("?") and set(rhs) <= set("?. ")))

    used, out = set(), []
    for inp in inputs:
        key = _norm_nospace(inp)
        pick = None
        if key:
            for j, ln in enumerate(lines):
                if j in used or "==" not in ln or unanswered(ln):
                    continue
                if key in _norm_nospace(ln):
                    pick = j
                    break
        if pick is None:
            for j, ln in enumerate(lines):
                if j in used or "==" not in ln or unanswered(ln):
                    continue
                pick = j
                break
        if pick is not None:
            used.add(pick)
        out.append(_exec_rhs(lines[pick]) if pick is not None else "")
    return out


def extract_top_bundle_answers(model_output, call_strs):
    """One answer string -> K per-test assert lines (missing ones as "").
    Line i answers call i: matched by the call text when present, else taken
    in order among the leftover assert lines."""
    text = model_output or ""
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip().startswith("assert")]
    if len(lines) < len(call_strs):
        fenced = extract_top_answer(text)
        if fenced:
            more = [ln.strip() for ln in fenced.splitlines() if ln.strip()]
            if len(more) > len(lines):
                lines = more
    used, out = set(), []
    for cs in call_strs:
        key = _norm_nospace(cs)
        pick = None
        if key:
            for j, ln in enumerate(lines):
                if j in used:
                    continue
                if key in _norm_nospace(ln):
                    pick = j
                    break
        if pick is None:
            for j, ln in enumerate(lines):
                if j not in used:
                    pick = j
                    break
        if pick is not None:
            used.add(pick)
            out.append(lines[pick])
        else:
            out.append("")
    return out


def _bundle_chunks(rows, k):
    """Chunk rows (already sorted, grouped by base question id) into consecutive
    full groups of k; a leftover of 1..k-1 rows at the end of a question is
    dropped, so every item is exactly k calls wide. Deterministic."""
    from itertools import groupby as _groupby

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
    """How many K-call bundle items this pool could supply, per K. This is what
    --pool-info prints and what the harden ratchet reads: --bundle K on a pool
    that holds no K groups cannot be run, and saying so before the GPU warms up
    beats an empty sample."""
    from collections import Counter

    caps = {}
    for k in range(2, 7):
        by_base = {}
        for p in problems:
            b = str(p["question_id"]).split("#")[0]
            by_base[b] = by_base.get(b, 0) + 1
        caps[str(k)] = sum(n // k for n in by_base.values())
    return caps


def bundle_exec_problems(rows, k):
    """K exec rows -> one all-or-nothing item. execution-v2 gives every row its
    own little program, so a bundle is simply K of those programs answered in
    one go (the same contest problem's rows stay together; their calls differ).
    The item passes only if all K answers are right."""
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
    """Rebuild the bundle prompts for --exec-cot (the loader built direct
    prompts; the cot text only differs in the instruction template)."""
    for p in problems_bundle:
        items = json.loads(p["eval_payload"])["items"]
        p["messages"] = format_prompt_exec_bundle(items, cot=True)
    return problems_bundle


def bundle_top_problems(rows, k):
    """K tests of the SAME problem -> one all-or-nothing item: the model reads
    the problem once and must predict the output of K different inputs, every
    one right or the item fails."""
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


def load_exec_problems(release, start_date, end_date, difficulty, limit,
                       random_sample=0, sample_ids_file=None, cot=False,
                       hardest=0, mix=None, bundle=1):
    from datasets import load_dataset

    ds = load_dataset(EXEC_DATASET_NAME, split="test", trust_remote_code=True)
    problems = []
    seen = {}
    for row in ds:
        # execution-v2 has several rows (input/output pairs) sharing one
        # question_id; make every row unique so checkpoints + sampling are stable
        base = str(row["question_id"])
        idx = seen.get(base, 0)
        seen[base] = idx + 1
        contest_date = row["contest_date"]
        if isinstance(contest_date, str):
            contest_date = dt.datetime.fromisoformat(contest_date)
        problems.append(
            {
                "question_id": f"{base}#{idx}",
                "difficulty": row["difficulty"],
                "contest_date": contest_date,
                "kind": "exec",
                "eval_payload": json.dumps(
                    {"code": row["code"], "input": row["input"],
                     "output": row["output"]},
                    ensure_ascii=False,
                ),
                # weight: how much code one call carries. exec rows are all
                # single calls, so this is the only size signal the row has.
                "weight": (len(row["code"] or "")
                           + len(str(row["output"] or ""))),
                "messages": format_prompt_exec(row["code"], row["input"], cot),
            }
        )
    if bundle and bundle > 1:
        caps = bundle_capacity(problems)
        raw_n = len(problems)
        raw_q = len({str(p["question_id"]).split("#")[0] for p in problems})
        if cot:
            packed = bundle_exec_problems(problems, bundle)
            problems = bundle_exec_cot(problems, bundle, packed)
        else:
            problems = bundle_exec_problems(problems, bundle)
        problems.sort(key=lambda p: str(p["question_id"]))
        out = filter_sample(
            problems, start_date, end_date, difficulty, limit,
            random_sample, sample_ids_file, hardest=hardest, mix=mix,
        )
        POOL_FACTS["bundle_cap"] = caps
        POOL_FACTS["bundle_k"] = bundle
        POOL_FACTS["raw_rows"] = raw_n
        POOL_FACTS["raw_questions"] = raw_q
        return out
    return filter_sample(
        problems, start_date, end_date, difficulty, limit,
        random_sample, sample_ids_file, hardest=hardest, mix=mix,
    )


def load_top_problems(release, start_date, end_date, difficulty, limit,
                      random_sample=0, sample_ids_file=None, hardest=0, mix=None,
                      bundle=1):
    from datasets import load_dataset

    ds = load_dataset(TOP_DATASET_NAME, split="test", trust_remote_code=True)
    problems = []
    for row in ds:
        tests = json.loads(row["test"])
        test = tests[0]
        contest_date = row["contest_date"]
        if isinstance(contest_date, str):
            contest_date = dt.datetime.fromisoformat(contest_date)
        problems.append(
            {
                "question_id": f"{row['question_id']}#{row['test_id']}",
                "difficulty": row["difficulty"],
                "contest_date": contest_date,
                "kind": "top",
                "eval_payload": json.dumps({"expected": test["output"]}),
                # weight: how much problem text the one predicted output has to
                # come out of.
                "weight": (len(row["question_content"] or "")
                           + len(row["starter_code"] or "")
                           + len(str(test["output"] or ""))),
                "messages": format_prompt_top(
                    row["question_content"], row["starter_code"],
                    row["function_name"], test["input"]
                ),
                # kept for --bundle: rebuild a multi-test prompt of the same
                # problem from these
                "q_content": row["question_content"],
                "starter": row["starter_code"],
                "fname": row["function_name"],
                "test_input": test["input"],
            }
        )
    if bundle and bundle > 1:
        caps = bundle_capacity(problems)
        raw_n = len(problems)
        raw_q = len({str(p["question_id"]).split("#")[0] for p in problems})
        problems = bundle_top_problems(problems, bundle)
        problems.sort(key=lambda p: str(p["question_id"]))
        out = filter_sample(
            problems, start_date, end_date, difficulty, limit,
            random_sample, sample_ids_file, hardest=hardest, mix=mix,
        )
        POOL_FACTS["bundle_cap"] = caps
        POOL_FACTS["bundle_k"] = bundle
        POOL_FACTS["raw_rows"] = raw_n
        POOL_FACTS["raw_questions"] = raw_q
        return out
    return filter_sample(
        problems, start_date, end_date, difficulty, limit,
        random_sample, sample_ids_file, hardest=hardest, mix=mix,
    )


def extract_exec_answer(model_output, cot=False):
    # verbatim port of lcb_runner extract_execution_code
    if cot and "[ANSWER]" in model_output:
        model_output = model_output.split("[ANSWER]")[1].strip()
    if "==" in model_output:
        model_output = model_output.split("==")[1].strip()
    if "[/ANSWER]" in model_output:
        model_output = model_output.split("[/ANSWER]")[0].strip()
    else:
        model_output = model_output.split("\n")[0].strip()
    return model_output.strip()


def extract_top_answer(model_output):
    # verbatim port of lcb_runner extract_test_output_code (OpenAI style)
    outputlines = model_output.split("\n")
    indexlines = [i for i, line in enumerate(outputlines) if line.startswith("assert")]
    if indexlines:
        return outputlines[indexlines[-1]]
    indexlines = [
        i for i, line in enumerate(outputlines)
        if "```python" in line or "```Python" in line
    ]
    if indexlines:
        start_index = indexlines[0]
    else:
        start_index = None
    indexlines = [i for i, line in enumerate(outputlines) if "```" in line]
    if start_index is not None:
        indexlines = [i for i in indexlines if i > start_index]
        indexlines = [start_index] + indexlines
    if len(indexlines) < 2:
        return ""
    return "\n".join(outputlines[indexlines[0] + 1: indexlines[1]])


# -----------------------------------------------------------------------------
# Generation phase
# -----------------------------------------------------------------------------

_tls = None
_API_KEY = None


def _auth_headers():
    """Bearer header for servers that want one (--api-key); empty otherwise."""
    return {"Authorization": f"Bearer {_API_KEY}"} if _API_KEY else {}


def _session():
    global _tls
    import threading

    if _tls is None:
        _tls = threading.local()
    if not hasattr(_tls, "session"):
        s = requests.Session()
        s.headers.update(_auth_headers())
        _tls.session = s
    return _tls.session


def generate_one(task, base_url, model_id, payload_extras, max_retries, read_timeout):
    qid, si, messages = task
    body = dict(payload_extras)
    body.update({"model": model_id, "messages": messages, "stream": False})
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            t0 = time.time()
            r = _session().post(
                f"{base_url}/chat/completions", json=body, timeout=(30, read_timeout)
            )
            r.raise_for_status()
            data = r.json()
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            output = msg.get("content") or ""
            # llama.cpp >= b4599 returns thinking separately; if content empty use it
            if not output and msg.get("reasoning_content"):
                output = msg["reasoning_content"]
            usage = data.get("usage") or {}
            timings = data.get("timings") or {}  # llama.cpp server-side, queue-free
            return {
                "qid": qid,
                "si": si,
                "output": output,
                "seconds": round(time.time() - t0, 2),
                "completion_tokens": usage.get("completion_tokens"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "max_tokens": payload_extras.get("max_tokens"),
                "timings": {
                    "prompt_n": timings.get("prompt_n"),
                    "prompt_ms": timings.get("prompt_ms"),
                    "predicted_n": timings.get("predicted_n"),
                    "predicted_ms": timings.get("predicted_ms"),
                } if timings else None,
                "failed": False,
            }
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)[:400]
            time.sleep(min(60, 10 * attempt))
    return {
        "qid": qid, "si": si, "output": "", "seconds": None,
        "completion_tokens": None, "failed": True, "error": last_err,
    }


def load_gen_checkpoint(gen_path):
    done = {}
    if os.path.exists(gen_path):
        with open(gen_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                key = (rec["qid"], rec["si"])
                if not rec.get("failed"):
                    done[key] = rec
                else:
                    done.pop(key, None)  # failed entries don't count
    return done


def speed_probe(base_url, model_id):
    """Measure llama.cpp server-side prefill and decode speed with 2 requests.
    Prefill = 'lecture' speed (prompt processing tok/s); decode = generation tok/s.
    Uses server-side timings when available (excludes queue wait)."""
    filler = "The quick brown fox jumps over the lazy dog near the riverbank. " * 105
    out = {}
    try:
        body = {"model": model_id, "max_tokens": 1, "temperature": 0.0, "stream": False,
                "messages": [{"role": "user", "content": filler + "\nReply with exactly: OK"}]}
        t0 = time.time()
        d = _session().post(f"{base_url}/chat/completions", json=body, timeout=(30, 3600)).json()
        u, tg = d.get("usage") or {}, d.get("timings") or {}
        if tg.get("prompt_n") and tg.get("prompt_ms"):
            out["prefill_tokens_per_sec"] = round(tg["prompt_n"] / (tg["prompt_ms"] / 1000), 1)
        elif u.get("prompt_tokens"):
            out["prefill_tokens_per_sec"] = round(u["prompt_tokens"] / (time.time() - t0), 1)
    except Exception as e:  # noqa: BLE001
        out["prefill_error"] = repr(e)[:200]
    try:
        body = {"model": model_id, "max_tokens": 300, "temperature": 0.0, "stream": False,
                "messages": [{"role": "user",
                              "content": "Repeat exactly 300 times, space separated: ping"}]}
        t0 = time.time()
        d = _session().post(f"{base_url}/chat/completions", json=body, timeout=(30, 600)).json()
        u, tg = d.get("usage") or {}, d.get("timings") or {}
        if tg.get("predicted_n") and tg.get("predicted_ms"):
            out["decode_tokens_per_sec"] = round(tg["predicted_n"] / (tg["predicted_ms"] / 1000), 1)
        elif u.get("completion_tokens"):
            out["decode_tokens_per_sec"] = round(u["completion_tokens"] / (time.time() - t0), 1)
    except Exception as e:  # noqa: BLE001
        out["decode_error"] = repr(e)[:200]
    return out


# -----------------------------------------------------------------------------
# Evaluation engine — Windows-safe port of lcb_runner testing_util semantics.
# Each generated code is executed in its own spawned child process; the parent
# supervises with a per-message wall-clock budget (replacement for SIGALRM).
# -----------------------------------------------------------------------------

import_string = (
    "from string import *\nfrom re import *\nfrom datetime import *\n"
    "from collections import *\nfrom heapq import *\nfrom bisect import *\n"
    "from copy import *\nfrom math import *\nfrom random import *\n"
    "from statistics import *\nfrom itertools import *\nfrom functools import *\n"
    "from operator import *\nfrom io import *\nfrom sys import *\nfrom json import *\n"
    "from builtins import *\nfrom typing import *\nimport string\nimport re\n"
    "import datetime\nimport collections\nimport heapq\nimport bisect\nimport copy\n"
    "import math\nimport random\nimport statistics\nimport itertools\n"
    "import functools\nimport operator\nimport io\nimport sys\nimport json\n"
    "sys.setrecursionlimit(50000)\n"
)


def truncatefn(s, length=300):
    if isinstance(s, str):
        pass
    else:
        s = str(s)
    if len(s) <= length:
        return s
    return s[: length // 2] + "...(truncated) ..." + s[-length // 2 :]


class Capturing(list):
    def __enter__(self):
        self._stdout = sys.stdout
        sys.stdout = self._stringio = StringIO()
        self._stringio.close = lambda x: 1
        return self

    def __exit__(self, *args):
        self.append(self._stringio.getvalue())
        del self._stringio
        sys.stdout = self._stdout


class MockStdinWithBuffer:
    def __init__(self, inputs: str):
        self.inputs = inputs
        self._stringio = StringIO(inputs)
        self.buffer = MockBuffer(inputs)

    def read(self, *args):
        return self.inputs

    def readline(self, *args):
        return self._stringio.readline(*args)

    def readlines(self, *args):
        return self.inputs.split("\n")

    def __getattr__(self, name):
        return getattr(self._stringio, name)


class MockBuffer:
    def __init__(self, inputs: str):
        self.inputs = inputs.encode("utf-8")

    def read(self, *args):
        return self.inputs

    def readline(self, *args):
        return self.inputs.split(b"\n")[0] + b"\n"


def clean_if_name(code: str) -> str:
    try:
        astree = ast.parse(code)
        last_block = astree.body[-1]
        if isinstance(last_block, ast.If):
            condition = last_block.test
            if ast.unparse(condition).strip() == "__name__ == '__main__'":
                code = ast.unparse(astree.body[:-1]) + "\n" + ast.unparse(last_block.body)
    except Exception:
        pass
    return code


def make_function(code: str) -> str:
    try:
        import_stmts = []
        all_other_stmts = []
        astree = ast.parse(code)
        for stmt in astree.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                import_stmts.append(stmt)
            else:
                all_other_stmts.append(stmt)
        function_ast = ast.FunctionDef(
            name="wrapped_function",
            args=ast.arguments(
                posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]
            ),
            body=all_other_stmts,
            decorator_list=[],
            lineno=-1,
        )
        return import_string + "\n" + ast.unparse(import_stmts) + "\n" + ast.unparse(function_ast)
    except Exception:
        return code


def call_method(method, inputs):
    if isinstance(inputs, list):
        inputs = "\n".join(inputs)
    inputs_line_iterator = iter(inputs.split("\n"))
    mock_stdin = MockStdinWithBuffer(inputs)

    @patch("builtins.open", mock_open(read_data=inputs))
    @patch("sys.stdin", mock_stdin)
    @patch("sys.stdin.readline", lambda *args: next(inputs_line_iterator))
    @patch("sys.stdin.readlines", lambda *args: inputs.split("\n"))
    @patch("sys.stdin.read", lambda *args: inputs)
    def _inner_call_method(_method):
        try:
            return _method()
        except SystemExit:
            pass

    return _inner_call_method(method)


def get_function(compiled_sol, fn_name: str):
    try:
        assert hasattr(compiled_sol, fn_name)
        return getattr(compiled_sol, fn_name)
    except Exception:
        return


def compile_code(code: str):
    tmp_sol = ModuleType("tmp_sol", "")
    exec(code, tmp_sol.__dict__)
    if "class Solution" in code:
        compiled_sol = tmp_sol.Solution()
    else:
        compiled_sol = tmp_sol
    if compiled_sol is None:
        raise RuntimeError("compile produced nothing")
    return compiled_sol


def convert_line_to_decimals(line: str):
    try:
        decimal_line = [Decimal(elem) for elem in line.split()]
    except Exception:
        return False, []
    return True, decimal_line


def get_stripped_lines(val: str):
    val = val.strip()
    return [val_line.strip() for val_line in val.split("\n")]


def grade_call_based(conn, code, all_inputs, all_outputs, fn_name):
    code = import_string + "\n\n" + code
    try:
        compiled_sol = compile_code(code)
    except BaseException as e:
        conn.send(("done", [-4], {"error": repr(e)[:300], "error_code": -4,
                                   "error_message": "Compile Error"}))
        return
    method = get_function(compiled_sol, fn_name)
    if method is None:
        conn.send(("done", [-4], {"error_code": -4, "error_message": "Function Not Found"}))
        return
    try:
        parsed_inputs = [[json.loads(line) for line in inputs.split("\n")] for inputs in all_inputs]
        parsed_outputs = [json.loads(output) for output in all_outputs]
    except Exception as e:
        conn.send(("done", [-4], {"error": repr(e)[:300], "error_code": -4,
                                  "error_message": "Input Parse Error"}))
        return
    conn.send(("compiled",))
    all_results = []
    for gt_inp, gt_out in zip(parsed_inputs, parsed_outputs):
        try:
            prediction = method(*gt_inp)
        except BaseException as e:
            all_results.append(-4)
            conn.send(("done", all_results, {
                "error": repr(e)[:300], "error_code": -4, "error_message": "Runtime Error",
                "inputs": truncatefn(gt_inp), "expected": truncatefn(gt_out)}))
            return
        if isinstance(prediction, tuple):
            prediction = list(prediction)
        if prediction == gt_out:
            all_results.append(True)
            conn.send(("test", True, {}))
            continue
        all_results.append(-2)
        conn.send(("done", all_results, {
            "output": truncatefn(prediction), "inputs": truncatefn(gt_inp),
            "expected": truncatefn(gt_out), "error_code": -2, "error_message": "Wrong Answer"}))
        return
    conn.send(("done", all_results, {"execution": "ok"}))


def grade_stdio(conn, code, all_inputs, all_outputs):
    code = clean_if_name(code)
    code = make_function(code)
    try:
        compiled_sol = compile_code(code)
    except BaseException as e:
        conn.send(("done", [-4], {"error": repr(e)[:300], "error_code": -4,
                                  "error_message": "Compile Error"}))
        return
    method = get_function(compiled_sol, "wrapped_function")
    if method is None:
        conn.send(("done", [-4], {"error_code": -4, "error_message": "No runnable code found"}))
        return
    conn.send(("compiled",))
    all_results = []
    for gt_inp, gt_out in zip(all_inputs, all_outputs):
        try:
            with Capturing() as captured_output:
                call_method(method, gt_inp)
        except BaseException as e:
            all_results.append(-4)
            conn.send(("done", all_results, {
                "error": repr(e)[:300], "error_code": -4, "error_message": "Runtime Error",
                "inputs": truncatefn(gt_inp), "expected": truncatefn(gt_out)}))
            return
        prediction = captured_output[0]
        stripped_prediction_lines = get_stripped_lines(prediction)
        stripped_gt_out_lines = get_stripped_lines(gt_out)
        if len(stripped_prediction_lines) != len(stripped_gt_out_lines):
            all_results.append(-2)
            conn.send(("done", all_results, {
                "output": truncatefn(prediction), "inputs": truncatefn(gt_inp),
                "expected": truncatefn(gt_out), "error_code": -2,
                "error_message": "Wrong answer: mismatched output length"}))
            return
        wrong = False
        for output_line_idx, (pl, gl) in enumerate(
            zip(stripped_prediction_lines, stripped_gt_out_lines)
        ):
            if pl == gl:
                continue
            ok1, dp = convert_line_to_decimals(pl)
            ok2, dg = convert_line_to_decimals(gl)
            if ok1 and ok2 and dp == dg:
                continue
            all_results.append(-2)
            conn.send(("done", all_results, {
                "output": truncatefn(prediction), "inputs": truncatefn(gt_inp),
                "expected": truncatefn(gt_out), "error_code": -2,
                "error_message": f"Wrong answer at line {output_line_idx}"}))
            wrong = True
            break
        if wrong:
            return
        all_results.append(True)
        conn.send(("test", True, {}))
    conn.send(("done", all_results, {"execution": "ok"}))


def _eval_child(conn):
    """Child process: evaluate ONE generated code against one problem's tests,
    streaming results to the parent (which enforces the timeout watchdog)."""
    try:
        try:
            sys.set_int_max_str_digits(1_000_000)
        except AttributeError:
            pass
        code, in_outs, _timeout = conn.recv()
        if not code or not code.strip():
            conn.send(("done", [-4], {"error_code": -4, "error_message": "Empty code"}))
            return
        fn_name = in_outs.get("fn_name")
        if fn_name is None:
            grade_stdio(conn, code, in_outs["inputs"], in_outs["outputs"])
        else:
            grade_call_based(conn, code, in_outs["inputs"], in_outs["outputs"], fn_name)
    except BaseException as e:
        try:
            conn.send(("fatal", repr(e)[:500]))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _eval_one_generation(code, in_outs, timeout):
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe()
    proc = ctx.Process(target=_eval_child, args=(child_conn,), daemon=True)
    proc.start()
    parent_conn.send((code, in_outs, timeout))

    results, meta = [-4], {"error_code": -4, "error_message": "Child produced nothing"}
    partial = None
    # generous first budget: windows process startup + payload size
    budget = timeout + 20.0
    got_any = False
    try:
        while True:
            if not parent_conn.poll(budget):
                # watchdog
                results = (partial if partial else []) + [-3]
                meta = {"error_code": -3, "error_message": "Time Limit Exceeded (watchdog)"}
                break
            msg = parent_conn.recv()
            got_any = True
            budget = timeout + 3.0
            if msg[0] == "compiled":
                continue
            if msg[0] == "test":
                partial = (partial or []) + [msg[1]]
                continue
            if msg[0] == "done":
                results, meta = msg[1], msg[2]
                break
            if msg[0] == "fatal":
                results = (partial if partial else []) + [-4]
                meta = {"error_code": -4, "error_message": msg[1]}
                break
    except EOFError:
        results = (partial if partial else [-4])
        meta = meta if got_any and results != [-4] else {
            "error_code": -4, "error_message": "Child process died"}
    finally:
        try:
            parent_conn.close()
        except Exception:
            pass
        if proc.is_alive():
            proc.kill()
        proc.join(timeout=10)
    return results, meta


def eval_one_problem(job):
    qid = job[0]
    in_out_str, codes, timeout = job[1], job[2], job[3]
    scenario = job[4] if len(job) > 4 else "codegen"
    if scenario == "exec":
        return eval_exec_problem(qid, in_out_str, codes, timeout)
    if scenario == "top":
        return eval_top_problem(qid, in_out_str, codes)
    if scenario == "execb":
        return eval_exec_bundle_problem(qid, in_out_str, codes, timeout)
    if scenario == "topb":
        return eval_top_bundle_problem(qid, in_out_str, codes)
    in_outs = json.loads(in_out_str)
    results, metas = [], []
    for code in codes:
        res, meta = _eval_one_generation(code, in_outs, timeout)
        results.append(res)
        metas.append(meta)
    return {"qid": qid, "results": results, "meta": metas}


# ---- code_execution scenario scoring (port of compute_code_execution_metrics)
# Official: exec BASE_IMPORTS + problem code + "assert {output} == {prediction}"
# in a sandbox; True iff no exception. Predictions whose text contains the raw
# input string are skipped (and excluded from n) exactly like the official code.

EXEC_BASE_IMPORTS = (
    "from itertools import accumulate, chain, combinations, count, permutations, "
    "product, groupby, islice, repeat\n"
    "from copy import deepcopy\nfrom string import ascii_lowercase\n"
    "from math import floor, log2, log10, sqrt, comb, gcd, ceil, inf, isqrt\n"
    "from collections import defaultdict, deque, Counter\n"
    "from bisect import bisect, bisect_left, bisect_right, insort\n"
    "from heapq import heappush, heappop, heapify, merge\n"
    "from functools import reduce, cache, lru_cache\n"
    "from random import randrange, shuffle\nfrom operator import itemgetter, sub\n"
    "from re import search as re_search\nfrom os.path import commonprefix\n"
    "from typing import List, Tuple, Dict, Set, Optional, Union, Any, Callable, "
    "Iterable, Iterator, Generator\nimport copy\nimport string\nimport math\n"
    "import collections\nimport bisect\nimport heapq\nimport functools\n"
    "import random\nimport itertools\nimport operator\nimport re\nimport numpy as np\n"
    "import pandas as pd\nfrom math import log, prod\nfrom collections import deque, "
    "defaultdict, Counter, OrderedDict\nfrom itertools import accumulate, permutations, "
    "combinations, product, groupby, islice, chain, repeat, zip_longest, cycle\n"
    "from functools import lru_cache, reduce, partial\nfrom operator import iand\n"
    "import sys\n"
)


def _exec_child(conn):
    try:
        try:
            sys.set_int_max_str_digits(1_000_000)
        except AttributeError:
            pass
        code, out_literal, answer = conn.recv()[:3]
        g = {}
        exec(EXEC_BASE_IMPORTS, g)
        conn.send(("ready", True))
        exec(code + "\n", g)
        conn.send(("compiled", True))
        try:
            ok = bool(eval(f"({out_literal}) == ({answer})", g))
        except BaseException:
            ok = False
        conn.send(("done", ok))
    except BaseException as e:
        try:
            conn.send(("failed", repr(e)[:300]))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _eval_exec_sample(inp, code, out_literal, answer, timeout):
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe()
    proc = ctx.Process(target=_exec_child, args=(child_conn,), daemon=True)
    proc.start()
    parent_conn.send((code, out_literal, answer))
    ok, meta = False, {"error_code": -4, "error_message": "Exec child failed"}
    # first budget covers Windows spawn + numpy/pandas imports (measured, not timed)
    stage_budgets = [("ready", 240.0), ("compiled", timeout + 2.0),
                     ("done", timeout + 2.0)]
    try:
        for want, budget in stage_budgets:
            if not parent_conn.poll(budget):
                meta = {"error_code": -3, "error_message": "Time Limit Exceeded (watchdog)"}
                ok = False
                return ok, meta
            msg = parent_conn.recv()
            if msg[0] == want:
                if want == "done":
                    ok, meta = msg[1], {"execution": "ok"}
                continue
            if msg[0] == "failed":
                meta = {"error_code": -4, "error_message": msg[1]}
                return ok, meta
    except EOFError:
        meta = {"error_code": -4, "error_message": "Exec child died"}
    finally:
        try:
            parent_conn.close()
        except Exception:
            pass
        if proc.is_alive():
            proc.kill()
        proc.join(timeout=10)
    return ok, meta


def eval_exec_problem(qid, payload_str, answers, timeout):
    d = json.loads(payload_str)
    results, skipped, metas = [], [], []
    for answer in answers:
        if d["input"] and d["input"] in answer:
            # official quirk: skip (exclude from n) answers echoing the input
            results.append([False])
            skipped.append(True)
            metas.append({"skipped": "input echoed in answer"})
            continue
        ok, meta = _eval_exec_sample(d["input"], d["code"], d["output"], answer, timeout)
        results.append([bool(ok)])
        skipped.append(False)
        metas.append(meta)
    return {"qid": qid, "kind": "exec", "results": results,
            "skipped": skipped, "meta": metas}


# ---- test_output_prediction scenario scoring (port of
# compute_test_output_prediction_metrics; eval restricted to literals)

def _top_parse_assert(statement):
    try:
        parsed = ast.parse(statement, mode="exec")
    except SyntaxError:
        return "Invalid syntax"
    if len(parsed.body) == 0:
        return "Empty statement"
    if not isinstance(parsed.body[0], ast.Assert):
        return "Not an assert statement"
    comparison = parsed.body[0].test
    if not isinstance(comparison, ast.Compare) or not isinstance(comparison.ops[0], ast.Eq):
        return "Not an equality assertion"
    return ast.get_source_segment(statement, comparison.comparators[0])


def _check_top(pred, expected):
    if len(pred.splitlines()) > 1:
        for line in pred.splitlines():
            if line.startswith("#"):
                continue
            if "assert" in line:
                pred = line
                break
    pred = pred.strip()
    if "assert" in pred:
        pred_str = str(_top_parse_assert(pred))
    else:
        pred_str = pred
    try:
        pred_val = eval(pred_str, {"__builtins__": {}}, {})
    except Exception:
        return False
    try:
        expected_val = json.loads(expected)
    except Exception:
        return False
    return pred_val == expected_val


def eval_top_problem(qid, payload_str, preds):
    expected = json.loads(payload_str)["expected"]
    results = [[bool(_check_top(p, expected))] for p in preds]
    return {"qid": qid, "kind": "top", "results": results, "meta": [{}, ] * len(preds)}


# ---- --bundle scoring: K sub-answers per generation, the item counts as
# passed only when every sub-answer is right (all or nothing). The sub-results
# ride along in "sub" so the score line can print the per-call rate too:
# bundle pass ~= p**K is the headroom the fast pools lost.

def eval_exec_bundle_problem(qid, payload_str, answers, timeout):
    items = json.loads(payload_str)["items"]
    results, skipped, metas, subs = [], [], [], []
    for answer in answers:
        ans = list(answer) if isinstance(answer, (list, tuple)) else [answer]
        ans = ans + [""] * (len(items) - len(ans))
        if any(it["input"] and it["input"] in a for it, a in zip(items, ans)):
            # official quirk, bundle-wide: one echoing answer skips the sample
            results.append([False])
            skipped.append(True)
            metas.append({"skipped": "input echoed in answer"})
            subs.append([False] * len(items))
            continue
        sub = []
        for it, a in zip(items, ans):
            ok, meta = _eval_exec_sample(it["input"], it["code"], it["output"],
                                         a, timeout)
            sub.append(bool(ok))
        results.append([all(sub)])
        skipped.append(False)
        metas.append({"sub": sub})
        subs.append(sub)
    return {"qid": qid, "kind": "execb", "results": results,
            "skipped": skipped, "meta": metas, "sub": subs}


def eval_top_bundle_problem(qid, payload_str, preds):
    items = json.loads(payload_str)["items"]
    results, metas, subs = [], [], []
    for pred in preds:
        pr = list(pred) if isinstance(pred, (list, tuple)) else [pred]
        pr = pr + [""] * (len(items) - len(pr))
        sub = [bool(_check_top(a, it["expected"])) for a, it in zip(pr, items)]
        results.append([all(sub)])
        metas.append({"sub": sub})
        subs.append(sub)
    return {"qid": qid, "kind": "topb", "results": results, "meta": metas,
            "sub": subs}


# -----------------------------------------------------------------------------
# Metrics (official pass@k formula from lcb_runner pass_k_utils)
# -----------------------------------------------------------------------------


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


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------


def slugify(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "model"


SCEN_SUFFIX = {"codegen": "", "exec": "-exec", "top": "-top"}
HARNESS_SUFFIX = {"yes": "-harness", "no": "-noharness"}


def harness_env_hint():
    """Only ever a HINT, never an assumption: agent toolkits whose environment
    happens to be visible in this shell. The harness value itself always comes
    from the human (--harness, or the question asked by ask_harness_mode)."""
    hints = []
    if os.environ.get("DSH_SESSION_ID") or os.environ.get("DSH_SHELL"):
        hints.append("deepseek-harness")
    if os.environ.get("CURSOR_TRACE_ID") or os.environ.get("CURSOR_SESSION_ID"):
        hints.append("cursor")
    if os.environ.get("CODEX_SHELL") or os.environ.get("CODEX_SANDBOX"):
        hints.append("codex-cli")
    if os.environ.get("CLAUDE_CODE_SESSION") or os.environ.get("CLAUDE_SESSION_ID"):
        hints.append("claude-code")
    return hints


def harness_url_hint():
    """An address for the harness, if this shell knows one (DSH_WEB_URL). It is a
    suggestion for the note only - it never decides yes/no on its own."""
    url = os.environ.get("DSH_WEB_URL") or ""
    return url.strip().rstrip("/") or None


def harness_target(note, quiet=False):
    """Turn a harness note into something reachable, or None if it is a plain name
    or a file path. Only loopback/private hosts are ever probed."""
    txt = (note or "").strip().strip('"')
    if not txt:
        return None
    if "\\" in txt or (len(txt) > 1 and txt[1] == ":" and txt[0].isalpha()):
        return None                      # a Windows path is not an address
    if "://" not in txt:
        # bare host:port / host - only if it looks like a host at all
        if "." not in txt and ":" not in txt:
            return None
        txt = "http://" + txt
    try:
        parsed = urlparse(txt)
    except Exception:
        return None
    if not parsed.netloc:
        return None
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    if not (host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
            or host.startswith("127.") or host.startswith("192.168.")
            or host.startswith("10.") or host.startswith("172.")
            or host.endswith(".local") or host.endswith(".localhost")):
        if not quiet:
            print(f"     note '{note}' looks like a remote host - not probing it,"
                  " only your own machine is.")
        return None
    return f"{parsed.scheme or 'http'}://{parsed.netloc}"


def probe_harness(note, timeout=3):
    """Evidence, not a claim: can this machine reach the harness address right now?
    Never decides the harness column - only records whether the address answers."""
    target = harness_target(note)
    if not target:
        return None
    rec = {"target": target, "checked_at": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        resp = requests.get(target, timeout=timeout, allow_redirects=False)
        rec.update(reachable=True, status=resp.status_code)
    except Exception as exc:
        rec.update(reachable=False, status=None, error=type(exc).__name__)
    return rec


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


HARNESS_ADDR_FILE = os.path.join("bench", "harness-address.json")


def harness_addr_candidates(limit=3):
    """Addresses worth offering as a choice, best first: the agent that started this
    shell, the address saved by an earlier run, and addresses recorded by recent
    harness=yes runs. Duplicates dropped; only your own machine is ever probed, so a
    remote note stays text it was never turned into a choice. Every entry is a
    suggestion to pick from - it never decides the harness column on its own."""
    out = []

    def add(raw, label, probe=True):
        if len(out) >= limit:
            return
        target = harness_target(raw, quiet=True)
        if not target or any(target == got[0] for got in out):
            return
        if not probe:
            out.append((target, label))
            return
        rec = probe_harness(target) or {}
        tag = (f"answers now (HTTP {rec.get('status')})" if rec.get("reachable")
               else "not answering")
        out.append((target, f"{label} - {tag}"))

    env = harness_url_hint()
    if env:
        add(env, "the agent that started this shell (DSH_WEB_URL)")
    saved = _load_json(HARNESS_ADDR_FILE) or {}
    if saved.get("address"):
        when = ((saved.get("last_check") or {}).get("checked_at")
                or saved.get("saved_at") or "")[:10]
        add(saved["address"], f"your saved harness address (saved {when})")
    found = []
    if os.path.isdir("bench"):
        for name in os.listdir("bench"):
            path = os.path.join("bench", name, "_summary.json")
            try:
                if os.path.isfile(path):
                    found.append((os.path.getmtime(path), path))
            except OSError:
                continue
    for _, path in sorted(found, reverse=True)[:limit]:
        data = _load_json(path) or {}
        if data.get("harness") == "yes" and data.get("harness_note"):
            folder = os.path.basename(os.path.dirname(path))
            add(data["harness_note"], f"recorded by run {data.get('name') or folder}")
    if not out:
        # Nothing known: ask the usual local port once. It only becomes a choice if
        # something actually answers there - a guess that nobody answers is never
        # offered, let alone used.
        probe = probe_harness("http://127.0.0.1:3080")
        if probe and probe.get("reachable"):
            out.append((probe["target"], f"found by asking 127.0.0.1:3080"
                                        f" (HTTP {probe.get('status')})"))
    return out[:limit]


def save_harness_addr(note, probe_rec=None):
    """Remember the harness address in use, so the next run offers it instead of
    asking you to type it again. Names and paths are not addresses: nothing saved."""
    target = harness_target(note)
    if not target:
        return
    data = {"address": target,
            "saved_at": dt.datetime.now().isoformat(timespec="seconds")}
    if probe_rec:
        data["last_check"] = {k: probe_rec.get(k)
                             for k in ("reachable", "status", "checked_at")}
    try:
        os.makedirs("bench", exist_ok=True)
        with open(HARNESS_ADDR_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except OSError:
        pass


def harness_auto_note():
    """The address for a run that cannot be asked (--harness yes, detached): the
    best candidate there is, or (None, None)."""
    cands = harness_addr_candidates(limit=1)
    return cands[0] if cands else (None, None)


def _ask(prompt):
    """input() that survives a closed stdin (piped runs, Start-Process, agents) and
    a Ctrl-C (no traceback - it says what to do instead)."""
    try:
        return input(prompt)
    except EOFError:
        print("\n(no keyboard here to answer with - rerun with the answer in the"
              " command line: --harness yes|no, and --name LABEL if you want a label)")
        return None
    except KeyboardInterrupt:
        raise SystemExit("\nInterrupted at the question, so nothing was started. Rerun"
                         " and answer it, or put the answers in the command line:"
                         " --harness yes|no (and --name LABEL).")


HARNESS_ANSWER_WORDS = {"1", "2", "3", "y", "n", "yes", "no", "u", "unknown"}


def ask_label(derived):
    """Ask what this run should be called. Refuses answers that clearly belong to
    the harness question below it - a run called "1" is not a name."""
    while True:
        got = _ask("\nQ1 - name question. What should this run be called in bench/"
                   " and in the report?\n"
                   f"     the model id is: {derived}\n"
                   "     Enter = use that; or type a shorter name (NOT 1/2/3, those"
                   " are the next question): ")
        if got is None:
            return derived
        got = got.strip()
        if not got:
            return derived
        if got.lower() in HARNESS_ANSWER_WORDS:
            print(f"     '{got}' is an answer to the harness question, which comes"
                  " after this one. Press Enter to take the model id, or type a name.")
            continue
        slug = slugify(got)
        if not slug or slug.strip("._-") == "":
            print("     That has no letters or digits in it. Press Enter to take the"
                  " model id, or type a name.")
            continue
        return slug


def ask_harness_mode(suggested, folders=None):
    """Ask the human how this run shares the model server. The bench never
    guesses: no answer recorded, no default taken silently.
    folders maps the answer to the run folder it produces, so the question shows
    which answer belongs to which row.
    Returns (mode, note, source) with mode in yes/no/None."""
    opts = {"no": 1, "yes": 2, None: 3}
    dflt = str(opts.get(suggested, 3))
    print("\nQ2 - harness question. This bench never guesses it, so it must be answered:")
    print("  Is an agent chat (a harness) open on this same model server right now?")
    for mode, num in (("no", 1), ("yes", 2)):
        extra = ("its requests queue behind the benchmark, so wall time (gen-min)"
                 " inflates, tok/s does not" if mode == "yes"
                 else "your own window, nothing else hits the model server")
        folder = f"   -> bench\\{folders[mode]}" if folders else ""
        print(f"  [{num}] {mode:<6} {extra}{folder}")
    unknown_extra = "leave that column empty" + (
        f"   -> bench\\{folders[None]}" if folders else "")
    print(f"  [3] unknown  {unknown_extra}")
    hints = harness_env_hint()
    if hints:
        print("  hint: this shell looks like it was started by"
              f" {', '.join(hints)}. That is a hint only - it is not assumed.")
    while True:
        ans = _ask(f"Your choice [1/2/3] (Enter = {dflt}): ")
        if ans is None:
            return None, None, "not answered (nothing to ask here)"
        ans = ans.strip().lower() or dflt
        if ans in ("1", "no", "n"):
            mode = "no"
            break
        if ans in ("2", "yes", "y"):
            mode = "yes"
            break
        if ans in ("3", "u", "unknown"):
            return None, None, "not answered"
        print("Please answer 1, 2 or 3.")
    note = None
    if mode == "yes":
        note = ask_harness_which(harness_addr_candidates())
    return mode, note, "you answered"


def ask_harness_which(cands):
    """Which harness it was: numbered choices, the first one is the default so
    Enter answers it and nothing has to be typed. A different address or a plain
    name can always be typed - last choice, or straight away."""
    if not cands:
        got = _ask("  Which harness is it? Its address (the http://127.0.0.1:3080"
                   " style) or a name. Enter to skip: ")
        return got.strip() if got else None
    print("  Which harness is it? The bench pings an address at the start and end")
    print("  of the run, so an address is evidence; nothing is ever routed through it.")
    for num, (addr, label) in enumerate(cands, 1):
        print(f"  [{num}] {addr:<26} {label}")
    nxt = len(cands) + 1
    print(f"  [{nxt}] another one: type its address (http://host:port) or a name")
    while True:
        ans = _ask(f"  Your choice [1-{nxt}] (Enter = 1): ")
        if ans is None:
            return None
        ans = ans.strip()
        if not ans:
            return cands[0][0]
        if ans.isdigit():
            pick = int(ans)
            if 1 <= pick <= len(cands):
                return cands[pick - 1][0]
            got = _ask("      address (http://host:port) or a name: ")
            return got.strip() if got else None
        return ans


def ask_harness_pair_note(preset=None):
    """Which harness shares the model server, asked once for a --both pair (the
    no-pass needs no answer). --harness-note answers it up front; detached, it
    is the saved address if there is one."""
    if preset:
        return preset
    if not sys.stdin.isatty():
        guess, label = harness_auto_note()
        if guess:
            print(f"         pass 2 takes the harness from {label}: {guess}")
        else:
            print("         no keyboard and no saved harness address: pass 2 records"
                  " its yes without an address (--harness-note names one)")
        return guess
    return ask_harness_which(harness_addr_candidates())


def both_answers(base, scen, preset_note=None):
    """--both: one command, both answers to the harness question, two comparable
    rows. The no pass comes first (nothing else should be on the model server
    while it runs), then the yes pass with the agent chat working as usual.
    Nothing is ever routed through the harness - it only shares the server."""
    print("\n--both: the same problems twice, one pass per answer to the harness"
          " question.")
    for num, mode in enumerate(("no", "yes"), 1):
        print(f"   pass {num}/2 answers '{mode}' -> bench\\{resolve_run_dir(base, scen, mode)}"
              + (" (nothing else on the server while it runs)" if mode == "no"
                 else " (agent chat working on its own tasks while it runs)"))
    print("   Both passes ask the model the same way: the chat never sees a"
          " problem,")
    print("   it only shares the server, so both rows are the raw model.")
    note = ask_harness_pair_note(preset_note)
    if preset_note:
        print(f"   pass 2/2 uses that harness: {note}")
    if sys.stdin.isatty():
        try:
            input("Press Enter to start pass 1/2 (close the agent chat first, so"
                  " the 'no' row really runs alone): ")
        except EOFError:
            print("   (no keyboard: starting right away)")
        except KeyboardInterrupt:
            raise SystemExit("\nStopped before pass 1/2, so nothing was started."
                             " One pass at a time works with --harness yes|no.")
    return [("no", None, "you answered: nothing else on the server (--both pass 1)"),
            ("yes", note, "you answered: the agent chat shared the server"
                          " (--both pass 2)")]


def recorded_harness(run_dir):
    """Harness value already recorded for an existing run dir, or None."""
    sp = os.path.join(run_dir, "_summary.json")
    if not os.path.isfile(sp):
        return None
    try:
        with open(sp, encoding="utf-8") as f:
            return json.load(f).get("harness")
    except Exception:
        return None


def resolve_run_dir(base, scen, mode):
    """Pick bench/<folder> for this run: one label per model + the scenario + the
    harness answer, so the same command answered twice gives two comparable rows.
    Legacy folders (no harness suffix) are reused when their recorded answer
    agrees with this one."""
    scen_suf = SCEN_SUFFIX[scen]
    if scen_suf and base.endswith(scen_suf):
        scen_suf = ""   # --name already carries it; do not build "...-exec-exec"
    cands = []
    if mode:
        cands.append(base + scen_suf + HARNESS_SUFFIX[mode])
    cands.append(base + scen_suf)
    for c in cands:
        d = os.path.join("bench", c)
        if not os.path.isdir(d):
            continue
        rec = recorded_harness(d)
        if mode is None or rec in (None, "unknown", mode):
            return c
    return cands[0]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--name", help="Short label for this model (folder + report row)."
                                   " Optional: without it the label comes from the model"
                                   " id the server reports, so the report always says"
                                   " which model a row is.")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080/v1",
                    help="OpenAI-compatible base URL (LM Studio: http://localhost:1234/v1)")
    ap.add_argument("--api-key", default=None,
                    help="Bearer token for servers that demand one (vLLM and friends)."
                         " Also read from LCB_API_KEY. Not needed for llama-server.")
    ap.add_argument("--model", default=None, help="Server model id (default: first from /v1/models)")
    ap.add_argument("--n", type=int, default=1, help="Samples per problem")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--top-p", type=float, default=0.95)
    ap.add_argument("--max-tokens", type=int, default=16384)
    ap.add_argument("--workers", type=int, default=1,
                    help="Concurrent requests; set = llama-server --parallel for max throughput")
    ap.add_argument("--extra-body", default=None,
                    help='Extra JSON merged into every request, e.g. '
                         '\'{"chat_template_kwargs": {"enable_thinking": false}}\'')
    ap.add_argument("--request-timeout", type=float, default=3600)
    ap.add_argument("--release", default="release_latest")
    ap.add_argument("--start-date", default=None, help="YYYY-MM-DD filter on problem publish date")
    ap.add_argument("--end-date", default=None)
    ap.add_argument("--difficulty", default=None, choices=["easy", "medium", "hard"])
    ap.add_argument("--limit", type=int, default=0, help="Only first N problems (smoke test)")
    ap.add_argument("--random-sample", type=int, default=0,
                    help="Stratified random subset of N problems (fair + fast; same subset "
                         "every time via fixed seed, saved to bench/<name>/sample_ids.json "
                         "so every model is scored on the identical problems)")
    ap.add_argument("--hardest", type=int, default=0, metavar="N",
                    help="Take N of the sample's slots for the hardest problems in"
                         " the release (hard label first, then the newest contest,"
                         " then the most test cases) and spread the rest evenly over"
                         " easy / medium / hard, so one command covers every"
                         " difficulty in one total: --random-sample 100 --hardest"
                         " 25 is 25 hardest + 25 easy + 25 medium + 25 hard. On its"
                         " own the run IS those N hardest problems. A small"
                         " easy-weighted sample lets a good model print 100%%.")
    ap.add_argument("--mix", default=None, metavar="A/B/C/D",
                    help="Fill --random-sample N by tier shares instead of one"
                         " hardest tier: --mix 50/25/15/10 is 50%% hardest, 25%%"
                         " hard, 15%% medium, 10%% easy of those N problems"
                         " (named form works too: --mix hardest=50,hard=25,"
                         "medium=15,easy=10). hardest is this bench's own ranking"
                         " (label, then newest, then biggest) because a dataset's"
                         " own hard label is often nearly empty - if a tier cannot"
                         " be filled the run says so and takes the best that is"
                         " left. Takes the place of --hardest; one or the other.")
    ap.add_argument("--pool-info", action="store_true",
                    help="Answer 'why did this scenario come out 100%%' without"
                         " waking the model: load the scenario's problem pool and"
                         " print how many problems it really holds, how many are"
                         " labelled hard, how old they are, what a --mix could"
                         " ever fill from it and what --bundle sizes it could"
                         " supply. No generation, no server.")
    ap.add_argument("--bundle", type=int, default=1, metavar="K",
                    help="Fast scenarios only: every scored item is K single"
                         " calls asked in ONE answer and graded all-or-nothing"
                         " (all K right or the item fails). A model that gets"
                         " p of the single calls right lands near p**K per"
                         " item, which is where a saturated pool (the whole"
                         " exec release came out 100%%) gets its headroom"
                         " back. --random-sample counts ITEMS then, not rows."
                         " --pool-info prints how many items each K can supply.")
    ap.add_argument("--harden-target", type=float, default=None, metavar="PCT",
                    help="Aim for a score under PCT%%: a run that lands at or"
                         " over it prints the exact next command that could"
                         " push this model under it (bigger --bundle on the"
                         " fast scenarios, later --start-date / heavier --mix"
                         " on code_generation). Default target: 90 fast, 99"
                         " long - the point is that no run just prints a"
                         " smiling 100%%.")
    ap.add_argument("--both", action="store_true",
                    help="One command, both rows: run the whole scenario twice on the"
                         " same problems - pass 1 with the harness question answered no"
                         " (nothing else on the model server), pass 2 answered yes (the"
                         " agent chat working while the bench runs). The pair is asked"
                         " up front once; no harness ever sees a problem.")
    ap.add_argument("--extractor", default="auto", choices=["auto", "official"],
                    help="auto = official last-fence rule + truncation/fenceless fallback")
    ap.add_argument("--timeout", type=int, default=6,
                    help="Eval time budget per test case, in seconds (official: 6)")
    ap.add_argument("--eval-workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--skip-generate", action="store_true")
    ap.add_argument("--skip-eval", action="store_true")
    ap.add_argument("--report-only", action="store_true",
                    help="Print the full comparison table of every run under bench/"
                         " (the bench console's 'report' command does the same)")
    ap.add_argument("--manage", action="store_true",
                    help="Open the bench console without a server, a model or a run:"
                         " list the scores, delete one, bring it back. Same commands as"
                         " while a run is going: type help.")
    ap.add_argument("--list-runs", action="store_true",
                    help="Print the numbered list of runs the bench console works on")
    ap.add_argument("--delete-run", default=None, metavar="NAME_OR_NUMBER",
                    help="Move one run out of the report into bench/_archive (reversible"
                         " with --restore-run). No server, no model needed.")
    ap.add_argument("--restore-run", default=None, metavar="NAME_OR_NUMBER",
                    help="Move a run back out of bench/_archive into bench/, so it"
                         " counts in the report again")
    ap.add_argument("--speed-probe", action="store_true",
                    help="Measure server prefill ('lecture') + decode tok/s with 2 probe requests")
    ap.add_argument("--probe-only", action="store_true",
                    help="With --speed-probe: measure tok/s and exit (no datasets, no "
                         "generation). The saved probe is reused by the real run.")
    ap.add_argument("--scenario", default="code_generation",
                    choices=["code_generation", "codegen", "code_execution", "exec",
                             "test_output_prediction", "top"],
                    help="Benchmark scenario. code_generation (default) is the main "
                         "leaderboard one; code_execution simulates code output "
                         "(livecodebench/execution-v2), test_output_prediction "
                         "predicts the test answer (livecodebench/test_generation).")
    ap.add_argument("--exec-cot", action="store_true",
                    help="Step-by-step examples for code_execution "
                         "(official --cot_code_execution); default is the direct prompt")
    ap.add_argument("--self-test", action="store_true",
                    help="Sanity-check the Windows evaluator and exit (no server needed)")
    ap.add_argument("--harness", default=None, choices=["yes", "no"],
                    help="Answer the harness question up front instead of being asked:"
                         " was an agent chat open on the same model server during this"
                         " run? Its requests queue behind the benchmark on an -np 1"
                         " server, which inflates wall time. Needed when the run is"
                         " detached or piped (nothing can answer). Never guessed.")
    ap.add_argument("--harness-note", default=None, metavar="ADDRESS_OR_NAME",
                    help="With --harness yes (or --both, for its yes pass): say"
                         " WHICH harness shared the server. An address is best"
                         " (e.g. --harness-note http://127.0.0.1:3080)"
                         " - the bench then checks that address is really answering and"
                         " records it; a name or path is recorded as said. Metadata"
                         " only: the yes/no answer is what separates the two runs.")
    ap.add_argument("--set-harness", default=None, choices=["yes", "no"],
                    metavar="{yes,no}",
                    help="With --name: backfill the harness column of an existing run "
                         "in its _summary.json and rebuild the report (no server, "
                         "no regeneration)")
    args = ap.parse_args()
    global _API_KEY, MIX_SPEC
    _API_KEY = args.api_key or os.environ.get("LCB_API_KEY")

    scen = {"code_generation": "codegen", "codegen": "codegen",
            "code_execution": "exec", "exec": "exec",
            "test_output_prediction": "top", "top": "top"}[args.scenario]

    if args.probe_only and not args.speed_probe:
        ap.error("--probe-only requires --speed-probe")
    if args.both and args.harness:
        ap.error("--both runs the scenario twice and answers the harness question"
                 " both ways; --harness answers it once. Use one or the other.")
    if args.harness_note and args.harness != "yes" and args.set_harness != "yes" \
            and not args.both:
        ap.error("--harness-note needs --harness yes (it says which harness it was)")
    if args.mix and args.hardest:
        ap.error("--mix and --hardest both decide how the sample gets its hard"
                 " problems; use one or the other")
    if args.bundle != 1:
        if args.bundle < 2:
            ap.error("--bundle takes K >= 2 (1 is no bundling at all)")
        if scen == "codegen":
            ap.error("--bundle is for the fast scenarios (code_execution /"
                     " test_output_prediction): one code_generation problem is"
                     " already a whole program - the knobs there are --mix /"
                     " --hardest / --start-date")
    if args.harden_target is not None and not (0 < args.harden_target <= 100):
        ap.error("--harden-target is a percentage between 0 and 100")
    if args.mix:
        if not args.random_sample:
            ap.error("--mix is a share *of a sample*: give it --random-sample N too")
        try:
            args.mix_parsed = parse_mix(args.mix)
        except ValueError as e:
            ap.error(str(e))
        MIX_SPEC = args.mix

    if args.self_test:
        sys.exit(self_test())

    if args.pool_info:
        show_pool_info(args, scen)
        return

    os.makedirs("bench", exist_ok=True)
    if args.report_only:
        build_report()
        return
    if args.list_runs:
        console_list("runs")
        if console_entries(archived=True):
            console_list("archive")
        return
    if args.delete_run is not None:
        rows = console_entries()
        row, complaint = console_find(args.delete_run, rows, "run")
        if complaint:
            console_list("runs")
            sys.exit(complaint)
        dest, complaint = archive_run(row)
        if complaint:
            sys.exit(complaint)
        print(f"deleted {row['label']}: moved to {dest}")
        print("nothing was erased - --restore-run (or 'restore' in the console)"
              " brings it back; bench/report.md + .csv are rebuilt without it")
        build_report(show=False)
        return
    if args.restore_run is not None:
        rows = console_entries(archived=True)
        row, complaint = console_find(args.restore_run, rows, "archived run")
        if complaint:
            print("Archived runs:")
            console_list("archive")
            sys.exit(complaint)
        back, complaint = restore_run(row)
        if complaint:
            sys.exit(complaint)
        print(f"restored {row['label']} -> {back}, it counts in the report again")
        build_report(show=False)
        return
    if args.manage:
        console_loop()
        return
    if args.set_harness:
        if not args.name:
            ap.error("--set-harness needs --name of an existing run folder")
        sp = os.path.join("bench", slugify(args.name), "_summary.json")
        if not os.path.isfile(sp):
            known = sorted(d for d in os.listdir("bench")
                           if os.path.isfile(os.path.join("bench", d, "_summary.json")))
            sys.exit(f"No summary for '{args.name}' at {sp} - nothing to annotate."
                     f" Runs you have: {', '.join(known) or '(none)'}")
        with open(sp, encoding="utf-8") as f:
            data = json.load(f)
        data["harness"] = args.set_harness
        if args.harness_note:
            data["harness_note"] = args.harness_note
            now = probe_harness(args.harness_note)
            save_harness_addr(args.harness_note, now)
            if now:
                now["note"] = ("checked now, after the run - not evidence about the"
                               " run itself")
                data["harness_probe"] = {"backfill": now}
                print(f"     {now['target']} is"
                      + (f" up now (HTTP {now['status']})" if now.get("reachable")
                         else f" not answering ({now.get('error')})"))
            else:
                data["harness_probe"] = None
            prev_src = data.get("harness_source")
            data["harness_source"] = (prev_src + "; note set with --set-harness"
                                      if prev_src else "backfilled with --set-harness")
        else:
            data["harness_source"] = "backfilled with --set-harness"
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"{slugify(args.name)}: harness = {args.set_harness}"
              + (f", note = {args.harness_note}" if args.harness_note else ""))
        build_report()
        return

    # ---- Who is being benched, and in which mode? Never guessed. -----------
    try:
        server_models = requests.get(f"{args.base_url}/models", timeout=10,
                                     headers=_auth_headers()).json()
        model_id = args.model or server_models["data"][0]["id"]
    except Exception as exc:
        model_id = args.model
        print(f"Could not query {args.base_url}/models ({type(exc).__name__}):"
              " pass --base-url and/or --model if this is not llama-server on :8080.")
    if args.name:
        base = slugify(args.name)
    elif model_id:
        base = ask_label(slugify(model_id))
    else:
        ap.error("the server reported no model id and no --name was given: pass"
                 " --name <label> (and --model <id> if the server needs it).")

    # Harness mode: --both asks for both answers at once; otherwise --harness
    # flag > asked out loud > already recorded > unknown. Never guessed.
    prev_harness = recorded_harness(os.path.join("bench", base + SCEN_SUFFIX[scen]))
    if args.both:
        passes = both_answers(base, scen, args.harness_note)
    else:
        if args.harness:
            harness_val, harness_note = args.harness, args.harness_note
            harness_src = "--harness flag"
        elif sys.stdin.isatty():
            stem = base + SCEN_SUFFIX[scen]
            folders = {mode: stem + HARNESS_SUFFIX[mode] for mode in HARNESS_SUFFIX}
            folders[None] = stem
            harness_val, harness_note, src = ask_harness_mode(prev_harness, folders)
            harness_src = src
        else:
            harness_val, harness_note = prev_harness, None
            harness_src = ("kept from an earlier phase of this run" if prev_harness
                           else "not answered (detached run) - use --harness yes|no")
        passes = [(harness_val, harness_note, harness_src)]
    done = []
    try:
        # passes come ordered: the no-harness one first, so it really runs alone
        for harness_val, harness_note, harness_src in passes:
            got = run_one(args, scen, base, model_id, harness_val, harness_note,
                          harness_src, report_after=(len(passes) == 1))
            if got:
                done.append(got)
    finally:
        if len(passes) > 1 and done:
            # the pair belongs together: show just these two rows, not the whole
            # history (that is what --report-only is for)
            build_report(only=done)



def run_one(args, scen, base, model_id, harness_val, harness_note,
          harness_src, report_after=True):
    """One bench pass, end to end: probe, pick problems, generate, extract,
    grade, write bench/<run>/_summary.json and print that run's row.

    Called once normally and twice with --both (the no-harness pass, then the
    with-harness pass). Returns the run folder name, or None when the pass
    stopped before producing anything (--probe-only).
    """
    run_slug = resolve_run_dir(base, scen, harness_val)
    run_dir = os.path.join("bench", run_slug)
    resuming = os.path.isfile(os.path.join(run_dir, "generations.jsonl"))
    os.makedirs(run_dir, exist_ok=True)
    if harness_val == "yes" and not harness_note:
        guess, label = harness_auto_note()
        if guess:
            harness_note = guess
            harness_src += f"; address = {label}, override with --harness-note"
    print(f"\nRun:     bench\\{run_slug}  ({'resuming' if resuming else 'new run'})")
    print(f"Model:   {model_id or 'UNKNOWN - pass --model <server model id>'}")
    print(f"Harness: {harness_val or 'unknown'} ({harness_src}"
          + (f"; {harness_note}" if harness_note else "") + ")")
    # The bench console: any key during the run opens it, so scores can be
    # listed and deleted while the model is still working. Only with a keyboard
    # attached; a piped or detached run never blocks on one.
    global CONSOLE_LIVE
    _CONSOLE_ACTIVE["run"] = run_dir
    if sys.stdin.isatty():
        CONSOLE_LIVE = True
        print("           console: type 'help' any time during the run (the first"
              " keypress opens\n           bench> - list / delete 2 / restore 1 /"
              " status / quit)")
    # The harness note may carry an address: check it is really there. Evidence
    # only - it can never turn a "no" into a "yes" or the other way round.
    harness_probe = {}
    if harness_val == "yes":
        start = probe_harness(harness_note)
        save_harness_addr(harness_note, start)
        if start:
            harness_probe["start"] = start
            print(f"           {start['target']} is"
                  + (f" up (HTTP {start['status']}) - the agent is really there"
                     if start.get("reachable") else
                     f" NOT answering ({start.get('error')}) - your answer stands"))
        else:
            print("           no address for the harness, so nothing to check: answer"
                  " the which-harness question with one (Enter takes choice 1), or"
                  " pass --harness-note http://127.0.0.1:3080")

    if args.speed_probe:
        sp_path = os.path.join(run_dir, "speed_probe.json")
        if os.path.exists(sp_path):
            print(f"Speed probe already recorded: {open(sp_path, encoding='utf-8').read().strip()}")
        else:
            print("Running server speed probe (may wait behind a generation)...")
            try:
                probe = speed_probe(args.base_url, model_id)
            except Exception as exc:
                sys.exit(f"Speed probe failed against {args.base_url}"
                         f" ({type(exc).__name__}): is the server up? For anything"
                         " that is not llama-server on :8080 pass --base-url (plus"
                         " --model and --api-key if that server wants them).")
            probe["model"] = model_id
            with open(sp_path, "w", encoding="utf-8") as f:
                json.dump(probe, f, indent=2)
            print(f"Speed probe: {probe}")

    if args.probe_only:
        print("--probe-only: stopping here, nothing generated. The same run resumed"
              " (same label, same harness answer) reuses the saved probe"
              " (bench/<run>/speed_probe.json).")
        return

    sample_file = os.path.join(run_dir, "sample_ids.json")
    mix = getattr(args, "mix_parsed", None)
    bundle = int(getattr(args, "bundle", 1) or 1)
    if scen == "codegen":
        problems = load_problems(args.release, args.start_date, args.end_date,
                                 args.difficulty, args.limit,
                                 args.random_sample, sample_file,
                                 hardest=args.hardest, mix=mix)
    elif scen == "exec":
        problems = load_exec_problems(args.release, args.start_date, args.end_date,
                                      args.difficulty, args.limit,
                                      args.random_sample, sample_file,
                                      cot=args.exec_cot, hardest=args.hardest,
                                      mix=mix, bundle=bundle)
    else:
        problems = load_top_problems(args.release, args.start_date, args.end_date,
                                     args.difficulty, args.limit,
                                     args.random_sample, sample_file,
                                     hardest=args.hardest, mix=mix, bundle=bundle)
    if bundle > 1 and problems and not problems[0].get("bundle"):
        # resumed run: the saved sample_ids are bundle items, so the pool above
        # was already bundled by the loader - nothing to do, this is the guard.
        print("WARNING: --bundle given but the sample is not bundled items")
    if bundle > 1:
        print(f"Bundle: every item is {bundle} calls answered in ONE go -"
              f" all {bundle} right or the item fails (score scales roughly"
              f" like per-call-rate^{bundle}, that is where the headroom is)")
    qorder = [p["question_id"] for p in problems]
    probs_by_id = {p["question_id"]: p for p in problems}
    gen_path = os.path.join(run_dir, "generations.jsonl")

    # ---- Phase 1: generate -----------------------------------------------
    if not args.skip_generate:
        done = load_gen_checkpoint(gen_path)
        if done:
            print(f"Resume: {len(done)} generations already done")
        tasks = []
        for p in problems:
            messages = p.get("messages") or format_prompt(
                p["question_content"], p["starter_code"])
            for si in range(args.n):
                if (p["question_id"], si) not in done:
                    tasks.append((p["question_id"], si, messages))
        if tasks:
            payload_extras = {
                "temperature": args.temperature,
                "top_p": args.top_p,
                "max_tokens": args.max_tokens,
            }
            if args.extra_body:
                payload_extras.update(json.loads(args.extra_body))
            print(f"Generating {len(tasks)} outputs with {args.workers} worker(s)...")
            t0 = time.time()
            gen_count = 0
            with open(gen_path, "a", encoding="utf-8") as out:
                with ThreadPoolExecutor(max_workers=args.workers) as pool:
                    futures = [
                        pool.submit(generate_one, t, args.base_url, model_id,
                                    payload_extras, 3, args.request_timeout)
                        for t in tasks
                    ]
                    for fut in futures:
                        rec = fut.result()
                        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        out.flush()
                        gen_count += 1
                        tok, secs = rec.get("completion_tokens"), rec.get("seconds")
                        tps = f" {tok / secs:6.1f} tok/s" if (tok and secs) else ""
                        fail = " FAILED" if rec.get("failed") else ""
                        print(f"[gen {gen_count}/{len(tasks)}] {rec['qid']}#{rec['si']} "
                              f"{secs}s{tps}{fail}", flush=True)
                        console_tick(run_dir)
            print(f"Generation phase done in {(time.time() - t0) / 60:.1f} min")

    done = load_gen_checkpoint(gen_path)

    # ---- Phase 2: extract --------------------------------------------------
    gen_map = {}
    for (qid, si), rec in done.items():
        gen_map.setdefault(qid, {})[si] = rec["output"]
    generations = []
    compat = []
    missing = []
    for qid in qorder:
        outs_map = gen_map.get(qid)
        if not outs_map:
            # No generation for this problem yet (interrupted run / failed
            # requests). Grading it as an empty answer would poison the score,
            # so it is left out and reported instead.
            missing.append(qid)
            continue
        outs = [outs_map.get(si, "") for si in range(args.n)]
        if scen == "codegen":
            codes = [extract_code(o, args.extractor) for o in outs]
        elif scen == "exec" and bundle > 1:
            codes = [extract_exec_bundle_answers(o, probs_by_id[qid].get("inputs") or [],
                                                 args.exec_cot) for o in outs]
        elif scen == "exec":
            codes = [extract_exec_answer(o, args.exec_cot) for o in outs]
        elif bundle > 1:
            codes = [extract_top_bundle_answers(o, probs_by_id[qid].get("calls") or [])
                     for o in outs]
        else:
            codes = [extract_top_answer(o) for o in outs]
        generations.append({"question_id": qid, "output_list": outs, "code_list": codes})
        compat.append({"question_id": qid,
                       "code_list": [c if isinstance(c, str) else json.dumps(c)
                                     for c in codes]})
    if missing:
        print(f"WARNING: {len(missing)}/{len(qorder)} problems have no generation yet "
              f"(rerun the same command to generate them) - they are NOT counted "
              f"in the score.")
    with open(os.path.join(run_dir, "generations.json"), "w", encoding="utf-8") as f:
        json.dump(generations, f, indent=2, ensure_ascii=False)
    with open(os.path.join(run_dir, "results_official_format.json"), "w",
              encoding="utf-8") as f:
        json.dump(compat, f, indent=2, ensure_ascii=False)
    print(f"Wrote generations.json ({len(generations)} problems, n={args.n})")

    # ---- Phase 3: evaluate ---------------------------------------------------
    eval_path = os.path.join(run_dir, "eval_results.jsonl")
    evaluated = {}
    if os.path.exists(eval_path):
        with open(eval_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    evaluated[rec["qid"]] = rec
        if evaluated:
            print(f"Resume: {len(evaluated)} problems already evaluated")

    jobs = []
    if not args.skip_eval:
        for entry in generations:
            qid = entry["question_id"]
            if qid in evaluated:
                continue
            # codegen problems carry "in_out"; the fast scenarios only "eval_payload".
            # (Writing .get("eval_payload", probs[..]["in_out"]) raises on the fast
            # scenarios, because the fallback argument is evaluated eagerly.)
            payload = probs_by_id[qid].get("eval_payload") or probs_by_id[qid].get("in_out")
            if payload is None:
                sys.exit(f"Internal error: problem {qid} has no eval payload "
                         f"(keys: {sorted(probs_by_id[qid])})")
            jobs.append((qid, payload,
                         entry["code_list"], args.timeout,
                         (scen + "b") if (bundle > 1 and scen in ("exec", "top"))
                         else scen))

    if jobs:
        print(f"Evaluating {len(jobs)} problems with {args.eval_workers} eval "
              f"workers ({args.timeout}s/test budget)...")
        t_eval = time.time()
        with open(eval_path, "a", encoding="utf-8") as out:
            with ThreadPoolExecutor(max_workers=args.eval_workers) as pool:
                done_ct = 0
                for rec in pool.map(eval_one_problem, jobs):
                    evaluated[rec["qid"]] = rec
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    out.flush()
                    done_ct += 1
                    if done_ct % 10 == 0 or done_ct == len(jobs):
                        print(f"[eval {done_ct}/{len(jobs)}] "
                              f"elapsed {(time.time() - t_eval) / 60:.1f} min", flush=True)
                    console_tick(run_dir)
    else:
        print("Evaluation already complete for current problem set")

    # ---- Phase 4: metrics ------------------------------------------------------
    per_problem, per_diff = [], {}
    per_problem_pass = {}
    sub_pass, sub_total = 0, 0  # --bundle: per-call (not per-item) counters
    for qid in qorder:
        if qid not in gen_map:
            continue  # stale verdict for a problem with no (kept) generation
        rec = evaluated.get(qid)
        if not rec:
            continue
        kind = rec.get("kind", "codegen")
        if kind == "codegen":
            n = len(rec["results"])
            c = sum(1 for res in rec["results"] if res and all(x > 0 for x in res))
        elif kind in ("exec", "execb"):
            # official: skipped (input-echoing) generations are excluded from n;
            # if everything was skipped, n = len(results) and c = 0
            skips = rec.get("skipped", [False] * len(rec["results"]))
            scored = [r for r, s in zip(rec["results"], skips) if not s]
            c = sum(1 for r in scored if r and all(bool(x) for x in r))
            n = len(scored) if scored else len(rec["results"])
        else:  # top / topb
            n = len(rec["results"])
            c = sum(1 for r in rec["results"] if r and all(bool(x) for x in r))
        for one in rec.get("sub") or []:
            if isinstance(one, list):
                sub_total += len(one)
                sub_pass += sum(1 for x in one if x)
        per_problem.append((n, c))
        per_problem_pass[qid] = c / n if n else 0.0
        diff = probs_by_id[qid]["difficulty"].lower()
        per_diff.setdefault(diff, []).append((n, c))

    metrics = compute_pass_metrics(per_problem)
    diff_metrics = {d: compute_pass_metrics(v) for d, v in sorted(per_diff.items())}
    # The --hardest tier on its own: without this a run of 25 hardest inside 100
    # problems only ever shows one blended "hard" number.
    hardest_rates = [per_problem_pass[qid] for qid in per_problem_pass
                     if str(qid) in HARDEST_IDS]
    hardest_m = {"n": len(hardest_rates),
                 "pass@1": round(sum(hardest_rates) / len(hardest_rates), 4)
                 if hardest_rates else None}

    gen_secs_total = sum(r["seconds"] for r in done.values() if r.get("seconds"))
    gen_tok_total = sum(r["completion_tokens"] or 0 for r in done.values())
    prompt_tok_total = sum(r.get("prompt_tokens") or 0 for r in done.values())
    n_gen = len(done)
    truncated = sum(
        1 for r in done.values()
        if r.get("completion_tokens")
        and r["completion_tokens"] >= (r.get("max_tokens") or args.max_tokens or 0)
    )
    trunc_qids = {
        (k[0] if isinstance(k, tuple) else k)
        for k, r in done.items()
        if r.get("completion_tokens")
        and r["completion_tokens"] >= (r.get("max_tokens") or args.max_tokens or 0)
    }
    empty_outputs = sum(1 for r in done.values() if not r.get("output"))
    # Pass rate among problems whose answer was NOT cut off at the token cap:
    # separates "can it solve the problem" from "did it fit in the budget".
    complete_rates = [v for q, v in per_problem_pass.items() if q not in trunc_qids]
    failed_requests = 0
    if os.path.exists(gen_path):
        with open(gen_path, encoding="utf-8") as f:
            failed_requests = sum(1 for line in f if line.strip()) - n_gen
    # Server-side timing aggregates (llama.cpp excludes queue wait): exact speed
    pn = sum((r.get("timings") or {}).get("prompt_n") or 0 for r in done.values())
    pms = sum((r.get("timings") or {}).get("prompt_ms") or 0 for r in done.values())
    pnn = sum((r.get("timings") or {}).get("predicted_n") or 0 for r in done.values())
    pnm = sum((r.get("timings") or {}).get("predicted_ms") or 0 for r in done.values())
    probe_path = os.path.join(run_dir, "speed_probe.json")
    probe = json.load(open(probe_path, encoding="utf-8")) if os.path.exists(probe_path) else {}
    prefill_tps = round(pn * 1000.0 / pms, 1) if pms and pn else probe.get("prefill_tokens_per_sec")
    decode_tps = round(pnn * 1000.0 / pnm, 1) if pnm and pnn else probe.get("decode_tokens_per_sec")
    if decode_tps is None:
        decode_tps = round(gen_tok_total / gen_secs_total, 1) if gen_secs_total else None
    summary_path = os.path.join(run_dir, "_summary.json")
    if harness_val == "yes":
        end = probe_harness(harness_note)
        if end:
            harness_probe["end"] = end
            print(f"End of run: {end['target']} is"
                  + (f" still up (HTTP {end['status']})" if end.get("reachable") else
                     f" not answering anymore ({end.get('error')})"))
    bundle_m = None
    if bundle > 1 and sub_total:
        bundle_m = {"k": bundle, "calls": sub_total,
                    "per_call_pass": round(sub_pass / sub_total, 4)}
    summary = {
        "name": run_slug,
        "model": model_id,
        "server_model": model_id,
        "harness": harness_val or "unknown",
        "harness_note": harness_note,
        "harness_source": harness_src,
        "harness_probe": harness_probe or None,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "config": {
            "n": args.n, "temperature": args.temperature, "top_p": args.top_p,
            "max_tokens": args.max_tokens, "release": args.release,
            "start_date": args.start_date, "end_date": args.end_date,
            "difficulty": args.difficulty, "limit": args.limit or None,
            "random_sample": args.random_sample or None,
            "hardest": args.hardest or None,
            "mix": args.mix or None,
            "sample_note": SAMPLE_NOTE or None,
            "extractor": args.extractor, "eval_timeout": args.timeout,
            "scenario": scen, "exec_cot": bool(args.exec_cot),
            "bundle": bundle if bundle > 1 else None,
            "problems_total": len(per_problem),
        },
        "sample_hash": hashlib.sha1(",".join(str(q) for q in sorted(
            str(p["question_id"]) for p in problems)).encode()).hexdigest()[:12],
        "counts": {d: len(v) for d, v in sorted(per_diff.items())},
        "hardest_tier": hardest_m if hardest_m["n"] else None,
        "bundle_tier": bundle_m,
        # What the sample was drawn from: rows, questions, labels, date range.
        # A 100% is only meaningful next to this.
        "pool": POOL_FACTS or None,
        "generation_stats": {
            "generated": n_gen,
            "failed_requests": failed_requests,
            "not_generated": len(missing),
            "truncated_at_max_tokens": truncated,
            "truncated_pct": round(100.0 * truncated / n_gen, 1) if n_gen else None,
            "problems_not_truncated": len(complete_rates),
            "pass_rate_when_not_truncated": round(
                100.0 * sum(complete_rates) / len(complete_rates), 1)
            if complete_rates else None,
            "empty_outputs": empty_outputs,
        },
        # SINGLE HEADLINE VALUE: pass@1 % on the sampled problem set. Only runs
        # with the same problem count / sample_hash are directly comparable.
        "score": round(metrics.get("pass@1", 0.0) * 100, 2) if metrics.get("pass@1") is not None else None,
        "metrics": metrics,
        "metrics_by_difficulty": diff_metrics,
        "speed": {
            "total_generation_seconds": round(gen_secs_total, 1),
            "total_completion_tokens": gen_tok_total,
            "mean_tokens_per_sec": round(gen_tok_total / gen_secs_total, 2)
            if gen_secs_total else None,
            "mean_seconds_per_problem": round(gen_secs_total / len(done), 1)
            if done else None,
            "avg_prompt_tokens": round(prompt_tok_total / len(done), 0)
            if (done and prompt_tok_total) else None,
            "avg_completion_tokens": round(gen_tok_total / len(done), 0) if done else None,
            "prefill_tokens_per_sec": prefill_tps,
            "decode_tokens_per_sec": decode_tps,
            "speed_probe": probe or None,
        },
        "per_problem_pass": per_problem_pass,
    }
    if per_problem:
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
    else:
        print("No evaluated problems; summary not written (probe saved if run).")

    print("\n=== Results ===")
    print(f"   model: {model_id or 'unknown'}   run: bench\\{run_slug}")
    if not per_problem:
        print("Nothing evaluated yet.")
    if summary.get("score") is not None:
        print(f"SCORE: {summary['score']}%  "
              f"({len(per_problem)} problems, {args.n} sample(s) each) "
              f"[sample {summary['sample_hash']}"
              + (f", {SAMPLE_NOTE}" if SAMPLE_NOTE else "") + "]")
    for k, v in metrics.items():
        print(f"{k:>8}: {v * 100:.2f}%")
    for d, mm in diff_metrics.items():
        if "pass@1" in mm:
            print(f"  {d:>7}: {mm['pass@1'] * 100:.2f}%  ({len(per_diff[d])} problems)")
    if hardest_m["n"] and hardest_m["n"] < len(per_problem):
        print(f"  hardest: {hardest_m['pass@1'] * 100:.2f}%"
              f"  ({hardest_m['n']} problems of the --hardest tier)")
    if bundle_m:
        print(f"  per-call: {bundle_m['per_call_pass'] * 100:.2f}% over"
              f" {bundle_m['calls']} single calls inside the"
              f" {bundle_m['k']}-call items (item = all {bundle_m['k']} right)")
    if summary.get("score") is not None:
        _sc = summary["score"]
        if _sc >= 99.0 or (scen != "codegen" and _sc >= 90.0):
            print(saturation_note(scen, _sc, len(per_problem), per_diff,
                                  hardest_m, bundle_m=bundle_m,
                                  harden_target=args.harden_target))
    spd = summary["speed"]
    if spd.get("decode_tokens_per_sec"):
        print(f"   speed: decode {spd['decode_tokens_per_sec']} tok/s, "
              f"prefill/lecture {spd['prefill_tokens_per_sec']} tok/s")
    if spd.get("mean_seconds_per_problem"):
        avg_p = spd.get("avg_prompt_tokens")
        avg_txt = f"avg {avg_p} prompt + " if avg_p else "avg ? prompt + "
        print(f"         {avg_txt}{spd['avg_completion_tokens']} completion tokens per problem, "
              f"{spd['mean_seconds_per_problem']}s/problem wall time")
    gs = summary.get("generation_stats") or {}
    if gs.get("truncated_at_max_tokens") or gs.get("failed_requests"):
        print(f"   gen: {gs.get('truncated_at_max_tokens', 0)}/{gs.get('generated', 0)} "
              f"hit the {args.max_tokens}-token cap (their answer is cut off"
              " mid-way - the partial code is still graded, and it rarely passes), "
              f"{gs.get('failed_requests', 0)} failed request(s) "
              f"(rerun the same command to retry them)")
    if gs.get("pass_rate_when_not_truncated") is not None:
        print(f"      when-complete: {gs['pass_rate_when_not_truncated']}% of the "
              f"{gs.get('problems_not_truncated')}/{gs.get('generated')} answers that fit "
              f"in the token budget passed (skill signal, budget aside)")
    probe_parts = []
    for key, where in (("start", "at start"), ("end", "at end")):
        p = harness_probe.get(key)
        if p:
            probe_parts.append(("up" if p.get("reachable")
                                else f"not answering ({p.get('error')})")
                               + f" {where}")
    print(f"   harness: {harness_val or 'unknown'} ({harness_src}"
          + (f"; {harness_note}" if harness_note else "")
          + (f" - {', '.join(probe_parts)}" if probe_parts else "") + ")")
    print(f"Summary: {summary_path}\n")

    if report_after:
        build_report(only=run_slug)
    return run_slug

# -----------------------------------------------------------------------------
# The bench console: see the scores, delete one, bring it back. Works while a
# run is going (type a line, it answers between two problems) and on its own
# with --manage. A delete never destroys anything: the folder moves to
# bench/_archive/<stamp>__<name> and restore moves it back.
# -----------------------------------------------------------------------------
ARCHIVE_DIR = os.path.join("bench", "_archive")
CONSOLE_LISTS = {"runs": None, "archive": None}

CONSOLE_HELP = """
Bench commands - type one any time (during a run, or with --manage):
  help             this list
  list             every run, numbered - same order as --report-only
  delete 2         take run 2 out of the report (numbers come from 'list')
  delete NAME      same, by folder or model name (a piece of it is enough)
  archive          the deleted runs, numbered
  restore 1        put archived run 1 back into the report
  status           how many runs and archives you have
  report           the full comparison table (what --report-only prints)
  quit             stop answering commands
A delete moves bench\\<name> to bench\\_archive\\<stamp>__<name>; nothing is
erased, and 'restore' brings it back. The run in progress cannot be deleted.
"""


def _report_key(row):
    summary = row["summary"] or {}
    score = summary.get("score")
    return (summary.get("config", {}).get("scenario", "") or "zz",
            -(score if isinstance(score, (int, float)) else -1.0), row["label"])


def console_entries(archived=False):
    """The run folders (bench/, or bench/_archive/ when asked) with their
    _summary.json contents, in report order: scenario, best score first."""
    root = ARCHIVE_DIR if archived else "bench"
    rows = []
    if not os.path.isdir(root):
        return rows
    for entry in sorted(os.listdir(root)):
        d = os.path.join(root, entry)
        if not os.path.isdir(d):
            continue
        if not archived and os.path.abspath(d) == os.path.abspath(ARCHIVE_DIR):
            continue
        sp = os.path.join(d, "_summary.json")
        summary = None
        if os.path.isfile(sp):
            try:
                with open(sp, encoding="utf-8") as f:
                    summary = json.load(f)
            except Exception:
                summary = None
        label = (summary or {}).get("name") or (
            entry.split("__", 1)[-1] if archived else entry)
        orig = entry.split("__", 1)[-1] if "__" in entry else entry
        rows.append({"path": d, "folder": entry, "label": label,
                     "orig": orig, "summary": summary})
    if archived:
        rows.sort(key=lambda r: r["folder"])
    else:
        rows.sort(key=_report_key)
    return rows


def console_print_rows(rows, title, active=None, archived=False):
    print(title)
    if not rows:
        print("     (none)")
        return rows
    for num, row in enumerate(rows, 1):
        s = row["summary"] or {}
        score = s.get("score")
        scen = (s.get("config") or {}).get("scenario") or "?"
        n = sum((s.get("counts") or {}).values())
        mark = "*" if active and os.path.abspath(row["path"]) == os.path.abspath(active) else " "
        print(f"  {mark}{num:>3}  {row['label']:<44}"
              f" {('%.1f%%' % score) if isinstance(score, (int, float)) else 'no score':>9}"
              f"  {'fast' if scen in ('exec', 'top') else 'slow'} {scen:<9}"
              f" {str(s.get('harness') or '?'):<7} {n if n else '-':>5}"
              f"  {(s.get('sample_hash') or '-')[:12]:<12}"
              f" {(s.get('generated_at') or '')[:10]}")
    if archived:
        print("     restore <number> puts one of these back into the report.")
    else:
        print("     * = the run in progress. delete <number> takes one out of the"
              " report.")
    return rows


def console_list(kind="runs"):
    """Numbered list of the runs (or of the archive), remembered so 'delete 2'
    knows what 2 was."""
    archived = (kind == "archive")
    rows = console_entries(archived=archived)
    CONSOLE_LISTS[kind] = rows
    title = ("Archived runs (restore <number> puts one back):" if archived
             else "Runs in the report:")
    return console_print_rows(rows, title, active=_CONSOLE_ACTIVE.get("run"),
                              archived=archived)


def console_find(arg, rows, what):
    """One run out of a numbered list: by its number, or by (a piece of) its
    name. Returns (row, None) or (None, complaint)."""
    if not rows:
        return None, (f"there is no {what} to pick from - 'list'"
                      " shows what exists")
    arg = str(arg).strip()
    if arg.isdigit():
        pick = int(arg)
        if 1 <= pick <= len(rows):
            return rows[pick - 1], None
        return None, f"there is no {what} number {pick} (1-{len(rows)} exist)"
    hits = [r for r in rows if r["label"].lower() == arg.lower()
            or r["folder"].lower() == arg.lower()]
    if not hits:
        hits = [r for r in rows if arg.lower() in r["label"].lower()
                or arg.lower() in r["folder"].lower()]
    if not hits:
        return None, f"no {what} matches '{arg}' - 'list' shows them numbered"
    if len(hits) > 1:
        names = ", ".join(r["label"] for r in hits[:6])
        return None, f"'{arg}' matches {len(hits)} of them ({names}) - be exact or use a number"
    return hits[0], None


def archive_run(row, active=None):
    """Move a run folder into bench/_archive. Reversible; never touches the run
    that is being written right now."""
    if active and os.path.abspath(row["path"]) == os.path.abspath(active):
        return None, ("that is the run in progress - it is being written now; stop"
                      " it first")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(ARCHIVE_DIR, f"{stamp}__{row['folder']}")
    step = 0
    while os.path.exists(dest):
        step += 1
        dest = os.path.join(ARCHIVE_DIR, f"{stamp}__{row['folder']}-{step}")
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        shutil.move(row["path"], dest)
    except OSError as exc:
        return None, f"could not move it ({type(exc).__name__}: {exc})"
    return dest, None


def restore_run(row):
    """Move an archived folder back under bench/ under its original name, so it
    counts in the report again."""
    back = os.path.join("bench", row["orig"])
    if os.path.exists(back):
        return None, (f"bench\\{row['label']} already exists - the name is taken;"
                      " delete or rename that one first")
    try:
        shutil.move(row["path"], back)
    except OSError as exc:
        return None, f"could not move it back ({type(exc).__name__}: {exc})"
    return back, None


def refresh_report_files():
    """bench/report.md + .csv rebuilt from what is left, without the table."""
    build_report(show=False)


def bench_command(line, active=None):
    """One console line. False means the console should stop listening."""
    parts = line.strip().split()
    if not parts:
        return True
    cmd, arg = parts[0].lower(), " ".join(parts[1:])
    if cmd in ("help", "?", "h"):
        print(CONSOLE_HELP)
        return True
    if cmd in ("list", "ls", "runs"):
        if arg in ("archive", "archived", "a"):
            console_list("archive")
        else:
            console_list("runs")
        return True
    if cmd in ("archive", "archived"):
        console_list("archive")
        return True
    if cmd in ("delete", "del", "rm", "remove"):
        if not arg:
            print("'delete' needs to know which one: 'list', then 'delete 2' (or"
                  " 'delete <name>').")
            return True
        rows = CONSOLE_LISTS["runs"]
        if rows is None:
            rows = console_list("runs")
        row, complaint = console_find(arg, rows, "run")
        if complaint:
            print(f"     {complaint}")
            return True
        dest, complaint = archive_run(row, active)
        if complaint:
            print(f"     not deleted: {complaint}")
            return True
        print(f"     deleted {row['label']}: moved to {dest}")
        print("     the report is rebuilt without it; 'archive' lists what is"
              " there, 'restore <number>' brings one back.")
        CONSOLE_LISTS["runs"] = None
        refresh_report_files()
        return True
    if cmd in ("restore", "undelete"):
        if not arg:
            console_list("archive")
            print("     'restore <number>' brings one of those back into the report.")
            return True
        rows = CONSOLE_LISTS["archive"]
        if rows is None:
            rows = console_list("archive")
        row, complaint = console_find(arg, rows, "archived run")
        if complaint:
            print(f"     {complaint}")
            return True
        back, complaint = restore_run(row)
        if complaint:
            print(f"     not restored: {complaint}")
            return True
        print(f"     restored {row['label']} -> {back}, it counts in the report"
              " again")
        CONSOLE_LISTS["runs"] = None
        CONSOLE_LISTS["archive"] = None
        refresh_report_files()
        return True
    if cmd in ("report", "table"):
        build_report()
        return True
    if cmd == "status":
        runs = console_entries()
        archives = console_entries(archived=True)
        scored = [r for r in runs if isinstance((r["summary"] or {}).get("score"),
                                               (int, float))]
        print(f"     {len(runs)} run(s) under bench/ ({len(scored)} with a score),"
              f" {len(archives)} archived. Reports: bench/report.md,"
              " bench/report.csv")
        return True
    if cmd in ("quit", "exit", "q"):
        return False
    print(f"     Unknown command '{cmd}'. Type help.")
    return True


_CONSOLE_ACTIVE = {"run": None}

try:
    import msvcrt as _msvcrt
except ImportError:          # not Windows
    _msvcrt = None


def _key_ready():
    """Is a line waiting on the keyboard? Never blocks, and says no when there
    is no keyboard to listen to (piped run, Start-Process, agent chat)."""
    if not CONSOLE_LIVE:
        return False
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    try:
        if _msvcrt is not None:
            return bool(_msvcrt.kbhit())
        import select
        return bool(select.select([sys.stdin], [], [], 0)[0])
    except Exception:
        return False


def console_tick(active=None):
    """One look at the keyboard between two problems: if you typed a bench
    command, it runs now. Costs nothing when nothing was typed."""
    global CONSOLE_LIVE
    if not _key_ready():
        return
    try:
        line = input("bench> ")
    except EOFError:
        CONSOLE_LIVE = False
        print("     (no keyboard to type to - the console is off; --manage opens"
              " it without a run)")
        return
    except KeyboardInterrupt:
        print("\n     (console: Ctrl-C again to stop the run)")
        return
    print(f"bench> {line}")
    if not bench_command(line, active):
        CONSOLE_LIVE = False
        print("     console off; 'help' turns it back on (any key, then help)")


def console_loop(active=None):
    """The bench console on its own (--manage): commands until you leave."""
    print(CONSOLE_HELP)
    console_list("runs")
    while True:
        try:
            line = input("bench> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not bench_command(line, active):
            return


# -----------------------------------------------------------------------------
# Cross-model report
# -----------------------------------------------------------------------------


def build_report(only=None, show=True):
    """Print the cross-run comparison table and rewrite bench/report.md/.csv.

    only: one run name (or a list of them) - the table then holds just that
    row, which is what a finished run prints. bench/report.md/.csv are always
    written from every run; --report-only prints all of them.
    """
    rows = []
    if os.path.isdir("bench"):
        for entry in sorted(os.listdir("bench")):
            sp = os.path.join("bench", entry, "_summary.json")
            if os.path.isfile(sp):
                with open(sp, encoding="utf-8") as f:
                    rows.append(json.load(f))
    if not rows:
        print("No summaries found under bench/ yet.")
        return
    rows.sort(key=lambda r: (r.get("config", {}).get("scenario", ""),
                             -(r.get("score") or 0.0)))
    wanted = None
    if only is not None:
        wanted = {only} if isinstance(only, str) else set(only)
    headers = ["run", "model", "harness", "test", "scenario", "SCORE", "problems", "n", "bundle", "cap",
               "trunc%", "when-complete", "easy", "medium", "hard", "hardest",
               "prefill-tok/s", "gen-tok/s", "gen-min", "sample", "date"]
    table = []
    for r in rows:
        m = r.get("metrics", {})
        dm = r.get("metrics_by_difficulty", {})
        sp = r.get("speed", {})
        cfg = r.get("config", {})
        gs = r.get("generation_stats", {})

        def fmt_pct(x):
            return f"{x * 100:.1f}" if isinstance(x, (int, float)) else "-"

        total = sum(r.get("counts", {}).values())
        decode = sp.get("decode_tokens_per_sec") or sp.get("mean_tokens_per_sec")
        scen = cfg.get("scenario", "codegen")
        b_tier = r.get("bundle_tier") or {}
        bundle_col = (f"x{cfg['bundle']}" if cfg.get("bundle") else
                      (f"x{b_tier.get('k')}" if b_tier.get("k") else "-"))
        table.append([
            r["name"], r.get("model") or r.get("server_model") or "-",
            r.get("harness") or "unknown",
            "fast" if scen in ("exec", "top") else "slow", scen,
            r.get("score"), total, cfg.get("n"),
            bundle_col,
            cfg.get("max_tokens"),
            gs.get("truncated_pct"),
            gs.get("pass_rate_when_not_truncated", "-") if gs.get("pass_rate_when_not_truncated") is not None else "-",
            fmt_pct(dm.get("easy", {}).get("pass@1")),
            fmt_pct(dm.get("medium", {}).get("pass@1")),
            fmt_pct(dm.get("hard", {}).get("pass@1")),
            fmt_pct((r.get("hardest_tier") or {}).get("pass@1")),
            sp.get("prefill_tokens_per_sec"),
            decode,
            round((sp.get("total_generation_seconds") or 0) / 60, 1),
            r.get("sample_hash") or "-",
            (r.get("generated_at") or "")[:10],
        ])
    all_tables = table
    table = (all_tables if wanted is None else
             [row for row in all_tables if row[0] in wanted])

    def render(rows_to_render):
        if not rows_to_render:
            return ""
        widths = [max(len(str(h)), *(len(str(r[i])) for r in rows_to_render))
                  for i, h in enumerate(headers)]
        header_line = "  ".join(f"{h:<{w}}" for h, w in zip(headers, widths))
        return "\n".join(["\n" + header_line, "-" * len(header_line)] +
                         ["  ".join(f"{str(v):<{w}}" for v, w in zip(r, widths))
                          for r in rows_to_render])

    text = render(table)
    full_text = render(all_tables)
    if show:
        if wanted is None:
            print("LiveCodeBench comparison:")
        elif len(wanted) == 1:
            print(f"This run ({', '.join(sorted(wanted))}) - every run side by side:"
                  " python lcb_bench.py --report-only")
        else:
            print(f"These runs ({', '.join(sorted(wanted))}) - every run side by"
                  " side: python lcb_bench.py --report-only")
        print(text)
        if wanted is not None and not table:
            print("     (no score recorded for it yet)")
    if show and wanted is None:
        print("\nSCORE = pass@1 % (the single number: higher is better). run = bench folder"
              "\nlabel, model = the model id the server reported for that run. when-complete ="
              "\npass rate over answers that did not hit the token cap. test = slow"
              "\n(code_generation, the main scenario) or fast (code_execution /"
              "\ntest_output_prediction - narrower skills, much quicker, comparable"
              "\nacross models but only against the same scenario's rows). harness = did an"
              "\nagent chat share the model server for that run; every run answers that"
              "\nquestion out loud (--harness yes|no when detached), it is never guessed,"
              "\nbecause those requests queue behind the benchmark and inflate"
              "\ngen-min. It never means the agent answered, and nothing is routed through"
              "\nit: no harness sees a problem, so both rows are the raw model. unknown ="
              "\nnobody answered (older run, or a detached run without --harness) - set it"
              "\nwith --set-harness. hardest = pass rate over the hardest tier of"
              "\nthat sample alone (--hardest N, or the 'hardest' share of a --mix) - '-' when the run had no such tier. bundle = xK"
              "\n= a fast row whose items bundle K calls in one answer, all-or-nothing (--bundle K); '-' = one-call items. Rows are"
              "\ndirectly comparable only when test / scenario / problems / n / bundle / cap /"
              "\nsample columns match.")
    os.makedirs("bench", exist_ok=True)
    with open("bench/report.md", "w", encoding="utf-8") as f:
        f.write("# LiveCodeBench model comparison\n\n```\n" + full_text + "\n```\n\n"
                "SCORE = pass@1 % on the sampled problem set - the single headline value.\n"
                "run = bench folder label; model = the model id the server reported for\n"
                "that run, so two rows for the same model are always the same model.\n"
                "prefill-tok/s = prompt-reading (lecture) speed; gen-tok/s = generation speed.\n"
                "cap = per-answer max_tokens (thinking budget); trunc% = share of answers that hit it.\n"
                "when-complete = pass rate over the answers that did NOT hit the cap (skill signal).\n"
                "hardest = pass rate over the --hardest tier of that sample alone (the N hardest\n"
                "problems of --hardest, or the 'hardest' share of a --mix, saved in that run's hardest_ids.json); '-' when the run had no\n"
                "such tier.\n"
                "test = slow (code_generation, the main scenario) or fast (code_execution /\n"
                "test_output_prediction): fast rows measure narrower skills, run much quicker,\n"
                "and compare across models only against the same scenario's own rows. A fast row a strong model clears is a ceiling check, not a ranking: those pools are small and old, and --pool-info prints what a pool can be sampled from.\n"
                "harness = did an agent chat share the model server during that run: every\n"
                "run answers that question out loud (never guessed), because those requests\n"
                "queue behind the benchmark and inflate gen-min (wall time) of harness=yes\n"
                "rows. It never means the agent answered, and nothing is routed\n"
                "through it: no harness sees a problem, so every row is the raw model\n"
                "answering for itself. A harness address given in the note is pinged at\n"
                "the start and end of the run and saved in that run's _summary.json as\n"
                "harness_probe; a name or path is stored as text only.\n"
                "unknown = nobody answered (older run, or a detached run started\n"
                "without --harness) - backfill it with --set-harness yes|no --name <run>.\n"
                "bundle = xK on a fast row: its items carry K calls answered in one go,\n"
                "all-or-nothing (--bundle K); '-' = classic one-call items.\n"
                "Rows are directly comparable only when test / scenario / problems / n /\n"
                "bundle / cap / sample columns match.\n")
    with open("bench/report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(all_tables)
    if show:
        print("Report saved: bench/report.md, bench/report.csv")


# -----------------------------------------------------------------------------
# Self test (no model server needed)
# -----------------------------------------------------------------------------


def self_test():
    timeout = 2
    cases = []

    def case(name, in_out, codes, expect):
        cases.append((name, in_out, codes, expect))

    stdio_in_out = json.dumps({
        "inputs": ["3 4\n", "10 20\n", "1 1\n"],
        "outputs": ["7", "30", "2"],
        "fn_name": None,
    })
    call_in_out = json.dumps({
        # call-based format: each input string is ONE test case; each LINE of it
        # is one JSON-encoded argument passed to fn_name(*args).
        "inputs": ["1\n2", "3\n4"],
        "outputs": ["3", "7"],
        "fn_name": "plus",
    })
    case("stdio good", stdio_in_out,
         ["import sys\ndata = sys.stdin.read().split()\nprint(int(data[0]) + int(data[1]))\n"], [True])
    case("stdio wrong", stdio_in_out, ["print(42)\n"], [False])
    case("stdio infinite loop (watchdog)", stdio_in_out, ["while True:\n    pass\n"], [False])
    case("stdio runtime error", stdio_in_out, ["raise ValueError('boom')\n"], [False])
    case("call good", call_in_out, ["def plus(a, b):\n    return a + b\n"], [True])
    case("call wrong", call_in_out, ["def plus(a, b):\n    return a - b\n"], [False])
    case("call missing function", call_in_out, ["print('hi')\n"], [False])
    case("mixed multi-sample", stdio_in_out, [
        "import sys\ndata = sys.stdin.read().split()\nprint(int(data[0]) + int(data[1]))\n",
        "print(42)\n",
        "while True:\n    pass\n",
    ], [True, False, False])

    ok = True
    for name, in_out, codes, expected in cases:
        t0 = time.time()
        rec = eval_one_problem(("selftest", in_out, codes, timeout))
        flags = [bool(r) and all(x > 0 for x in r) for r in rec["results"]]
        status = "OK " if flags == expected else "FAIL"
        if flags != expected:
            ok = False
        print(f"[{status}] {name}: got {flags} want {expected} "
              f"({time.time() - t0:.1f}s)")
        for m in rec["meta"]:
            print(f"      meta: code={m.get('error_code')} msg={m.get('error_message')!r}")
    # --- scenario checks: code_execution + test_output_prediction -----------
    exec_payload = json.dumps(
        {"code": "def f(x):\n    return x * 2\n", "input": "f(3)", "output": "6"})
    t0 = time.time()
    rec = eval_one_problem(("selftest", exec_payload, ["6", "7", "f(3)"], timeout, "exec"))
    flags = [bool(r[0]) for r in rec["results"]]
    want = [True, False, False]
    sok = flags == want and rec.get("skipped") == [False, False, True]
    ok = ok and sok
    print(f"[{'OK ' if sok else 'FAIL'}] exec scenario (numpy/pandas warm child): "
          f"got {flags} skipped={rec.get('skipped')} want {want} "
          f"({time.time() - t0:.1f}s)")

    top_payload = json.dumps({"expected": '"ab"'})
    preds = [
        'assert func("a") == "ab"',
        'assert func("a") == "a"',
        "Here you go:\n```python\nassert func('a') == 'ab'\n```",
    ]
    rec = eval_one_problem(("selftest", top_payload, preds, timeout, "top"))
    flags = [bool(r[0]) for r in rec["results"]]
    want = [True, False, True]
    tok_ok = flags == want
    ok = ok and tok_ok
    print(f"[{'OK ' if tok_ok else 'FAIL'}] top scenario: got {flags} want {want}")

    # --- --bundle checks: K calls in one answer, all-or-nothing -------------
    xb_payload = json.dumps({"items": [
        {"code": "def f(x):\n    return x * 2\n", "input": "f(3)", "output": "6"},
        {"code": "def h(x):\n    return x + 1\n", "input": "h(4)", "output": "5"},
    ]})
    xb_answers = [
        ["6", "5"],        # all right -> item right
        ["6", "4"],        # one wrong -> item wrong (that is the whole point)
        ["6"],             # missing answer -> wrong
        ["f(3)", "5"],     # echoes an input -> skipped, official quirk
    ]
    rec = eval_one_problem(("selftest", xb_payload, xb_answers, timeout, "execb"))
    flags = [bool(r[0]) for r in rec["results"]]
    want = [True, False, False, False]
    bok = flags == want and rec.get("skipped") == [False, False, False, True]
    subs_ok = (rec["sub"][0] == [True, True] and rec["sub"][1] == [True, False])
    ok = ok and bok and subs_ok
    print(f"[{'OK ' if bok and subs_ok else 'FAIL'}] exec bundle (all-or-nothing"
          f" + per-call sub): got {flags} skipped={rec.get('skipped')} want {want}"
          f" sub0={rec['sub'][0]} sub1={rec['sub'][1]}")

    tb_payload = json.dumps({"items": [{"expected": "6"}, {"expected": "7"}]})
    tb_preds = [
        ["assert f(3) == 6", "assert g(2) == 7"],
        ["assert f(3) == 6", "assert g(2) == 8"],
        ["assert f(3) == 6"],
    ]
    rec = eval_one_problem(("selftest", tb_payload, tb_preds, timeout, "topb"))
    flags = [bool(r[0]) for r in rec["results"]]
    want = [True, False, False]
    tb_ok = flags == want
    ok = ok and tb_ok
    print(f"[{'OK ' if tb_ok else 'FAIL'}] top bundle (all-or-nothing):"
          f" got {flags} want {want}")

    xb_extract = [
        ("execb in-order block",
         extract_exec_bundle_answers(
             "[ANSWER]\nassert f(3) == 6\nassert h(4) == 5\n[/ANSWER]",
             ["f(3)", "h(4)"]), ["6", "5"]),
        ("execb matched by call text",
         extract_exec_bundle_answers(
             "[ANSWER]\nassert h(4) == 5\nassert f(3) == 6\n[/ANSWER]",
             ["f(3)", "h(4)"]), ["6", "5"]),
        ("execb cot block",
         extract_exec_bundle_answers(
             "step by step...\n[ANSWER]\nassert f(3) == 6\nassert h(4) == 5\n"
             "[/ANSWER]", ["f(3)", "h(4)"], cot=True), ["6", "5"]),
        ("execb missing stays empty",
         extract_exec_bundle_answers("[ANSWER]\nassert f(3) == 6\n[/ANSWER]",
                                     ["f(3)", "h(4)"]), ["6", ""]),
        ("topb assert lines",
         extract_top_bundle_answers(
             "here:\n```python\nassert f(3) == 6\nassert g(2) == 7\n```",
             ["f(3)", "g(2)"]),
         ["assert f(3) == 6", "assert g(2) == 7"]),
    ]
    for name, got, want in xb_extract:
        eok = got == want
        ok = ok and eok
        print(f"[{'OK ' if eok else 'FAIL'}] {name}: got {got!r} want {want!r}")

    fake_rows = ([{"question_id": f"7#{i}", "difficulty": "medium"}
                  for i in range(5)]
                 + [{"question_id": f"8#{i}", "difficulty": "easy"}
                    for i in range(3)])
    chunks = _bundle_chunks(fake_rows, 2)
    bok2 = ([c[2][0]["question_id"] for c in chunks] == ["7#0", "7#2", "8#0"]
            and all(len(c[2]) == 2 for c in chunks))
    ok = ok and bok2
    print(f"[{'OK ' if bok2 else 'FAIL'}] bundle chunking is deterministic and"
          f" drops the leftover of a question (5+3 rows -> 3 pairs)")

    checks = [
        ("exec-extract direct",
         extract_exec_answer('assert f(3) == 6\ntrailing noise', cot=False), "6"),
        ("exec-extract cot",
         extract_exec_answer('thinking...\n[ANSWER]\nassert f(3) == 42\n[/ANSWER]', cot=True), "42"),
        ("top-extract assert line",
         extract_top_answer('blah\nassert func(1) == 2\ntrailing'), "assert func(1) == 2"),
        ("top-extract fence",
         extract_top_answer('Here:\n```python\nassert func(1) == 2\n```'), "assert func(1) == 2"),
    ]
    for name, got, want in checks:
        eok = got == want
        ok = ok and eok
        print(f"[{'OK ' if eok else 'FAIL'}] {name}: got {got!r} want {want!r}")

    print("\nSelf-test:", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    main()

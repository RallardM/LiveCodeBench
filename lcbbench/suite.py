"""The one-command suites.

  --suite fast   4 skills (code, math, science, reading) x 4 tiers (easy,
                 medium, hard, hardest), short answer caps, 120 min in total
  --suite long   code generation x 4 tiers, full answer cap, 300 min in total

Both are time-boxed: items come in one fixed order (round r = item r of every
skill x tier cell, easy first), generation stops starting new items when the
budget is spent, and whatever finished is graded. The score is the mean of the
cell scores, so every skill x tier cell weighs the same. With --both the second
pass (agent chat sharing the server) replays exactly the items the first pass
finished."""

import datetime as dt
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from . import state
from .console import console_tick
from .extract import extract_code, extract_exec_answer, extract_exec_bundle_answers
from .generate import (ensure_speed_probe, generate_one, generation_stats,
                       load_gen_checkpoint, speed_summary)
from .grading import eval_one_problem
from .harness import begin_pass, end_probe, harness_line, resolve_run_dir
from .pipeline import print_speed_lines
from .report import build_report
from .scoring import count_pass
from .suite_data import SEED, SKILLS, TIERS, build_pools, item_sequence

SUITES = {
    "fast": {"skills": ("code", "math", "science", "reading"), "budget_min": 120,
             "caps": {"easy": 3072, "medium": 4096, "hard": 6144, "hardest": 8192},
             "rounds": 12},
    "long": {"skills": ("code",), "budget_min": 300, "caps": None, "rounds": 40},
}


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _pct(x):
    return f"{x * 100:5.1f}%" if x is not None else "    - "


def extract_for(item, out):
    """The answer of one item, in the shape its grader wants."""
    kind = item["eval_kind"]
    if kind == "codegen":
        return extract_code(out, "auto")
    if kind == "exec":
        return extract_exec_answer(out, False)
    if kind == "execb":
        return extract_exec_bundle_answers(out, item.get("inputs") or [], False)
    return out   # math and mcq graders read the raw text


def pass_budget_seconds(clock):
    """This pass's share of the command's total time. One pass gets it all; with
    --both pass 1 gets half and pass 2 gets whatever is left after pass 1
    (grading included)."""
    total = clock["total_s"]
    if clock["passes"] == 1:
        return total
    if clock["pass_index"] == 0:
        return total * 0.5
    return max(60.0, total - (time.time() - clock["start"]))


def run_suite(args, kind, base, model_id, harness_val, harness_note, harness_src,
              clock, report_after=True):
    """One pass of a suite. Returns the run folder name."""
    cfg = SUITES[kind]
    skills = list(args.skills_parsed or cfg["skills"])
    run_slug = resolve_run_dir(base, kind, harness_val)
    run_dir = os.path.join("bench", run_slug)
    harness_note, harness_probe, _ = begin_pass(
        run_slug, run_dir, model_id, harness_val, harness_note, harness_src)
    ensure_speed_probe(args.base_url, model_id, run_dir)
    if args.probe_only:
        print("--probe-only: stopping here, nothing generated.")
        return None

    rounds = min(cfg["rounds"], args.rounds) if args.rounds else cfg["rounds"]
    pools = build_pools(skills, args.release, args.start_date, apex=args.apex)
    skills = [s for s in skills if s in pools]
    seq = item_sequence(pools, rounds)
    by_id = {it["question_id"]: it for it in seq}

    replay = None
    if harness_val == "yes":
        first = os.path.join("bench", resolve_run_dir(base, kind, "no"),
                             "suite_items.json")
        if os.path.isfile(first):
            with open(first, encoding="utf-8") as f:
                keep = set(json.load(f))
            seq = [it for it in seq if it["question_id"] in keep]
            replay = first
            print(f"Replay: pass 1 finished {len(keep)} items, this pass runs"
                  " exactly those, in the same order.")
    print(f"Suite {kind}: {len(seq)} items planned over {len(skills)} skill(s), up to"
          f" {rounds} rounds, skills {', '.join(skills)}")

    # ---- time budget ------------------------------------------------------
    pass_s = pass_budget_seconds(clock)
    state_path = os.path.join(run_dir, "suite_state.json")
    spent = 0.0
    if os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                spent = float(json.load(f).get("spent_s", 0.0))
        except Exception:
            spent = 0.0
    left = max(0.0, pass_s - spent)
    print(f"Time budget of this pass: {pass_s / 60:.0f} min generation"
          + (f" ({spent / 60:.0f} min already spent by an earlier session)"
             if spent else "")
          + ", plus grading. New items stop starting when it is spent.")

    # ---- generation -------------------------------------------------------
    gen_path = os.path.join(run_dir, "generations.jsonl")
    done = load_gen_checkpoint(gen_path)
    tasks = [it for it in seq if (it["question_id"], 0) not in done]
    if done:
        print(f"Resume: {len(done)} generations already done")
    caps = cfg["caps"]

    def cap_of(item):
        return min(caps[item["tier"]], args.max_tokens) if caps else args.max_tokens

    deadline = time.time() + left
    if tasks and left > 0:
        base_extras = {"temperature": args.temperature, "top_p": args.top_p}
        if args.extra_body:
            base_extras.update(json.loads(args.extra_body))

        def guarded(item):
            if time.time() >= deadline:
                return None
            extras = dict(base_extras, max_tokens=cap_of(item))
            return generate_one((item["question_id"], 0, item["messages"]),
                                args.base_url, model_id, extras, 3,
                                args.request_timeout)

        print(f"Generating up to {len(tasks)} items with {args.workers} worker(s)...")
        t0 = time.time()
        got = 0
        with open(gen_path, "a", encoding="utf-8") as out:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [pool.submit(guarded, it) for it in tasks]
                for it, fut in zip(tasks, futures):
                    rec = fut.result()
                    if rec is None:
                        continue
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    out.flush()
                    got += 1
                    tok, secs = rec.get("completion_tokens"), rec.get("seconds")
                    tps = f" {tok / secs:6.1f} tok/s" if (tok and secs) else ""
                    fail = " FAILED" if rec.get("failed") else ""
                    print(f"[gen {got}] {it['skill']}/{it['tier']} {secs}s{tps}{fail}"
                          f"  ({(time.time() - t0) / 60:.0f} of {left / 60:.0f} min)",
                          flush=True)
                    console_tick(run_dir)
        spent += time.time() - t0
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"spent_s": round(spent, 1)}, f)
        if got < len(tasks):
            print(f"Time budget reached: {got} of {len(tasks)} pending items were"
                  " generated, the rest is left out of the score.")
        print(f"Generation phase done in {(time.time() - t0) / 60:.1f} min")
    elif tasks:
        print("The time budget of this pass is already spent: grading what exists.")
    done = load_gen_checkpoint(gen_path)

    finished = [it for it in seq if (it["question_id"], 0) in done]
    with open(os.path.join(run_dir, "suite_items.json"), "w", encoding="utf-8") as f:
        json.dump([it["question_id"] for it in finished], f)
    if not finished:
        print("Nothing was generated, so there is nothing to score.")
        return None

    # ---- grading ----------------------------------------------------------
    eval_path = os.path.join(run_dir, "eval_results.jsonl")
    evaluated = {}
    if os.path.exists(eval_path):
        with open(eval_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    evaluated[rec["qid"]] = rec
    jobs = []
    for it in finished:
        qid = it["question_id"]
        if qid in evaluated:
            continue
        out = done[(qid, 0)]["output"]
        jobs.append((qid, it["eval_payload"], [extract_for(it, out)], args.timeout,
                     it["eval_kind"]))
    if jobs:
        print(f"Grading {len(jobs)} items with {args.eval_workers} workers...")
        t_eval = time.time()
        with open(eval_path, "a", encoding="utf-8") as out:
            with ThreadPoolExecutor(max_workers=args.eval_workers) as pool:
                for n_done, rec in enumerate(pool.map(eval_one_problem, jobs), 1):
                    evaluated[rec["qid"]] = rec
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    out.flush()
                    if n_done % 10 == 0 or n_done == len(jobs):
                        print(f"[eval {n_done}/{len(jobs)}] "
                              f"elapsed {(time.time() - t_eval) / 60:.1f} min",
                              flush=True)
                    console_tick(run_dir)

    # ---- scores -----------------------------------------------------------
    cells, per_problem_pass = {}, {}
    for it in finished:
        rec = evaluated.get(it["question_id"])
        if not rec:
            continue
        n, c = count_pass(rec)
        p = c / n if n else 0.0
        per_problem_pass[it["question_id"]] = p
        cells.setdefault((it["skill"], it["tier"]), []).append(p)
    if not cells:
        print("Nothing was graded, so there is nothing to score.")
        return None
    cell_mean = {k: _mean(v) for k, v in cells.items()}
    total = _mean(list(cell_mean.values()))
    by_tier = {t: _mean([cell_mean.get((s, t)) for s in skills]) for t in TIERS}
    by_skill = {s: _mean([cell_mean.get((s, t)) for t in TIERS]) for s in skills}
    cells_total = len(skills) * len(TIERS)
    rounds_done = min(len(v) for v in cells.values()) if len(cells) == cells_total else 0
    n_items = len(per_problem_pass)

    end_probe(harness_val, harness_note, harness_probe)
    gs = generation_stats(done, gen_path, args.max_tokens, per_problem_pass,
                          len(seq) - len(finished))
    speed = speed_summary(done, run_dir)
    tier_counts = {t: sum(len(v) for (s, tt), v in cells.items() if tt == t)
                   for t in TIERS}
    cap_label = (f"{min(caps.values())}-{min(max(caps.values()), args.max_tokens)}"
                 if caps else args.max_tokens)
    design = "|".join([kind, SEED, ",".join(skills), str(rounds), str(args.start_date),
                       str(bool(args.apex))])
    summary = {
        "name": run_slug, "model": model_id, "server_model": model_id,
        "harness": harness_val or "unknown", "harness_note": harness_note,
        "harness_source": harness_src, "harness_probe": harness_probe or None,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "config": {
            "n": 1, "temperature": args.temperature, "top_p": args.top_p,
            "max_tokens": cap_label, "release": args.release,
            "start_date": args.start_date, "scenario": kind, "skills": skills,
            "budget_min": round(clock["total_s"] / 60), "rounds": rounds,
            "apex": bool(args.apex), "bundle": None,
            "problems_total": n_items,
        },
        "sample_hash": hashlib.sha1(design.encode()).hexdigest()[:12],
        "counts": {t: v for t, v in tier_counts.items() if v},
        "hardest_tier": ({"n": tier_counts["hardest"], "pass@1": round(by_tier["hardest"], 4)}
                         if by_tier.get("hardest") is not None else None),
        "suite": {
            "cells": {f"{s}/{t}": {"n": len(v), "passed": round(sum(v), 2)}
                      for (s, t), v in sorted(cells.items())},
            "cells_total": cells_total, "cells_covered": len(cells),
            "rounds_done": rounds_done, "items_finished": n_items,
            "items_planned": len(seq), "pass_budget_s": round(pass_s),
            "spent_s": round(spent), "replay_of": replay,
        },
        "generation_stats": gs,
        "score": round(total * 100, 2),
        "metrics": {"pass@1": total},
        "metrics_by_difficulty": {t: {"pass@1": v} for t, v in by_tier.items()
                                  if v is not None},
        "metrics_by_skill": {s: v for s, v in by_skill.items() if v is not None},
        "speed": speed,
        "per_problem_pass": per_problem_pass,
    }
    summary_path = os.path.join(run_dir, "_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\n=== Results ({kind} suite) ===")
    print(f"   model: {model_id or 'unknown'}   run: bench\\{run_slug}")
    print(f"SCORE: {summary['score']}%  ({n_items} of {len(seq)} planned items,"
          f" {len(cells)}/{cells_total} cells, {rounds_done} full round(s))"
          f" [design {summary['sample_hash']}]")
    print("  " + " " * 9 + "".join(f"{t:>15}" for t in TIERS) + f"{'all tiers':>11}")
    for s in skills:
        row = f"  {s:<9}"
        for t in TIERS:
            v = cells.get((s, t))
            row += (f"{cell_mean[(s, t)] * 100:5.1f}% {int(round(sum(v)))}/{len(v)}".rjust(15)
                    if v else f"{'-':>15}")
        print(row + f"{_pct(by_skill.get(s)):>11}")
    print("  " + f"{'all skills':<9}" + "".join(f"{_pct(by_tier.get(t)):>15}" for t in TIERS)
          + f"{_pct(total):>11}")
    if len(cells) < cells_total:
        missing = [f"{s}/{t}" for s in skills for t in TIERS if (s, t) not in cells]
        print(f"      no item finished for: {', '.join(missing)} - the budget ended"
              " before the first round completed, so SCORE covers fewer cells")
    print_speed_lines(speed, gs, cap_label)
    print(harness_line(harness_val, harness_src, harness_note, harness_probe))
    print(f"Summary: {summary_path}\n")
    if report_after:
        build_report(only=run_slug)
    return run_slug

"""The cross-run comparison table: one row per run, best first, written to
bench/report.md and bench/report.csv."""

import csv
import json
import os

SKILLS = ("code", "math", "science", "reading")


def _fmt_pct(x):
    return f"{x * 100:.1f}" if isinstance(x, (int, float)) else "-"


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
    headers = ["run", "model", "harness", "test", "scenario", "SCORE", "problems", "n",
               "bundle", "cap", "trunc%", "when-complete", "easy", "medium", "hard",
               "hardest", "code", "math", "science", "reading", "prefill-tok/s",
               "gen-tok/s", "gen-min", "sample", "date"]
    table = []
    for r in rows:
        dm = r.get("metrics_by_difficulty", {})
        sp = r.get("speed", {})
        cfg = r.get("config", {})
        gs = r.get("generation_stats", {})
        skills = r.get("metrics_by_skill") or {}
        total = sum(r.get("counts", {}).values())
        decode = sp.get("decode_tokens_per_sec") or sp.get("mean_tokens_per_sec")
        scen = cfg.get("scenario", "codegen")
        b_tier = r.get("bundle_tier") or {}
        bundle_col = (f"x{cfg['bundle']}" if cfg.get("bundle") else
                      (f"x{b_tier.get('k')}" if b_tier.get("k") else "-"))
        table.append([
            r["name"], r.get("model") or r.get("server_model") or "-",
            r.get("harness") or "unknown",
            "fast" if scen in ("exec", "top", "fast") else "slow", scen,
            r.get("score"), total, cfg.get("n"),
            bundle_col,
            cfg.get("max_tokens"),
            gs.get("truncated_pct"),
            gs.get("pass_rate_when_not_truncated", "-")
            if gs.get("pass_rate_when_not_truncated") is not None else "-",
            _fmt_pct(dm.get("easy", {}).get("pass@1")),
            _fmt_pct(dm.get("medium", {}).get("pass@1")),
            _fmt_pct(dm.get("hard", {}).get("pass@1")),
            _fmt_pct((r.get("hardest_tier") or {}).get("pass@1")),
        ] + [_fmt_pct(skills.get(s)) for s in SKILLS] + [
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
        print("\n" + REPORT_LEGEND)
    os.makedirs("bench", exist_ok=True)
    with open("bench/report.md", "w", encoding="utf-8") as f:
        f.write("# LiveCodeBench model comparison\n\n```\n" + full_text + "\n```\n\n"
                + REPORT_LEGEND + "\n")
    with open("bench/report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(all_tables)
    if show:
        print("Report saved: bench/report.md, bench/report.csv")


REPORT_LEGEND = (
    "SCORE = pass@1 % (the single headline number, higher is better). run = bench\n"
    "folder label; model = the model id the server reported for that run.\n"
    "test = slow (code_generation and the long suite) or fast (the fast suite and\n"
    "the code_execution / test_output_prediction scenarios). Fast rows compare only\n"
    "with fast rows of the same scenario.\n"
    "Suite rows (scenario fast / long): SCORE is the mean of the tier cells, every\n"
    "skill x tier cell weighs the same; easy / medium / hard / hardest are the tier\n"
    "scores over all skills, code / math / science / reading the skill scores over\n"
    "all tiers; problems = items actually finished inside the time budget.\n"
    "prefill-tok/s = prompt-reading speed, gen-tok/s = generation speed, both from\n"
    "the server's own timings. cap = per-answer max_tokens; trunc% = share of answers\n"
    "that hit it; when-complete = pass rate over the answers that did NOT hit it.\n"
    "harness = did an agent chat share the model server during that run (asked out\n"
    "loud, never guessed): its requests queue behind the benchmark and inflate gen-min.\n"
    "Nothing is routed through the harness, so both rows are the raw model. unknown =\n"
    "nobody answered - set it with --set-harness. bundle = xK: items of K calls\n"
    "answered in one go, all-or-nothing (--bundle K).\n"
    "Rows are directly comparable only when test / scenario / problems / n / bundle /\n"
    "cap / sample columns match (suite rows: same scenario and about the same\n"
    "problems, the sample is a fixed order and a faster model just goes further).\n"
)

# Benchmarking your local LLMs on LiveCodeBench

> Quick path for any model, no per-model setup: [README_MINIMAL.md](README_MINIMAL.md).
> This guide = per-model commands and the reasoning behind them.

**Tool: `lcb_bench.py`** — a fast, resumable, Windows-native LiveCodeBench
`code_generation_lite` harness for any OpenAI-compatible local server
(llama.cpp / llama-server, LM Studio, vLLM, Ollama with OpenAI shim).

It gives you one **single headline score per model** (`SCORE` = pass@1 %
on a fixed, difficulty-stratified problem set) plus **speed**:
`prefill-tok/s` (prompt "lecture" speed) and `gen-tok/s` (generation speed).

## Why not the other options?

| Option | Problem |
|---|---|
| `lcb_llama.py` (yours) | Generates answers but **never scores them**; raw prompt, no format instructions (hurts scores); no code extraction; no resume. |
| Official `lcb_runner/` (this repo) | Its `OpenAIRunner` **hardcodes api.openai.com** (can't point at llama.cpp) and its evaluator uses `signal.SIGALRM`, which **does not exist on Windows**. |
| `lcb_bench.py` | Official chat prompt + official grading (`grade_stdio` / `grade_call_based`) ported to Windows child-process watchdogs; checkpoints/resume; pass@1/pass@k; per-difficulty split; server-accurate speed metrics; cross-model report. |

## Your models — run this once per model

Start the server (your usual command), then run the bench. In these examples
`python` means **`.venv\Scripts\python.exe`** (the venv in this repo). With
`-np 1` keep `--workers 1` — more workers don't add throughput on a single slot.

```
# Qwen3.8-Flash-Next Q2_K_XL (131k ctx)
llama-server -hf unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL -ncmoe 45 --load-mode none --no-mmproj -ctk q8_0 -ctv q8_0 -fit off -kvu -np 1 --min-p 0 --override-kv "qwen4exp.attention.indexer.top_k=int:4096" --temp 1 --top-k 20 -c 131072 -t 10 -fa on --no-context-shift
python lcb_bench.py --name qwen38-flash-next-q2 --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8

# Qwen3.8-Flash-Next IQ4_XS
llama-server -hf unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS -cmoe --load-mode none --no-mmproj -ctk q8_0 -ctv q8_0 -fit off -kvu -np 1 --min-p 0 --override-kv "qwen4exp.attention.indexer.top_k=int:4096" --temp 1 --top-k 20 -c 65536 -t 12 -fa on --no-context-shift
python lcb_bench.py --name qwen38-flash-next-iq4xs --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8

# Qwen3.8-Flash-Next Q4_K_XL
llama-server -hf unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q4_K_XL -cmoe --load-mode none --no-mmproj -ctk q8_0 -ctv q8_0 -fit off -kvu -np 1 --min-p 0 --override-kv "qwen4exp.attention.indexer.top_k=int:4096" --temp 1 --top-k 20 -c 131072 -t 10 -fa on
python lcb_bench.py --name qwen38-flash-next-q4kxl --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8

# Qwen3-Coder-Next IQ4_XS  (32k ctx -> use --max-tokens 12288, see context note)
llama-server -hf unsloth/Qwen3-Coder-Next-GGUF:UD-IQ4_XS -cmoe --load-mode none --no-mmproj -ctk q8_0 -ctv q8_0 -fit off -kvu -np 1 --temp 0.7 --top-k 20 --top-p 0.8 -c 32768 -t 12 -fa on --no-context-shift -ub 2048
python lcb_bench.py --name qwen3-coder-next-iq4xs --speed-probe --random-sample 100 --workers 1 --max-tokens 12288 --eval-workers 8
```

Then: `python lcb_bench.py --report-only` → comparison table (`bench/report.md`).

**Context size note:** the bench asks for up to 16,384 output tokens; prompts
are ≤ ~4k, so `-c 32768` is the minimum that fits, `-c 65536+` is comfortable.
If you keep a `-c 32768` server config, use `--max-tokens 12288` instead.

All runs share the **same 100 problems** (fixed seed → same `sample_hash`),
so scores are directly comparable. If `sample_ids.json` exists in the run dir
it is reused; otherwise the same seed picks the identical subset.

## What you get

```
SCORE: 73.0%  (100 problems, 1 sample(s) each) [sample d46433bd9f02]
  pass@1: 73.00%   easy: 96.77% (31 problems)   medium: 97.22% (36)   hard: 24.24% (33)
speed: decode 18.3 tok/s, prefill/lecture 365.8 tok/s
       avg ~4.1k prompt + 7176 completion tokens per problem, 451.1s/problem wall
```

* `SCORE` = pass@1 % — the single number for "how good at code + problem solving".
* **prefill/lecture tok/s**: how fast it *reads* prompts (2-request probe,
  server-side timing; also measured automatically per problem once records
  include server timings).
* **gen tok/s**: decode speed. Wall time/total tokens from the run itself
  (queue-free when only the bench uses the server).

## Useful flags

```
--scenario NAME           code_generation (default) | code_execution | test_output_prediction
--exec-cot                step-by-step examples for code_execution scoring prompt
--speed-probe             measure prefill + decode tok/s with 2 extra requests
--probe-only              with --speed-probe: measure tok/s and exit (no
                          datasets, no generation); the saved probe is reused
--random-sample N         stratified RANDOM subset (fair + fast); stable across models
--n K                     samples/problem -> pass@K (official = n=10; multiplies time)
--max-tokens              16384 for thinking models; lower for short-ctx servers
--difficulty easy|medium|hard      --start-date/--end-date  contamination filters
--timeout 6               per-test-case seconds in the sandbox (official default)
--eval-workers N          concurrent test subprocesses (CPU-bound)
--extractor auto|official auto (default) also handles truncated/fenceless output
--skip-generate           re-score existing generations only
--skip-eval               generation only
--report-only             rebuild the comparison table
--extra-body JSON         e.g. '{"chat_template_kwargs":{"enable_thinking":false}}'
--self-test               verify the evaluator on your machine (no server needed)
--harness yes|no          record whether the deepseek-harness/agent chat was open on
                          the same server while the run generated (report column
                          "harness": wall time of those rows is queue-inflated).
                          Default: auto-detected from DSH_* env vars.
--set-harness yes|no      backfill that column for an existing run (with --name)
```

## Fair-comparison tips

* **One model server at a time, nothing else hitting it during generation**
  (your chat/dsh sessions included): the `-np 1` server queues requests, and
  queue time inflates wall-based speed numbers.
* Same `--random-sample` size and same `--n` across models; check `sample`
  column matches in `bench/report.md`.
* **pass@1 with n=1** on 100 problems ≈ ±5%; for publishable numbers use
  `--n 10` on the full set (~10× time).
* Thinking models: score at the setting you'd actually use, but be consistent.
* **The thinking cap matters more than the model:** on the Q2 100-problem run,
  29% of generations hit a 16,384-token cap; truncated answers auto-fail
  (3 of 29 truncated passed = 10%, vs 98.6% of the 71 complete ones). The
  report's **`when-complete`** column isolates that: `SCORE` 73.0 vs
  `when-complete` 98.6 means *this model solves nearly everything it finishes
  writing; the score is budget-limited, not skill-limited*. `_summary.json`
  records `generation_stats.truncated_pct` and
  `generation_stats.pass_rate_when_not_truncated`.
  Measured sensitivity: re-running the 15 truncated problems with a 32,768-token
  budget, 7 finished (5 of them needing 17k-26k tokens), 8 still ran past 32k.
  If `trunc%` is high, raise `--max-tokens` (respect server context) — but keep
  the cap identical across models you compare.

## Outputs

* `bench/<name>/generations.jsonl` — raw outputs + per-request token/timing stats (append-only checkpoint)
* `bench/<name>/generations.json` + `results_official_format.json` — official LCB formats
* `bench/<name>/eval_results.jsonl` — per-test verdicts (Wrong Answer / TLE / RE...)
* `bench/<name>/speed_probe.json` — prefill + decode tok/s probe
* `bench/<name>/_summary.json` — SCORE, pass@k, per-difficulty, speed
* `bench/report.md` / `report.csv` — cross-model comparison table

## Benchmark scenarios (`--scenario`)

| Scenario | Dataset | What the model must do |
|---|---|---|
| `code_generation` (default) | `livecodebench/code_generation_lite` | Solve the problem: write the program. **Main leaderboard scenario — the best single measure of coding + problem solving.** |
| `code_execution` | `livecodebench/execution-v2` (479 items) | Read a given function + a call, predict the exact output literal (no execution tools). Pure "simulate the code mentally". |
| `test_output_prediction` | `livecodebench/test_generation` (442 items) | Given problem + starter + one test input, write the full `assert f(...) == <correct output>`. |

Prompts, answer extraction and grading are ports of the official
`lcb_runner` code for each scenario, so the three SCOREs are "official-style".
Each scenario gets its own run directory by choosing a distinct `--name`, e.g.

```
python lcb_bench.py --name qwen38-flash-next-q2-exec --scenario code_execution --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8
python lcb_bench.py --name qwen38-flash-next-q2-top  --scenario test_output_prediction --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8
```

The two extra scenarios are much shorter than code_generation (most answers
are one line even with thinking on), so they run ~10-30x faster; sampling 100
items is quick. `bench/report.md` has a `scenario` column, so all your rows
sitting side by side. `--exec-cot` switches code_execution to the official
step-by-step-example prompt (default is the direct prompt).

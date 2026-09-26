# Minimal Benchmark — LiveCodeBench for your local llama.cpp models

> Want to bench *any* model without model-specific setup? Start with
> [README_MINIMAL.md](README_MINIMAL.md) (5 commands). This file is the longer
> measurement detail for your own models.

**Score any local model on real competitive programming with one command.**
You get one headline number — **`SCORE`** (pass@1 %, higher is better) — plus
how fast the model **writes** (`gen tok/s`) and **reads** (`prefill tok/s`).

Tool: [`lcb_bench.py`](lcb_bench.py) — Windows-native, resumable, official
LiveCodeBench prompts + grading. Deep dive: [BENCHMARK_GUIDE.md](BENCHMARK_GUIDE.md).

## Your current scoreboard

| model | SCORE | when-complete | easy | medium | hard | gen tok/s | prefill tok/s | run time |
|---|---|---|---|---|---|---|---|---|
| **Qwen3.8-Flash-Next UD-Q2_K_XL** | **73.0** | 98.6 | 96.8 | 97.2 | 24.2 | 18.3 | 365.8 | 12.5 h |

100 difficulty-stratified LiveCodeBench problems, thinking mode **on**,
16,384-token answer budget (`bench/qwen38-flash-next-q2/_summary.json`).
Read that as: **it solves 98.6 % of the problems it finishes writing**, and
loses 26 points purely because a third of its answers ran past the thinking
budget. See [Reading the numbers](#reading-the-numbers-honestly).

---

## 0. Prerequisites (once)

Everything runs from `B:\repos\clones\LiveCodeBench`.

```powershell
cd B:\repos\clones\LiveCodeBench
.venv\Scripts\python.exe lcb_bench.py --self-test
```

`Self-test: PASSED` (14 checks, no server needed) = the Windows evaluator works.
It spawns real subprocesses, so **run it from a normal PowerShell window** —
an agent/IDE sandbox usually blocks subprocess pipes and the self-test fails
with `WinError 5 / Access is denied`.

## Two ways to run: without the harness, or with it

The bench is a plain Python script — **deepseek-harness is not required**, and
Mode A is the only way to test models other than the one serving the harness.

**Mode A — harness closed (recommended).** Open a normal PowerShell window,
`cd` to the repo, run the commands below. Nothing else should hit the server,
so speed numbers are clean.

**Mode B — harness open (current model only).** The same commands work while
deepseek-harness is up, with two costs: the server runs `-np 1` (one slot), so
your chat messages and the benchmark queue behind each other — wall time
inflates and this agent gets very slow while it generates; and you cannot
switch models, because restarting that server kills the session driving the run.
`gen-tok/s` stays honest either way (it comes from server-side timings).

**Interruptions are cheap.** Every phase checkpoints into `bench/<name>/`.
After a crash, reboot, Ctrl-C or a server restart, rerun the **exact same
command**: finished problems are skipped, failed ones are retried.

## 1. Start the server for one model

The bench never starts or stops the server. Use your usual llama-server
command, and write down a short `--name` for the model.

| model | llama-server (key flags) | ctx | `--max-tokens` |
|---|---|---|---|
| Qwen3.8-Flash-Next **UD-Q2_K_XL** | `-ncmoe 45 … -c 131072 -t 10` | 131k | 16384 |
| Qwen3.8-Flash-Next **UD-IQ4_XS** | `-cmoe … -c 65536 -t 12` | 65k | 16384 |
| Qwen3.8-Flash-Next **UD-Q4_K_XL** | `-cmoe … -c 131072 -t 10` | 131k | 16384 |
| Qwen3-Coder-Next **UD-IQ4_XS** | `-cmoe … --temp 0.7 --top-k 20 --top-p 0.8 -ub 2048 -c 32768 -t 12` | 32k | **12288** |

Full commands per model: [BENCHMARK_GUIDE.md](BENCHMARK_GUIDE.md#your-models--run-this-once-per-model).

**Context rule:** the server must fit prompt + answer in `-c`. Code-generation
prompts measured ~0.5–1.3k tokens (allow 4k), so on a `-c 32768` server use
`--max-tokens 12288`; on `-c 65536` and up, use `--max-tokens 16384`.

Keep `--workers 1` — with `-np 1` more workers add nothing.

**Measure first (~1 min, recommended):** two probe requests, no datasets, no
generation. The result lands in `bench\<name>\speed_probe.json` and the real
run reuses it instead of probing again:

```powershell
.venv\Scripts\python.exe lcb_bench.py --name qwen38-flash-next-q4kxl --speed-probe --probe-only
```

Your box measured **prefill 350 tok/s, decode 17.4 tok/s** just now while this
page was being written (365.8 / 18.3 when nothing else touches the server).
Turn that into a runtime estimate: `hours ≈ 100 × 7200 ÷ decode_tok_s ÷ 3600`
→ 17.4 tok/s ≈ 11.5 h for the 100-problem code-generation run.

## 2. Benchmark it — code generation (the main test)

```powershell
# current model (already done: nothing left to generate, so this just re-grades
# the stored answers - ~10-30 min - and refreshes the summary/report)
.venv\Scripts\python.exe lcb_bench.py --name qwen38-flash-next-q2 --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8
```

For a new model, same line with a new `--name` (e.g.
`--name qwen38-flash-next-q4kxl`). `--name` is **required** and names the
folder `bench\<name>\`; it also decides which previous run gets resumed.

What it prints when it lands:

```
=== Results ===
SCORE: 73.0%  (100 problems, 1 sample(s) each) [sample d46433bd9f02]
  pass@1: 73.00%
     easy: 96.77%  (31 problems)
   medium: 97.22%  (36 problems)
     hard: 24.24%  (33 problems)
   speed: decode 18.3 tok/s, prefill/lecture 365.8 tok/s
          avg ? prompt + 7176 completion tokens per problem, 451.1s/problem wall
   gen: 29/100 hit the 16384-token cap (truncated = auto-fail), 0 failed request(s)
      when-complete: 98.6% of the 71/100 answers that fit in the token budget passed
Summary: bench\qwen38-flash-next-q2\_summary.json
```

`bench\report.md` and `bench\report.csv` are rebuilt automatically.

**Run it detached** so a closed window does not kill a 12-hour run:

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" -WorkingDirectory "B:\repos\clones\LiveCodeBench" `
  -RedirectStandardOutput "bench\qwen38-flash-next-q4kxl.out.log" `
  -RedirectStandardError  "bench\qwen38-flash-next-q4kxl.err.log" `
  -ArgumentList "--name","qwen38-flash-next-q4kxl","--speed-probe","--random-sample","100","--workers","1","--max-tokens","16384","--eval-workers","8"
Get-Content bench\qwen38-flash-next-q4kxl.out.log -Wait -Tail 20   # watch progress
```

## 3. Optional: the two extra scenarios (much faster)

Same tool, different LiveCodeBench datasets. Answers are one line, so these
are cheap — but they measure narrower skills than code generation.

```powershell
# mental execution: predict what a given function call returns (479 items)
.venv\Scripts\python.exe lcb_bench.py --name qwen38-flash-next-q2-exec --scenario code_execution --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8

# test-output prediction: write the full assert f(...) == <output> (442 items)
.venv\Scripts\python.exe lcb_bench.py --name qwen38-flash-next-q2-top --scenario test_output_prediction --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8
```

These use their own `--name` (own folder, own `sample_ids.json`), so they never
disturb the code-generation run. Use the same name pattern per model
(`<name>-exec`, `<name>-top`) to keep report rows tidy.

*Not run yet for your models* — the graders are ports of the official ones and
`--self-test` exercises both, but nothing has been scored on these two datasets
so far. Run one of them once and check the printed `problems` count looks sane
(100) before trusting the number.

## 4. Compare all your models

```powershell
.venv\Scripts\python.exe lcb_bench.py --report-only
```

→ `bench\report.md` / `bench\report.csv`, best row first:

```
model                 scenario  SCORE  problems  n  cap    trunc%  when-complete  easy  medium  hard  prefill-tok/s  gen-tok/s  gen-min  sample        date
qwen38-flash-next-q2  codegen   73.0   100       1  16384  29.0    98.6           96.8  97.2    24.2  365.8          18.3       751.8    d46433bd9f02  2026-09-23
```

Rows are directly comparable **only** when `scenario` / `problems` / `n` /
`cap` / `sample` match. To re-score without regenerating anything:
`--skip-generate` (reads the stored answers, re-runs the grader).

---

## Reading the numbers honestly

* **SCORE is pass@1 %** on *your* 100-problem subset — a problem passes only if
  every public + private test passes. It is a consistent yardstick between your
  own models; it is **not** comparable to published LiveCodeBench numbers
  (different release window, 1 sample, thinking on).
* **Noise:** 100 problems at ~73 % → standard error ≈ ±4.4 %; two models
  differing by less than ~9 points are not clearly different. For a tighter
  comparison use `--random-sample 200` (≈ ±3.1 %, 2× the time) or keep 100 and
  add `--n 2` (2 samples per problem, SCORE becomes the average pass rate,
  ≈ ±3.1 % for 2× the time).
* **`trunc%` is the budget trap, and `when-complete` is the escape hatch.**
  `trunc%` is the share of answers cut off at `--max-tokens`; truncated answers
  auto-fail. On the Q2 run 29 % hit the 16,384 cap, and only **3 of those 29
  passed (10 %)** while **70 of the 71 complete answers passed (98.6 %)**. So
  `SCORE` 73.0 + `when-complete` 98.6 = *the model can code; it just rambles*.
  Measured sensitivity: re-running 15 of those problems with a 32,768-token
  budget let **7 of 15 finish** (5 needed 17–26k tokens) and **8 still ran past
  32k**, so a bigger budget buys a few points and does not fix the ramblers.
  Raise `--max-tokens` (respect the context rule) if you want that variant —
  but keep the cap identical on every row you compare.
* **`gen tok/s` vs wall time.** `gen tok/s` is pure decode speed (queue-free);
  wall time also contains prefill and server queueing, so `gen-min` in the
  report is only comparable between runs that were equally quiet.
* Thinking burn dominates: **~7.2k completion tokens per problem on average**
  (median 4.3k, p90 = capped) — the thinking, not the answer, costs the time.

## Estimated runtimes

Measured on your RTX 5070 12 GB, single-slot server, nothing else hitting it.
Rough formula: `hours ≈ problems × avg_completion_tokens ÷ decode_tok_s ÷ 3600`
(use `--speed-probe --probe-only`, ~1 min, to get the real tok/s).

| Test | Q2_K_XL @18.3 tok/s | other quants @12–15 tok/s |
|---|---|---|
| self-test (no server) | ~1 min | ~1 min |
| speed probe only (`--speed-probe --probe-only`) | ~1–2 min | ~1–2 min |
| **codegen, 100 problems, cap 16384** | **12.5 h measured** (11.2 h quiet) | ~11–16 h |
| codegen, 50 problems | ~6 h | ~6–8 h |
| codegen, 100 problems, cap 32768 | ~1.3–2× the 16k run | varies |
| codegen, full set (1055 problems) | ~5–6 **days** | ~6–8 days |
| code_execution, 100 problems | ~1–3 h (estimate) | ~1–3 h |
| test_output_prediction, 100 problems | ~1–3 h (estimate) | ~1–3 h |
| re-score (`--skip-generate`) | 10–30 min | 10–30 min |
| `--report-only` | seconds | seconds |

Generation is ~95 % of the cost; the evaluator is CPU-only. Grading does
compete with a CPU-offloaded MoE (`-ncmoe 45`), so never grade while another
model run is generating.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `WinError 5 Access is denied` in the evaluator or in `huggingface` file locks | You are inside an agent/IDE sandbox that blocks subprocess pipes and writes under `C:\Users\remal\.cache`. Run from a normal PowerShell window. |
| `ConnectionError` / refused connection | Server not up, or wrong `--base-url` (default `http://127.0.0.1:8080/v1`). |
| Generations stop mid-run / `failed_requests` | Server restarted or OOM. Rerun the same command — it resumes and retries failures. |
| `trunc%` high | Answer budget too small → raise `--max-tokens` (see context rule). |
| `429`/slow everything while benching | Something else is hitting the `-np 1` server (harness included). Close it, or expect inflated wall time. |
| `Warning: You are sending unauthenticated requests to the HF Hub` | Harmless; datasets are already cached. `HF_HUB_OFFLINE=1` silences it. |
| A model scores 0 with everything failing | Code extraction issue or a thinking model that ignores the output format — check `bench\<name>\generations.jsonl` and try `--extractor auto` (default) or `--extra-body` to toggle thinking. |

## Quick flag reference

| Flag | Meaning |
|---|---|
| `--name` | **required**: run folder + report row (`bench\<name>\`), also the resume key |
| `--scenario` | `code_generation` (default) / `code_execution` / `test_output_prediction` |
| `--speed-probe` | measure prefill + decode tok/s with 2 extra requests |
| `--probe-only` | with `--speed-probe`: measure and exit (no datasets, no generation) |
| `--random-sample N` | fixed difficulty-stratified subset; ids saved per run so every model gets the identical problems |
| `--max-tokens` | thinking + answer budget (default 16384); truncated answers auto-fail |
| `--n K` | K samples per problem → pass@1 becomes the average over K samples |
| `--temperature` / `--top-p` | per-request sampling (defaults 0.2 / 0.95, official) |
| `--difficulty`, `--start-date`, `--end-date` | problem filters |
| `--skip-generate` / `--skip-eval` | re-score only / generate only |
| `--eval-workers N` | concurrent grading subprocesses (CPU-bound, default cpu/2) |
| `--timeout` | per-test-case seconds in the grader (official: 6) |
| `--extra-body` | extra JSON per request, e.g. `'{"chat_template_kwargs":{"enable_thinking":false}}'` |
| `--self-test` | check the evaluator works (no server, no datasets) |
| `--report-only` | rebuild the comparison table |
| `--harness yes\|no` | record in the report whether the run happened while the harness/agent chat was open on the same server (default: auto-detected) |
| `--set-harness yes\|no` | backfill that column for an existing run (`--name` too) |

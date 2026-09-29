# Minimal Benchmark — LiveCodeBench for your local llama.cpp models

> Want to bench *any* model without model-specific setup? Start with
> [EXTRA_MINIMAL_BENCHMARK.md](EXTRA_MINIMAL_BENCHMARK.md) (copy-paste only) or
> [README_MINIMAL.md](README_MINIMAL.md) (5 commands). This file is the longer
> measurement detail for your own models.

**Score any local model on real competitive programming with one command.**
You get one headline number — **`SCORE`** (pass@1 %, higher is better) — plus
how fast the model **writes** (`gen tok/s`) and **reads** (`prefill tok/s`).

Tool: [`lcb_bench.py`](lcb_bench.py) — Windows-native, resumable, official
LiveCodeBench prompts + grading. Deep dive: [BENCHMARK_GUIDE.md](BENCHMARK_GUIDE.md).

## Your current scoreboard

| model | harness | SCORE | when-complete | easy | medium | hard | gen tok/s | prefill tok/s | run time |
|---|---|---|---|---|---|---|---|---|---|
| **Qwen3.8-Flash-Next UD-Q2_K_XL** | no | 69.0 | 97.1 | 100.0 | 86.1 | 21.2 | 18.1 | 243.7 | 11.4 h |
| **Qwen3.8-Flash-Next UD-Q2_K_XL** | yes | **73.0** | 98.6 | 96.8 | 97.2 | 24.2 | 18.3 | 365.8 | 12.5 h |

100 difficulty-stratified LiveCodeBench problems, thinking mode **on**,
16,384-token answer budget (`bench/qwen38-flash-next-q2-harness/_summary.json`,
and `…-noharness` for the other row). The two rows are the *same* run plan — the
`harness` column says whether an agent chat was sharing the server while it ran
(its requests queue behind the chat, which is why prefill/speeds differ).
Read that as: **it solves 98.6 % of the problems it finishes writing**, and
loses 26 points purely because a third of its answers ran past the thinking
budget. See [Reading the numbers](#reading-the-numbers-honestly).

---

## 0. Prerequisites (once)

Everything runs from the project root — the folder holding `lcb_bench.py`
(here: `B:\repos\MyProjects\_LiveCodeBench`).

```powershell
cd B:\repos\MyProjects\_LiveCodeBench
.venv\Scripts\python.exe lcb_bench.py --self-test
```

`Self-test: PASSED` (14 checks, no server needed) = the Windows evaluator works.
It spawns real subprocesses, so **run it from a normal PowerShell window** —
an agent/IDE sandbox usually blocks subprocess pipes and the self-test fails
with `WinError 5 / Access is denied`.

## Two ways to run: without the harness, or with it

The bench is a plain Python script — no agent is required, and Mode A is the only
way to test a model other than the one serving that agent. Which mode a run
happened in is **never guessed**: a run in a terminal answers the question out
loud, a detached or agent-driven run says it with `--harness yes|no`. The answer
picks the run folder (`…-noharness` / `…-harness`) and the report row, so one
model gives you two comparable rows and neither overwrites the other.

**Or take both rows in one command: `--both`.** In your own window, add `--both` and
the scenario runs twice on identical problems — pass 1 with the harness question
answered *no* (nothing else on the server: close the chat, it says so and waits for
Enter), pass 2 answered *yes* (chat open and working). It asks which harness, once,
before anything starts, then prints the pair together at the end. Without `--both` a
run answers its harness question **once** and writes **one** row: a fast test that
produced a single row did exactly what it was told, it was not missing a second half.

**Mode A — no agent on the server (recommended).** Open a normal PowerShell
window, `cd` to the repo, run the commands below, and answer `1` when asked.
Nothing else should hit the server, so speed numbers are clean.

**Mode B — agent chat open (current model only).** The same commands work while
an agent is up, with two costs: the server runs `-np 1` (one slot), so your chat
messages and the benchmark queue behind each other — wall time inflates and the
chat gets very slow while it generates; and you cannot switch models, because
restarting that server kills the session driving the run. Nothing can answer the
harness question from that shell, so append `--harness yes`: it fills in the harness
address it knows — this shell's own agent (`DSH_WEB_URL`) if there is one, otherwise
the address your earlier runs used — and pings it. Add
`--harness-note "http://127.0.0.1:3080"` only when the harness you mean is a different
one. `gen-tok/s` stays honest either way (it comes from server-side timings).

**What `harness = yes` does and does not claim.** The bench always asks the model
itself: one prompt in, one completion out, nothing else allowed to touch the answer.
No agent is ever asked to solve a problem, so `harness = yes` does **not** mean "this
model is better with an agent". It records only that a second client was sharing the
one server slot while the numbers were measured, which inflates wall time (`gen-min`)
and nothing else. Wanting "model + agent" as a capability is a reasonable question —
but that is a *different* benchmark (the agent reads the problem, runs the tests,
fixes, retries), and its scores are comparable only with other agent runs, never with
these rows. That is also why the fast scenarios score higher: different task, not a
helped model.

**Which harness, asked as choices.** Say yes and one more question follows, and it
offers answers rather than staring at you: every address it knows gets a numbered
line, marked with what it is and whether it answers right now.

```
  [1] http://127.0.0.1:3080      your saved harness address (saved 2026-09-25) - answers now (HTTP 401)
  [2] another one: type its address (http://host:port) or a name
```

**Enter takes `[1]`**, so in a normal window nothing has to be typed. That first
address comes from wherever it is known: the agent that started this shell
(`DSH_WEB_URL`), the address your earlier runs used (kept in
`bench\harness-address.json`, written by the run that used it), or the address a
recent harness run recorded. Same question, same choices, in your own PowerShell as
in an agent chat. `[2]` is for a new harness: type its address, or a name. The bench
then **pings** the address you picked at the start and at the end of the run and
stores what it got in `_summary.json` as `harness_probe` — evidence the thing was
really there. Nothing is ever *routed* through it: it receives one unauthenticated
GET, and problems only ever go to your model server (`--base-url`). A path or a bare
name is stored as text only: nothing in it is opened, read or executed. Only your own
machine is ever probed (`127.0.0.1`, `.local`, `192.168/10/172` private ranges); a
remote address is recorded as said and never touched. A `?token=…` in what you type
is dropped before it is saved.

Ctrl-C at any of these questions stops before anything starts, with one line of
advice instead of a traceback — the same answers can be given in the command line
(`--name LABEL`, `--harness yes|no`, `--harness-note "ADDRESS"`).

**Interruptions are cheap.** Every phase checkpoints into `bench/<run>/`.
After a crash, reboot, Ctrl-C or a server restart, rerun the **exact same
command**: finished problems are skipped, failed ones are retried.

## What a run measures, and what "harness" has to do with it

The problems come from LiveCodeBench, the answers come from the model, the grading is
the official one: a prompt goes in, **one** completion comes out, and the model's own
code is run against that problem's tests in a sandboxed subprocess. No agent is ever
asked a problem, never shown a test, never given a second attempt. So both the
`…-noharness` and the `…-harness` row are the **raw** model — they differ only in who
else was queuing at the one-slot server while the stopwatch ran.

That is all the harness column is for. `harness = yes` is not a capability column and
not "this model was helped": wall time (`gen-min`) of a run measured while an agent
chat shares the server is slower, and mixing it with a quiet run would make that column
meaningless. It is never guessed, and an address you give it gets pinged so the row
carries evidence rather than a claim.

Your instinct — that some models would win by much more with a harness than others —
is a good instinct, but it measures a different thing. A model allowed to run the
tests, read the failure and retry (what an agent does) clears problems it cannot
one-shot, and how much that helps varies per model. That is an **agentic** benchmark:
multiple turns, real tool use, minutes per problem instead of one completion. Those
scores would be comparable only with other agentic runs, never with the rows in this
table — so it is a separate test to build, not a flag on this one.

## 1. Start the server for one model

The bench never starts or stops the server. Use your usual llama-server command —
the bench reads the model id off the server itself and asks you for a short label
(press Enter to accept the one it derived from that id), so any model you can serve
is benchable, listed here or not.

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
generation. It asks **Q1** what to call the run (Enter takes the model id from the
server; a bare `1`/`2`/`3` is refused there — those answer Q2) and then **Q2**, the
harness answer; the result lands in `bench\<run>\speed_probe.json` and the real run
reuses it instead of probing again:

```powershell
.venv\Scripts\python.exe lcb_bench.py --speed-probe --probe-only
```

Or pin the label up front (also what a detached run must do):
`--name qwen38-flash-next-q4kxl --harness no` before the flags above.

Your box measured **prefill 350 tok/s, decode 17.4 tok/s** just now while this
page was being written (365.8 / 18.3 when nothing else touches the server).
Turn that into a runtime estimate: `hours ≈ 100 × 7200 ÷ decode_tok_s ÷ 3600`
→ 17.4 tok/s ≈ 11.5 h for the 100-problem code-generation run.

## 2. Benchmark it — code generation (the main test)

```powershell
# the same command for any model: it asks Q1 (what to call the run — Enter takes
# the server's model id) and then which harness shares the second pass (--both)
.venv\Scripts\python.exe lcb_bench.py --speed-probe --random-sample 100 --hardest 25 --workers 1 --max-tokens 16384 --eval-workers 8 --both
```

The answers decide the run: label + scenario + mode → folder
`bench\<label>-noharness\` (or `…-harness`), and that name is the report row.
Rerun the identical command to resume it; pass `--name` to pick a label up front,
which is also how you re-grade or resume an older run by name.

What it prints when it lands:

```
=== Results ===
   model: unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL   run: bench\qwen38-flash-next-q2-harness
SCORE: 73.0%  (100 problems, 1 sample(s) each) [sample d46433bd9f02]
  pass@1: 73.00%
     easy: 96.77%  (31 problems)
   medium: 97.22%  (36 problems)
     hard: 24.24%  (33 problems)
   speed: decode 18.3 tok/s, prefill/lecture 365.8 tok/s
         avg ? prompt + 7176.0 completion tokens per problem, 451.1s/problem wall time
   gen: 29/100 hit the 16384-token cap (truncated = auto-fail), 0 failed request(s)
      when-complete: 98.6% of the 71/100 answers that fit in the token budget passed (skill signal, budget aside)
   harness: yes (you answered; http://127.0.0.1:3080 - up at start, up at end)
Summary: bench\qwen38-flash-next-q2-harness\_summary.json
```

`bench\report.md` and `bench\report.csv` are rebuilt automatically. The run prints
**its own row** (or the two rows of a `--both` pair) and points at
`--report-only` for the full table — the history is in the files, not in your
terminal. That example is a plain `--random-sample 100` run: with `--hardest 25` the
sample hash is a different one, the difficulty line reads 25 easy / 25 medium / 50 hard,
and one more line appears under them — `hardest: 12.00%  (25 problems of the --hardest
tier)`.

**Why `--hardest 25` is in that line.** `--random-sample 100` is stratified, and on
the code-generation release that means roughly 31 easy / 36 medium / 33 hard. A good
model clears the easy and medium ones and prints a soft number, so `--hardest 25` takes
25 of the sample's 100 slots for the hardest problems the release has (hard first, then
the ones with the most test cases, then the newest contest) and spreads the other 75
evenly over the difficulties — about 25 easy, 25 medium, 25 hard, 25 hardest, in one run
of exactly 100 problems. The tier is also scored by itself: the results gain a line
`hardest: 8.00%  (25 problems of the --hardest tier)`, the report gains a `hardest`
column, and `bench\<run>\hardest_ids.json` keeps those ids. Alone, `--hardest N` makes
the run *be* those N problems; with `--limit` it is ignored, because the limit already
says which problems to run. It changes the `sample` hash, so the old rows stay separate
and comparable on their own terms instead of mixing. Resuming a run keeps its saved
sample (`sample_ids.json` plus `hardest_ids.json`) — the picks do not move underneath a
half-finished run. A run that scores 100% on a small or easy-weighted sample says so out
loud instead of letting the number stand on its own.

**Run it detached** so a closed window does not kill a 12-hour run. Nothing can be
asked there, so the label and the harness answer go in the argument list:

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" -WorkingDirectory "B:\repos\MyProjects\_LiveCodeBench" `
  -RedirectStandardOutput "bench\qwen38-flash-next-q4kxl-noharness.out.log" `
  -RedirectStandardError  "bench\qwen38-flash-next-q4kxl-noharness.err.log" `
  -ArgumentList "--name","qwen38-flash-next-q4kxl","--harness","no","--speed-probe","--random-sample","100","--hardest","25","--workers","1","--max-tokens","16384","--eval-workers","8"
Get-Content bench\qwen38-flash-next-q4kxl-noharness.out.log -Wait -Tail 20   # watch progress
```

## 3. Optional: the two extra scenarios (much faster)

Same tool, different LiveCodeBench datasets. Answers are one line, so these are
cheap — but they measure narrower skills than code generation. The report
flags these rows `test = fast` (the main run `test = slow`); fast scores are
comparable between models only within the **same scenario**. Each scenario gets
its own run folder automatically (`-exec`, `-top` appended to your label), so
they never disturb the code-generation run: no `--name` juggling needed.

```powershell
# mental execution: predict what a given function call returns (479 items)
.venv\Scripts\python.exe lcb_bench.py --scenario code_execution --speed-probe --random-sample 100 --hardest 25 --workers 1 --max-tokens 16384 --eval-workers 8 --both

# test-output prediction: write the full assert f(...) == <output> (442 items)
.venv\Scripts\python.exe lcb_bench.py --scenario test_output_prediction --speed-probe --random-sample 100 --hardest 25 --workers 1 --max-tokens 16384 --eval-workers 8 --both
```

Without `--both` each of these answers the harness question once and writes **one**
row — that is the shape of a single run, not a lost second half. The harness never
sees a problem in either pass: these scenarios are scored by running the model's own
answer against the dataset's tests.

A word on the numbers here: the exec/top releases are easier for a strong model than
code generation, so a clean 100% on `code_execution` is a normal outcome and says more
about the scenario than about the model. The commands above already carry
`--hardest 25`, and for exec that tier honestly has to say
`25 hardest (9 hard, 16 medium)` — the unfiltered exec pool only holds **9 problems
flagged `hard`**, so the rest of the tier is the meatiest mediums. Read the difficulty
split and the `hardest` line, not just the headline.

*Not scored for your models yet* — the graders are ports of the official ones,
and both have been run end-to-end here on 2-problem samples
(generation → extraction → grading → summary, all green), but no 100-problem
fast run has been done. Run one and check the printed `problems` count looks
sane (100) before trusting the number.

## 4. Compare all your models

```powershell
.venv\Scripts\python.exe lcb_bench.py --report-only
```

→ `bench\report.md` / `bench\report.csv`, best row first:

```
run                             model                                       harness  test  scenario  SCORE  problems  n  cap    trunc%  when-complete  easy   medium  hard  prefill-tok/s  gen-tok/s  gen-min  sample        date
qwen38-flash-next-q2-harness    unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  yes      slow  codegen   73.0   100       1  16384  29.0    98.6           96.8   97.2    24.2  365.8          18.3       751.8    d46433bd9f02  2026-09-23
qwen38-flash-next-q2-noharness  unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  no       slow  codegen   69.0   100       1  16384  31.0    97.1           100.0  86.1    21.2  243.7          18.1       686.1    d46433bd9f02  2026-09-24
```

`run` is the `bench\<run>\` folder, `model` is the model id the server reported for
that run — so a row always says which model it is, never a placeholder name.

Rows are directly comparable **only** when `test` / `scenario` / `problems` / `n` /
`cap` / `sample` match. To re-score without regenerating anything:
`--skip-generate` (reads the stored answers, re-runs the grader).

## 4b. Take a score out of the table

A cut-short run or a smoke run that landed in the report is one row you have to sit
past every time. `--manage` opens the bench console — the runs listed with numbers, and
commands to work on them, no server or model involved:

```powershell
.venv\Scripts\python.exe lcb_bench.py --manage      # or: --list-runs / --delete-run / --restore-run
```

```
Runs in the report:
  1  qwen38-flash-next-q2-harness       73.0%  slow codegen   yes  100  d46433bd9f02  2026-09-23
  2  qwen38-flash-next-q2-noharness     69.0%  slow codegen   no   100  d46433bd9f02  2026-09-24
  *  qwen38-flash-next-q4kxl-noharness   ...      <- the run in progress; it cannot be deleted
help  list [archive]  delete <number|name>  restore <number|name>  report  status  quit
```

`delete 2` moves run 2 out of the report; `restore 1` moves it back. Nothing is
erased: a deleted run goes to `bench\_archive\<stamp>__<folder>` and comes back from
there. The same commands work **during** a run — press a key in the window it is
running in and `bench>` answers between two problems (`help` for the list). A name
must identify exactly one run; a prefix matching several is refused, not guessed.

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
| bench console (`--manage`, `--list-runs`, `--delete-run`) | seconds | seconds |

`--both` doubles all of the above: it is the same scenario twice on the same problems
(pass 1 alone, pass 2 with the chat working). Both passes resume independently, so a
crash in pass 2 does not cost you pass 1.

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
| It asks the label / harness question where you cannot answer | Normal: a terminal answers. Detached, piped or agent-driven shells cannot, so put both answers in the command (`--name LABEL`, `--harness yes\|no`); a run that answers nothing is recorded `harness = unknown`. |
| `Speed probe failed against …` | Server not up or not answering at that `--base-url`; the probe is the cheapest place to notice. |

## Quick flag reference

| Flag | Meaning |
|---|---|
| `--name` | optional label for the run folder + report row (`bench\<label>…\`), also the resume key. Without it the label is asked, derived from the server's model id |
| `--scenario` | `code_generation` (default) / `code_execution` / `test_output_prediction`; each scenario keeps its own run folder (`-exec` / `-top` on the label) |
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
| `--report-only` | print the comparison table of every run (a run itself prints only its own row, or its pair) |
| `--both` | one command, both rows: the scenario runs twice on identical problems — pass 1 answered *no* (nothing else on the model server), pass 2 answered *yes* (the agent chat working). Asked once up front; the harness never sees a problem |
| `--hardest N` | give N slots of the run to the hardest problems the release has (hard, then most test cases, then newest). With `--random-sample 100` the run stays exactly 100 problems: N hardest + the rest spread evenly over easy/medium/hard; alone: the run is those N; with `--limit`: ignored. Changes `sample`, so old rows stay separate |
| `--manage` | the bench console without a server or model: numbered runs, `delete <n>`, `restore <n>`, `report`, `status`. Same commands work during a run — press a key, `bench>` answers between two problems |
| `--list-runs` / `--delete-run` / `--restore-run` | the console's one-line forms. A delete is a move into `bench\_archive\<stamp>__<folder>`, never an erase; the run in progress cannot be deleted |
| `--harness yes\|no` | answer the harness question up front instead of at a prompt: was an agent chat sharing this model server? Required for detached/piped runs. Never guessed. `yes` also fills in the harness address it knows — this shell's own agent (`DSH_WEB_URL`), else the one your earlier runs used — and pings it |
| `--harness-note "ADDRESS"` | only when the harness is *not* the one it offers: say which it was (address preferred). The bench pings an address at the start and end of the run and saves the result as evidence in `_summary.json` (`harness_probe`); a name or path is stored as text only and never opened, and remote hosts are never probed. An address you use is remembered in `bench\harness-address.json`, so the next run offers it as choice 1 |
| `--set-harness yes\|no` | backfill that answer for a run that never gave one (`--name` = its folder) |

# README_MINIMAL — bench ANY local model in 5 commands

Not model-specific: any GGUF you just downloaded, any quant you already own,
LM Studio / vLLM / Ollama too. You end up with one number per model —
**SCORE** = % of real competitive-programming problems solved — plus speeds, and
a table comparing every model you have tested.

Every run asks you two things out loud, because it refuses to guess them:

* **which model is this?** — press Enter and the label comes from the server's own
  model id, so each row names the model rather than a placeholder.
* **harness: yes or no?** — is an agent chat sharing the model server while the
  bench runs? Answer `1` (no) in your own PowerShell window; from the agent chat,
  where nothing can answer, say it up front with `--harness yes`. If you say yes it
  asks *which* one, with **choice 1 = the harness address it knows** — the agent that
  started this window, or the address your earlier runs used — so **Enter is enough**
  and nothing has to be typed; choice `2` takes a new address or a name. The
  bench pings the address at the start and end of the run so the row carries evidence
  instead of a claim. The agent never takes the test: the model always answers on its
  own — the bench only ever talks to your model server — so `harness = yes` means
  "someone else was sharing the server", never "this model was helped".

The answers decide the run folder (`bench\<model>-noharness\`, `bench\<model>-harness\`)
and the report rows, so one model produces a clean, comparable pair of rows. Run the
same line twice — once each way — and you have both. **Or add `--both` in your own
window**: one command runs the scenario twice on identical problems (pass 1 answered
*no*, pass 2 answered *yes*) and gives you both rows at once. A run without `--both`
answers the harness question once and writes **one** row — that is normal, not a
failure; the second row is the second answer, and `--both` is what asks for both.

Shorter still, copy-paste only: [EXTRA_MINIMAL_BENCHMARK.md](EXTRA_MINIMAL_BENCHMARK.md).
Long versions (only if you want the detail): [MINIMAL_BENCHMARK.md](MINIMAL_BENCHMARK.md),
[BENCHMARK_GUIDE.md](BENCHMARK_GUIDE.md).

```powershell
cd B:\repos\MyProjects\_LiveCodeBench
```

Run from a **normal PowerShell window**, not from an agent/IDE sandbox: the
grader spawns child processes and writes to the Hugging Face cache in
`C:\Users\<you>\.cache`. Sandboxes block both → `WinError 5 / Access is denied`.

## Step 0 (once, ~1 min, no server): is the grader working?

```powershell
.venv\Scripts\python.exe lcb_bench.py --self-test
```

Ends with `Self-test: PASSED`. If you get `WinError 5`, you are in a sandbox.

## Step 1: serve the model you want to test

Either from Hugging Face (downloads the quant if missing) or from a local file —
whichever llama-server flags you normally use are yours to keep:

```powershell
# A) straight off Hugging Face:  -hf  user/Model-GGUF:QUANT  (downloads if missing)
llama-server -hf "unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q4_K_XL" -ngl 99 -c 65536

# B) a .gguf file you downloaded yourself
llama-server -m "D:\models\some-model.Q4_K_M.gguf" -ngl 99 -c 65536
```

Keep the default `--port 8080` — that is the address the bench expects. Different
server or port? Add `--base-url` to every bench command:

| server | add this |
|---|---|
| llama-server on 8080 (default) | nothing |
| LM Studio | `--base-url http://localhost:1234/v1` |
| vLLM | `--base-url http://localhost:8000/v1` (+ `--api-key KEY` if it demands one; `LCB_API_KEY` works too) |
| Ollama (OpenAI shim) | `--base-url http://localhost:11434/v1` |

**The one rule:** the server context `-c` must fit prompt (~4k) **plus** the
answer budget `--max-tokens`. `-c 65536` or more → `--max-tokens 16384`;
`-c 32768` → `--max-tokens 12288`. Small model, small disk, no preference? Use
`-c 65536` + `--max-tokens 16384`.

## Step 2 (~2 min): can it answer, and how fast?

```powershell
.venv\Scripts\python.exe lcb_bench.py --speed-probe --probe-only
```

It asks for a **label** (just press Enter: it takes the model id from the server and
shortens it), then asks the **harness question** — answer `1` in this window. No problems
yet: this fires 2 requests and prints prefill/decode tok/s, saved for reuse. Rough
runtime: `hours ≈ problems × 7200 ÷ decode_tok_s ÷ 3600` (7200 = what a thinking
model burns per problem; plain models use far less). Want your own shorter label?
Type it at the prompt, or pass `--name MYMODEL` to skip the asking.


## Step 3 (~10–40 min): does the whole pipeline score it?

```powershell
.venv\Scripts\python.exe lcb_bench.py --name smoke --harness no --limit 3 --max-tokens 16384 --eval-workers 8
```

Only 3 problems, so the SCORE here is noise — you are checking plumbing
(answers come out, code gets extracted, the grader runs). The throwaway `--name`
plus `--harness no` keeps it out of your real model's folders. Clean up after:
`Remove-Item -Recurse -Force bench\smoke-noharness`.

## Step 4 (hours, resumable): the real score

```powershell
.venv\Scripts\python.exe lcb_bench.py --speed-probe --random-sample 100 --hardest 25 --workers 1 --max-tokens 16384 --eval-workers 8 --both
```

**Q1** (the name): press Enter to take the model id the server reports, or type your
own label — a bare `1`/`2`/`3` is refused, those are answers to Q2. Then it asks which
harness shares the server for the second pass (`--both` runs the scenario twice on
identical problems: pass 1 alone, pass 2 with the chat working) and tells you to close
the chat while pass 1 runs. `[1]` is the address it knows — the agent that started the
window, or the one your earlier runs used — so Enter is enough; `[2]` takes a new
address or a name. Same problems for every model → comparable. Ctrl+C, a crash or a
server restart are cheap: **rerun the exact same command**, it skips what is finished.
Ctrl+C at one of these questions stops before anything is started, with advice instead
of a traceback. Keep the flags you started with (they are what gets recorded).

`--random-sample 100 --hardest 25` is **one sample that covers every difficulty**: 25 of
the 100 problems go to the hardest the release has (hard first, then the ones with the
most test cases, then the newest contest) and the other 75 are spread evenly over
easy / medium / hard — about 25 easy, 25 medium, 25 hard, 25 hardest, in one run of
exactly 100 problems. The hardest tier is also scored by itself: a `hardest: …%` line
under the score, and a `hardest` column in the report (its problem ids land in
`bench\<run>\hardest_ids.json`). A plain `--random-sample 100` is easy-weighted enough
that a strong model prints a clean 100%, which tells you nothing. Alone, `--hardest N`
makes the run *be* those N problems. It changes `sample`, so old rows stay separate
instead of mixing with the new one.

While it runs you can press a key in that window and type: `bench>` answers between two
problems, `help` lists what you can type (`list`, `delete 2`, `restore 1`, `report`,
`status`, `quit`). Deleting never erases — the folder moves to `bench\_archive\`.

12 h is long, so detach it — close the window, it keeps running. A detached run
cannot answer questions, so say both answers in the command line instead
(`--harness no` here; add your own `--name` if you want a specific label):

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" -WorkingDirectory "B:\repos\MyProjects\_LiveCodeBench" `
  -RedirectStandardOutput "bench\long-run.out.log" -RedirectStandardError "bench\long-run.err.log" `
  -ArgumentList "--speed-probe","--random-sample","100","--hardest","25","--workers","1","--max-tokens","16384","--eval-workers","8","--harness","no"
Get-Content bench\long-run.out.log -Wait -Tail 20
```

What it prints at the end (your model's id and folder appear in the first lines):

```
=== Results ===
   model: unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL   run: bench\qwen38-flash-next-q2-noharness
SCORE: 69.0%  (100 problems, 1 sample(s) each) [sample d46433bd9f02]
  pass@1: 69.00%
     easy: 100.00%  (31 problems)
   medium: 86.11%  (36 problems)
     hard: 21.21%  (33 problems)
   speed: decode 18.1 tok/s, prefill/lecture 243.7 tok/s
         avg 666.0 prompt + 7423.0 completion tokens per problem, 411.7s/problem wall time
   gen: 31/100 hit the 16384-token cap (truncated = auto-fail), 0 failed request(s) (rerun the same command to retry them)
      when-complete: 97.1% of the 69/100 answers that fit in the token budget passed (skill signal, budget aside)
   harness: no (you answered)
Summary: bench\qwen38-flash-next-q2-noharness\_summary.json
```

`SCORE` is the headline. `trunc%` big + `when-complete` high = the model can
code, it just rambles past the budget → raise `--max-tokens` (respect the `-c`
rule) and keep the same cap on every row you compare.

## Step 5: the total report of every model you tested

```powershell
.venv\Scripts\python.exe lcb_bench.py --report-only
```

→ `bench\report.md` (and `report.csv`), best row first, one row per run. That file
holds **every** run; a finished run itself prints only its own row (or the two rows of
a `--both` pair) and points here, because a table that grows every time is not a
result. It looks like this:

```
run                             model                                       harness  test  scenario  SCORE  problems  n  cap    trunc%  when-complete ...
qwen38-flash-next-q2-harness    unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  yes      slow  codegen   73.0   100       1  16384  29.0    98.6
qwen38-flash-next-q2-noharness  unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  no       slow  codegen   69.0   100       1  16384  31.0    97.1
```

**`run`** is the folder label, **`model`** the model id the server reported for
that run, **`harness`** the answer that run gave: `yes` means an agent chat was
sharing the server, so those requests queued behind the chat and the wall time
(`gen-min`) is inflated — tok/s is not. `unknown` means nobody answered (a run made
before the question existed, or a detached run without `--harness`); answer it later
with the folder name:

```powershell
.venv\Scripts\python.exe lcb_bench.py --set-harness no --name qwen38-flash-next-q2-noharness
```

Rows are comparable only when `test` / `scenario` / `problems` / `n` / `cap` / `sample`
match (`test`: `slow` = main run, `fast` = the two optional quick tests, see
[EXTRA_MINIMAL_BENCHMARK.md](EXTRA_MINIMAL_BENCHMARK.md)). To
re-grade stored answers without regenerating:
`--name RUN-NAME --skip-generate --skip-eval --random-sample 100`.

## Taking a score out of the report

A run that was cut short, or a smoke run that landed in the table, is one row you
have to sit past. `--manage` opens the bench console: the runs listed with numbers,
and commands to take one out or put it back — no server, no model, no run needed.
The same commands work *during* a run: press a key in its window and `bench>` answers
between two problems.

```powershell
.venv\Scripts\python.exe lcb_bench.py --manage
```

```
Runs in the report:
  1  qwen38-flash-next-q2-harness        73.0%  slow codegen  yes  100  d46433bd9f02  2026-09-23
  2  qwen38-flash-next-q2-noharness      69.0%  slow codegen  no   100  d46433bd9f02  2026-09-24
help  list [archive]  delete <number|name>  restore <number|name>  report  status  quit
```

`delete 2` takes run 2 out of the report; `restore 1` brings it back. Nothing is ever
erased — a deleted run moves to `bench\_archive\<stamp>__<folder>` and comes back from
there. The current run (marked `*`) refuses to be deleted while it is writing.

Same thing in one line, for scripts: `--list-runs`, `--delete-run 2`,
`--delete-run qwen38-flash-next-q2-noharness`, `--restore-run 1`. A name has to
identify one run; a prefix matching several is refused, not guessed.

## With the harness or without it

A **harness** is an agent chat (deepseek-harness, Cursor, Codex CLI, Claude Code,
an IDE copilot…) that talks to the same model server you are benchmarking. Its
requests share the single `-np 1` slot, so they queue — the score stands, but wall
time (`gen-min`) inflates. The bench therefore refuses to guess which situation a
run is in, and asks instead. That is also how you can bench a model nobody thought
to pre-register: it never needs to know your harness, only which run you mean.

**Without harness (the comparable baseline).** Run the steps above from your own
PowerShell, chat closed, and answer `1`. Nothing else may hit the server or the
speed columns lie. Switch models freely: stop the server, start another, and the
next run labels itself from that server.

**With the harness open (`harness = yes`).** The same commands work from inside an
agent chat, but only for the model that chat runs on — restarting that server kills
the chat driving the run. Expect it to be slower: `-np 1` means one slot, so chat
and benchmark queue behind each other. Nothing can answer the question in that
shell, so say it in the command line. The address is not something you have to
remember: it comes from that shell's own environment, or from what your earlier runs
used, so this is all you add:

```powershell
.venv\Scripts\python.exe lcb_bench.py --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8 --harness yes
```

Add `--harness-note "http://host:port"` only when the agent you mean is not the one it
offers — in a window's questions that is choice `[2]`.

That is exactly one command per test, twice: once answered in your window (`1`),
once declared from the chat. Each answer gets its own folder and its own report row
(`...-noharness`, `...-harness`), so the two never overwrite each other. `--both` in
your own window does those two passes back to back in one command and prints the pair
together at the end.

## Flags you may actually need

| Flag | Why |
|---|---|
| `--name` | optional: your own label instead of the one asked for (run folder + report row + resume key) |
| `--base-url` / `--api-key` / `--model` | any server that is not llama-server on 8080 (LM Studio, vLLM, Ollama, another machine). `--api-key` sends `Authorization: Bearer`; `LCB_API_KEY` works too |
| `--max-tokens` | thinking + answer budget (16384 default); truncated = auto-fail |
| `--limit N` | first N problems only (quick check) |
| `--workers N` | concurrent requests; 1 for a `-np 1` server, = llama-server `--parallel` otherwise |
| `--random-sample 100` | the fixed shared 100-problem set for comparable scores |
| `--hardest N` | give N slots of the run to the hardest problems the release has (hard first, then most test cases, then newest). With `--random-sample 100` the run stays exactly 100 problems: N hardest + the rest spread evenly over easy/medium/hard. Alone the run IS those N; with `--limit` it is ignored. Stops a strong model printing a meaningless 100% off an easy-weighted sample; it changes `sample`, so old rows stay separate |
| `--extra-body JSON` | model-specific request extras, e.g. thinking off: `'{"chat_template_kwargs":{"enable_thinking":false}}'` (flag name differs per family) |
| `--difficulty easy\|medium\|hard` | subset |
| `--skip-generate` / `--skip-eval` | re-grade what you have / generate only |
| `--harness yes\|no` | answer the harness question up front — required for detached / piped runs, where nothing can be asked. `yes` also fills in the harness address it knows (this shell's own agent via `DSH_WEB_URL`, else the one your earlier runs used) and pings it |
| `--harness-note "ADDRESS"` | only when the harness is *not* the one it offers: say which it was (address preferred). The bench pings an address at the start and end of the run and saves the result as evidence in `_summary.json` (`harness_probe`). A name or path is stored as text only, nothing is opened. An address you use is remembered in `bench\harness-address.json`, so the next run offers it as choice `[1]` |
| `--set-harness yes\|no` | backfill the answer for a run that never gave one (`--name` = its folder) |
| `--scenario code_execution\|test_output_prediction` | the two optional fast tests (much quicker, narrower skills; the report flags them `test = fast`; each scenario keeps its own folder `-exec` / `-top`) |
| `--both` | one command, both rows: the scenario runs twice on identical problems — pass 1 answered *no* (nothing else on the model server), pass 2 answered *yes* (the agent chat working). Asked once, up front. Never guessed, and the harness never sees a problem |
| `--manage` / `--list-runs` / `--delete-run` / `--restore-run` | the bench console and its one-line forms: numbered list of runs, take one out of the report, bring it back. No server and no model needed; nothing is erased (moves to `bench\_archive\`) |

## When something is wrong

| Symptom | Fix |
|---|---|
| `WinError 5 Access is denied` | you are in an agent/IDE sandbox — use a normal PowerShell |
| `ConnectionError` / refused | server not up, or wrong `--base-url` |
| generations stop mid-run | server restarted or OOM — rerun the same command, it resumes |
| score 0, everything fails | check answers in `bench\<run>\generations.jsonl`; try `--extra-body` to disable thinking |
| only **one** row appeared, you expected two | normal: one run answers the harness question once and writes one row. `--both` asks for both answers and writes both rows |
| SCORE came out **100%** | the sample was too small or too easy for that model — read the easy/medium/hard split and the `hardest` line, then rerun the same line with `--random-sample 100 --hardest 25` in it. The bench says this out loud rather than letting the 100 stand |
| old rows keep showing in the print | they are in `report.md` on purpose; a run prints only its own row(s). Take a run out with `--delete-run` (or `delete` in the console) |
| `trunc%` high | raise `--max-tokens` (respect `-c`), keep the cap identical across models |
| It asks the label / harness question where you cannot type | Fine in a normal PowerShell (it answers); detached, piped or agent shells cannot, so pass `--name LABEL` and `--harness yes\|no` in the command. A run that answers nothing is recorded `harness = unknown` |

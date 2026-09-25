# README_MINIMAL — bench ANY local model in 5 commands

Not model-specific: any GGUF you just downloaded, any quant you already own,
LM Studio / vLLM / Ollama too. You end up with one number per model —
**SCORE** = % of real competitive-programming problems solved — plus speeds, and
a table comparing every model you have tested.

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
| vLLM | `--base-url http://localhost:8000/v1` (+ `--api-key KEY` if it demands one) |
| Ollama (OpenAI shim) | `--base-url http://localhost:11434/v1` |

**The one rule:** the server context `-c` must fit prompt (~4k) **plus** the
answer budget `--max-tokens`. `-c 65536` or more → `--max-tokens 16384`;
`-c 32768` → `--max-tokens 12288`. Small model, small disk, no preference? Use
`-c 65536` + `--max-tokens 16384`.

## Step 2 (~2 min): can it answer, and how fast?

```powershell
.venv\Scripts\python.exe lcb_bench.py --name MYMODEL --speed-probe --probe-only
```

`MYMODEL` is your own short label — it becomes `bench\MYMODEL\` and the report
row. No problems yet: this fires 2 requests and prints prefill/decode tok/s,
saved for reuse. Rough runtime: `hours ≈ problems × 7200 ÷ decode_tok_s ÷ 3600`
(7200 = what a thinking model burns per problem; plain models use far less).

## Step 3 (~10–40 min): does the whole pipeline score it?

```powershell
.venv\Scripts\python.exe lcb_bench.py --name MYMODEL-smoke --limit 3 --max-tokens 16384 --eval-workers 8
```

Only 3 problems, so the SCORE here is noise — you are checking plumbing
(answers come out, code gets extracted, the grader runs). Clean up after:
`Remove-Item -Recurse -Force bench\MYMODEL-smoke`.

## Step 4 (hours, resumable): the real score

```powershell
.venv\Scripts\python.exe lcb_bench.py --name MYMODEL --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8
```

Same 100 problems for every model → comparable. Ctrl+C, a crash or a server
restart are cheap: **rerun the exact same command**, it skips what is finished.
Keep the flags you started with (they are what gets recorded).

12 h is long, so detach it — close the window, it keeps running:

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" -WorkingDirectory "B:\repos\MyProjects\_LiveCodeBench" `
  -RedirectStandardOutput "bench\MYMODEL.out.log" -RedirectStandardError "bench\MYMODEL.err.log" `
  -ArgumentList "--name","MYMODEL","--speed-probe","--random-sample","100","--workers","1","--max-tokens","16384","--eval-workers","8"
Get-Content bench\MYMODEL.out.log -Wait -Tail 20
```

What it prints at the end:

```
SCORE: 73.0%  (100 problems, 1 sample(s) each) [sample d46433bd9f02]
  pass@1: 73.00%   easy: 96.77% (31)   medium: 97.22% (36)   hard: 24.24% (33)
  speed: decode 18.3 tok/s, prefill/lecture 365.8 tok/s
  gen: 29/100 hit the 16384-token cap (truncated = auto-fail), 0 failed request(s)
     when-complete: 98.6% of the 71/100 answers that fit in the budget passed
   harness: no
```

`SCORE` is the headline. `trunc%` big + `when-complete` high = the model can
code, it just rambles past the budget → raise `--max-tokens` (respect the `-c`
rule) and keep the same cap on every row you compare.

## Step 5: the total report of every model you tested

```powershell
.venv\Scripts\python.exe lcb_bench.py --report-only
```

→ `bench\report.md` (and `report.csv`), best row first, one row per run:

```
model                 harness  scenario  SCORE  problems  n  cap    trunc%  when-complete ...
qwen38-flash-next-q2  yes      codegen   73.0   100       1  16384  29.0    98.6
```

**`harness`** says whether that run happened while the deepseek-harness chat was
open on the same server — those rows queue behind the agent, so their wall time
(`gen-min`) is inflated. Runs made before this column exist show `unknown`;
label one:

```powershell
.venv\Scripts\python.exe lcb_bench.py --set-harness no --name MYMODEL
```

Rows are comparable only when `scenario` / `problems` / `n` / `cap` / `sample`
match. To re-grade stored answers without regenerating:
`--name MYMODEL --skip-generate --skip-eval --random-sample 100`.

## With the harness or without it

**No harness (recommended — gets `harness = no` automatically).** Run the steps
above from your own PowerShell and keep the chat closed while it generates.
Nothing else may hit the server or the speed columns lie. You can switch models
freely: stop the server, start another, use a new `--name`.

**With the harness open (`harness = yes`).** Same commands work from inside an
agent chat, but only for the model that chat is running on — restarting that
server kills the chat driving the run, so you cannot test other models this way.
It is also slower: `-np 1` means one slot, chat and benchmark queue behind each
other. Auto-detection only looks at the `DSH_*` env vars of the shell that
started the bench, so a run launched from your own PowerShell while the chat is
open needs the flag spelled out:

```powershell
.venv\Scripts\python.exe lcb_bench.py --name MYMODEL --harness yes --speed-probe --random-sample 100 --workers 1 --max-tokens 16384 --eval-workers 8
```

## Flags you may actually need

| Flag | Why |
|---|---|
| `--name` | required: run folder + report row + resume key |
| `--base-url` / `--api-key` / `--model` | server that is not llama-server on 8080 |
| `--max-tokens` | thinking + answer budget (16384 default); truncated = auto-fail |
| `--limit N` | first N problems only (quick check) |
| `--workers N` | concurrent requests; 1 for a `-np 1` server, = llama-server `--parallel` otherwise |
| `--random-sample 100` | the fixed shared 100-problem set for comparable scores |
| `--extra-body JSON` | model-specific request extras, e.g. thinking off: `'{"chat_template_kwargs":{"enable_thinking":false}}'` (flag name differs per family) |
| `--difficulty easy\|medium\|hard` | subset |
| `--skip-generate` / `--skip-eval` | re-grade what you have / generate only |
| `--harness yes\|no`, `--set-harness` | report column above |
| `--scenario code_execution\|test_output_prediction` | the two optional extra datasets (much quicker, narrower skills) |

## When something is wrong

| Symptom | Fix |
|---|---|
| `WinError 5 Access is denied` | you are in an agent/IDE sandbox — use a normal PowerShell |
| `ConnectionError` / refused | server not up, or wrong `--base-url` |
| generations stop mid-run | server restarted or OOM — rerun the same command, it resumes |
| score 0, everything fails | check answers in `bench\MYMODEL\generations.jsonl`; try `--extra-body` to disable thinking |
| `trunc%` high | raise `--max-tokens` (respect `-c`), keep the cap identical across models |

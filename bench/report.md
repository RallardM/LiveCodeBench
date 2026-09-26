# LiveCodeBench model comparison

```

run                                                     model                                       harness  test  scenario  SCORE  problems  n  cap    trunc%  when-complete  easy   medium  hard   prefill-tok/s  gen-tok/s  gen-min  sample        date      
----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
qwen38-flash-next-q2-harness                            unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  yes      slow  codegen   73.0   100       1  16384  29.0    98.6           96.8   97.2    24.2   365.8          18.3       751.8    d46433bd9f02  2026-09-23
qwen38-flash-next-q2-noharness                          unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  no       slow  codegen   69.0   100       1  16384  31.0    97.1           100.0  86.1    21.2   243.7          18.1       686.1    d46433bd9f02  2026-09-24
unsloth-Qwen3.8-Flash-Next-GGUF-UD-IQ4_XS-exec-harness  unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS   yes      fast  exec      100.0  100       1  16384  0.0     100.0          100.0  100.0   100.0  61.8           14.5       56.0     d29d79fc371c  2026-09-25
qwen38-flash-next-q2-exec-harness                       unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  yes      fast  exec      98.0   100       1  16384  0.0     98.0           100.0  96.2    100.0  207.1          18.6       39.1     d29d79fc371c  2026-09-24
```

SCORE = pass@1 % on the sampled problem set - the single headline value.
run = bench folder label; model = the model id the server reported for
that run, so two rows for the same model are always the same model.
prefill-tok/s = prompt-reading (lecture) speed; gen-tok/s = generation speed.
cap = per-answer max_tokens (thinking budget); trunc% = share of answers that hit it.
when-complete = pass rate over the answers that did NOT hit the cap (skill signal).
test = slow (code_generation, the main scenario) or fast (code_execution /
test_output_prediction): fast rows measure narrower skills, run much quicker,
and compare across models only against the same scenario's own rows.
harness = did an agent chat share the model server during that run: every
run answers that question out loud (never guessed), because those requests
queue behind the benchmark and inflate gen-min (wall time) of harness=yes
rows. It never means the agent answered, and nothing is routed
through it: no harness sees a problem, so every row is the raw model
answering for itself. A harness address given in the note is pinged at
the start and end of the run and saved in that run's _summary.json as
harness_probe; a name or path is stored as text only.
unknown = nobody answered (older run, or a detached run started
without --harness) - backfill it with --set-harness yes|no --name <run>.
Rows are directly comparable only when test / scenario / problems / n / cap /
sample columns match.

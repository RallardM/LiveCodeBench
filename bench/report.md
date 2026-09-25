# LiveCodeBench model comparison

```

model                 harness  scenario  SCORE  problems  n  cap    trunc%  when-complete  easy   medium  hard  prefill-tok/s  gen-tok/s  gen-min  sample        date      
---------------------------------------------------------------------------------------------------------------------------------------------------------------------------
qwen38-flash-next-q2  yes      codegen   73.0   100       1  16384  29.0    98.6           96.8   97.2    24.2  365.8          18.3       751.8    d46433bd9f02  2026-09-23
RUN                   no       codegen   69.0   100       1  16384  31.0    97.1           100.0  86.1    21.2  243.7          18.1       686.1    d46433bd9f02  2026-09-24
```

SCORE = pass@1 % on the sampled problem set - the single headline value.
prefill-tok/s = prompt-reading (lecture) speed; gen-tok/s = generation speed.
cap = per-answer max_tokens (thinking budget); trunc% = share of answers that hit it.
when-complete = pass rate over the answers that did NOT hit the cap (skill signal).
harness = was the deepseek-harness/agent chat open on the same model server
during that run: its requests queue behind the benchmark, so gen-min (wall time)
of harness=yes rows is inflated; unknown = run predates this column, backfill
it with --set-harness yes|no --name <run>.
Rows are directly comparable only when scenario / problems / n / cap /
sample columns match.

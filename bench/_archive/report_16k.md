# LiveCodeBench model comparison

```

model                 scenario  SCORE  problems  n  pass@5  easy  medium  hard  prefill-tok/s  gen-tok/s  gen-min  sample        date      
-------------------------------------------------------------------------------------------------------------------------------------------
qwen38-flash-next-q2  codegen   73.0   100       1  -       96.8  97.2    24.2  365.8          18.3       751.8    d46433bd9f02  2026-09-23
```

SCORE = pass@1 % on the sampled problem set — the single headline value.
prefill-tok/s = prompt-reading (lecture) speed; gen-tok/s = generation speed.
Rows are directly comparable only when problems / n / sample match.

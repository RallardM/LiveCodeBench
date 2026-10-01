# LiveCodeBench model comparison

```

run                                                        model                                       harness  test  scenario  SCORE  problems  n  bundle  cap        trunc%  when-complete  easy   medium  hard   hardest  code  math  science  reading  prefill-tok/s  gen-tok/s  gen-min  sample        date      
----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
qwen38-flash-next-q2-harness                               unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  yes      slow  codegen   73.0   100       1  -       16384      29.0    98.6           96.8   97.2    24.2   -        -     -     -        -        365.8          18.3       751.8    d46433bd9f02  2026-09-23
qwen38-flash-next-q2-noharness                             unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  no       slow  codegen   69.0   100       1  -       16384      31.0    97.1           100.0  86.1    21.2   -        -     -     -        -        243.7          18.1       686.1    d46433bd9f02  2026-09-24
zz-mix-cg2025-cap32k-harness                               unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  yes      slow  codegen   50.0   14        1  -       32768      50.0    71.4           100.0  100.0   36.4   44.4     -     -     -        -        90.3           16.8       301.4    ed62141d80cf  2026-09-27
zz-mix-cg2025-harness                                      unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  yes      slow  codegen   40.0   20        1  -       16384      70.0    100.0          100.0  66.7    26.7   30.0     -     -     -        -        189.8          18.0       271.2    ed62141d80cf  2026-09-27
unsloth-Qwen3.8-Flash-Next-GGUF-UD-IQ4_XS-exec-harness     unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS   yes      fast  exec      100.0  100       1  -       16384      0.0     100.0          100.0  100.0   100.0  -        -     -     -        -        61.8           14.5       56.0     d29d79fc371c  2026-09-26
unsloth-Qwen3.8-Flash-Next-GGUF-UD-IQ4_XS-exec-noharness   unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS   no       fast  exec      100.0  100       1  -       16384      0.0     100.0          100.0  100.0   100.0  100.0    -     -     -        -        63.0           15.0       61.4     f96d1cedd828  2026-09-26
zz-bundle4-exec-harness                                    unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS   yes      fast  exec      100.0  40        1  x4      16384      0.0     100.0          100.0  100.0   100.0  -        -     -     -        -        31.7           13.5       170.5    3dd1f120ed86  2026-09-28
zz-hd-exec-harness                                         unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  yes      fast  exec      100.0  60        1  -       16384      0.0     100.0          -      100.0   100.0  100.0    -     -     -        -        197.5          18.6       96.3     a784281ba2c2  2026-09-26
zz-mix-exec-harness                                        unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  yes      fast  exec      100.0  10        1  -       16384      0.0     100.0          100.0  100.0   100.0  100.0    -     -     -        -        186.9          18.2       28.1     60bf18eca65a  2026-09-26
unsloth-Qwen3.8-Flash-Next-GGUF-UD-Q2_K_XL-exec-noharness  unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  no       fast  exec      99.0   100       1  x3      16384      0.0     99.0           100.0  98.4    100.0  100.0    -     -     -        -        240.6          18.5       118.3    4a2f47435cf9  2026-09-29
qwen38-flash-next-q2-exec-harness                          unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  yes      fast  exec      98.0   100       1  -       16384      0.0     98.0           100.0  96.2    100.0  -        -     -     -        -        207.1          18.6       39.1     d29d79fc371c  2026-09-24
unsloth-Qwen3.8-Flash-Next-GGUF-UD-Q2_K_XL-exec-harness    unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  yes      fast  exec      98.0   100       1  x3      16384      0.0     98.0           100.0  96.7    100.0  96.0     -     -     -        -        237.0          17.8       132.9    4a2f47435cf9  2026-09-29
unsloth-Qwen3.6-35B-A3B-GGUF-UD-Q4_K_XL-fast-noharness     unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL     no       fast  fast      50.0   32        1  -       3072-8192  53.1    93.3           50.0   62.5    50.0   37.5     12.5  62.5  50.0     75.0     369.3          38.8       60.1     c13dd219018f  2026-09-30
unsloth-Qwen3.6-35B-A3B-GGUF-UD-Q4_K_XL-fast-harness       unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL     yes      fast  fast      46.88  31        1  -       3072-8192  54.8    85.7           37.5   62.5    50.0   37.5     12.5  62.5  37.5     75.0     365.5          39.9       56.6     c13dd219018f  2026-09-30
unsloth-Qwen3.8-Flash-Next-GGUF-UD-IQ4_XS-top-noharness    unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS   no       fast  top       100.0  100       1  x2      16384      0.0     100.0          100.0  100.0   100.0  100.0    -     -     -        -        40.7           14.9       144.9    43ffff4bbbf0  2026-09-29
zz-bundle2-top-harness                                     unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS   yes      fast  top       95.83  24        1  x2      16384      4.2     100.0          100.0  91.7    100.0  -        -     -     -        -        47.3           14.3       58.2     76e0194692f2  2026-09-28
zz-hd-top-harness                                          unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL  yes      fast  top       95.0   60        1  -       16384      1.7     96.6           -      -       95.0   95.0     -     -     -        -        148.6          18.6       112.8    d732b02367fb  2026-09-26
```

SCORE = pass@1 % (the single headline number, higher is better). run = bench
folder label; model = the model id the server reported for that run.
test = slow (code_generation and the long suite) or fast (the fast suite and
the code_execution / test_output_prediction scenarios). Fast rows compare only
with fast rows of the same scenario.
Suite rows (scenario fast / long): SCORE is the mean of the tier cells, every
skill x tier cell weighs the same; easy / medium / hard / hardest are the tier
scores over all skills, code / math / science / reading the skill scores over
all tiers; problems = items actually finished inside the time budget.
prefill-tok/s = prompt-reading speed, gen-tok/s = generation speed, both from
the server's own timings. cap = per-answer max_tokens; trunc% = share of answers
that hit it; when-complete = pass rate over the answers that did NOT hit it.
harness = did an agent chat share the model server during that run (asked out
loud, never guessed): its requests queue behind the benchmark and inflate gen-min.
Nothing is routed through the harness, so both rows are the raw model. unknown =
nobody answered - set it with --set-harness. bundle = xK: items of K calls
answered in one go, all-or-nothing (--bundle K).
Rows are directly comparable only when test / scenario / problems / n / bundle /
cap / sample columns match (suite rows: same scenario and about the same
problems, the sample is a fixed order and a faster model just goes further).


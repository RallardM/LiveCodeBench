import json, io
raw = open("zz_run_cg.out", "rb").read()
txt = raw.decode("utf-16-le", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
lines = txt.splitlines()
idx = [i for i, l in enumerate(lines) if l.strip().startswith("SCORE")]
print("SCORE blocks:", len(idx))
for l in lines[idx[-1]-6:]:
    print(l)

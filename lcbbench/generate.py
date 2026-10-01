"""Talking to the model server: requests, checkpoint file, speed probe and the
speed / truncation numbers that every summary carries."""

import json
import os
import sys
import threading
import time

import requests

from . import state

_tls = None


def auth_headers():
    """Bearer header for servers that want one (--api-key); empty otherwise."""
    return {"Authorization": f"Bearer {state.API_KEY}"} if state.API_KEY else {}


def _session():
    global _tls
    if _tls is None:
        _tls = threading.local()
    if not hasattr(_tls, "session"):
        s = requests.Session()
        s.headers.update(auth_headers())
        _tls.session = s
    return _tls.session


def generate_one(task, base_url, model_id, payload_extras, max_retries, read_timeout):
    qid, si, messages = task
    body = dict(payload_extras)
    body.update({"model": model_id, "messages": messages, "stream": False})
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            t0 = time.time()
            r = _session().post(
                f"{base_url}/chat/completions", json=body, timeout=(30, read_timeout)
            )
            r.raise_for_status()
            data = r.json()
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            output = msg.get("content") or ""
            # llama.cpp >= b4599 returns thinking separately; if content empty use it
            if not output and msg.get("reasoning_content"):
                output = msg["reasoning_content"]
            usage = data.get("usage") or {}
            timings = data.get("timings") or {}  # llama.cpp server-side, queue-free
            return {
                "qid": qid,
                "si": si,
                "output": output,
                "seconds": round(time.time() - t0, 2),
                "completion_tokens": usage.get("completion_tokens"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "max_tokens": payload_extras.get("max_tokens"),
                "timings": {
                    "prompt_n": timings.get("prompt_n"),
                    "prompt_ms": timings.get("prompt_ms"),
                    "predicted_n": timings.get("predicted_n"),
                    "predicted_ms": timings.get("predicted_ms"),
                } if timings else None,
                "failed": False,
            }
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)[:400]
            time.sleep(min(60, 10 * attempt))
    return {
        "qid": qid, "si": si, "output": "", "seconds": None,
        "completion_tokens": None, "failed": True, "error": last_err,
    }


def load_gen_checkpoint(gen_path):
    done = {}
    if os.path.exists(gen_path):
        with open(gen_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                key = (rec["qid"], rec["si"])
                if not rec.get("failed"):
                    done[key] = rec
                else:
                    done.pop(key, None)  # failed entries don't count
    return done


def speed_probe(base_url, model_id):
    """Measure llama.cpp server-side prefill and decode speed with 2 requests.
    Prefill = 'lecture' speed (prompt processing tok/s); decode = generation tok/s.
    Uses server-side timings when available (excludes queue wait)."""
    filler = "The quick brown fox jumps over the lazy dog near the riverbank. " * 105
    out = {}
    try:
        body = {"model": model_id, "max_tokens": 1, "temperature": 0.0, "stream": False,
                "messages": [{"role": "user", "content": filler + "\nReply with exactly: OK"}]}
        t0 = time.time()
        d = _session().post(f"{base_url}/chat/completions", json=body, timeout=(30, 3600)).json()
        u, tg = d.get("usage") or {}, d.get("timings") or {}
        if tg.get("prompt_n") and tg.get("prompt_ms"):
            out["prefill_tokens_per_sec"] = round(tg["prompt_n"] / (tg["prompt_ms"] / 1000), 1)
        elif u.get("prompt_tokens"):
            out["prefill_tokens_per_sec"] = round(u["prompt_tokens"] / (time.time() - t0), 1)
    except Exception as e:  # noqa: BLE001
        out["prefill_error"] = repr(e)[:200]
    try:
        body = {"model": model_id, "max_tokens": 300, "temperature": 0.0, "stream": False,
                "messages": [{"role": "user",
                              "content": "Repeat exactly 300 times, space separated: ping"}]}
        t0 = time.time()
        d = _session().post(f"{base_url}/chat/completions", json=body, timeout=(30, 600)).json()
        u, tg = d.get("usage") or {}, d.get("timings") or {}
        if tg.get("predicted_n") and tg.get("predicted_ms"):
            out["decode_tokens_per_sec"] = round(tg["predicted_n"] / (tg["predicted_ms"] / 1000), 1)
        elif u.get("completion_tokens"):
            out["decode_tokens_per_sec"] = round(u["completion_tokens"] / (time.time() - t0), 1)
    except Exception as e:  # noqa: BLE001
        out["decode_error"] = repr(e)[:200]
    return out


def ensure_speed_probe(base_url, model_id, run_dir):
    """Run the probe once per run folder and remember it in speed_probe.json."""
    sp_path = os.path.join(run_dir, "speed_probe.json")
    if os.path.exists(sp_path):
        print(f"Speed probe already recorded: {open(sp_path, encoding='utf-8').read().strip()}")
        return
    print("Running server speed probe (may wait behind a generation)...")
    try:
        probe = speed_probe(base_url, model_id)
    except Exception as exc:
        sys.exit(f"Speed probe failed against {base_url}"
                 f" ({type(exc).__name__}): is the server up? For anything"
                 " that is not llama-server on :8080 pass --base-url (plus"
                 " --model and --api-key if that server wants them).")
    probe["model"] = model_id
    with open(sp_path, "w", encoding="utf-8") as f:
        json.dump(probe, f, indent=2)
    print(f"Speed probe: {probe}")


def speed_summary(done, run_dir):
    """The read (prefill) and write (decode) speed of a whole run in tok/s, from
    the server's own timings (queue wait excluded), plus per-problem averages."""
    secs = sum(r["seconds"] for r in done.values() if r.get("seconds"))
    tok = sum(r["completion_tokens"] or 0 for r in done.values())
    ptok = sum(r.get("prompt_tokens") or 0 for r in done.values())
    pn = sum((r.get("timings") or {}).get("prompt_n") or 0 for r in done.values())
    pms = sum((r.get("timings") or {}).get("prompt_ms") or 0 for r in done.values())
    pnn = sum((r.get("timings") or {}).get("predicted_n") or 0 for r in done.values())
    pnm = sum((r.get("timings") or {}).get("predicted_ms") or 0 for r in done.values())
    probe_path = os.path.join(run_dir, "speed_probe.json")
    probe = json.load(open(probe_path, encoding="utf-8")) if os.path.exists(probe_path) else {}
    prefill = round(pn * 1000.0 / pms, 1) if pms and pn else probe.get("prefill_tokens_per_sec")
    decode = round(pnn * 1000.0 / pnm, 1) if pnm and pnn else probe.get("decode_tokens_per_sec")
    if decode is None:
        decode = round(tok / secs, 1) if secs else None
    return {
        "total_generation_seconds": round(secs, 1),
        "total_completion_tokens": tok,
        "mean_tokens_per_sec": round(tok / secs, 2) if secs else None,
        "mean_seconds_per_problem": round(secs / len(done), 1) if done else None,
        "avg_prompt_tokens": round(ptok / len(done), 0) if (done and ptok) else None,
        "avg_completion_tokens": round(tok / len(done), 0) if done else None,
        "prefill_tokens_per_sec": prefill,
        "decode_tokens_per_sec": decode,
        "speed_probe": probe or None,
    }


def hit_cap(rec, default_cap):
    return bool(rec.get("completion_tokens")
                and rec["completion_tokens"] >= (rec.get("max_tokens") or default_cap or 0))


def generation_stats(done, gen_path, default_cap, per_problem_pass, missing_n):
    """Truncation and failure counters. A truncated answer is cut off mid-way
    (its partial code is still graded and rarely passes)."""
    n_gen = len(done)
    truncated = sum(1 for r in done.values() if hit_cap(r, default_cap))
    trunc_qids = {k[0] for k, r in done.items() if hit_cap(r, default_cap)}
    complete = [v for q, v in per_problem_pass.items() if q not in trunc_qids]
    failed = 0
    if os.path.exists(gen_path):
        with open(gen_path, encoding="utf-8") as f:
            failed = sum(1 for line in f if line.strip()) - n_gen
    return {
        "generated": n_gen,
        "failed_requests": failed,
        "not_generated": missing_n,
        "truncated_at_max_tokens": truncated,
        "truncated_pct": round(100.0 * truncated / n_gen, 1) if n_gen else None,
        "problems_not_truncated": len(complete),
        "pass_rate_when_not_truncated": round(100.0 * sum(complete) / len(complete), 1)
        if complete else None,
        "empty_outputs": sum(1 for r in done.values() if not r.get("output")),
    }

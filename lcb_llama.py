import json
import os
import time
import requests
from datasets import load_dataset

URL = "http://127.0.0.1:8080/v1/chat/completions"
OUT_FILE = "llama_results.json"
CONNECT_TIMEOUT = 30          # seconds to establish the connection
READ_TIMEOUT = 3600           # seconds to wait for a full response (16k tokens can take a while)
MAX_RETRIES = 3

# Resume: load any results already computed and skip those questions.
if os.path.exists(OUT_FILE):
    with open(OUT_FILE, encoding="utf-8") as f:
        results = json.load(f)
else:
    results = []
done_ids = {r["question_id"] for r in results}
print(f"Resuming: {len(done_ids)} problems already done, {len(results) - len(done_ids)} duplicate entries found")

dataset = load_dataset(
    "livecodebench/code_generation_lite",
    split="test",
    trust_remote_code=True
)


def save_results():
    # Write to a temp file then swap, so a crash mid-write can't corrupt the output.
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    os.replace(tmp, OUT_FILE)


failed = []

for i, problem in enumerate(dataset):
    qid = problem["question_id"]
    if qid in done_ids:
        continue

    print(f"[{i + 1}/{len(dataset)}] {qid}")

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                URL,
                json={
                    "messages": [
                        {
                            "role": "user",
                            "content": problem["question_content"]
                        }
                    ],
                    "temperature": 0.2,
                    "max_tokens": 16384
                },
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
            )
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"]
            last_err = None
            break
        except (requests.Timeout, requests.ConnectionError, requests.RequestException) as e:
            last_err = e
            print(f"    attempt {attempt}/{MAX_RETRIES} failed: {e}")
            time.sleep(15 * attempt)

    if last_err is not None:
        print(f"    GIVING UP on {qid} after {MAX_RETRIES} attempts")
        failed.append(qid)
        continue  # keep going; a re-run will retry these

    results.append({
        "question_id": qid,
        "code_list": [answer]
    })
    save_results()  # save after every problem so progress is never lost

print()
print(f"Finished. {len(results)} results saved to {OUT_FILE}")
if failed:
    print(f"{len(failed)} problems failed (re-run the script to retry them): {failed}")

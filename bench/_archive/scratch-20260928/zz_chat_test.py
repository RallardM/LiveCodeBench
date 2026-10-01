import json, urllib.request
body = {"model": "unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q2_K_XL", "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 64}
req = urllib.request.Request("http://127.0.0.1:8080/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
d = json.load(urllib.request.urlopen(req, timeout=120))
print("reply:", (d["choices"][0]["message"]["content"] or "")[:80])

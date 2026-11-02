import os, time, statistics, uuid, requests
from concurrent.futures import ThreadPoolExecutor

URL = "https://cushy-excavator-prism.ngrok-free.dev/v1/chat/completions"

PAYLOAD = {
    "model": "Qwen/Qwen3-8B-AWQ",
    "temperature": 0.4,
    "stream": False,
    "chat_template_kwargs": {"enable_thinking": False},
}

def make_payload():
    shared = "Server log line: service state changed, retry scheduled. " * 400   # same every call
    tail = f"Ticket {uuid.uuid4()}: " + ("Tool result: AppXSvc status Stopped, error 0. " * 60)
    return {
        **PAYLOAD,
        "messages": [{"role": "user", "content": f"{shared}\n{tail}\nWrite a short incident note."}],
        "max_tokens": 200,
    }

def one(_):
    t0 = time.time()
    try:
        r = requests.post(URL, json=make_payload(), timeout=180)
        dt = time.time() - t0
        usage = r.json().get("usage", {}) if r.status_code == 200 else {}
        print(f"  status={r.status_code} prompt_tokens={usage.get('prompt_tokens')} "
              f"completion_tokens={usage.get('completion_tokens')} t={dt:.1f}s")
        return r.status_code, dt
    except requests.RequestException as e:
        return type(e).__name__, time.time() - t0

for n in (1, 2, 4, 8, 12):
    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(one, range(n)))
    ok = [t for code, t in results if code == 200]
    errs = [code for code, _ in results if code != 200]
    if ok:
        print(f"N={n:>2}  ok={len(ok):>2}/{n}  p50={statistics.median(ok):.1f}s  "
              f"max={max(ok):.1f}s  errors={errs}")
    else:
        print(f"N={n:>2}  all failed: {errs}")
    time.sleep(5)
import json
import time
import urllib.request
from datetime import datetime

# Нынешний демо-прогон: надсмотрщик не умеет пинать queued (баг воркера
# «resume-not-noticed») — этот хелпер делает это за него, пока идёт демо.
WORKFLOWS = {
    "reader": "wf_1e0bd7a5cfb14d13a60c596e06c85fd9",
    # pkg-reverso-m2 / pkg-backup-m3: работа слита в main, воркфлоу мертвы —
    # не будим (иначе бесконечный цикл фейлов на удалённых worktree)
}


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        "http://127.0.0.1:8420" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main():
    nudged = set()
    for _ in range(120):  # до 4 часов, шаг 2 мин
        time.sleep(120)
        for name, wid in WORKFLOWS.items():
            if name in nudged:
                continue
            try:
                w = api(f"/api/workflows/{wid}")
            except Exception as e:
                print(f"[{datetime.now():%H:%M}] {name}: err {e}", flush=True)
                continue
            if w["status"] == "queued":
                try:
                    api(f"/api/workflows/{wid}/sync", "POST",
                        {"expected_version": w["state_version"]})
                    print(f"[{datetime.now():%H:%M}] {name}: queued -> sync-пинок",
                          flush=True)
                except Exception as e:
                    print(f"[{datetime.now():%H:%M}] {name}: sync err {e}", flush=True)


if __name__ == "__main__":
    main()

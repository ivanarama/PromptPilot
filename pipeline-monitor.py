import json
import sqlite3
import time
import urllib.request
from datetime import datetime

WF = "wf_1e0bd7a5cfb14d13a60c596e06c85fd9"
DB = r"C:\Users\Nachfin\.promptpilot\promptpilot.db"
LOG = r"C:\Users\Nachfin\.promptpilot\verdict-repair.log"


def fetch_workflow():
    with urllib.request.urlopen(
        f"http://127.0.0.1:8420/api/workflows/{WF}", timeout=10
    ) as r:
        return json.loads(r.read())


def last_task():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=4)
    try:
        return conn.execute(
            "SELECT id, provider, status FROM tasks ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def watcher_tail(lines=3):
    try:
        with open(LOG, encoding="utf-8") as f:
            return f.read().strip().split("\n")[-lines:]
    except OSError:
        return []


def main():
    last_snapshot = ""
    for _ in range(150):  # до ~7.5 часов, шаг 3 мин
        time.sleep(180)
        stamp = datetime.now().strftime("%H:%M")
        try:
            w = fetch_workflow()
            t = last_task()
            snap = json.dumps(
                {"s": w["status"], "r": w.get("current_round"), "t": t},
                ensure_ascii=False,
            )
            if snap != last_snapshot:
                print(f"[{stamp}] status={w['status']} round={w.get('current_round')} "
                      f"last_task={t}", flush=True)
                last_snapshot = snap
            for line in watcher_tail():
                if "AUTO-RESUME" in line or "без действий" in line:
                    print("WATCHER:", line, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[{stamp}] err: {e}", flush=True)


if __name__ == "__main__":
    main()

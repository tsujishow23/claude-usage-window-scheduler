#!/usr/bin/env python3
"""Claude の5時間利用枠を自動でアンカーするスクリプト。

GitHub Actions から30分ごとに呼ばれ、
  - 当日まだ ping していなければ FIRST_PING_TIME 以降に1回目を送る
  - 前回成功した ping から 5時間 + マージン 経過していれば次の ping を送る
を判定する。成功した ping の時刻は state/last_ping.json に保存する。

既定値 (FIRST_PING_TIME=07:00) では枠が 07-12 / 12-17 / 17-22 となり、
10:00〜19:00 の間に 2h / 5h / 2h の3つの枠を使える。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time as time_module
from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "last_ping.json"


def env(name: str, default: str) -> str:
    # 未設定のリポジトリ変数は空文字で渡ってくるので既定値に倒す
    return os.environ.get(name) or default


TZ = ZoneInfo(env("TIMEZONE", "Asia/Tokyo"))
FIRST_PING_TIME = time.fromisoformat(env("FIRST_PING_TIME", "07:00"))
LAST_PING_TIME = time.fromisoformat(env("LAST_PING_TIME", "18:59"))
WINDOW = timedelta(hours=float(env("WINDOW_HOURS", "5")))
MARGIN = timedelta(minutes=int(env("MARGIN_MINUTES", "2")))
# 次の ping 時刻がこの分数以内なら、次の cron を待たずにジョブ内で待機して時刻ぴったりに送る
MAX_WAIT = timedelta(minutes=int(env("MAX_WAIT_MINUTES", "15")))
MAX_PINGS = int(env("MAX_PINGS_PER_DAY", "3"))
# 1=月 ... 7=日。既定は毎日
ACTIVE_DAYS = {int(d) for d in env("ACTIVE_DAYS", "1,2,3,4,5,6,7").split(",") if d.strip()}
MODEL = env("PING_MODEL", "haiku")
PROMPT = env("PING_PROMPT", "Reply with just: ok")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def decide(now: datetime, state: dict) -> tuple[bool, str, timedelta]:
    """(ping するか, 理由, ping 前に待つ時間) を返す"""
    if now.isoweekday() not in ACTIVE_DAYS:
        return False, f"非稼働曜日 ({now:%a})", timedelta()
    if now.time() < FIRST_PING_TIME:
        return False, f"{FIRST_PING_TIME:%H:%M} より前", timedelta()
    if now.time() > LAST_PING_TIME:
        return False, f"{LAST_PING_TIME:%H:%M} より後", timedelta()

    today = now.date().isoformat()
    pings = state.get("pings", []) if state.get("date") == today else []
    if not pings:
        return True, "本日1回目", timedelta()
    if len(pings) >= MAX_PINGS:
        return False, f"本日の ping 上限 {MAX_PINGS} 回に到達", timedelta()

    last = datetime.fromisoformat(pings[-1])
    due = last + WINDOW + MARGIN
    if now >= due:
        return True, f"前回 {last:%H:%M} から5時間経過", timedelta()
    if due - now <= MAX_WAIT:
        return True, f"{due:%H:%M} まで待機してから ping", due - now
    return False, f"次の ping は {due:%H:%M} 以降", timedelta()


def clean_env() -> dict:
    # ターミナルからコピーしたトークンに混ざりがちな改行・空白・枠線文字を取り除く
    env_vars = dict(os.environ)
    token = env_vars.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    env_vars["CLAUDE_CODE_OAUTH_TOKEN"] = re.sub(r"[^A-Za-z0-9_-]", "", token)
    return env_vars


def send_ping() -> bool:
    cmd = ["claude", "-p", PROMPT, "--model", MODEL, "--max-turns", "1", "--output-format", "json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=clean_env())
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"ping 失敗: {e}")
        return False
    print(proc.stdout.strip()[:2000])
    if proc.stderr.strip():
        print(proc.stderr.strip()[:2000], file=sys.stderr)
    if proc.returncode != 0:
        return False
    try:
        return not json.loads(proc.stdout).get("is_error", False)
    except json.JSONDecodeError:
        return False


def main() -> int:
    now = datetime.now(TZ).replace(microsecond=0)
    force = os.environ.get("FORCE", "").lower() == "true"
    state = load_state()

    should, reason, wait = (True, "手動実行 (force)", timedelta()) if force else decide(now, state)
    print(f"[{now:%Y-%m-%d %H:%M %Z}] {'ping する' if should else 'スキップ'}: {reason}")

    if "--check" in sys.argv:
        # ワークフロー側で Claude Code のインストールを省略するための判定のみ
        if out := os.environ.get("GITHUB_OUTPUT"):
            with open(out, "a") as f:
                f.write(f"should_ping={'true' if should else 'false'}\n")
        return 0
    if not should:
        return 0

    if wait:
        time_module.sleep(wait.total_seconds())
        now = datetime.now(TZ).replace(microsecond=0)

    if not send_ping():
        # 利用上限到達 (=前の枠がまだ有効) や一時的な障害。記録せず次回に再試行する
        print("ping に失敗したので記録しません。次回の実行で再試行します。")
        return 1

    today = now.date().isoformat()
    pings = state.get("pings", []) if state.get("date") == today else []
    pings.append(now.isoformat())
    save_state({"date": today, "pings": pings})
    print(f"ping 成功。新しい枠は {now:%H:%M}〜{now + WINDOW:%H:%M} の想定")
    return 0


if __name__ == "__main__":
    sys.exit(main())

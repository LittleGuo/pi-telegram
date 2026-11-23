#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram ↔ 正在运行的 pi TUI 桥（经独立扩展，不依赖 pi-super-vim）

用法：
  python telegram-tui-bridge.py          后台长轮询
  python telegram-tui-bridge.py --once   只轮询一轮就退出

扩展路径：~/.pi/agent/extensions/pi-telegram-tui-bridge.ts
控制目录：%TEMP%\\pi-telegram-tui-bridge\\
  <sid>.req       原子写入的投递请求（text/mode/sid/ts）
  <sid>.settled   扩展在 agent_settled 时写入
  <sid>.alive     当前 TUI 会话在线标记
  current.json    最近一个 TUI 会话的 sid / cwd / sessionFile

扩展用 pi.sendUserMessage() 投递（TUI 可见；默认不把 / 当斜杠命令）。
完成以扩展写出的 .settled 为准，再从会话 jsonl 抽取本轮 assistant 文本。
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HOME = os.path.expanduser("~")
AGENT_DIR = os.path.join(HOME, ".pi", "agent")
CONFIG = os.path.join(AGENT_DIR, "notify.json")
STATE = os.path.join(AGENT_DIR, "telegram-tui-bridge-state.json")
LOG = os.path.join(AGENT_DIR, "telegram-tui-bridge.log")
PID_FILE = os.path.join(AGENT_DIR, "telegram-tui-bridge.pid")
OLD_STATE = os.path.join(AGENT_DIR, "telegram-bridge-state.json")
SESSIONS_ROOT = os.path.join(AGENT_DIR, "sessions")
CTRL_DIR = os.path.join(
    os.environ.get("PI_TELEGRAM_TUI_BRIDGE_DIR")
    or os.environ.get("TEMP")
    or os.environ.get("TMP")
    or os.environ.get("TMPDIR")
    or "/tmp",
    "pi-telegram-tui-bridge",
)
DEFAULT_CWD = "D:\\new-start\\make-class"
EXCLUDED_SIDS = {"telegram-bridge"}

LONG_POLL = 50
TURN_TIMEOUT = 900
DELIVER_TIMEOUT = 45
SETTLE_QUIET = 1.5
MAX_INPUT = 4000
MAX_REPLY = 3900
POLL_INTERVAL = 0.25


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    with io.open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_config():
    with io.open(CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)["telegram"]
    return cfg["token"], cfg.get("proxy"), str(cfg["chatId"])


TOKEN, PROXY, CHAT_ID = load_config()
if PROXY:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
    )
else:
    opener = urllib.request.build_opener()


def api(method, fields=None, timeout=LONG_POLL + 25):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(fields or {}).encode()
    with opener.open(url, data=data, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def send(text: str) -> None:
    if not text:
        text = "（pi 本轮没有文本回复）"
    for start in range(0, len(text), MAX_REPLY):
        api(
            "sendMessage",
            {"chat_id": CHAT_ID, "text": text[start : start + MAX_REPLY]},
            timeout=40,
        )


def load_offset():
    try:
        with io.open(STATE, encoding="utf-8") as fh:
            return json.load(fh).get("offset")
    except (OSError, ValueError):
        pass
    try:
        with io.open(OLD_STATE, encoding="utf-8") as fh:
            offset = json.load(fh).get("offset")
        if offset is not None:
            save_offset(offset)
            log("已从旧桥 state 继承 offset，之后只使用新 state 文件")
            return offset
    except (OSError, ValueError):
        pass
    return None


def save_offset(offset) -> None:
    tmp = STATE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"offset": offset}, fh)
    os.replace(tmp, STATE)


def write_pid() -> None:
    with io.open(PID_FILE, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))


# ---------------------------------------------------------------------------
# 窗口标题与会话发现
# ---------------------------------------------------------------------------

def wt_titles() -> list[str]:
    titles: list[str] = []
    if os.name != "nt":
        return titles
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title = buf.value or ""
        if cls.value == "CASCADIA_HOSTING_WINDOW_CLASS" or title.startswith("π") or "Windows Terminal" in title:
            if title:
                titles.append(title)
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return titles


def read_json(path: str):
    try:
        with io.open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def read_jsonl_header(path: str):
    try:
        with io.open(path, encoding="utf-8") as fh:
            line = fh.readline()
        if not line.strip():
            return None
        obj = json.loads(line)
        if obj.get("type") == "session":
            return obj
    except (OSError, ValueError):
        return None
    return None


def parse_session_filename(name: str):
    if not name.endswith(".jsonl"):
        return None
    stem = name[:-6]
    if "_" not in stem:
        return None
    ts, sid = stem.split("_", 1)
    return ts, sid


def settled_mtime(sid: str) -> float:
    p = os.path.join(CTRL_DIR, sid + ".settled")
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def load_presence_records() -> list[dict]:
    records: list[dict] = []
    if not os.path.isdir(CTRL_DIR):
        return records
    seen = set()
    for name in os.listdir(CTRL_DIR):
        if name != "current.json" and not name.endswith(".alive"):
            continue
        path = os.path.join(CTRL_DIR, name)
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        sid = str(data.get("sid") or "")
        if not sid or sid in EXCLUDED_SIDS or sid.startswith("telegram-bridge"):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        session_file = data.get("sessionFile") or ""
        rec = {
            "sid": sid,
            "file": session_file,
            "cwd": data.get("cwd") or "",
            "name": data.get("name") or "",
            "mtime": mtime,
            "from_extension": True,
        }
        key = (sid, session_file)
        if key in seen:
            # 取更新的那份
            for existing in records:
                if existing["sid"] == sid and existing["file"] == session_file and mtime > existing["mtime"]:
                    existing.update(rec)
            continue
        seen.add(key)
        records.append(rec)
    return records


def list_sessions_from_disk() -> list[dict]:
    found: list[dict] = []
    root = Path(SESSIONS_ROOT)
    if not root.is_dir():
        return found
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        for file in folder.glob("*.jsonl"):
            parsed = parse_session_filename(file.name)
            if not parsed:
                continue
            _, sid = parsed
            if sid in EXCLUDED_SIDS or sid.startswith("telegram-bridge"):
                continue
            header = read_jsonl_header(str(file)) or {}
            try:
                mtime = file.stat().st_mtime
            except OSError:
                continue
            found.append(
                {
                    "sid": header.get("id") or sid,
                    "file": str(file),
                    "cwd": header.get("cwd") or "",
                    "name": "",
                    "mtime": mtime,
                    "from_extension": False,
                }
            )
    return found


def resolve_tui_session() -> dict | None:
    present = load_presence_records()
    disk = list_sessions_from_disk()
    # 扩展在线记录优先；用磁盘 jsonl 补全缺失的路径
    by_sid = {item["sid"]: item for item in disk}
    sessions = []
    for rec in present:
        if not rec.get("file") and rec["sid"] in by_sid:
            rec = dict(rec)
            rec["file"] = by_sid[rec["sid"]]["file"]
            rec["cwd"] = rec["cwd"] or by_sid[rec["sid"]].get("cwd") or ""
        if rec.get("file"):
            sessions.append(rec)
    extension_loaded = bool(sessions)
    if not sessions:
        sessions = disk

    if not sessions:
        return None

    titles = wt_titles()
    title_blob = " ".join(titles)
    default_cwd = os.path.normcase(os.path.abspath(DEFAULT_CWD))

    def score(item: dict) -> float:
        sid = item["sid"]
        points = item["mtime"] / 1e12
        if item.get("from_extension"):
            points += 2000 + item["mtime"]
        art = settled_mtime(sid)
        if art:
            points += 100 + art
        cwd = os.path.normcase(os.path.abspath(item["cwd"])) if item["cwd"] else ""
        if cwd == default_cwd:
            points += 200
        if title_blob:
            if "make-class" in title_blob and "make-class" in (item["cwd"] or "").replace("\\", "/"):
                points += 80
            if item.get("name") and item["name"] in title_blob:
                points += 60
            if "π" in title_blob:
                points += 10
        return points

    ranked = sorted(sessions, key=score, reverse=True)
    best = ranked[0]
    best["extension_loaded"] = extension_loaded
    return best


# ---------------------------------------------------------------------------
# 忙碌 / 投递 / 完成
# ---------------------------------------------------------------------------

def iso_to_epoch(ts: str) -> float:
    if not ts:
        return 0.0
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return 0.0


def iter_jsonl_from(path: str, start: int):
    try:
        fh = io.open(path, "r", encoding="utf-8")
    except OSError:
        return
    with fh:
        fh.seek(start)
        while True:
            pos = fh.tell()
            line = fh.readline()
            if not line:
                break
            if not line.strip():
                continue
            if not line.endswith("\n"):
                break
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            yield pos, fh.tell(), obj


def last_user_message(path: str):
    last = None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    for pos, end, obj in iter_jsonl_from(path, 0):
        if obj.get("type") == "message" and (obj.get("message") or {}).get("role") == "user":
            last = {
                "id": obj.get("id"),
                "ts": iso_to_epoch(obj.get("timestamp") or ""),
                "end": end,
            }
    if last is None:
        last = {"id": None, "ts": 0.0, "end": size}
    return last


def is_pi_busy(sid: str, jsonl_path: str) -> bool:
    """True only when a turn looks in-flight.

    After a TUI restart the extension has not written .settled yet. A
    historical last-user timestamp must not be treated as "busy" (that
    produced a false 「pi 正在忙」 notice on an idle session).
    """
    art = settled_mtime(sid)
    user = last_user_message(jsonl_path)
    now = time.time()
    try:
        jsonl_mtime = os.path.getmtime(jsonl_path)
    except OSError:
        jsonl_mtime = 0.0

    if art <= 0:
        return jsonl_mtime > 0 and (now - jsonl_mtime) < 8

    if user and user.get("id") and user["ts"] > art + 0.5:
        # User message after last settle: busy only if jsonl is still moving.
        return (now - jsonl_mtime) < 30

    if jsonl_mtime > art + 0.5 and (now - jsonl_mtime) < 8:
        return True
    return False


def write_request(sid: str, text: str, mode: str) -> str:
    os.makedirs(CTRL_DIR, exist_ok=True)
    payload = {
        "sid": sid,
        "text": text.replace("\r\n", "\n").strip("\n"),
        "mode": mode,
        "ts": int(time.time()),
    }
    path = os.path.join(CTRL_DIR, f"{sid}.req")
    tmp = path + ".tmp"
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
    if os.path.exists(path):
        os.remove(path)
    os.replace(tmp, path)
    return path


def wait_until_consumed(req_path: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.exists(req_path):
            return True
        time.sleep(POLL_INTERVAL)
    return not os.path.exists(req_path)


def assistant_text_from_entry(obj: dict) -> str:
    if obj.get("type") != "message":
        return ""
    msg = obj.get("message") or {}
    if msg.get("role") != "assistant":
        return ""
    content = msg.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            typ = block.get("type")
            if typ in ("text", "output_text"):
                text = block.get("text") or ""
                if text:
                    parts.append(text)
    return "\n".join(parts).strip()


def extract_turn_text(jsonl_path: str, start_pos: int, user_id: str | None) -> str:
    texts: list[str] = []
    seen_user = user_id is None
    for _pos, _end, obj in iter_jsonl_from(jsonl_path, start_pos):
        if obj.get("type") == "message":
            role = (obj.get("message") or {}).get("role")
            if role == "user":
                if user_id and obj.get("id") == user_id:
                    seen_user = True
                    texts = []
                    continue
                if seen_user and user_id and obj.get("id") != user_id:
                    break
            if seen_user:
                piece = assistant_text_from_entry(obj)
                if piece:
                    texts.append(piece)
    return "\n\n".join(texts).strip()


def wait_for_user_entry(jsonl_path: str, start_pos: int, expected: str, timeout: float):
    deadline = time.time() + timeout
    expected_norm = expected.replace("\r\n", "\n").strip()
    while time.time() < deadline:
        for pos, end, obj in iter_jsonl_from(jsonl_path, start_pos):
            if obj.get("type") != "message":
                continue
            msg = obj.get("message") or {}
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    (b.get("text") or "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") in ("text", "output_text")
                )
            if text.replace("\r\n", "\n").strip() == expected_norm:
                return obj.get("id"), end
        time.sleep(POLL_INTERVAL)
    return None, None


def wait_for_settled(sid: str, jsonl_path: str, mark: float, timeout: float) -> bool:
    deadline = time.time() + timeout
    settled = os.path.join(CTRL_DIR, sid + ".settled")
    while time.time() < deadline:
        try:
            art = os.path.getmtime(settled)
        except OSError:
            art = 0.0
        if art > mark + 0.05:
            last_growth = art
            try:
                last_growth = max(last_growth, os.path.getmtime(jsonl_path))
            except OSError:
                pass
            quiet_until = time.time() + SETTLE_QUIET
            while time.time() < quiet_until:
                time.sleep(0.2)
                try:
                    m = os.path.getmtime(jsonl_path)
                except OSError:
                    m = 0
                if m > last_growth + 0.02:
                    last_growth = m
                    quiet_until = time.time() + SETTLE_QUIET
            return True
        time.sleep(POLL_INTERVAL)
    return False


NOT_LOADED = (
    "独立扩展还没有加载到当前正在开着的 pi TUI 会话里，所以这条没有投递。"
    "请在 Windows Terminal 的那个 pi 会话里输入 /reload（不必关掉会话，也不必卸载 pi-super-vim）。"
    "之后再把消息发一次即可。"
)


def deliver_and_collect(text: str) -> str:
    session = resolve_tui_session()
    if not session:
        return "找不到正在 Windows Terminal 里开着的 pi TUI 会话。请确认家里电脑上的 pi 还在跑。"
    if not session.get("extension_loaded"):
        return NOT_LOADED

    sid = session["sid"]
    jsonl_path = session["file"]
    # 会话文件是 pi 收到第一条用户消息时才创建的，别把它当成「扩展没加载」
    if not jsonl_path:
        return NOT_LOADED
    log(f"目标会话 sid={sid} jsonl={os.path.basename(jsonl_path)}")

    busy = is_pi_busy(sid, jsonl_path)
    mode = "followUp" if busy else "auto"
    log(f"投递方式 mode={mode} busy={busy}")
    if busy:
        send("pi 正在忙，这条已按排队方式投递，当前这轮结束后会处理。")
    else:
        send("已投递到电脑上的 pi，等待回复…")

    try:
        start_pos = os.path.getsize(jsonl_path)
    except OSError:
        start_pos = 0
    mark = max(settled_mtime(sid), time.time() - 0.05)

    req_path = write_request(sid, text, mode)
    if not wait_until_consumed(req_path, DELIVER_TIMEOUT):
        try:
            os.remove(req_path)
        except OSError:
            pass
        return (
            "扩展没有取走请求文件。若刚安装扩展，请先在 pi TUI 里执行 /reload；"
            "并确认该会话仍在 Windows Terminal 中开着。"
        )

    user_id, user_end = wait_for_user_entry(jsonl_path, start_pos, text, DELIVER_TIMEOUT)
    if not user_id:
        return "请求已被取走，但会话文件里迟迟没有出现这条用户消息。请看一下电脑屏幕上的 pi。"

    if not wait_for_settled(sid, jsonl_path, mark, TURN_TIMEOUT):
        partial = extract_turn_text(jsonl_path, start_pos, user_id)
        if partial:
            return f"等待完成超时（{TURN_TIMEOUT} 秒）。以下是目前已经落到会话里的助手文本：\n\n{partial}"
        return f"已投递，但在 {TURN_TIMEOUT} 秒内没有等到 agent_settled。请看电脑屏幕。"

    reply = extract_turn_text(jsonl_path, start_pos, user_id)
    if not reply:
        return "pi 本轮已结束，但会话里没有可发送的助手文本（可能只跑了工具或回复是空的）。"
    return reply


# ---------------------------------------------------------------------------
# Telegram 主循环
# ---------------------------------------------------------------------------

def handle(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message") or {}
    if not msg:
        return
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if chat_id != CHAT_ID:
        log("忽略来自其他 chat 的消息")
        return
    text = (msg.get("text") or "").strip()
    if not text:
        send("目前只处理文字消息。")
        return
    text = text[:MAX_INPUT]
    preview = re.sub(r"\s+", " ", text)[:120]
    log(f"收到：{preview}")
    try:
        reply = deliver_and_collect(text)
    except Exception as exc:
        log(f"投递或等待失败：{exc}")
        reply = f"处理这条时出错：{exc}"
    send(reply)
    out_preview = re.sub(r"\s+", " ", reply)[:120]
    log(f"已回复：{out_preview}")


def call_get_me() -> str:
    result = api("getMe", {}, timeout=20)
    if not result.get("ok"):
        raise RuntimeError(f"getMe 失败：{result}")
    username = (result.get("result") or {}).get("username") or "?"
    return username


def main() -> None:
    once = "--once" in sys.argv
    write_pid()
    offset = load_offset()
    try:
        username = call_get_me()
        log(f"启动：bot=@{username} offset={offset} 长轮询 {LONG_POLL}s ctrl={CTRL_DIR}")
    except Exception as exc:
        log(f"启动失败，无法连接 Telegram：{exc}")
        raise

    while True:
        fields = {
            "timeout": LONG_POLL,
            "limit": 10,
            "allowed_updates": '["message","edited_message"]',
        }
        if offset is not None:
            fields["offset"] = offset
        try:
            result = api("getUpdates", fields)
        except Exception as exc:
            log(f"getUpdates 失败：{exc}")
            time.sleep(15)
            continue
        if not result.get("ok"):
            log(f"getUpdates 返回不成功：{result.get('description') or result}")
            time.sleep(15)
            continue
        for update in result.get("result", []):
            offset = update["update_id"] + 1
            save_offset(offset)
            try:
                handle(update)
            except Exception as exc:
                log(f"处理失败：{exc}")
                try:
                    send(f"处理这条时出错：{exc}")
                except Exception:
                    pass
        if once:
            log("--once 结束")
            return


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"进程退出：{exc}")
        raise

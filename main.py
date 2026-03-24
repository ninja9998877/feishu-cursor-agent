# -*- coding: utf-8 -*-
"""
飞书长连接 -> 本机 Cursor CLI (agent)。事件回调尽快返回，具体任务在后台线程执行。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import lark_oapi as lark
from dotenv import load_dotenv
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreateMessageResponse,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

load_dotenv(Path(__file__).resolve().parent / ".env")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, LOG_LEVEL, logging.INFO)
_log_format = "%(asctime)s [%(levelname)s] %(message)s"
_root_logger = logging.getLogger()
_root_logger.setLevel(_log_level)
if not _root_logger.handlers:
    _stream = logging.StreamHandler()
    _stream.setFormatter(logging.Formatter(_log_format))
    _root_logger.addHandler(_stream)
_log_dir = Path(__file__).resolve().parent / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
_file = logging.FileHandler(_log_dir / "gateway.log", encoding="utf-8")
_file.setFormatter(logging.Formatter(_log_format))
_root_logger.addHandler(_file)

APP_ID = os.getenv("FEISHU_APP_ID") or os.getenv("APP_ID", "")
APP_SECRET = os.getenv("FEISHU_APP_SECRET") or os.getenv("APP_SECRET", "")
DEFAULT_WORKDIR = os.getenv("DEFAULT_WORKDIR", os.getcwd())
CURSOR_AGENT_CMD = os.getenv("CURSOR_AGENT_CMD", "agent").strip() or "agent"
CURSOR_AGENT_EXTRA_ARGS = os.getenv("CURSOR_AGENT_EXTRA_ARGS", "").strip()
CURSOR_AGENT_PROMPT_PREFIX = os.getenv("CURSOR_AGENT_PROMPT_PREFIX", "").strip()
AGENT_TIMEOUT_SEC = int(os.getenv("AGENT_TIMEOUT_SEC", "3600"))
FEISHU_REQUIRE_GROUP_MENTION = os.getenv("FEISHU_REQUIRE_GROUP_MENTION", "true").lower() in (
    "1",
    "true",
    "yes",
)
FEISHU_BOT_OPEN_ID = os.getenv("FEISHU_BOT_OPEN_ID", "").strip()
MAX_CHUNK = int(os.getenv("MAX_REPLY_CHARS", "1800"))
CARD_LOG_LINES = int(os.getenv("CARD_LOG_LINES", "12"))
CARD_UPDATE_INTERVAL_SEC = float(os.getenv("CARD_UPDATE_INTERVAL_SEC", "1.5"))


def _resolve_agent_cmd() -> str:
    """
    解析可执行的 Cursor agent 命令。
    优先 .env 指定值；若不可用则回退到常见安装路径。
    """
    configured = CURSOR_AGENT_CMD
    if configured:
        p = Path(configured)
        if p.is_file() or shutil.which(configured):
            return configured

    fallback = Path(r"C:\Users\super\AppData\Local\cursor-agent\agent.cmd")
    if fallback.is_file():
        logging.warning("CURSOR_AGENT_CMD=%s 不可用，回退到 %s", configured, str(fallback))
        return str(fallback)

    return configured or "agent"


EFFECTIVE_AGENT_CMD = _resolve_agent_cmd()
BUILD_TAG = "card-v2"


def _parse_id_list(raw: str) -> set[str]:
    return {x.strip() for x in raw.split(",") if x.strip()}


ALLOWED_CHAT_IDS = _parse_id_list(os.getenv("ALLOWED_CHAT_IDS", ""))
ALLOWED_SENDER_OPEN_IDS = _parse_id_list(os.getenv("ALLOWED_SENDER_OPEN_IDS", ""))

_chat_cwd: dict[str, str] = {}
_chat_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cursor-agent")

_lark_client: Optional[lark.Client] = None


def _get_client() -> lark.Client:
    global _lark_client
    if _lark_client is None:
        if not APP_ID or not APP_SECRET:
            raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET（或 APP_ID / APP_SECRET）")
        _lark_client = (
            lark.Client.builder()
            .app_id(APP_ID)
            .app_secret(APP_SECRET)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
    return _lark_client


def _send_text(chat_id: str, text: str) -> None:
    """向会话发送文本（自动拆包）。"""
    client = _get_client()
    t = text if text else "(空输出)"
    for i in range(0, len(t), MAX_CHUNK):
        chunk = t[i : i + MAX_CHUNK]
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": chunk}, ensure_ascii=False))
            .uuid(str(uuid.uuid4()))
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        resp: CreateMessageResponse = client.im.v1.message.create(req)
        if not resp.success():
            logging.error(
                "发送消息失败 code=%s msg=%s log_id=%s",
                resp.code,
                resp.msg,
                resp.get_log_id(),
            )


def _build_card_content(
    *,
    phase: str,
    user_text: str,
    cwd: str,
    cmd_display: str,
    lines: list[str],
    duration_sec: Optional[float] = None,
    return_code: Optional[int] = None,
) -> str:
    status_icon = "⏳"
    status_text = "执行中"
    template = "blue"
    if phase == "已接收":
        status_icon = "🟦"
        status_text = "已接收"
        template = "wathet"
    elif phase == "完成":
        status_icon = "✅"
        status_text = "已完成"
        template = "green"
    elif phase == "失败":
        status_icon = "❌"
        status_text = "执行失败"
        template = "red"

    title = f"{status_icon} Cursor 任务{status_text}"
    meta_lines = []
    if duration_sec is not None:
        meta_lines.append(f"耗时：{duration_sec:.1f}s")
    if return_code is not None:
        meta_lines.append(f"状态码：{return_code}")
    if not meta_lines:
        meta_lines.append("请稍候，正在处理你的请求...")

    log_lines = lines[-CARD_LOG_LINES:] if lines else ["（暂无输出）"]
    log_text = "\n".join(log_lines)
    if len(log_text) > 1200:
        log_text = log_text[-1200:]

    card = {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**你的请求**\n{user_text}",
                },
            },
            {
                "tag": "hr",
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**任务状态**\n" + "\n".join(meta_lines),
                },
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**执行详情（最近日志）**\n```text\n" + log_text + "\n```",
                },
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "由 Feishu Cursor Gateway 推送",
                    }
                ],
            },
        ],
    }
    return json.dumps(card, ensure_ascii=False)


def _send_card(chat_id: str, content: str) -> Optional[str]:
    client = _get_client()
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(chat_id)
        .msg_type("interactive")
        .content(content)
        .uuid(str(uuid.uuid4()))
        .build()
    )
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(body)
        .build()
    )
    resp: CreateMessageResponse = client.im.v1.message.create(req)
    if not resp.success():
        logging.error("创建卡片失败 code=%s msg=%s log_id=%s", resp.code, resp.msg, resp.get_log_id())
        return None
    mid = resp.data.message_id if resp.data else None
    logging.info("创建卡片成功 message_id=%s chat_id=%s", mid, chat_id)
    return mid


def _patch_card(message_id: str, content: str) -> bool:
    client = _get_client()
    req = (
        PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(PatchMessageRequestBody.builder().content(content).build())
        .build()
    )
    resp = client.im.v1.message.patch(req)
    if not resp.success():
        logging.error("更新卡片失败 code=%s msg=%s log_id=%s", resp.code, resp.msg, resp.get_log_id())
        return False
    logging.debug("更新卡片成功 message_id=%s", message_id)
    return True


def _strip_feishu_mentions(text: str) -> str:
    t = re.sub(r"@_user_\d+\s*", "", text)
    return t.strip()


def _mention_open_ids(message: Any) -> set[str]:
    out: set[str] = set()
    mentions = getattr(message, "mentions", None) or []
    for m in mentions:
        mid = getattr(m, "id", None)
        if mid is None:
            continue
        oid = getattr(mid, "open_id", None)
        if oid:
            out.add(oid)
    return out


def _extract_payload(data: Any) -> Optional[dict[str, Any]]:
    event = getattr(data, "event", None)
    if event is None:
        logging.warning("事件无 event 字段，data 类型=%s", type(data).__name__)
        return None
    message = getattr(event, "message", None)
    if message is None:
        logging.warning("事件无 message 字段")
        return None
    sender = getattr(event, "sender", None)
    sender_type = getattr(sender, "sender_type", "") if sender else ""
    if sender_type == "app":
        logging.info("忽略应用自身消息 sender_type=app")
        return None

    sender_id = getattr(sender, "sender_id", None) if sender else None
    sender_open_id = getattr(sender_id, "open_id", "") if sender_id else ""

    chat_id = getattr(message, "chat_id", "") or ""
    chat_type = getattr(message, "chat_type", "") or ""
    msg_type = getattr(message, "message_type", "") or ""
    content_raw = getattr(message, "content", "") or ""

    logging.info(
        "收到消息 chat_type=%s msg_type=%s chat_id=%s sender_open_id=%s content_preview=%s",
        chat_type,
        msg_type,
        chat_id,
        sender_open_id,
        (content_raw[:120] + "…") if len(content_raw) > 120 else content_raw,
    )

    if not chat_id:
        logging.warning("消息无 chat_id，已忽略")
        return None

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        logging.info("忽略 chat_id（不在白名单）: %s", chat_id)
        return None
    if ALLOWED_SENDER_OPEN_IDS and sender_open_id not in ALLOWED_SENDER_OPEN_IDS:
        logging.info("忽略发送者（不在白名单）: %s", sender_open_id)
        return None

    if msg_type != "text":
        return {
            "chat_id": chat_id,
            "skip_agent": True,
            "reply": "当前仅支持文本消息。",
        }

    try:
        obj = json.loads(content_raw)
        raw_text = obj.get("text", "")
    except json.JSONDecodeError:
        raw_text = content_raw

    text = _strip_feishu_mentions(raw_text)
    if not text:
        logging.info("去 @ 后文本为空，已忽略（原始 content=%s）", content_raw[:200])
        return None

    if chat_type == "group" and FEISHU_REQUIRE_GROUP_MENTION:
        if not FEISHU_BOT_OPEN_ID:
            return {
                "chat_id": chat_id,
                "skip_agent": True,
                "reply": "群聊需要 @ 机器人，但未配置 FEISHU_BOT_OPEN_ID。请在 .env 中填写机器人 open_id。",
            }
        if FEISHU_BOT_OPEN_ID not in _mention_open_ids(message):
            logging.info("群消息未 @ 机器人，已忽略（需 FEISHU_BOT_OPEN_ID=%s 出现在 mentions）", FEISHU_BOT_OPEN_ID)
            return None

    return {
        "chat_id": chat_id,
        "sender_open_id": sender_open_id,
        "text": text,
        "skip_agent": False,
    }


def _run_agent(chat_id: str, user_text: str) -> None:
    logging.info("进入 _run_agent，build=%s", BUILD_TAG)
    cwd = _chat_cwd.get(chat_id, DEFAULT_WORKDIR)
    if not Path(cwd).is_dir():
        _send_text(chat_id, f"工作目录无效：{cwd}，请使用 /cd 设置有效路径。")
        return

    parts: list[str] = []
    if CURSOR_AGENT_PROMPT_PREFIX:
        parts.append(CURSOR_AGENT_PROMPT_PREFIX)
    parts.append(user_text)
    prompt = "\n\n".join(parts)

    cmd = [EFFECTIVE_AGENT_CMD, "-p", prompt, "--output-format", "text"]
    if CURSOR_AGENT_EXTRA_ARGS:
        cmd.extend(shlex.split(CURSOR_AGENT_EXTRA_ARGS, posix=os.name != "nt"))

    cmd_display = subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(shlex.quote(c) for c in cmd)
    card_message_id = _send_card(
        chat_id,
        _build_card_content(
            phase="已接收",
            user_text=user_text,
            cwd=cwd,
            cmd_display=cmd_display,
            lines=["任务已接收，准备启动 Cursor Agent..."],
        ),
    )
    if not card_message_id:
        _send_text(chat_id, f"已开始处理：{user_text}")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            encoding="utf-8",
            errors="replace",
        )

        q: queue.Queue[tuple[str, str]] = queue.Queue()
        lines: list[str] = []
        start_ts = time.time()
        last_patch = 0.0

        def _reader(pipe: Any, stream_name: str) -> None:
            try:
                if pipe is None:
                    return
                for line in iter(pipe.readline, ""):
                    q.put((stream_name, line.rstrip("\r\n")))
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass

        t1 = threading.Thread(target=_reader, args=(proc.stdout, "stdout"), daemon=True)
        t2 = threading.Thread(target=_reader, args=(proc.stderr, "stderr"), daemon=True)
        t1.start()
        t2.start()

        while True:
            now = time.time()
            try:
                stream, line = q.get(timeout=0.2)
                lines.append(line)
            except queue.Empty:
                pass

            if card_message_id and (now - last_patch >= CARD_UPDATE_INTERVAL_SEC):
                _patch_card(
                    card_message_id,
                    _build_card_content(
                        phase="执行中",
                        user_text=user_text,
                        cwd=cwd,
                        cmd_display=cmd_display,
                        lines=lines if lines else ["任务执行中..."],
                        duration_sec=now - start_ts,
                    ),
                )
                last_patch = now

            if proc.poll() is not None and q.empty():
                break

            if now - start_ts > AGENT_TIMEOUT_SEC:
                proc.kill()
                lines.append(f"执行超时（>{AGENT_TIMEOUT_SEC}s），已终止。")
                break

        t1.join(timeout=0.5)
        t2.join(timeout=0.5)
        ret = proc.poll()
        final_phase = "完成" if ret == 0 else "失败"
        if card_message_id:
            _patch_card(
                card_message_id,
                _build_card_content(
                    phase=final_phase,
                    user_text=user_text,
                    cwd=cwd,
                    cmd_display=cmd_display,
                    lines=lines if lines else ["(无输出)"],
                    duration_sec=time.time() - start_ts,
                    return_code=ret,
                ),
            )
        else:
            text = "\n".join(lines) if lines else "已完成，但没有返回内容。"
            if ret not in (None, 0):
                text = f"任务执行失败（状态码 {ret}）\n{text}"
            _send_text(chat_id, text)
    except FileNotFoundError:
        _send_text(
            chat_id,
            f"找不到可执行文件：{EFFECTIVE_AGENT_CMD}。请安装 Cursor CLI 并加入 PATH，或修改 CURSOR_AGENT_CMD。",
        )
    except subprocess.TimeoutExpired:
        _send_text(chat_id, f"执行超时（>{AGENT_TIMEOUT_SEC}s），已终止。")
    except Exception as e:
        logging.exception("执行 agent 失败")
        _send_text(chat_id, f"执行异常：{e!r}")


def _process_payload(payload: dict[str, Any]) -> None:
    chat_id = payload["chat_id"]
    with _chat_locks[chat_id]:
        if payload.get("skip_agent"):
            _send_text(chat_id, payload.get("reply", ""))
            return

        text = payload["text"]
        if text.startswith("/help"):
            _send_text(
                chat_id,
                "命令：\n"
                "/help — 帮助\n"
                "/cd <绝对路径> — 设置本会话工作目录\n"
                "/pwd — 查看工作目录\n"
                "其它文本 — 交给本机 `agent -p` 执行\n\n"
                f"默认目录：{DEFAULT_WORKDIR}",
            )
            return
        if text.startswith("/pwd"):
            _send_text(chat_id, f"当前工作目录：{_chat_cwd.get(chat_id, DEFAULT_WORKDIR)}")
            return
        if text.startswith("/cd"):
            rest = text[3:].strip().strip('"')
            if not rest:
                _send_text(chat_id, "用法：/cd D:\\\\Project\\\\zombie_dev")
                return
            p = Path(rest)
            if not p.is_dir():
                _send_text(chat_id, f"路径不存在或不是目录：{rest}")
                return
            _chat_cwd[chat_id] = str(p.resolve())
            _send_text(chat_id, f"已设置工作目录：{_chat_cwd[chat_id]}")
            return

        _run_agent(chat_id, text)


def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    try:
        payload = _extract_payload(data)
        if payload is None:
            return
        _executor.submit(_process_payload, payload)
    except Exception:
        logging.exception("处理 im.message.receive_v1 失败")


def main() -> None:
    if not APP_ID or not APP_SECRET:
        raise SystemExit("请在 .env 中配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .build()
    )
    cli = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    logging.info(
        "飞书 Cursor Agent 桥接启动（长连接）。BUILD=%s DEFAULT_WORKDIR=%s, AGENT_CMD=%s",
        BUILD_TAG,
        DEFAULT_WORKDIR,
        EFFECTIVE_AGENT_CMD,
    )
    cli.start()


if __name__ == "__main__":
    main()

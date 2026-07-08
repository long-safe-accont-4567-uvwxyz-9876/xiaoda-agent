#!/usr/bin/env python3
from typing import Any
import sys
import json
import asyncio
import uuid
import os
import re
import base64
import argparse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from utils.logging_config import setup_logging
setup_logging()

from loguru import logger

logger.remove()
logger.add(sys.stderr, format="{time:HH:mm:ss} | {level} | {message}", level="WARNING")


class XiaodaAcpServer:
    """小妲 ACP 服务器，处理 JSON-RPC 消息并调度 Agent 处理。"""

    def __init__(self) -> None:
        """初始化 ACP 服务器，设置会话和状态变量。"""
        self.agent = None
        self.sessions = {}
        self.initialized = False
        self._cancelled = False
        self._loop = None
        self._coze_agent_id = ""
        self._coze_session_id = ""

    async def _init_agent(self) -> None:
        """初始化并加载 AgentCore。"""
        from agent_core import AgentCore
        self.agent = AgentCore()
        await self.agent.init()
        logger.info("xiaoda_acp.agent_initialized")

    def _read_message(self) -> Any:
        """从标准输入读取一行并解析为 JSON。"""
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            logger.warning("xiaoda_acp.json_decode_error", line=line[:100])
            return None

    def _write_message(self, msg: Any) -> None:
        """将 JSON-RPC 消息写入标准输出。"""
        payload = json.dumps(msg, ensure_ascii=False)
        sys.stdout.write(payload + '\n')
        sys.stdout.flush()

    def _handle_initialize(self, msg: Any) -> dict:
        """处理 initialize 请求，返回协议版本和代理能力信息。"""
        self.initialized = True
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id", 0),
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": False,
                    "promptCapabilities": {
                        "image": True,
                        "audio": False,
                        "embeddedContext": False
                    }
                },
                "agentInfo": {
                    "name": "xiaoda",
                    "title": "小妲 AI Agent",
                    "version": "1.0.0"
                },
                "authMethods": []
            }
        }

    def _handle_session_new(self, msg: Any) -> dict:
        """处理 session/new 请求，创建新会话并返回会话 ID。"""
        _params = msg.get("params", {})
        session_id = f"xiaoda_{uuid.uuid4().hex[:12]}"
        self.sessions[session_id] = {"created": True}
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id", 1),
            "result": {
                "sessionId": session_id
            }
        }

    def _strip_coze_context(self, text: Any) -> Any:
        """从文本中移除 <coze-context>...</coze-context> 标签块。"""
        cleaned = re.sub(r'<coze-context>.*?</coze-context>\s*', '', text, flags=re.DOTALL)
        return cleaned.strip()

    def _extract_coze_ids(self, text: Any) -> Any:
        """从 <coze-context> 块中解析 agent-id / session-id 等键值对。"""
        m = re.search(r'<coze-context>(.*?)</coze-context>', text, re.DOTALL)
        if not m:
            return {}
        block = m.group(1)
        ids = {}
        for line in block.strip().splitlines():
            ln = line.strip()
            if ':' in ln:
                key, _, val = ln.partition(':')
                key = key.strip().lower().replace('_', '-')
                val = val.strip()
                if key in ('agent-id', 'session-id', 'account-id', 'group-id'):
                    ids[key] = val
        return ids

    def _encode_image(self, path: Any) -> Any:
        """将图片文件编码为 Base64 格式的字典。"""
        try:
            p = Path(path)
            if not p.exists() or not p.is_file():
                return None
            suffix = p.suffix.lower()
            mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
            mime = mime_map.get(suffix, "image/png")
            data = p.read_bytes()
            if len(data) > 10 * 1024 * 1024:
                return None
            return {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}
        except Exception:
            return None

    async def _send_audio_file(self, audio_path: Any, acp_session_id: str) -> None:
        """通过 coze-bridge 发送音频文件（bridge 不可用时仅记录日志）。"""
        try:
            p = Path(audio_path)
            if not p.exists() or not p.is_file():
                logger.warning("xiaoda_acp.audio_file_missing", path=str(audio_path))
                return

            if self._coze_agent_id and self._coze_session_id:
                bridge_bin = Path.home() / ".coze" / "bridge" / "bin" / "coze-bridge"
                if bridge_bin.exists():
                    cmd = [
                        str(bridge_bin), "send", "file", str(p),
                        "--agent-id", self._coze_agent_id,
                        "--session-id", self._coze_session_id,
                        "--name", p.name,
                        "--caption", "🎙️ 语音消息",
                    ]
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                    if proc.returncode == 0:
                        logger.info("xiaoda_acp.audio_sent_via_bridge", file=p.name)
                    else:
                        logger.warning("xiaoda_acp.audio_bridge_failed",
                                       code=proc.returncode, stderr=stderr.decode()[:200])
                else:
                    logger.warning("xiaoda_acp.audio_no_bridge")
            else:
                logger.warning("xiaoda_acp.audio_no_coze_ids",
                               has_agent_id=bool(self._coze_agent_id),
                               has_session_id=bool(self._coze_session_id))
        except Exception as e:
            logger.warning("xiaoda_acp.audio_send_failed", error=str(e))

    async def _handle_session_prompt(self, msg: Any) -> dict:
        """处理 session/prompt：解析输入 → 调用 agent → 回复文本/贴纸/音频。"""
        params = msg.get("params", {})
        session_id = params.get("sessionId", "")
        prompt_blocks = params.get("prompt", [])

        meta = params.get("_meta", {})
        if meta.get("cozeAgentId") and not self._coze_agent_id:
            self._coze_agent_id = meta["cozeAgentId"]
        if session_id and not session_id.startswith("xiaoda_") and not self._coze_session_id:
            self._coze_session_id = session_id

        text_parts, image_data = self._parse_prompt_blocks(prompt_blocks)
        user_text = "\n".join(text_parts).strip()

        coze_ids = self._extract_coze_ids(user_text)
        if coze_ids.get('agent-id'):
            self._coze_agent_id = coze_ids['agent-id']
        if coze_ids.get('session-id'):
            self._coze_session_id = coze_ids['session-id']

        user_text = self._strip_coze_context(user_text)
        if not user_text:
            return {
                "jsonrpc": "2.0",
                "id": msg.get("id", 2),
                "result": {"stopReason": "end_turn"}
            }

        self._cancelled = False

        reply, sticker_path, audio_path = await self._call_agent_process_safe(
            user_text, session_id, image_data
        )

        if self._cancelled:
            return {
                "jsonrpc": "2.0",
                "id": msg.get("id", 2),
                "result": {"stopReason": "cancelled"}
            }

        self._write_message({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": reply}
                }
            }
        })

        if sticker_path:
            await self._send_sticker(sticker_path, session_id)

        if audio_path:
            await self._send_audio_file(audio_path, session_id)

        return {
            "jsonrpc": "2.0",
            "id": msg.get("id", 2),
            "result": {"stopReason": "end_turn"}
        }

    @staticmethod
    def _parse_prompt_blocks(prompt_blocks: list) -> tuple:
        """解析 prompt blocks，返回 (text_parts, image_data)。"""
        text_parts = []
        image_data = []
        for block in prompt_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "resource":
                res = block.get("resource", {})
                if res.get("text"):
                    text_parts.append(res.get("text", ""))
            elif block.get("type") == "image":
                img_b64 = block.get("data", "")
                mime = block.get("mimeType", "image/jpeg")
                if img_b64:
                    image_data.append({"mimeType": mime, "data": img_b64})
                else:
                    uri = block.get("uri", "")
                    name = block.get("name", "image")
                    if uri:
                        text_parts.append(f"[图片: {name}]")
        return text_parts, image_data

    async def _call_agent_process_safe(self, user_text: str, session_id: str,
                                        image_data: list) -> tuple:
        """调用 agent.process，异常时返回错误回复。返回 (reply, sticker_path, audio_path)。"""
        sticker_path = None
        audio_path = None
        try:
            result = await self.agent.process(
                user_text,
                user_id="cli_coze_bridge",
                source="cli",
                session_id=session_id,
                image_data=image_data if image_data else None
            )
            reply = result.reply
            sticker_path = result.sticker_path
            audio_path = result.audio_path
        except Exception as e:
            logger.error("xiaoda_acp.process_error", error=str(e))
            reply = f"嗯……出了点小问题：{str(e)[:200]}"
        return reply, sticker_path, audio_path

    def _send_inline_image(self, session_id: str, image_path: str) -> None:
        """通过 session/update 内联发送图片（bridge 不可用时的 fallback）。"""
        img = self._encode_image(image_path)
        if not img:
            return
        self._write_message({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {
                        "type": "image",
                        "mimeType": img["mimeType"],
                        "data": img["data"]
                    }
                }
            }
        })

    async def _send_sticker(self, sticker_path: str, session_id: str) -> None:
        """发送贴纸：优先 coze-bridge，失败或无 bridge 时内联发送图片。"""
        sticker_p = Path(sticker_path)
        if not (self._coze_agent_id and self._coze_session_id):
            self._send_inline_image(session_id, sticker_path)
            return

        bridge_bin = Path.home() / ".coze" / "bridge" / "bin" / "coze-bridge"
        if not bridge_bin.exists():
            self._send_inline_image(session_id, sticker_path)
            return

        try:
            cmd = [
                str(bridge_bin), "send", "image", str(sticker_p),
                "--agent-id", self._coze_agent_id,
                "--session-id", self._coze_session_id,
                "--caption", "🌿",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                logger.info("xiaoda_acp.sticker_sent_via_bridge", file=sticker_p.name)
            else:
                logger.warning("xiaoda_acp.sticker_bridge_failed",
                               code=proc.returncode, stderr=stderr.decode()[:200])
                self._send_inline_image(session_id, sticker_path)
        except Exception as e:
            logger.warning("xiaoda_acp.sticker_send_error", error=str(e))

    def _handle_session_cancel(self, msg: Any) -> Any:
        """处理 session/cancel 请求，标记当前会话为已取消。"""
        self._cancelled = True
        if msg.get("id") is not None:
            return {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": None
            }
        return None

    def _handle_session_load(self, msg: Any) -> dict:
        """处理 session/load 请求，当前不支持该方法。"""
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id", 1),
            "error": {
                "code": -32601,
                "message": "session/load not supported"
            }
        }

    def _make_error(self, msg: Any, code: Any, message: Any) -> dict:
        """构造 JSON-RPC 错误响应。"""
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "error": {"code": code, "message": message}
        }

    async def run(self) -> None:
        """启动 ACP 服务器主循环，读取并分发 JSON-RPC 消息。"""
        await self._init_agent()

        logger.info("xiaoda_acp.ready")

        while True:
            try:
                msg = self._read_message()
            except Exception:
                break

            if msg is None:
                break

            method = msg.get("method", "")
            msg_id = msg.get("id")

            try:
                if method == "initialize":
                    response = self._handle_initialize(msg)
                    self._write_message(response)

                elif method == "session/new":
                    response = self._handle_session_new(msg)
                    self._write_message(response)

                elif method == "session/prompt":
                    response = await self._handle_session_prompt(msg)
                    if response:
                        self._write_message(response)

                elif method == "session/cancel":
                    response = self._handle_session_cancel(msg)
                    if response:
                        self._write_message(response)

                elif method == "session/load":
                    self._write_message(self._handle_session_load(msg))

                elif method == "session/close":
                    session_id = msg.get("params", {}).get("sessionId", "")
                    self.sessions.pop(session_id, None)
                    if msg_id is not None:
                        self._write_message({
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": {}
                        })

                else:
                    if msg_id is not None:
                        self._write_message(
                            self._make_error(msg, -32601, f"Method not found: {method}")
                        )
            except Exception as e:
                logger.error("xiaoda_acp.handler_error", method=method, error=str(e))
                if msg_id is not None:
                    self._write_message(
                        self._make_error(msg, -32603, f"Internal error: {str(e)[:200]}")
                    )


def main() -> None:
    """命令行入口：解析参数并启动 ACP 服务器。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--acp-version", action="store_true")
    args, _ = parser.parse_known_args()

    if args.version or args.acp_version:
        print("xiaoda-acp 1.0.0")
        sys.exit(0)

    server = XiaodaAcpServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
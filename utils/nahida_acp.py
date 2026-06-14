#!/usr/bin/env python3
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


class NahidaAcpServer:
    def __init__(self):
        self.agent = None
        self.sessions = {}
        self.initialized = False
        self._cancelled = False
        self._loop = None
        self._coze_agent_id = ""
        self._coze_session_id = ""

    async def _init_agent(self):
        from agent_core import AgentCore
        self.agent = AgentCore()
        await self.agent.init()
        logger.info("nahida_acp.agent_initialized")

    def _read_message(self):
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            logger.warning("nahida_acp.json_decode_error", line=line[:100])
            return None

    def _write_message(self, msg):
        payload = json.dumps(msg, ensure_ascii=False)
        sys.stdout.write(payload + '\n')
        sys.stdout.flush()

    def _handle_initialize(self, msg):
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
                    "name": "nahida",
                    "title": "纳西妲 AI Agent",
                    "version": "1.0.0"
                },
                "authMethods": []
            }
        }

    def _handle_session_new(self, msg):
        params = msg.get("params", {})
        session_id = f"nahida_{uuid.uuid4().hex[:12]}"
        self.sessions[session_id] = {"created": True}
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id", 1),
            "result": {
                "sessionId": session_id
            }
        }

    def _strip_coze_context(self, text):
        cleaned = re.sub(r'<coze-context>.*?</coze-context>\s*', '', text, flags=re.DOTALL)
        return cleaned.strip()

    def _extract_coze_ids(self, text):
        m = re.search(r'<coze-context>(.*?)</coze-context>', text, re.DOTALL)
        if not m:
            return {}
        block = m.group(1)
        ids = {}
        for line in block.strip().splitlines():
            line = line.strip()
            if ':' in line:
                key, _, val = line.partition(':')
                key = key.strip().lower().replace('_', '-')
                val = val.strip()
                if key in ('agent-id', 'session-id', 'account-id', 'group-id'):
                    ids[key] = val
        return ids

    def _encode_image(self, path):
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

    async def _send_audio_file(self, audio_path, acp_session_id: str):
        try:
            p = Path(audio_path)
            if not p.exists() or not p.is_file():
                logger.warning("nahida_acp.audio_file_missing", path=str(audio_path))
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
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                    if proc.returncode == 0:
                        logger.info("nahida_acp.audio_sent_via_bridge", file=p.name)
                    else:
                        logger.warning("nahida_acp.audio_bridge_failed",
                                       code=proc.returncode, stderr=stderr.decode()[:200])
                else:
                    logger.warning("nahida_acp.audio_no_bridge")
            else:
                logger.warning("nahida_acp.audio_no_coze_ids",
                               has_agent_id=bool(self._coze_agent_id),
                               has_session_id=bool(self._coze_session_id))
        except Exception as e:
            logger.warning("nahida_acp.audio_send_failed", error=str(e))

    async def _handle_session_prompt(self, msg):
        params = msg.get("params", {})
        session_id = params.get("sessionId", "")
        prompt_blocks = params.get("prompt", [])

        meta = params.get("_meta", {})
        if meta.get("cozeAgentId") and not self._coze_agent_id:
            self._coze_agent_id = meta["cozeAgentId"]
        if session_id and not session_id.startswith("nahida_") and not self._coze_session_id:
            self._coze_session_id = session_id

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
            logger.error("nahida_acp.process_error", error=str(e))
            reply = f"嗯……出了点小问题：{str(e)[:200]}"

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
                    "content": {
                        "type": "text",
                        "text": reply
                    }
                }
            }
        })

        if sticker_path:
            sticker_p = Path(sticker_path)
            if self._coze_agent_id and self._coze_session_id:
                bridge_bin = Path.home() / ".coze" / "bridge" / "bin" / "coze-bridge"
                if bridge_bin.exists():
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
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                        if proc.returncode == 0:
                            logger.info("nahida_acp.sticker_sent_via_bridge", file=sticker_p.name)
                        else:
                            logger.warning("nahida_acp.sticker_bridge_failed",
                                           code=proc.returncode, stderr=stderr.decode()[:200])
                            img = self._encode_image(sticker_path)
                            if img:
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
                    except Exception as e:
                        logger.warning("nahida_acp.sticker_send_error", error=str(e))
                else:
                    img = self._encode_image(sticker_path)
                    if img:
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
            else:
                img = self._encode_image(sticker_path)
                if img:
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

        if audio_path:
            await self._send_audio_file(audio_path, session_id)

        return {
            "jsonrpc": "2.0",
            "id": msg.get("id", 2),
            "result": {"stopReason": "end_turn"}
        }

    def _handle_session_cancel(self, msg):
        self._cancelled = True
        if msg.get("id") is not None:
            return {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": None
            }
        return None

    def _handle_session_load(self, msg):
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id", 1),
            "error": {
                "code": -32601,
                "message": "session/load not supported"
            }
        }

    def _make_error(self, msg, code, message):
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "error": {"code": code, "message": message}
        }

    async def run(self):
        await self._init_agent()

        logger.info("nahida_acp.ready")

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
                logger.error("nahida_acp.handler_error", method=method, error=str(e))
                if msg_id is not None:
                    self._write_message(
                        self._make_error(msg, -32603, f"Internal error: {str(e)[:200]}")
                    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--acp-version", action="store_true")
    args, _ = parser.parse_known_args()

    if args.version or args.acp_version:
        print("nahida-acp 1.0.0")
        sys.exit(0)

    server = NahidaAcpServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()

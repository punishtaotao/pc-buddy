"""
mock_llm.py —— 假的 OpenAI 兼容接口。仅用于自测，不参与实际使用。

按剧本走：
  第 1 轮（还没有工具结果）  -> 调用 run_powershell 执行 echo PC_BUDDY_OK
  第 2 轮（已有 1 个工具结果）-> 调用 run_powershell 执行 shutdown /s /t 0（危险，应触发确认）
  第 3 轮（已有 2 个工具结果）-> 给出最终答复
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _tool_call(name: str, args: dict, cid: str) -> dict:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:
        pass

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n).decode("utf-8"))
        msgs = req.get("messages", [])

        # 只看「最后一次用户发言」之后的工具消息。
        # 真实使用时 Agent 会保留跨任务的历史，按全局计数会被上一轮干扰。
        user_idx = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
        last_user_idx = user_idx[-1] if user_idx else 0
        first_user = msgs[last_user_idx].get("content", "") or ""
        tool_msgs = [m for m in msgs[last_user_idx:] if m.get("role") == "tool"]
        ocr_seen = "本地 OCR 识别结果" in first_user
        vision_seen = "视觉模型看图结果" in first_user

        if len(tool_msgs) == 0:
            msg = {"role": "assistant", "content": None,
                   "tool_calls": [_tool_call("run_powershell", {"command": "echo PC_BUDDY_OK"}, "call_1")]}
            finish = "tool_calls"
        elif len(tool_msgs) == 1:
            # 故意用「含 shutdown 字样、但本身无害」的命令：
            # 它能命中危险规则触发确认流程，万一行程有 bug 放行了，也只是回显一行字，不会真关机。
            msg = {"role": "assistant", "content": None,
                   "tool_calls": [_tool_call("run_powershell",
                                             {"command": "echo shutdown /s /t 0"}, "call_2")]}
            finish = "tool_calls"
        else:
            joined = "\n".join(m.get("content", "") for m in tool_msgs)
            text = ("已完成。\n"
                    f"看到手机照片的文字: {'YES' if ocr_seen else 'NO'}\n"
                    f"看到视觉模型描述: {'YES' if vision_seen else 'NO'}\n"
                    f"第 1 步工具输出：{tool_msgs[0].get('content', '')}\n"
                    f"第 2 步被拦下，工具返回：{tool_msgs[-1].get('content', '')[:200]}\n"
                    f"（完整工具记录长度 {len(joined)}）")
            msg = {"role": "assistant", "content": text}
            finish = "stop"

        resp = {
            "id": "mock-completion",
            "object": "chat.completion",
            "model": req.get("model", "mock"),
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    return srv

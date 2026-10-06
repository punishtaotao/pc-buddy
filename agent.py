"""
agent.py —— 大脑。用 DeepSeek 的 function calling 把「一句话」变成一串真实操作。

流程：
    用户消息(可能带图) → DeepSeek → 要调工具？ → 执行 → 结果回喂 → 再问 → ... → 最终答复
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

import tools as T
from tools import RT

SYSTEM_PROMPT = """你是「电脑管家」，一个真正运行在用户 Windows 电脑上的执行型 AI。用户通常在手机上下指令，你在电脑上把事办完。

你的能力：执行 PowerShell 命令、读写文件、截图看屏幕、鼠标点击、键盘输入、开关软件和网页、识别图片文字。

工作原则：
1. 先看清再动手。要点击屏幕上的东西之前，先用 screenshot 截图确认位置，或先 list_windows 看当前开着什么。屏幕分辨率用 get_screen_size 查。
2. 一步步来，每做一步就检查结果。命令看退出码，界面操作后可以再截一张图确认。
3. 不要猜。信息不够、目标不明确，就先把能查的查清楚，实在不行就在答复里说明你需要用户补充什么。
4. 做完要说人话。用户在手机上看结果，所以结论要短、要具体，别甩一堆原始日志。成功了说清做了什么、结果在哪；失败了说清卡在哪一步、什么报错、下一步怎么办。
5. 中文回答。除非用户用别的语言。
6. 涉及删除、格式化、关机、改系统设置这类危险操作，工具会自动让用户在手机上确认，你照常调用即可；被拒绝就换个更安全的方式。
7. 用户从手机发来的照片，图片本身你未必能直接看见（取决于是否配置了视觉模型）。遇到照片，先用 ocr_image 读出里面的文字，再据此判断。

操作系统：Windows。默认 shell 是 PowerShell，别用 Linux 语法。
"""


class DeepSeekError(RuntimeError):
    pass


def _post_json(url: str, payload: dict, api_key: str, timeout: int = 180) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise DeepSeekError(f"接口返回 HTTP {e.code}：{body[:600]}") from e
    except urllib.error.URLError as e:
        raise DeepSeekError(f"连不上接口（{url}）：{e.reason}") from e
    except TimeoutError as e:
        raise DeepSeekError(f"接口 {timeout} 秒没响应") from e


def _chat(cfg: dict, messages: list[dict], use_tools: bool = True) -> dict:
    dc = cfg["deepseek"]
    key = (dc.get("api_key") or "").strip()
    # 占位说明文字、带空格或非 ASCII 的都不算 Key，免得拿着示例文案去请求接口，
    # 结果报一个看不懂的 401。
    if not key or not key.isascii() or any(c.isspace() for c in key):
        raise DeepSeekError(
            "还没填 DeepSeek API Key。打开 D:\\DSH\\pc-buddy\\config.json，"
            "把 api_key 填上（形如 sk-xxxxxxxx），存盘后重启本程序。"
        )

    base = (dc.get("base_url") or "https://api.deepseek.com").rstrip("/")
    if not base.endswith("/v1") and "api.deepseek.com" not in base:
        url = f"{base}/v1/chat/completions"
    else:
        url = f"{base}/chat/completions"

    payload: dict = {
        "model": dc.get("model") or "deepseek-chat",
        "messages": messages,
        "temperature": dc.get("temperature", 0.2),
        "stream": False,
    }
    if use_tools:
        payload["tools"] = T.openai_tool_schemas()
        payload["tool_choice"] = "auto"

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            return _post_json(url, payload, dc["api_key"])
        except DeepSeekError as e:
            last_err = e
            msg = str(e)
            if "HTTP 4" in msg and "429" not in msg:
                raise
            if attempt < 2:
                RT.publish({"type": "warn", "text": f"接口调用失败，{2 - attempt} 秒后重试：{msg[:200]}"})
                time.sleep(2 + attempt * 2)
    raise last_err or DeepSeekError("调用失败")


# ---------------------------------------------------------------- 视觉（可选）

def describe_image_with_vision(cfg: dict, image_path: Path) -> str:
    """可选：配置了视觉模型时，真正「看」一眼图片。没配就返回空串。"""
    vc = cfg.get("vision") or {}
    if not vc.get("enabled") or not vc.get("model") or not vc.get("base_url"):
        return ""

    try:
        raw = image_path.read_bytes()
    except Exception as e:
        return f"[视觉模型读图失败] {e}"

    if len(raw) > 4_000_000:
        return "[图片超过 4MB，跳过视觉模型]"

    b64 = base64.b64encode(raw).decode("ascii")
    ext = image_path.suffix.lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif"}.get(ext, "png")

    base = vc["base_url"].rstrip("/")
    url = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"
    payload = {
        "model": vc["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "用中文客观描述这张图的全部重要信息（画面内容、界面元素、可读文字）。控制在 200 字内。"},
                {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
            ],
        }],
        "temperature": 0.1,
        "stream": False,
    }
    try:
        resp = _post_json(url, payload, vc.get("api_key") or cfg["deepseek"]["api_key"], timeout=120)
        return (resp["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        return f"[视觉模型调用失败] {e}"


# ---------------------------------------------------------------- 主循环

class Agent:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.max_steps = int(cfg["agent"].get("max_steps", 25))
        self.history: list[dict] = []
        self._system = self._build_system()

    def _build_system(self) -> str:
        prompt = SYSTEM_PROMPT
        prompt += f"\n当前工作目录：{RT.work_dir}（写这以外的位置需要用户确认）\n"

        mem = T.DATA / "memory.md"
        if mem.exists():
            try:
                note = mem.read_text(encoding="utf-8").strip()
                if note:
                    prompt += f"\n【你之前记下的笔记】\n{note[-3000:]}\n"
            except Exception:
                pass

        extra = (self.cfg["agent"].get("system_extra") or "").strip()
        if extra:
            prompt += f"\n【用户附加要求】\n{extra}\n"
        return prompt

    def reset(self) -> None:
        self.history.clear()
        self._system = self._build_system()

    def _build_user_message(self, text: str, images: Iterable[Path]) -> str:
        blocks: list[str] = []
        for img in images:
            blocks.append(f"【用户从手机发来一张照片】已保存到：{img}")

            vision = describe_image_with_vision(self.cfg, img)
            if vision:
                blocks.append(f"视觉模型看图结果：{vision}")

            ocr = T.ocr_image(str(img))
            blocks.append(f"本地 OCR 识别结果：\n{ocr}")
            blocks.append("（提示：如需再次识图，可用 ocr_image 工具）")

        if text.strip():
            blocks.append(f"【用户的话】{text.strip()}")
        elif images:
            blocks.append("【用户的话】（没打字，只发了图。请根据图片内容判断他想让你做什么；如果实在看不出，就问一句。）")
        return "\n\n".join(blocks)

    def run(self, text: str, images: list[Path]) -> str:
        """跑完一整轮任务，返回最终答复。中间过程通过 RT.publish 实时推给手机。"""
        RT.check_stop()
        self._system = self._build_system()

        user_msg = self._build_user_message(text, images)
        self.history.append({"role": "user", "content": user_msg})
        self._trim_history()

        messages = [{"role": "system", "content": self._system}] + self.history
        RT.publish({"type": "thinking", "text": "已收到，开始想办法…"})

        final_text = ""

        for step in range(1, self.max_steps + 1):
            RT.check_stop()
            RT.publish({"type": "step", "n": step, "text": f"第 {step} 步：思考中…"})

            resp = _chat(self.cfg, messages)
            try:
                msg = resp["choices"][0]["message"]
            except Exception:
                raise DeepSeekError(f"接口返回格式异常：{json.dumps(resp, ensure_ascii=False)[:500]}")

            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                final_text = (msg.get("content") or "").strip() or "（任务结束，但没有回复内容）"
                self.history.append({"role": "assistant", "content": final_text})
                RT.publish({"type": "final", "text": final_text})
                return final_text

            # 记下模型的工具意图
            messages.append(msg)
            self.history.append(msg)

            for call in tool_calls:
                RT.check_stop()
                fn = (call.get("function") or {})
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {}

                preview = _preview_args(args)
                RT.publish({"type": "action", "name": name, "detail": preview, "step": step})

                result = T.call_tool(name, args)
                brief = _first_line(result)
                RT.publish({"type": "result", "name": name, "ok": not result.startswith("["), "brief": brief})

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{step}_{name}",
                    "content": result[:20000],
                }
                messages.append(tool_msg)
                self.history.append(tool_msg)

            self._trim_history()

        final_text = f"（已经连续做了 {self.max_steps} 步还没收尾，我先停下来等你指示。你可以说得更具体一点，或者让我继续。）"
        RT.publish({"type": "final", "text": final_text})
        return final_text

    def _trim_history(self, keep: int = 40) -> None:
        """别让上下文无限膨胀：保留最近若干轮，但别把带 tool_call_id 的配对拆散。"""
        if len(self.history) <= keep:
            return
        cut = len(self.history) - keep
        # 往前找到不是 tool 消息的位置，避免孤儿 tool 消息
        while cut < len(self.history) and self.history[cut].get("role") == "tool":
            cut += 1
        self.history = self.history[cut:]


def _preview_args(args: dict) -> str:
    for key in ("command", "path", "text", "target", "keys", "url", "action", "seconds", "note", "title"):
        if key in args and args[key] not in (None, ""):
            s = str(args[key]).replace("\n", " ⏎ ")
            return s[:120]
    return json.dumps(args, ensure_ascii=False)[:120] if args else ""


def _first_line(text: str, limit: int = 150) -> str:
    line = (text or "").strip().splitlines()[0] if text.strip() else ""
    return line[:limit]

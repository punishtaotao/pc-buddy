"""
server.py —— 电脑端的小服务。手机连上来发指令，这里负责收、跑、把过程推回手机。

路由：
    GET  /                 手机页面
    GET  /api/info         基本信息
    POST /api/send         发一条指令（可带图）
    GET  /api/events       SSE 实时进度
    POST /api/stop         急停
    POST /api/confirm      危险操作确认
    GET  /file/<子目录>/<文件名>   截图 / 上传的图片
"""

from __future__ import annotations

import base64
import json
import queue
import re
import socket
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agent as A
import config as C
import tools as T
from tools import DATA, RT, SHOTS, UPLOADS

CFG = C.load()
MOBILE = Path(__file__).resolve().parent / "mobile"


# ---------------------------------------------------------------- 事件总线

class Bus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._events: list[dict] = []
        self._subs: list[queue.Queue] = []

    def publish(self, ev: dict) -> None:
        with self._lock:
            self._seq += 1
            ev = dict(ev)
            ev["seq"] = self._seq
            ev["ts"] = datetime.now().strftime("%H:%M:%S")
            self._events.append(ev)
            if len(self._events) > 500:
                self._events = self._events[-500:]
            for q in list(self._subs):
                try:
                    q.put_nowait(ev)
                except Exception:
                    pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def recent(self, since: int = 0) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["seq"] > since]


BUS = Bus()
RT.publish = BUS.publish


# ---------------------------------------------------------------- 危险操作确认

class ConfirmCenter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self._counter = 0

    def ask(self, kind: str, detail: str) -> bool:
        timeout = int(CFG["agent"].get("confirm_timeout", 120))
        with self._lock:
            self._counter += 1
            cid = f"c{self._counter}"
            ev = threading.Event()
            self._pending[cid] = {"event": ev, "allow": False}

        BUS.publish({"type": "confirm_request", "id": cid, "kind": kind,
                     "detail": detail, "timeout": timeout})
        got = ev.wait(timeout)

        with self._lock:
            rec = self._pending.pop(cid, None)
        allowed = bool(rec and rec["allow"]) if got else False
        BUS.publish({"type": "confirm_done", "id": cid, "allow": allowed,
                     "reason": "已确认" if allowed else ("超时未确认" if not got else "已拒绝")})
        return allowed

    def resolve(self, cid: str, allow: bool) -> bool:
        with self._lock:
            rec = self._pending.get(cid)
            if not rec:
                return False
            rec["allow"] = allow
            rec["event"].set()
            return True

    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._pending)


CONFIRM = ConfirmCenter()
RT.request_confirm = CONFIRM.ask


# ---------------------------------------------------------------- 任务执行

class Runner:
    """单线程串行跑任务：一条没完，下一条排队。"""

    def __init__(self) -> None:
        self._inbox: queue.Queue = queue.Queue()
        self._agent = A.Agent(CFG)
        self.busy = False
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, text: str, images: list[Path]) -> int:
        self._inbox.put((text, images))
        return self._inbox.qsize()

    def stop(self) -> None:
        RT.stop_flag = True
        BUS.publish({"type": "warn", "text": "收到急停，正在中止当前动作…"})

    def _loop(self) -> None:
        while True:
            text, images = self._inbox.get()
            self.busy = True
            RT.reset_stop()
            RT.work_dir = Path(CFG["agent"]["work_dir"])
            started = time.time()
            BUS.publish({"type": "task_start", "text": text or "（只发了图片）",
                         "images": [f"/file/uploads/{p.name}" for p in images]})
            try:
                answer = self._agent.run(text, images)
                BUS.publish({"type": "task_end", "ok": True, "text": answer,
                             "elapsed": round(time.time() - started, 1)})
            except InterruptedError:
                BUS.publish({"type": "task_end", "ok": False, "text": "已按你的要求中止。",
                             "elapsed": round(time.time() - started, 1)})
            except A.DeepSeekError as e:
                BUS.publish({"type": "task_end", "ok": False, "text": f"调用模型失败：{e}",
                             "elapsed": round(time.time() - started, 1)})
            except Exception as e:
                traceback.print_exc()
                BUS.publish({"type": "task_end", "ok": False,
                             "text": f"出错了：{type(e).__name__}: {e}",
                             "elapsed": round(time.time() - started, 1)})
            finally:
                RT.stop_flag = True  # 空闲时保持「停」状态，避免下一轮没重置
                self.busy = False
                self._inbox.task_done()


RUNNER = Runner()


# ---------------------------------------------------------------- HTTP

def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "PCBuddy/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 小工具 ----

    def _token_ok(self, qs: dict) -> bool:
        want = CFG["server"]["token"]
        got = (qs.get("t", [""])[0]) or self.headers.get("X-Token", "")
        if not got:
            cookie = self.headers.get("Cookie", "")
            m = re.search(r"pcbuddy_token=([^;]+)", cookie)
            got = m.group(1) if m else ""
        return bool(want) and got == want

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _deny(self) -> None:
        # 必须在读完/丢弃请求体之前就断开连接，否则残留的 POST body 会被
        # 当成下一个请求去解析，客户端收到莫名其妙的 400。
        self.close_connection = True
        self._send(403, "访问令牌不对。请用电脑上显示的完整网址（带 ?t=...）打开。".encode("utf-8"),
                   extra={"Connection": "close"})

    def log_message(self, fmt, *args) -> None:  # 安静点
        if "/api/events" in str(args[0] if args else ""):
            return

    # ---- GET ----

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # 图标要在鉴权之前放行：iOS「添加到主屏幕」抓图标时不一定带上 Cookie，
        # 若被 403 掉，桌面上就会显示成一张网页截图。图标本身不含任何秘密。
        if path in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
                    "/icon-180.png", "/icon-512.png"):
            icon = MOBILE / ("icon-512.png" if path == "/icon-512.png" else "icon-180.png")
            if icon.exists():
                self._send(200, icon.read_bytes(), "image/png",
                           {"Cache-Control": "public, max-age=86400"})
            else:
                self._send(404, b"no icon")
            return

        if not self._token_ok(qs):
            self._deny()
            return

        if path == "/":
            page = MOBILE / "index.html"
            if not page.exists():
                self._send(500, b"mobile/index.html missing")
                return
            body = page.read_text(encoding="utf-8")
            self._send(200, body.encode("utf-8"), "text/html; charset=utf-8",
                       {"Set-Cookie": f"pcbuddy_token={CFG['server']['token']}; Path=/; Max-Age=31536000"})
            return

        if path == "/api/info":
            self._json({
                "model": CFG["deepseek"]["model"],
                "has_key": bool(CFG["deepseek"].get("api_key")),
                "key": C.masked_key(CFG),
                "work_dir": CFG["agent"]["work_dir"],
                "busy": RUNNER.busy,
                "vision": bool((CFG.get("vision") or {}).get("enabled")),
                "lan_ip": lan_ip(),
                "port": CFG["server"]["port"],
            })
            return

        if path == "/api/events":
            self._sse(qs)
            return

        if path.startswith("/file/"):
            self._serve_file(path[len("/file/"):])
            return

        self._send(404, b"not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if not self._token_ok(qs):
            self._deny()
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            return

        if path == "/api/send":
            self._handle_send(data)
        elif path == "/api/stop":
            RUNNER.stop()
            for cid in CONFIRM.pending_ids():
                CONFIRM.resolve(cid, False)
            self._json({"ok": True})
        elif path == "/api/confirm":
            ok = CONFIRM.resolve(str(data.get("id", "")), bool(data.get("allow")))
            self._json({"ok": ok})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    # ---- 具体处理 ----

    def _handle_send(self, data: dict) -> None:
        text = str(data.get("text") or "").strip()
        images_in = data.get("images") or []

        if not text and not images_in:
            self._json({"ok": False, "error": "什么都没发"}, 400)
            return

        saved: list[Path] = []
        for i, item in enumerate(images_in[:4]):
            try:
                name = re.sub(r"[^A-Za-z0-9._-]", "", str(item.get("name") or "")) or "photo"
                b64 = str(item.get("data") or "")
                if "," in b64 and b64.strip().startswith("data:"):
                    b64 = b64.split(",", 1)[1]
                blob = base64.b64decode(b64)
                if len(blob) > 12_000_000:
                    continue
                ext = Path(name).suffix.lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    ext = ".jpg"
                out = UPLOADS / f"phone_{datetime.now():%Y%m%d_%H%M%S}_{i}{ext}"
                out.write_bytes(blob)
                saved.append(out)
            except Exception as e:
                BUS.publish({"type": "warn", "text": f"有张图片没存下来：{e}"})

        if RUNNER.busy:
            BUS.publish({"type": "warn", "text": "上一件事还没做完，这条排到后面了。"})

        queued = RUNNER.submit(text, saved)
        self._json({"ok": True, "queued": queued, "images": len(saved)})

    def _serve_file(self, rel: str) -> None:
        safe = urllib.parse.unquote(rel).replace("\\", "/").lstrip("/")
        if ".." in safe:
            self._send(400, b"bad path")
            return
        base = DATA.resolve()
        target = (DATA / safe).resolve()
        try:
            target.relative_to(base)
        except Exception:
            self._send(403, b"forbidden")
            return
        if not target.is_file():
            self._send(404, b"no such file")
            return

        ext = target.suffix.lower()
        ctype = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".txt": "text/plain; charset=utf-8",
            ".log": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
        }.get(ext, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _sse(self, qs: dict) -> None:
        try:
            since = int((qs.get("since") or ["0"])[0])
        except Exception:
            since = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = BUS.subscribe()
        try:
            for ev in BUS.recent(since):
                self._sse_send(ev)
            last = time.time()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self._sse_send(ev)
                    last = time.time()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    if time.time() - last > 3600:
                        break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(q)

    def _sse_send(self, ev: dict) -> None:
        payload = json.dumps(ev, ensure_ascii=False)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


def main() -> None:
    host = CFG["server"]["host"]
    port = int(CFG["server"]["port"])
    ip = lan_ip()
    url = f"http://{ip}:{port}/?t={CFG['server']['token']}"

    print("=" * 68)
    print("  电脑管家 PC-Buddy  已启动")
    print("=" * 68)
    print(f"  模型      : {CFG['deepseek']['model']}   Key: {C.masked_key(CFG)}")
    print(f"  工作目录  : {CFG['agent']['work_dir']}")
    print(f"  本机地址  : http://{ip}:{port}/")
    print()
    print("  ★ 手机和电脑连同一个 WiFi，然后用手机浏览器打开：")
    print(f"    {url}")
    print()
    if not CFG["deepseek"].get("api_key"):
        print("  ⚠ 还没填 DeepSeek API Key！打开 config.json 填 api_key 后重启。")
        print()
    print("  按 Ctrl+C 退出。")
    print("=" * 68)

    try:
        import qrcode  # type: ignore
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        print("  ↑ 扫码直达（微信/相机扫一扫都行）")
    except Exception:
        pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[退出] 再见。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

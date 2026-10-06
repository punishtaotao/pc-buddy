"""
e2e_test.py —— 不依赖真实 API Key 的端到端自测。

跑法： python tests/e2e_test.py

它会起一个假模型接口，用临时配置拉起 pc-buddy 服务，然后模拟两台手机的两次请求：
  任务 1：纯文字指令 -> 验证工具调用、危险命令确认与拒绝、最终答复回流
  任务 2：文字 + 照片 -> 验证图片上传落盘、本地 OCR 取字、OCR 结果进入模型上下文、截图回传
跑完自动还原 config.json。
"""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

import mock_llm  # noqa: E402

TOKEN = "e2e-token-123"
PASS, FAIL = "PASS", "FAIL"
passed_count = 0
failed_names: list[str] = []


def check(name: str, cond: bool) -> bool:
    global passed_count
    if cond:
        passed_count += 1
        print(f"  [{PASS}] {name}")
    else:
        failed_names.append(name)
        print(f"  [{FAIL}] {name}")
    return bool(cond)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def post(url: str, payload: dict, timeout: int = 20) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(url: str, timeout: int = 20) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def run_task(base: str, q: str, text: str, images: list[dict], cursor: dict,
             label: str) -> tuple[list[dict], float]:
    """模拟手机发一条指令，订阅 SSE 收全过程，自动拒绝危险操作。"""
    events: list[dict] = []
    done = threading.Event()

    def listen() -> None:
        try:
            url = f"{base}/api/events?since={cursor['seq']}&t={TOKEN}"
            with urllib.request.urlopen(urllib.request.Request(url), timeout=120) as stream:
                for raw in stream:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except Exception:
                        continue
                    events.append(ev)
                    cursor["seq"] = max(cursor["seq"], ev.get("seq", 0))
                    if ev.get("type") == "confirm_request":
                        post(f"{base}/api/confirm{q}",
                             {"id": ev["id"], "allow": False})
                        print(f"  · 危险确认已自动拒绝：{(ev.get('detail') or '')[:60]}")
                    if ev.get("type") == "task_end":
                        done.set()
                        return
        except Exception as e:
            print(f"  · SSE 中断：{e}")
            done.set()

    threading.Thread(target=listen, daemon=True).start()
    time.sleep(0.5)

    t0 = time.time()
    resp = post(f"{base}/api/send{q}", {"text": text, "images": images})
    assert resp.get("ok"), f"发送失败: {resp}"

    if not done.wait(timeout=100):
        print(f"  · {label} 超时没结束")
    return events, time.time() - t0


def main() -> int:
    cfg_path = ROOT / "config.json"
    backup = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None

    mock_port, srv_port = free_port(), free_port()

    mock = mock_llm.serve(mock_port)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    cfg_path.write_text(json.dumps({
        "deepseek": {"base_url": f"http://127.0.0.1:{mock_port}", "api_key": "test-key",
                     "model": "mock-model", "temperature": 0.0},
        "server": {"host": "127.0.0.1", "port": srv_port, "token": TOKEN},
        "agent": {"max_steps": 8, "confirm_timeout": 25,
                  "work_dir": str(ROOT / "data" / "workspace"), "system_extra": ""},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    base, q = f"http://127.0.0.1:{srv_port}", f"?t={TOKEN}"
    uploads_dir = ROOT / "data" / "uploads"
    before_uploads = set(uploads_dir.glob("phone_*")) if uploads_dir.exists() else set()
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        info = None
        for _ in range(60):
            try:
                info = get_json(base + "/api/info" + q, timeout=2)
                break
            except Exception:
                time.sleep(0.4)
        if not info:
            print("[e2e] 服务没起来")
            return 1
        print(f"[e2e] 服务已起：模型={info['model']}  有Key={info['has_key']}  工作目录={info['work_dir']}\n")

        cursor = {"seq": 0}

        # ---------------- 任务 1：纯文字 ----------------
        print("[e2e] === 任务 1：纯文字指令 ===")
        ev1, t1 = run_task(base, q, "先跑个命令，再试个危险的", [], cursor, "任务1")
        kinds1 = [e.get("type") for e in ev1]
        final1 = next((e.get("text", "") for e in ev1 if e.get("type") == "final"), "")

        check("收到 task_start", "task_start" in kinds1)
        check("模型请求调用工具", "action" in kinds1)
        check("工具真实执行并回流", any(e.get("type") == "result" for e in ev1))
        check("危险命令触发手机确认", "confirm_request" in kinds1)
        check("拒绝后有回执", any(e.get("type") == "confirm_done" and not e.get("allow") for e in ev1))
        check("危险命令确实没有执行", "[已拒绝]" in final1 or "拒绝" in final1)
        check("最终答复里带上真实命令输出", "PC_BUDDY_OK" in final1)
        check("任务正常结束", "task_end" in kinds1)
        print(f"  · 用时 {t1:.1f}s，{len(ev1)} 条事件\n")

        # ---------------- 任务 2：文字 + 照片 ----------------
        print("[e2e] === 任务 2：文字 + 照片 ===")
        img = _make_test_photo()
        img_b64 = base64.b64encode(img.read_bytes()).decode("ascii")
        images = [{"name": "phone.png", "data": "data:image/png;base64," + img_b64}]

        ev2, t2 = run_task(base, q, "这张图里写了啥？", images, cursor, "任务2")
        kinds2 = [e.get("type") for e in ev2]
        final2 = next((e.get("text", "") for e in ev2 if e.get("type") == "final"), "")

        check("图片被接收并回落", any(e.get("type") == "task_start" and e.get("images") for e in ev2))
        check("OCR 结果进入模型上下文", "看到手机照片的文字: YES" in final2)
        check("照片路径已落盘", any((ROOT / "data" / "uploads").glob("phone_*")))
        check("任务正常结束", "task_end" in kinds2)
        print(f"  · 用时 {t2:.1f}s，{len(ev2)} 条事件\n")

        # ---------------- 任务 3：接口健壮性 ----------------
        print("[e2e] === 任务 3：接口与鉴权 ===")
        info2 = get_json(base + "/api/info" + q)
        check("info 接口正常", bool(info2.get("work_dir")))
        check("错误口令访问被拒绝", _forbidden(base))
        check("急停接口可用", post(f"{base}/api/stop{q}", {}).get("ok") is True)

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        mock.shutdown()

        # 只清掉本次测试自己产生的图片，不碰用户已有的文件
        for f in (uploads_dir.glob("phone_*") if uploads_dir.exists() else []):
            if f not in before_uploads:
                try:
                    f.unlink()
                except Exception:
                    pass
        tmp_photo = ROOT / "data" / "_e2e_photo.png"
        if tmp_photo.exists():
            try:
                tmp_photo.unlink()
            except Exception:
                pass

        if backup is not None:
            cfg_path.write_text(backup, encoding="utf-8")
            print("[e2e] 已还原原来的 config.json")
        elif cfg_path.exists():
            cfg_path.unlink()
            print("[e2e] 已删除测试用 config.json")

    print(f"\n[e2e] 通过 {passed_count} 项，失败 {len(failed_names)} 项")
    if failed_names:
        print("[e2e] 失败项：" + " / ".join(failed_names))
    print("[e2e] " + ("ALL PASS" if not failed_names else "FAILED"))
    return 0 if not failed_names else 1


def _make_test_photo() -> Path:
    """现场生成一张带中文的图，避免把二进制素材塞进仓库。"""
    from PIL import Image, ImageDraw, ImageFont

    p = ROOT / "data" / "_e2e_photo.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (900, 260), "white")
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 40)
    except Exception:
        font = ImageFont.load_default()
    d.text((30, 40), "帮我看看这个截图", fill="black", font=font)
    d.text((30, 130), "PC_BUDDY 12345", fill="black", font=font)
    im.save(p)
    return p


def _forbidden(base: str) -> bool:
    try:
        urllib.request.urlopen(base + "/api/info?t=wrong-token", timeout=5)
        return False
    except urllib.error.HTTPError as e:
        return e.code == 403
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())

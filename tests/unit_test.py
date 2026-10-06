"""
unit_test.py —— 针对已经踩过的坑做回归测试。跑法： python tests/unit_test.py

每个用例都对应一个真实修过的 bug，防止以后改回去。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

import agent as A  # noqa: E402
import config as C  # noqa: E402
import tools as T  # noqa: E402

passed = 0
failed: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed.append(name)
        print(f"  [FAIL] {name} {extra}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pcbuddy_unit_"))
    orig_cfg_path = C.CONFIG_PATH
    orig_example = C.EXAMPLE_PATH

    print("== 配置读取 ==")
    try:
        # 1) 记事本存的 JSON 带 BOM，必须能正常读（否则 Key 会「填了不生效」）
        C.CONFIG_PATH = tmp / "config.json"
        C.CONFIG_PATH.write_bytes(
            b'\xef\xbb\xbf{"server":{"token":"bom-token"},"deepseek":{"api_key":"sk-from-notepad"}}'
        )
        cfg = C.load()
        check("带 BOM 的 config.json 能正确解析", cfg["server"]["token"] == "bom-token",
              f"实际 token={cfg['server']['token']!r}")
        check("带 BOM 时 Key 不丢失", cfg["deepseek"]["api_key"] == "sk-from-notepad")

        # 2) JSON 写坏了，绝不能把用户文件覆盖掉
        broken = '{\n  "deepseek": {"api_key": "sk-mine" },\n}'
        C.CONFIG_PATH.write_text(broken, encoding="utf-8")
        C.load()
        check("配置解析失败时不回写、不毁用户文件",
              C.CONFIG_PATH.read_text(encoding="utf-8") == broken)

        # 3) 空 token 自动生成，且格式稳定
        C.CONFIG_PATH.write_text(json.dumps({"server": {"token": ""}}), encoding="utf-8")
        cfg = C.load()
        check("空 token 会自动生成", len(cfg["server"]["token"]) >= 8)

        # 4) 缺字段时用默认值补齐
        C.CONFIG_PATH.write_text(json.dumps({"deepseek": {"api_key": "sk-x"}}), encoding="utf-8")
        cfg = C.load()
        check("缺字段用默认值补齐", cfg["server"]["port"] == 8765 and cfg["agent"]["max_steps"] == 25)
    finally:
        C.CONFIG_PATH = orig_cfg_path
        C.EXAMPLE_PATH = orig_example

    print("== 危险操作判定 ==")
    check("shutdown 被判为危险", T._danger_reason("shutdown /s /t 0") is not None)
    check("递归强删被判为危险", T._danger_reason("Remove-Item C:\\x -Recurse -Force") is not None)
    check("注册表删除被判为危险", T._danger_reason("reg delete HKLM\\Software\\Foo") is not None)
    check("普通 echo 不误报", T._danger_reason("echo hello world") is None)
    check("普通 dir 不误报", T._danger_reason("Get-ChildItem D:\\") is None)
    check("写 C:\\Windows 会被标记", T._protected_reason("C:\\Windows\\System32\\drivers\\etc\\hosts") is not None)
    check("写工作目录不会被标记", T._protected_reason(str(ROOT / "data" / "workspace" / "a.txt")) is None)

    print("== OCR 文本规整 ==")
    check("汉字之间的空格被去掉", T._fix_cjk_spaces("帮 我 看 看 这 个 截 图") == "帮我看看这个截图")
    check("汉字与标点之间的空格被去掉", T._fix_cjk_spaces("好 的 。") == "好的。")
    check("英文单词之间的空格保留",
          T._fix_cjk_spaces("hello world 123") == "hello world 123")
    check("中英混排不乱删", "API" in T._fix_cjk_spaces("DeepSeek API 已 连 接"))

    print("== API Key 校验 ==")
    fake_cfg = {"deepseek": {"api_key": "在这里填你的 DeepSeek API Key", "model": "deepseek-chat",
                             "base_url": "https://api.deepseek.com"}}
    try:
        A._chat(fake_cfg, [{"role": "user", "content": "hi"}])
        check("中文占位文案不会被当成 Key 发出去", False, "居然没报错")
    except A.DeepSeekError:
        check("中文占位文案不会被当成 Key 发出去", True)
    except Exception as e:
        check("中文占位文案不会被当成 Key 发出去", False, f"抛了别的异常 {e}")

    empty_cfg = {"deepseek": {"api_key": "   ", "model": "deepseek-chat",
                              "base_url": "https://api.deepseek.com"}}
    try:
        A._chat(empty_cfg, [{"role": "user", "content": "hi"}])
        check("空 Key 直接给出可读提示", False)
    except A.DeepSeekError as e:
        check("空 Key 直接给出可读提示", "config.json" in str(e))

    print("== 工具注册表 ==")
    schemas = T.openai_tool_schemas()
    check("工具数量合理", len(schemas) >= 14, f"实际 {len(schemas)}")
    check("每个工具都有名称/描述/JSON Schema",
          all(s["type"] == "function" and s["function"]["name"]
              and s["function"]["description"]
              and s["function"]["parameters"]["type"] == "object"
              for s in schemas))
    missing = [s["function"]["name"] for s in T.TOOLS.values()
               if not callable(s.get("fn"))]
    check("每个工具都有实现函数", not missing, str(missing))
    check("未知工具返回错误字符串而不是抛异常",
          T.call_tool("no_such_tool", {}).startswith("[错误]"))

    print("== 文本截断 ==")
    long = "x" * 10000
    t = T._truncate(long, 1000)
    check("长文本被截断且保留首尾", len(t) < 1200 and t.startswith("x") and t.endswith("x"))

    print(f"\n[unit] 通过 {passed} 项，失败 {len(failed)} 项")
    if failed:
        print("[unit] 失败项：" + " / ".join(failed))
    print("[unit] " + ("ALL PASS" if not failed else "FAILED"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

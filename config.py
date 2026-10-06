"""config.py —— 读写 config.json，第一次运行自动生成一份带随机令牌的配置。"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"

DEFAULTS: dict = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.2,
    },
    "vision": {
        "enabled": False,
        "base_url": "",
        "api_key": "",
        "model": "",
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8765,
        "token": "",
    },
    "agent": {
        "max_steps": 25,
        "confirm_timeout": 120,
        "work_dir": "",
        "system_extra": "",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    cfg = DEFAULTS
    parse_failed = False

    if CONFIG_PATH.exists():
        try:
            # utf-8-sig：记事本保存的 JSON 常带 BOM，用普通 utf-8 读会解析失败。
            raw = CONFIG_PATH.read_text(encoding="utf-8-sig")
            cfg = _merge(DEFAULTS, json.loads(raw))
        except Exception as e:
            parse_failed = True
            print("!" * 68)
            print(f"[配置] config.json 解析失败：{e}")
            print("[配置] 为了不覆盖你的文件，这次先按默认值运行，并且不会回写。")
            print("[配置] 请检查 json 格式（常见原因：多了逗号、少了引号、用了中文引号）。")
            print("!" * 68)
    elif EXAMPLE_PATH.exists():
        try:
            cfg = _merge(DEFAULTS, json.loads(EXAMPLE_PATH.read_text(encoding="utf-8-sig")))
            cfg["deepseek"]["api_key"] = ""   # 示例里的占位说明不算 Key
        except Exception:
            pass

    if not cfg["server"]["token"]:
        cfg["server"]["token"] = secrets.token_urlsafe(12)

    if not cfg["agent"]["work_dir"]:
        cfg["agent"]["work_dir"] = str(ROOT / "data" / "workspace")

    # 解析失败时绝不回写，否则用户填好的 Key 会被默认值冲掉。
    if not parse_failed:
        save(cfg)
    return cfg


def save(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception as e:
        print(f"[配置] 写 config.json 失败：{e}")


def masked_key(cfg: dict) -> str:
    key = (cfg.get("deepseek") or {}).get("api_key") or ""
    if not key:
        return "（未填）"
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:6]}...{key[-4:]}"

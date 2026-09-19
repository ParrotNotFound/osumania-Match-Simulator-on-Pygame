#!/usr/bin/env python3
# main.py
"""osu!mania 模拟对战 —— 程序入口。

用法：
    python main.py                     # 使用项目根目录的 config.toml
    python main.py --config my.toml    # 使用指定的配置文件
"""
from __future__ import annotations

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.utils.config import DEFAULT_CONFIG_NAME, ConfigError, load_config  # noqa: E402


def _fix_console_encoding() -> None:
    """Windows 控制台默认不是 UTF-8，避免中文日志直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main(argv=None) -> int:
    _fix_console_encoding()

    parser = argparse.ArgumentParser(description="osu!mania 模拟对战")
    parser.add_argument(
        "-c", "--config",
        default=os.path.join(PROJECT_ROOT, DEFAULT_CONFIG_NAME),
        help=f"配置文件路径（默认项目根目录的 {DEFAULT_CONFIG_NAME}）",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"配置错误：{error}")
        return 1

    # 延迟导入：配置有误时不用先把 pygame 拉起来
    from src.core.game import OsuGame

    OsuGame(config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

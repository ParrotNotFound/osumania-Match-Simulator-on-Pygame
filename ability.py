#!/usr/bin/env python3
# ability.py
"""独立小工具：输入选手名字，输出他的能力值。

不依赖 pygame、不读谱面、不启动游戏 —— 只跑「名字哈希 → 能力值」那一段逻辑。
能力值的生成逻辑直接复用 src/entities/player.py，所以这里看到的结果
和游戏里同一名字打出来的完全一致（基准与风格只由名字决定，每局都一样）。

用法：
    python ability.py 名字 [名字 ...]        # 直接查，多个名字给表格
    python ability.py --form 20 名字        # 指定手感幅度（默认取 config.toml）
    python ability.py --rolls 5 名字        # 连抽 5 局的手感看看
    python ability.py --styles              # 打印选手风格表
    python ability.py                       # 交互模式：一行一个名字，空行结束
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional


def _base_dir() -> str:
    """本程序所在目录。

    打包成 exe 之后 `__file__` 指向解包出来的临时目录，配置要按 exe 自己的位置找，
    所以这里区分一下 —— 这样 exe 拷到哪儿、旁边放不放 config.toml 都能正常工作。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


PROJECT_ROOT = _base_dir()
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.entities.player import (  # noqa: E402
    ABILITY_KEYS, ABILITY_LABELS, BASE_MAX, BASE_MIN, FORM_KEYS, SPEED_GAP_MAX, SPEED_GAP_MIN,
    STYLES, Player, timing_sigma_for,
)
from src.utils.config import DEFAULT_CONFIG_NAME  # noqa: E402


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _display_width(text: object) -> int:
    """终端显示宽度：中日韩字符按 2 格算，好让表格对齐。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(text))


def _pad(text: object, width: int, align: str = "left") -> str:
    text = str(text)
    space = " " * max(0, width - _display_width(text))
    return space + text if align == "right" else text + space


def _read_form_range(path: str) -> Optional[int]:
    """从一份 TOML 里只读 [players] form_range。

    刻意不走 load_config 的完整校验：分发给别人时，exe 旁边放一行
    `[players]` + `form_range = 33` 就该生效，不该逼人家准备一整份配置。
    """
    import tomllib

    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    players = data.get("players")
    if not isinstance(players, dict):
        return None
    value = players.get("form_range")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def default_form_range() -> int:
    """默认手感幅度取 config.toml 里 [players] 的 form_range，读不到就用 10。

    先找程序旁边的 config.toml（分发给别人时可以丢一份在 exe 边上），
    再找当前目录，都没有就用内置默认值 —— 所以不带配置也能跑。
    """
    for folder in (PROJECT_ROOT, os.getcwd()):
        value = _read_form_range(os.path.join(folder, DEFAULT_CONFIG_NAME))
        if value is not None:
            return value
    return 10


def make_player(name: str, form_range: int = 0) -> Player:
    """造一个只带名字的选手；队伍/位置编号不影响能力值。"""
    return Player(name, 0, 0, form_range=form_range)


def sigma_of(accuracy: int) -> float:
    """准度 → 落点误差的标准差（毫秒）。公式在 player.py 里，和游戏用的是同一个。"""
    return timing_sigma_for(accuracy)


def gap_of(speed: int) -> float:
    """手速 → 能"从容处理"的同键间隔（毫秒），越小说明能吃越密的谱。"""
    return SPEED_GAP_MAX - (SPEED_GAP_MAX - SPEED_GAP_MIN) * (speed / 100.0)


def first_roll(name: str, form_range: int) -> Player:
    return make_player(name, form_range)


# ----------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------
def show_one(name: str, form_range: int, rolls: int) -> None:
    player = make_player(name)                  # 手感为 0：拿到纯基准与风格
    base = player.base_abilities
    # 手感幅度要用"带手感重掷一次"才会算出来，直接读 form=0 的实例永远是 0
    swing = make_player(name, form_range).form_swing

    print(f"{name}")
    print(f"  风格    {player.style}")
    print("  基准    " + "   ".join(f"{ABILITY_LABELS[key]} {base[key]:>3}" for key in ABILITY_KEYS))

    if form_range <= 0 or swing <= 0:
        print("  手感    ±0（没有手感波动）")
    else:
        float_keys = "、".join(ABILITY_LABELS[key] for key in FORM_KEYS)
        fixed_keys = "、".join(ABILITY_LABELS[key] for key in ABILITY_KEYS if key not in FORM_KEYS)
        print(f"  手感    ±{swing}（幅度由基准稳定性 {base['consistency']} 决定，"
              f"上限 ±{form_range}；只浮动 {float_keys}，{fixed_keys} 每局恒为基准值）")
        for index in range(max(1, rolls)):
            rolled = make_player(name, form_range)
            values = "   ".join(
                f"{ABILITY_LABELS[key]} {getattr(rolled, key):>3}"
                f"({rolled.ability_form[key]:+d})" for key in ABILITY_KEYS)
            print(("  本局    " if index == 0 else "          ") + values)

    print(f"  换算    基准准度 {base['avg_accuracy']:>3} → 落点误差 σ≈{sigma_of(base['avg_accuracy']):.1f}ms"
          f"    基准手速 {base['speed']:>3} → 能从容处理 {gap_of(base['speed']):.0f}ms 的同键间隔")
    print()


def show_table(names, form_range: int) -> None:
    header = ["名字", "风格"] + [ABILITY_LABELS[key] for key in ABILITY_KEYS] + ["手感"]
    rows = []
    for name in names:
        player = make_player(name, form_range)   # 基准不受手感影响，但 form_swing 要这样才算得出来
        base = player.base_abilities
        rows.append([name, player.style] + [base[key] for key in ABILITY_KEYS] + [f"±{player.form_swing}"])

    widths = [max(_display_width(header[i]), *(_display_width(row[i]) for row in rows))
              for i in range(len(header))]

    def line(cells, align_right_from=2):
        parts = []
        for i, cell in enumerate(cells):
            align = "right" if i >= align_right_from and i < len(cells) - 1 else "left"
            parts.append(_pad(cell, widths[i], align))
        return "  ".join(parts).rstrip()

    print(line(header))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print(line(row))
    if form_range > 0:
        print(f"\n手感幅度默认为 ±{form_range}（每局在此范围内浮动，稳定性越高越小）；"
              f"可用 --form 指定。")
    print()


def show_styles(form_range: int) -> None:
    print("选手风格由名字哈希决定（同一个名字风格固定），会给五项能力加偏置：\n")
    header = ["风格"] + [ABILITY_LABELS[key] for key in ABILITY_KEYS]
    body = []
    for style_name, bias in STYLES:
        body.append([style_name] + [f"{bias.get(key, 0):+d}" if bias.get(key, 0) else "-"
                                    for key in ABILITY_KEYS])
    widths = [max(_display_width(header[i]), *(_display_width(row[i]) for row in body))
              for i in range(len(header))]
    print("  ".join(_pad(header[i], widths[i]) for i in range(len(header))).rstrip())
    print("  ".join("-" * width for width in widths))
    for row in body:
        print("  ".join(_pad(row[i], widths[i], "right" if i else "left")
                        for i in range(len(header))).rstrip())
    print(f"\n基准值 = 名字哈希在 {BASE_MIN}~{BASE_MAX} 上取值 + 风格偏置，"
          f"再叠加每局手感（默认 ±{form_range}）。")


def interactive(form_range: int, rolls: int) -> None:
    print(f"输入选手名字查看能力值（手感 ±{form_range}）。直接回车或 Ctrl+Z 结束。\n")
    while True:
        try:
            name = input("名字> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not name:
            return
        show_one(name, form_range, rolls)


def main(argv=None) -> int:
    # 输出编码：对着控制台时保持系统默认（Windows 上中文正好显示正常），
    # 被重定向/管道接走时统一成 UTF-8 —— 这样存出来的文件编码是确定的。
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="输入名字，输出这个选手的能力值（独立小工具，不需要 pygame）")
    parser.add_argument("names", nargs="*", help="选手名字，可以给多个；不填则进入交互模式")
    parser.add_argument("-f", "--form", type=int, default=None,
                        help="手感波动幅度（默认取 config.toml 的 [players] form_range）")
    parser.add_argument("-r", "--rolls", type=int, default=1,
                        help="单个名字时连抽几局手感看（默认 1）")
    parser.add_argument("--styles", action="store_true", help="打印选手风格表")
    args = parser.parse_args(argv)

    form_range = default_form_range() if args.form is None else args.form
    if form_range < 0:
        print("配置错误：手感幅度不能是负数")
        return 1

    if args.styles:
        show_styles(form_range)
        return 0

    if not args.names:
        interactive(form_range, max(1, args.rolls))
        # 打包成 exe 双击运行时，交互模式结束后窗口会立刻关掉，留一下
        if getattr(sys, "frozen", False):
            try:
                input("按回车键退出...")
            except (EOFError, KeyboardInterrupt):
                pass
    elif len(args.names) == 1:
        show_one(args.names[0], form_range, max(1, args.rolls))
    else:
        show_table(args.names, form_range)
    return 0


if __name__ == "__main__":
    sys.exit(main())

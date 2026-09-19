# src/utils/axis_to_track.py
"""把 osu!mania 谱面里音符的 x 坐标换算成键位（轨道）编号。

4K 谱面的 x 固定是 64 / 192 / 320 / 448，正好落在 x // 128 = 0 / 1 / 2 / 3。
"""
from __future__ import annotations

TRACK_COUNT = 4


def axis_to_4k(x: int) -> int:
    """x 坐标 -> 0~3 的轨道编号（本模拟器只按 4K 处理）。"""
    return max(0, min(TRACK_COUNT - 1, int(x) // 128))

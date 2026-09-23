# src/core/judge.py
"""判定系统：把"打早了/打晚了多少毫秒"换算成判定等级。

分值不在这里 —— 计分照搬 osu!lazer 的 mania 方案，权重写在 src/entities/player.py。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from ..utils.config import JudgeSettings

# osu!lazer 的 mania 判定窗口区间，每个是 (OD0, OD5, OD10) 三点，单位毫秒（半宽）。
# 源码：osu.Game.Rulesets.Mania/Scoring/ManiaHitWindows.cs
# 注意窗口随 OD 升高而**变窄**，所以 OD0 的值最大。
MANIA_WINDOW_RANGES: Dict[str, Tuple[float, float, float]] = {
    'perfect_g': (22.4, 19.4, 13.9),   # lazer: Perfect
    'perfect': (64.0, 49.0, 34.0),     # lazer: Great
    'great': (97.0, 82.0, 67.0),       # lazer: Good
    'good': (127.0, 112.0, 97.0),      # lazer: Ok
    'bad': (151.0, 136.0, 121.0),      # lazer: Meh  ← 本项目最宽的那档对标 lazer 的 Meh
}


def difficulty_range(difficulty: float, window_range: Tuple[float, float, float]) -> float:
    """lazer 的 IBeatmapDifficultyInfo.DifficultyRange：过 (0,min) (5,mid) (10,max) 的折线。

    OD 超出 0~10 时会外推（和 lazer 一致，不做截断）。
    """
    low, mid, high = window_range
    if difficulty > 5:
        return mid + (high - mid) * (difficulty - 5) / 5
    if difficulty < 5:
        return mid + (mid - low) * (difficulty - 5) / 5
    return mid


def windows_for_od(overall_difficulty: float) -> Dict[str, float]:
    """按谱面 OD 算出五个判定窗口。

    lazer 最后会 `Math.Floor(x) + 0.5`，所以结果都是 x.5；
    顺带说一句，lazer 还有一个 Miss 窗口（OD10 时 158.5ms），
    那是"音符过期"的时间，不是判定档位 —— 本项目超过 bad 就直接算 miss。
    """
    return {key: math.floor(difficulty_range(overall_difficulty, window_range)) + 0.5
            for key, window_range in MANIA_WINDOW_RANGES.items()}


@dataclass
class JudgementConfig:
    """判定窗口（数值全部来自 config.toml 的 [judge] 段）。

    默认值是 osu!lazer 的 mania 窗口在 OD10 下的取值（半宽，单位毫秒），
    五档一一对应 lazer 的 Perfect / Great / Good / Ok / Meh；
    超过 bad 一律算 miss。
    开了 follow_chart_od 之后，窗口会按每张谱自己的 OD 重算（见 create_windows）。
    """
    perfect_g: float = 13.5      # lazer: Perfect
    perfect: float = 34.5        # lazer: Great
    great: float = 67.5          # lazer: Good
    good: float = 97.5           # lazer: Ok
    bad: float = 121.5           # lazer: Meh

    @classmethod
    def from_settings(cls, settings: JudgeSettings) -> "JudgementConfig":
        return cls(
            perfect_g=settings.perfect_g,
            perfect=settings.perfect,
            great=settings.great,
            good=settings.good,
            bad=settings.bad,
        )

    def use_overall_difficulty(self, overall_difficulty: float) -> None:
        """把五个窗口换成这张谱 OD 对应的 lazer 窗口。"""
        for key, value in windows_for_od(overall_difficulty).items():
            setattr(self, key, value)


class JudgeSystem:
    def __init__(self, config: Optional[JudgementConfig] = None):
        self.config = config or JudgementConfig()

    def get_judgement(self, time_diff: int) -> str:
        """根据时间差（谱面时间 - 当前时间）返回判定结果。

        负数代表打早了，正数代表打晚了；超过 bad 窗口一律算 miss。
        """
        abs_diff = abs(time_diff)
        cfg = self.config
        if abs_diff <= cfg.perfect_g:
            return 'perfect_g'
        if abs_diff <= cfg.perfect:
            return 'perfect'
        if abs_diff <= cfg.great:
            return 'great'
        if abs_diff <= cfg.good:
            return 'good'
        if abs_diff <= cfg.bad:
            return 'bad'
        return 'miss'

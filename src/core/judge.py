# src/core/judge.py
"""判定系统：把"打早了/打晚了多少毫秒"换算成判定等级与分值。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from ..utils.config import DEFAULT_BONUS, DEFAULT_SCORES, JudgeSettings


@dataclass
class JudgementConfig:
    """判定窗口与分值配置（数值全部来自 config.toml 的 [judge] 段）

    超过 bad 一律算 miss，所以没有单独的 miss 窗口。
    """
    perfect_g: int = 5
    perfect: int = 25
    great: int = 45
    good: int = 60
    bad: int = 80
    score_values: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SCORES))
    bonus_values: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_BONUS))
    bonus_start: float = 100.0
    bonus_max: float = 100.0

    @classmethod
    def from_settings(cls, settings: JudgeSettings) -> "JudgementConfig":
        return cls(
            perfect_g=settings.perfect_g,
            perfect=settings.perfect,
            great=settings.great,
            good=settings.good,
            bad=settings.bad,
            score_values=dict(settings.score),
            bonus_values=dict(settings.bonus),
            bonus_start=settings.bonus_start,
            bonus_max=settings.bonus_max,
        )


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

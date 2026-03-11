# src/core/judge.py
from dataclasses import dataclass
from typing import Dict, Any

@dataclass
class JudgementConfig:
    """判定窗口配置"""
    perfect_g: int = 10
    perfect: int = 30
    great: int = 45
    good: int = 60
    bad: int = 80
    miss: int = 80
    
    score_values: Dict[str, int] = None
    bonus_values: Dict[str, float] = None
    
    def __post_init__(self):
        if self.score_values is None:
            self.score_values = {
                'perfect_g': 320, 'perfect': 300, 'great': 200, 'good': 100,
                'bad': 50, 'miss': 0
            }
        if self.bonus_values is None:
            self.bonus_values = {
                'perfect_g': 0.5, 'perfect': 0.25, 'great': 0, 'good': -1,
                'bad': -3, 'miss': -50
            }

class JudgeSystem:
    def __init__(self, config: JudgementConfig = None):
        self.config = config or JudgementConfig()
    
    def get_judgement(self, time_diff: int) -> str:
        """根据时间差返回判定结果"""
        abs_diff = abs(time_diff)
        if abs_diff <= self.config.perfect:
            return 'perfect_g'
        if abs_diff <= self.config.perfect:
            return 'perfect'
        elif abs_diff <= self.config.great:
            return 'great'
        elif abs_diff <= self.config.good:
            return 'good'
        elif abs_diff <= self.config.bad:
            return 'bad'
        elif abs_diff <= self.config.miss:
            return 'miss'
        return 'miss'  # 超时
    
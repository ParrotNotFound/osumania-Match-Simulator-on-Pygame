# src/core/judge.py
from dataclasses import dataclass
from typing import Dict, Any

@dataclass
class JudgementConfig:
    """判定窗口配置"""
    perfect: int = 12
    great: int = 25
    good: int = 50
    bad: int = 70
    miss: int = 80
    
    score_values: Dict[str, int] = None
    bonus_values: Dict[str, float] = None
    
    def __post_init__(self):
        if self.score_values is None:
            self.score_values = {
                'perfect': 300, 'great': 200, 'good': 100,
                'bad': 50, 'miss': 0
            }
        if self.bonus_values is None:
            self.bonus_values = {
                'perfect': 0.5, 'great': 0.25, 'good': -1,
                'bad': -3, 'miss': -50
            }

class JudgeSystem:
    def __init__(self, config: JudgementConfig = None):
        self.config = config or JudgementConfig()
    
    def get_judgement(self, time_diff: int) -> str:
        """根据时间差返回判定结果"""
        abs_diff = abs(time_diff)
        
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
    
    def calculate_score(self, time_diff: int, combo: int) -> Dict[str, Any]:
        """计算单次击打分数"""
        judgement = self.get_judgement(time_diff)
        
        base_score = self.config.score_values[judgement]
        bonus = self.config.bonus_values[judgement]
        
        # 连击加成（示例）
        combo_multiplier = 1.0 + (combo // 10) * 0.1
        
        return {
            'judgement': judgement,
            'base_score': base_score,
            'bonus': bonus,
            'combo_multiplier': combo_multiplier,
            'score': base_score * combo_multiplier
        }
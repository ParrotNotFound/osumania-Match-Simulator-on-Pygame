# src/entities/team.py
from __future__ import annotations

from typing import List, Optional

from ..core.judge import JudgeSystem
from ..utils.config import TeamConfig
from .player import Player


class Team:
    def __init__(self, config: TeamConfig, team_id: int = 0,
                 judge_system: Optional[JudgeSystem] = None, form_range: int = 0):
        self.id = team_id
        self.name = config.name
        self.color = tuple(config.color)
        self.players: List[Player] = [
            Player(name, team_index=team_id, player_index=index,
                   judge_system=judge_system, form_range=form_range)
            for index, name in enumerate(config.players)
        ]

    @property
    def total_score(self) -> float:
        """队伍总分数"""
        return sum(player.std_score for player in self.players)

    @property
    def avg_accuracy(self) -> float:
        """队伍平均准确率"""
        if not self.players:
            return 0.0
        return sum(player.accuracy for player in self.players) / len(self.players)

    def reset_for_new_song(self) -> None:
        """为新歌曲重置玩家状态（能力值保留，只清空每首歌的临时状态）"""
        for player in self.players:
            player.reset_for_new_song()

    def reroll_abilities(self, form_range: int) -> None:
        """重新生成本局每名队员的能力值（含量手感加成）。"""
        for player in self.players:
            player.roll_abilities(form_range)

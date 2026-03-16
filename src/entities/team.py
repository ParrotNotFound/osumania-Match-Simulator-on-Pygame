# src/entities/team.py
from typing import List
from .player import Player

class Team:
    def __init__(self, name: str, color: tuple, player_names: List[str], id: int = 0):
        self.id = id
        self.name = name
        self.color = color
        self.players: List[Player] = []
        
        # 初始化队员
        for i, player_name in enumerate(player_names):
            player = Player(player_name, team_index=0 if "red" in name.lower() else 1, player_index=i)
            self.players.append(player)
    
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
    
    def reset_for_new_song(self):
        """为新歌曲重置玩家状态（保留能力值）"""
        for player in self.players:
            player.init_abilities()
            player.score = 0.0
            player.combo = 0
            player.stamina_left = [10000, 10000]
            player.tap_times = [-3000, -3000, -3000, -3000]  
            player.active_notes = []
            player.judgement_counts = {k: 0 for k in ['perfect_g', 'perfect', 'great', 'good', 'bad', 'miss']}
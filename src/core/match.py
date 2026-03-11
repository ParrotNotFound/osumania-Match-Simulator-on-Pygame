# src/core/match.py
from typing import List, Optional, Dict
from ..entities.team import Team
from ..entities.song import Song

class Match:
    def __init__(self, name: str, rounds_to_win: int = 1):
        self.name = name
        self.rounds_to_win = rounds_to_win
        
        self.teams: List[Team] = []
        self.song_pool: List[Song] = []
        self.selected_songs: List[Song] = []
        
        self.scores: List[int] = [0, 0]  # 两队得分
        self.current_round: int = 0
        self.winner: Optional[Team] = None
        self.is_finished: bool = False
    
    def add_team(self, team: Team):
        """添加队伍"""
        if len(self.teams) < 2:
            self.teams.append(team)
    
    def add_song(self, song: Song):
        """添加歌曲到曲库"""
        self.song_pool.append(song)
    
    def select_song(self, song: Song):
        """选择一首歌进行比赛"""
        self.selected_songs.append(song)
        self.current_round = len(self.selected_songs) - 1
    
    def record_round_result(self, winning_team_index: int):
        """记录一局结果"""
        self.scores[winning_team_index] += 1
        
        # 检查是否有队伍获胜
        for i, score in enumerate(self.scores):
            if score >= self.rounds_to_win:
                self.winner = self.teams[i]
                self.is_finished = True
                break
    
    def get_match_progress(self) -> Dict:
        """获取比赛进度"""
        return {
            'name': self.name,
            'scores': self.scores.copy(),
            'rounds_to_win': self.rounds_to_win,
            'current_round': self.current_round,
            'winner': self.winner.name if self.winner else None,
            'is_finished': self.is_finished
        }
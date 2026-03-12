# src/core/match.py
from typing import List, Optional, Dict
from ..entities.team import Team
from ..entities.song import Song
from ..utils.file_loader import write_results,load_results

class Match:
    def __init__(self, name: str, rounds_to_win: int = 1, results_file: str = "data/matchdata.txt"):
        self.name = name
        self.rounds_to_win = rounds_to_win
        self.results_file = results_file
        self.teams: List[Team] = []
        self.song_pool: List[Song] = []
        self.selected_songs: List[Dict] = []
        
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
    
    def select_song(self, song: Song, team_index: int = 0):
        """选择一首歌进行比赛"""
        self.selected_songs.append({"song":song,"team":team_index})
        self.current_round = len(self.selected_songs) - 1
    
    def record_round_result(self, winning_team_index: int):
        """记录一局结果"""
        self.scores[winning_team_index] += 1
        write_results(self.results_file,winning_team_index)
        # 检查是否有队伍获胜
        for i, score in enumerate(self.scores):
            if score >= self.rounds_to_win:
                self.winner = self.teams[i]
                self.is_finished = True
                break
    def _load_total_results(self):
        results = load_results(self.results_file)
        self.scores = [0,0]
        self.current_round = 0
        for i in results:
            self.scores[i] += 1
            self.current_round += 1
        for i, score in enumerate(self.scores):
            if score >= self.rounds_to_win:
                self.winner = self.teams[i]
                self.is_finished = True
                break
    def get_match_progress(self, load_results: bool = False) -> Dict:
        """获取比赛进度"""
        if load_results:
            self._load_total_results(self)
        return {
            'name': self.name,
            'scores': self.scores.copy(),
            'rounds_to_win': self.rounds_to_win,
            'current_round': self.current_round,
            'winner': self.winner.name if self.winner else None,
            'is_finished': self.is_finished
        }
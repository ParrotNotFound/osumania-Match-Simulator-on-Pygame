# src/core/match.py
"""一场比赛：两队、曲库、选曲安排、大比分与赛果缓存。

赛果（每局一个胜者序号）直接缓存在 config.toml 的 [match] results 里：
每局打完立刻写回，下次启动读出来接着算。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..entities.song import Song
from ..entities.team import Team
from ..utils.config import PickConfig, save_match_results
from .judge import JudgeSystem


class Match:
    def __init__(self, name: str, rounds_to_win: int = 1,
                 results: Optional[List[int]] = None,
                 config_path: str = "",
                 picks: Optional[List[PickConfig]] = None,
                 judge_system: Optional[JudgeSystem] = None):
        self.name = name
        self.rounds_to_win = rounds_to_win
        self.config_path = config_path
        self.picks: List[PickConfig] = list(picks or [])
        self.judge_system = judge_system or JudgeSystem()

        self.teams: List[Team] = []
        self.song_pool: List[Song] = []
        self.selected_songs: List[Dict] = []
        # 历史赛果：每一局一个胜者序号，写回 config.toml
        self.results: List[int] = list(results or [])

        self.scores: List[int] = [0, 0]  # 两队大比分
        self.current_round: int = 0      # 正在进行的这一局的序号（从 0 开始）
        self.winner: Optional[Team] = None
        self.is_finished: bool = False

    # ------------------------------------------------------------------
    # 组建
    # ------------------------------------------------------------------
    def add_team(self, team: Team) -> None:
        """添加队伍（配置已保证只有两支）。"""
        if len(self.teams) < 2:
            self.teams.append(team)

    def add_song(self, song: Song) -> None:
        """添加歌曲到曲库"""
        self.song_pool.append(song)

    def replace_teams(self, teams: List[Team]) -> None:
        """整队替换（每首歌开始时重新读取队员名单用），大比分保持不变。"""
        self.teams = list(teams)

    def find_song(self, song_id: str) -> Optional[Song]:
        wanted = song_id.strip()
        for song in self.song_pool:
            if song.id.strip() == wanted:
                return song
        return None

    # ------------------------------------------------------------------
    # 选曲
    # ------------------------------------------------------------------
    def next_round_index(self) -> int:
        """下一局（还没选歌的那局）的序号。"""
        return len(self.selected_songs)

    def pick_for_round(self, index: int) -> Tuple[Optional[Song], int]:
        """返回第 index 局该用的 (曲目, 选曲队伍)，无法选曲时曲目为 None。

        - index 在 picks 范围内：用配置里的安排，team 没写就按轮次自动轮换
        - picks 用完之后：在曲库里按顺序循环
        """
        team_index = index % 2
        song: Optional[Song] = None

        if 0 <= index < len(self.picks):
            entry = self.picks[index]
            if entry.team is not None:
                team_index = entry.team
            song = self.find_song(entry.song)
            if song is None:
                print(f"警告：选曲配置里的 '{entry.song}' 不在曲库中，改用曲库第 1 首")

        if song is None and self.song_pool:
            song = self.song_pool[index % len(self.song_pool)]

        if self.teams:
            team_index = max(0, min(len(self.teams) - 1, team_index))
        else:
            team_index = 0
        return song, team_index

    def select_song(self, song: Song, team_index: int = 0) -> None:
        """记录"某一轮选了这首歌"，并把轮次推进到这一轮。"""
        self.selected_songs.append({"song": song, "team": team_index})
        self.current_round = len(self.selected_songs) - 1

    def prepare_round(self, form_range: int = 0) -> None:
        """每首歌开始时调用：清空上一局的临时状态，并重新生成本局能力值（含量随机手感）。"""
        for team in self.teams:
            team.reset_for_new_song()
            team.reroll_abilities(form_range)

    # ------------------------------------------------------------------
    # 结果与赛果缓存
    # ------------------------------------------------------------------
    def record_round_result(self, winning_team_index: int) -> None:
        """记录一局结果，写回配置文件的缓存，并检查比赛是否结束。"""
        if not self.scores:
            return
        winning_team_index = max(0, min(len(self.scores) - 1, winning_team_index))
        self.scores[winning_team_index] += 1
        self.results.append(winning_team_index)
        self.save_results()

        for index, score in enumerate(self.scores):
            if score >= self.rounds_to_win:
                if index < len(self.teams):
                    self.winner = self.teams[index]
                self.is_finished = True
                break

    def save_results(self) -> None:
        """把到本场为止的全部赛果写回 config.toml 的 [match] results。"""
        if not self.config_path:
            return
        save_match_results(self.config_path, self.results)

    def clear_results(self) -> None:
        """清空赛果缓存（同时写回配置文件）。"""
        self.results = []
        self.save_results()

    def reset_progress(self) -> None:
        """清空大比分、胜者与轮次（不动 results 缓存）。"""
        self.scores = [0] * max(2, len(self.teams))
        self.selected_songs = []
        self.current_round = 0
        self.winner = None
        self.is_finished = False

    def apply_cached_results(self) -> None:
        """按缓存里的赛果重建大比分与轮次（启动时用）。"""
        self.reset_progress()
        for index, winner in enumerate(self.results):
            song, team_index = self.pick_for_round(index)
            if song is not None:
                self.select_song(song, team_index)
            if 0 <= winner < len(self.scores):
                self.scores[winner] += 1
            else:
                print(f"警告：赛果缓存里的第 {index + 1} 条是无效的队伍序号 {winner}，已忽略")
            if any(score >= self.rounds_to_win for score in self.scores):
                self.is_finished = True
                break

        if self.is_finished:
            for index, score in enumerate(self.scores):
                if score >= self.rounds_to_win and index < len(self.teams):
                    self.winner = self.teams[index]
                    break

    def get_match_progress(self, load_results: bool = False) -> Dict:
        """获取比赛进度（load_results=True 时先按缓存重建一次）。"""
        if load_results:
            self.apply_cached_results()
        return {
            'name': self.name,
            'scores': self.scores.copy(),
            'rounds_to_win': self.rounds_to_win,
            'current_round': self.current_round,
            'winner': self.winner.name if self.winner else None,
            'is_finished': self.is_finished,
        }

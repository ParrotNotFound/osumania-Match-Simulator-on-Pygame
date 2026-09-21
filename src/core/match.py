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
                 judge_system: Optional[JudgeSystem] = None,
                 score_mode: bool = False):
        self.name = name
        self.rounds_to_win = rounds_to_win
        self.config_path = config_path
        self.picks: List[PickConfig] = list(picks or [])
        self.judge_system = judge_system or JudgeSystem()
        # 计分赛：曲库里的歌各打一遍，按总分排名
        self.score_mode = score_mode

        self.teams: List[Team] = []
        self.song_pool: List[Song] = []
        self.selected_songs: List[Dict] = []
        # 历史赛果：每一局一个胜者序号，写回 config.toml
        self.results: List[int] = list(results or [])
        # 每一局的成绩快照（歌曲 + 各队总分 + 各队每个队员的分数），用于结算榜和 Excel
        self.round_records: List[Dict] = []
        # 计分赛的曲目安排（第一次用到时算好并缓存）
        self._score_tracks: Optional[List[Tuple[Song, int]]] = None

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

    def score_tracks(self) -> List[Tuple[Song, int]]:
        """计分赛要打的曲目安排：配置里的 [[picks]] 按顺序各打一遍，返回 [(曲目, 选曲队)]。

        picks 一首都没写、或者写了但一首都对不上曲库时，退化成整个曲库按顺序打一遍。
        结果会缓存，避免每次调用都重复打警告。
        """
        if self._score_tracks is None:
            self._score_tracks = self._build_score_tracks()
        return self._score_tracks

    def _build_score_tracks(self) -> List[Tuple[Song, int]]:
        tracks: List[Tuple[Song, int]] = []
        for index, entry in enumerate(self.picks):
            song = self.find_song(entry.song)
            if song is None:
                print(f"警告：选曲配置里的 '{entry.song}' 不在曲库中，计分赛已跳过这一首")
                continue
            team_index = entry.team if entry.team is not None else index % 2
            if self.teams:
                team_index = max(0, min(len(self.teams) - 1, team_index))
            tracks.append((song, team_index))
        if tracks:
            return tracks
        if self.picks:
            print("警告：[[picks]] 里没有一首能在曲库中找到，计分赛改为打整个曲库")
        return [(song, index % 2) for index, song in enumerate(self.song_pool)]

    def total_tracks(self) -> Optional[int]:
        """计分赛要打的曲目数；不是计分赛就返回 None（由 rounds_to_win 决定何时结束）。"""
        return len(self.score_tracks()) if self.score_mode else None

    def pick_for_round(self, index: int) -> Tuple[Optional[Song], int]:
        """返回第 index 局该用的 (曲目, 选曲队伍)，无法选曲时曲目为 None。

        计分赛：按 [[picks]] 的顺序打，每一条各打一遍，打满为止。
        普通模式：
        - index 在 picks 范围内：用配置里的安排，team 没写就按轮次自动轮换
        - picks 用完之后：在曲库里按顺序循环
        """
        team_index = index % 2
        song: Optional[Song] = None

        if self.score_mode:
            tracks = self.score_tracks()
            if tracks:
                song, chosen_team = tracks[index % len(tracks)]
                team_index = chosen_team
        elif 0 <= index < len(self.picks):
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
        self.round_records.append(self._snapshot_round())
        self.save_results()

        if self.score_mode:
            # 计分赛：把 [[picks]] 全打一遍才结束，胜者由总分决定
            if len(self.round_records) >= (self.total_tracks() or 0):
                self.is_finished = True
                totals = self.team_totals()
                best = max(range(len(totals)), key=lambda i: totals[i]) if totals else 0
                if best < len(self.teams):
                    self.winner = self.teams[best]
            return

        for index, score in enumerate(self.scores):
            if score >= self.rounds_to_win:
                if index < len(self.teams):
                    self.winner = self.teams[index]
                self.is_finished = True
                break

    def _snapshot_round(self) -> Dict:
        """把这一局的成绩拍个快照（下一局开始选手状态就会被重置）。"""
        chosen = self.selected_songs[-1]["song"] if self.selected_songs else None
        return {
            'song_id': chosen.id if chosen else "",
            'song_title': chosen.title if chosen else "",
            'teams': [
                {
                    'name': team.name,
                    'total': team.total_score,
                    'players': [(player.name, player.std_score) for player in team.players],
                }
                for team in self.teams
            ],
        }

    # ------------------------------------------------------------------
    # 计分赛的统计
    # ------------------------------------------------------------------
    def team_totals(self) -> List[float]:
        """每支队在所有曲目上的总分。"""
        totals = [0.0] * max(2, len(self.teams))
        for record in self.round_records:
            for index, team in enumerate(record['teams']):
                totals[index] += team['total']
        return totals

    def team_ranks(self) -> List[int]:
        """每支队的名次（1 起）：按总分从高到低，同分并列取较小名次。"""
        totals = self.team_totals()
        order = sorted(range(len(totals)), key=lambda i: -totals[i])
        ranks = [1] * len(totals)
        for position, index in enumerate(order):
            if position == 0:
                ranks[index] = 1
            else:
                previous = order[position - 1]
                ranks[index] = ranks[previous] if totals[index] == totals[previous] \
                    else position + 1
        return ranks

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
        """清空大比分、胜者、轮次与逐局成绩快照（不动 results 缓存）。"""
        self.scores = [0] * max(2, len(self.teams))
        self.selected_songs = []
        self.round_records = []
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

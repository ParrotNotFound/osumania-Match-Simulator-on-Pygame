# src/core/game.py
"""主游戏循环：状态机 + 全部渲染。

状态流转：
    MENU（菜单，停 countdown_menu 毫秒）
      -> SONG_SELECT（选曲界面，停 countdown_song_select 毫秒）
      -> PLAYING（对局：先空等 lead_in 毫秒，音乐响起后开始判定）
      -> 分出这一局胜负 -> MENU，或比赛结束 -> FINISHED
"""
from __future__ import annotations

import math
from typing import Dict, Optional

import pygame

from ..entities.player import Player
from ..entities.song import Song
from ..entities.team import Team
from ..utils.config import DEFAULT_POOL_COLOR, ConfigError, GameConfig, load_config
from ..utils.file_loader import clear_results
from .judge import JudgeSystem, JudgementConfig
from .match import Match

FONT_SIZES = tuple(range(20, 80, 5))


class OsuGame:
    def __init__(self, config: GameConfig):
        self.config = config
        self.settings = config.game

        pygame.init()
        pygame.font.init()

        # 没有声卡也不该直接崩，静音继续跑
        try:
            pygame.mixer.init()
            self.audio_ok = True
        except pygame.error as error:
            print(f"警告：音频初始化失败（{error}），本次运行将静音")
            self.audio_ok = False

        self.screen = pygame.display.set_mode(
            (self.settings.screen_width, self.settings.screen_height)
        )
        pygame.display.set_caption("Osu! 模拟对战")

        self.clock = pygame.time.Clock()
        self.fps = self.settings.fps
        self.running = False
        self.max_render_dist = self.settings.max_render_dist
        self.pool_colors = config.pool_colors
        # 每首歌开始时按这份设置重新结算选手能力值（可在对局中热更新）
        self.player_settings = config.players

        # 游戏组件
        self.judge_system = JudgeSystem(JudgementConfig.from_settings(config.judge))
        self.match = Match(
            name=config.match.name,
            rounds_to_win=config.match.rounds_to_win,
            results_file=config.resolve(config.match.results_file),
            picks=config.picks,
            judge_system=self.judge_system,
        )
        self.current_song: Optional[Song] = None
        # 本局待发的音符（Song.notes 是谱面母本，不能被消耗，否则同一首歌第二次打就没音符了）
        self.playlist: list = []

        # 游戏状态
        self.game_state = "MENU"  # MENU, SONG_SELECT, PLAYING, FINISHED
        self.state_entered_at = 0
        self.current_time = 0     # 歌曲时间轴（负数代表还在 lead_in）
        self.song_start_time = 0
        self.last_frame_tick = 0
        self.music_started = False

        # 加载字体与资源
        self.fonts: Dict[int, pygame.font.Font] = {}
        self._load_fonts()
        self._load_resources()
        self._restore_or_reset_results()

        self.state_entered_at = pygame.time.get_ticks()
        if self.match.is_finished:
            self._set_state("FINISHED")

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _load_fonts(self, font: Optional[str] = None) -> None:
        for size in FONT_SIZES:
            self.fonts[size] = pygame.font.Font(font, size)

    def _load_resources(self) -> None:
        """按配置加载曲库与队伍（玩家、选曲、曲目全部来自 config.toml）"""
        for song_config in self.config.songs:
            song = Song(song_config, self.config.root)
            if not song.load_beatmap():
                print(f"警告：曲目 {song.id} 没有可用谱面，这一局不会有音符")
            elif not song.notes:
                print(f"警告：曲目 {song.id} 的谱面里没有音符")
            elif song.key_count != 4:
                print(f"警告：曲目 {song.id} 是 {song.key_count}K 谱面，本模拟器只按 4K 处理")
            self.match.add_song(song)

        for index, team_config in enumerate(self.config.teams):
            self.match.add_team(Team(team_config, team_id=index, judge_system=self.judge_system))

    def _restore_or_reset_results(self) -> None:
        """根据 [match] resume 决定是接着上次的比赛，还是重开一局。"""
        if self.config.match.resume:
            self.match.get_match_progress(True)
            if self.match.is_finished:
                print("读到的比赛记录已经分出胜负，直接进入结算画面"
                      "（想重新开始比赛，把 config.toml 里 [match] resume 改成 false）")
        else:
            clear_results(self.match.results_file)

    def run(self) -> None:
        """主游戏循环"""
        self.running = True
        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                else:
                    self._handle_event(event)

            self._update()
            self._render()
            self.clock.tick(self.fps)

        pygame.quit()

    def _handle_event(self, event) -> None:
        """处理输入事件。

        本模拟器是自动对局，不需要玩家操作；这里只保留退出快捷键。
        """
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.running = False

    # ------------------------------------------------------------------
    # 状态切换
    # ------------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        self.game_state = state
        self.state_entered_at = pygame.time.get_ticks()

    def _state_elapsed(self) -> int:
        return pygame.time.get_ticks() - self.state_entered_at

    def _update(self) -> None:
        """更新游戏逻辑"""
        if self.game_state == "MENU":
            if self._state_elapsed() > self.settings.countdown_menu:
                self._start_song_select()
        elif self.game_state == "SONG_SELECT":
            if self._state_elapsed() > self.settings.countdown_song_select:
                self._start_playing()
        elif self.game_state == "PLAYING":
            self._update_playing()

    def _start_song_select(self) -> None:
        """选曲：选出这一轮要打的歌，并播放它的试听片段"""
        song = self._select_song()
        if song is None:
            print("错误：没有可用曲目，无法开始比赛")
            self.running = False
            return
        self.current_song = song
        self.playlist = list(song.notes)
        self._play_select_audio(song)
        self._set_state("SONG_SELECT")

    def _select_song(self) -> Optional[Song]:
        """按 config.toml 的 [[picks]] 选出这一轮的曲目。"""
        index = self.match.next_round_index()
        song, team_index = self.match.pick_for_round(index)
        if song is None:
            return None
        self.match.select_song(song, team_index)
        self._prepare_players(index, song)
        return song

    def _prepare_players(self, round_index: int, song: Song) -> None:
        """每首歌开始时：重新读取队员名单、重新生成本局能力值（含随机手感），并打印到控制台。"""
        if self.player_settings.reread_roster:
            self._reload_roster()

        self.match.prepare_round(self.player_settings.form_range)

        # 赛点：已经有队伍站在"再赢一局就赢下整场比赛"的位置
        match_point = bool(self.match.scores) and max(self.match.scores) >= self.match.rounds_to_win - 1
        for team in self.match.teams:
            for player in team.players:
                player.match_point = match_point
                player.update_maxscore(len(song.notes))

        self._print_abilities(round_index, song)

    def _reload_roster(self) -> None:
        """重新读取 config.toml 里的队员名单（只取 [[teams]] 与 [players]）。

        配置改坏了不会影响正在进行的比赛：读失败就继续用原来的名单。
        """
        try:
            fresh = load_config(self.config.path)
        except ConfigError as error:
            print(f"警告：重新读取队员名单失败，本局继续使用原名单（{error}）")
            return

        before = [(team.name, tuple(p.name for p in team.players)) for team in self.match.teams]
        teams = [Team(team_config, team_id=index, judge_system=self.judge_system)
                 for index, team_config in enumerate(fresh.teams)]
        after = [(team.name, tuple(p.name for p in team.players)) for team in teams]

        self.match.replace_teams(teams)
        # [players] 里的手感设置也跟着热更新
        self.player_settings = fresh.players
        if before and before != after:
            print("提示：检测到 config.toml 的队员名单有变化，已从本局开始生效")

    def _print_abilities(self, round_index: int, song: Song) -> None:
        """把本局每名队员的能力值打到控制台。"""
        scores = self.match.scores or [0, 0]
        print(f"\n===== 第 {round_index + 1} 局 · {song.id} {song.title} · 本局选手能力值 =====")
        if max(scores) >= self.match.rounds_to_win - 1:
            print(f"  ★ 赛点（大比分 {scores[0]}:{scores[1]}，心态差的选手会被压力影响）")
        for team in self.match.teams:
            print(f"  [{team.name}]")
            for player in team.players:
                print(f"    {player.name:<14} {player.describe_abilities()}")
        print(f"  （手感基准 ±{self.player_settings.form_range}，稳定性越高实际波动越小；"
              f"括号内为本局手感偏移）")

    def _start_playing(self) -> None:
        """进入对局：先留 lead_in 毫秒的准备时间，再开始放歌。

        注意：音频必须在这里（而不是歌曲时间走到 0 的那一帧）载入。
        如果淡出还没结束就调用 music.load()，SDL_mixer 会一直阻塞到淡出结束，
        那一帧会卡住好几秒，开头几秒的音符会被所有人一起漏掉。
        """
        song = self.current_song
        self._stop_music()
        if song is not None:
            self._load_song_audio(song)

        self.song_start_time = pygame.time.get_ticks() + self.settings.lead_in
        self.current_time = -self.settings.lead_in
        self.last_frame_tick = pygame.time.get_ticks()
        self.music_started = False
        self._set_state("PLAYING")

    # ------------------------------------------------------------------
    # 对局进行中
    # ------------------------------------------------------------------
    def _update_playing(self) -> None:
        song = self.current_song
        if song is None:
            return

        now = pygame.time.get_ticks()
        frame_gap = now - self.last_frame_tick
        self.last_frame_tick = now
        self.current_time = now - self.song_start_time
        if frame_gap > 500:
            # 卡顿会让这一段时间里的音符直接过期，说一声方便排查
            print(f"警告：对局中卡顿了 {frame_gap}ms，可能有音符被跳过")

        if not self.music_started:
            # lead_in 期间不判定，音符也不会提前滚出来
            if self.current_time >= 0:
                self.music_started = True
                self._play_song_audio()
            return

        self._update_notes()
        self._update_players()

        if self._song_finished():
            self._finish_song()

    def _update_notes(self) -> None:
        """把进入视野的音符发给每一名玩家（同一个 Note 对象共享给所有人）。"""
        deadline = self.current_time + self.max_render_dist
        while self.playlist and self.playlist[0].time < deadline:
            note = self.playlist.pop(0)
            for team in self.match.teams:
                for player in team.players:
                    player.active_notes.append(note)

    def _update_players(self) -> None:
        for team in self.match.teams:
            for player in team.players:
                player.play(self.current_time)

    def _song_finished(self) -> bool:
        """歌曲是否已经打完：谱面发完 + 没人手里还有音符 + 音乐放完。"""
        if self.current_song is None:
            return True
        if self.playlist:
            return False
        if any(player.active_notes for team in self.match.teams for player in team.players):
            return False
        if self._music_busy():
            return False
        return True

    def _finish_song(self) -> None:
        """结算本局，记录大比分，然后回到菜单或进入结算画面。"""
        totals = [team.total_score for team in self.match.teams]
        winning_team = max(range(len(totals)), key=lambda index: totals[index]) if totals else 0
        self.match.record_round_result(winning_team)
        self._stop_music()
        # 玩家状态与能力值留到下一首开始时由 _prepare_players 统一重置
        if self.match.is_finished:
            self._set_state("FINISHED")
        else:
            self._set_state("MENU")

    # ------------------------------------------------------------------
    # 音频（没声卡时全部退化成空操作）
    # ------------------------------------------------------------------
    def _play_select_audio(self, song: Song) -> None:
        if not self.audio_ok:
            return
        try:
            pygame.mixer.music.load(song.load_audio())
            pygame.mixer.music.play()
        except Exception as error:
            print(f"警告：选曲音频播放失败（{error}）")

    def _play_song_audio(self) -> None:
        """歌曲时间走到 0 时把已经载入的音频播出去（这里不要再 load，load 可能阻塞）。"""
        if not self.audio_ok:
            return
        try:
            pygame.mixer.music.play()
        except pygame.error as error:
            print(f"警告：曲目音频播放失败（{error}），本局静音进行")

    def _load_song_audio(self, song: Song) -> None:
        """提前载入本局音频，把潜在的阻塞挪到歌曲时间开始之前。"""
        if not self.audio_ok:
            return
        try:
            pygame.mixer.music.load(song.load_audio())
        except Exception as error:
            print(f"警告：曲目音频载入失败（{error}），本局静音进行")

    def _stop_music(self) -> None:
        if not self.audio_ok:
            return
        try:
            pygame.mixer.music.stop()
        except pygame.error:
            pass

    def _music_busy(self) -> bool:
        if not self.audio_ok:
            return False
        try:
            return pygame.mixer.music.get_busy()
        except pygame.error:
            return False

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def _render(self) -> None:
        """渲染游戏画面"""
        self.screen.fill((0, 0, 0))  # 黑色背景

        if self.game_state == "MENU":
            self._render_menu()
        elif self.game_state == "SONG_SELECT":
            self._render_song_select()
        elif self.game_state == "PLAYING":
            self._render_gameplay()
        elif self.game_state == "FINISHED":
            self._render_ending()

        if self.settings.debug:
            self._render_debug_info()
        pygame.display.flip()

    def _render_debug_info(self) -> None:
        text = f"{self.current_time}ms  state={self.game_state}  fps={self.clock.get_fps():.0f}"
        text_image = self.fonts[20].render(text, True, (120, 120, 120))
        self.screen.blit(text_image, (4, 4))

    def _render_ending(self) -> None:
        """显示比赛结果"""
        self._render_team_big_points()
        bigfont = pygame.font.Font(None, 70)
        who_wins = f"{self.match.winner.name} wins!" if self.match.winner else "Match over"
        text_image = bigfont.render(who_wins, True, (255, 255, 255))
        t_width, _ = bigfont.size(who_wins)
        self.screen.blit(text_image, (640 - t_width / 2, 360))

    def _render_team_big_points(self) -> None:
        """显示大比分"""
        teams = self.match.teams
        if not teams:
            return

        text_image = self.fonts[40].render(str(teams[0].name), True, teams[0].color)
        self.screen.blit(text_image, (0, 20))

        if len(teams) > 1:
            t_width, _ = self.fonts[40].size(str(teams[1].name))
            text_image = self.fonts[40].render(str(teams[1].name), True, teams[1].color)
            self.screen.blit(text_image, (1270 - t_width, 20))

        # 队名后面是小分（赢一局点一个格子）
        progress = self.match.get_match_progress(False)
        windata = progress["scores"]
        wins = progress["rounds_to_win"]
        colors = [(100, 100, 100), (255, 215, 0)]
        for i in range(wins):
            pygame.draw.rect(self.screen, colors[windata[0] > i],
                             [640 - 0.5 * 600 + i * 40 - 25, 20, 25, 25])
            if len(windata) > 1:
                pygame.draw.rect(self.screen, colors[windata[1] > i],
                                 [640 + 0.5 * 600 - i * 40, 20, 25, 25])

    def _render_menu(self) -> None:
        """初始页面（和选曲页面共用画面）"""
        self._render_song_select()

    def _render_song_select(self) -> None:
        """选歌页面"""
        self._render_team_big_points()

        songs = self.match.song_pool
        if not songs:
            return

        s_width = 500
        s_height = 600 / len(songs)
        for index, chosen in enumerate(self.match.selected_songs):
            team_index = chosen["team"]
            if team_index >= len(self.match.teams):
                continue
            color = self.match.teams[team_index].color
            pygame.draw.rect(self.screen, color,
                             [640 - s_width / 2, 70 + s_height * index, s_width, s_height - 6])

        for index, song in enumerate(songs):
            song_key = song.id
            pygame.draw.rect(self.screen, (100, 100, 100),
                             [640 - s_width / 2 + 5, 70 + s_height * index + 5, s_width - 10, s_height - 16])
            key_color = self.pool_colors.get(song_key[:2].upper(), DEFAULT_POOL_COLOR)
            text_image = self.fonts[40].render(song_key, True, key_color)
            self.screen.blit(text_image, (640 - s_width / 2 + 10, 80 + index * s_height))
            text_image = self.fonts[40].render(song.title, True, (255, 255, 255))
            self.screen.blit(text_image, (640 - s_width / 2 + 80, 80 + index * s_height))

    def _render_gameplay(self) -> None:
        """渲染游戏进行中的画面"""
        teams = self.match.teams
        if len(teams) < 2 or self.current_song is None:
            return

        for index, team in enumerate(teams):
            # 队伍总分
            score_text = self.fonts[50].render(f"{team.name}: {team.total_score:.0f}", True, team.color)
            self.screen.blit(score_text, (50 + index * 900, 20))

            # 玩家信息
            for player_index, player in enumerate(team.players):
                xpos = [150, 0, 300]
                self._render_player(player, index * 700 + xpos[player_index],
                                    360 - 290 * min(player_index, 1))

        # 以下这段是显示底下那一坨分数的，老代码石山搬过来的，这段比较复杂不好动
        score_a = teams[0].total_score
        score_b = teams[1].total_score
        team_color = [teams[0].color, teams[1].color]
        teamgap = math.pow(abs(score_a - score_b) * 70, 0.35) if score_a != score_b else 0

        diff_text = str(int(abs(score_a - score_b)))
        t_width, _ = self.fonts[25].size(diff_text)
        if score_a > score_b:
            self.screen.blit(self.fonts[25].render(diff_text, True, (255, 255, 255)), (640 - t_width, 635))
            t_width, _ = self.fonts[60].size(str(int(score_a)))
            self.screen.blit(self.fonts[60].render(str(int(score_a)), True, team_color[0]),
                             (640 - t_width - teamgap, 660))
            self.screen.blit(self.fonts[40].render(str(int(score_b)), True, team_color[1]), (640, 660))
            pygame.draw.rect(self.screen, team_color[0], [640 - teamgap, 650, teamgap, 10])
        else:
            self.screen.blit(self.fonts[25].render(diff_text, True, (255, 255, 255)), (640, 635))
            t_width, _ = self.fonts[40].size(str(int(score_a)))
            self.screen.blit(self.fonts[60].render(str(int(score_b)), True, team_color[1]),
                             (640 + teamgap, 660))
            self.screen.blit(self.fonts[40].render(str(int(score_a)), True, team_color[0]),
                             (640 - t_width, 660))
            pygame.draw.rect(self.screen, team_color[1], [640, 650, teamgap, 10])

        # 最后再画大比分和歌名
        song_name = self.current_song.title
        text_image = self.fonts[40].render(song_name, True, (255, 255, 255))
        t_width, _ = self.fonts[40].size(song_name)
        self.screen.blit(text_image, (640 - t_width / 2, 600))
        self._render_team_big_points()

    def _render_player(self, player: Player, x: int, y: int) -> None:
        """绘制单个玩家信息"""
        # 背景板
        pygame.draw.rect(self.screen, (0, 0, 0), [x, y, 240, 290])

        # 音符
        for note in player.active_notes:
            rect_x = x + 50 + int(note.x) * 0.3
            rect_y = y + 260 - (int(note.time) - self.current_time) * 0.6
            pygame.draw.rect(self.screen, (255, 255, 255), [rect_x, rect_y, 35, 10])

        # 挡板（只遮住自己这一列，遮太宽会把旁边玩家的画面涂黑）
        pygame.draw.rect(self.screen, (0, 0, 0), [x, y - 460, 240, 480])
        pygame.draw.rect(self.screen, (50, 50, 50), [x + 50 + 19.2, y + 260, 150, 20])

        # 分数、准确率、连击
        color = self.match.teams[player.team_index].color
        score_text = self.fonts[35].render(f"{player.std_score:.0f}", True, (200, 200, 200))
        acc_text = self.fonts[35].render(f"{player.accuracy:.2f}%", True, (200, 200, 200))
        combo_text = self.fonts[40].render(str(player.combo), True, color)

        self.screen.blit(combo_text, (x + 140 - int(math.log10(max(1, player.combo))) * 10, y + 120))
        self.screen.blit(acc_text, (x + 180, y))
        self.screen.blit(score_text, (x + 140 - int(math.log10(max(1, player.std_score))) * 10, y))

        # 判定文字（过一会儿自动消失）
        last_time = player.last_judge_time.get(player.last_judgement, -114514)
        judge_text = "" if last_time < self.current_time - (400 if player.last_judgement == 'great' else 1000) \
            else player.last_judgement
        judge_color = {
            '': (0, 0, 0), 'perfect_g': (255, 193, 37), 'perfect': (255, 193, 37),
            'great': (127, 255, 0), 'good': (135, 206, 250), 'bad': (100, 100, 100),
            'miss': (255, 0, 0),
        }
        t_width, _ = self.fonts[30].size(judge_text.upper())
        text_image = self.fonts[30].render(judge_text.upper(), True,
                                          judge_color.get(judge_text, (255, 255, 255)))
        self.screen.blit(text_image, (x + 140 - t_width / 2, y + 150))

        # 玩家名（刚 miss 过会闪红）
        last_miss = max(0, 255 + 0.1 * (player.last_judge_time.get('miss', -114514) - self.current_time))
        name_text = self.fonts[30].render(player.name, True, (255, 255 - last_miss, 255 - last_miss))
        self.screen.blit(name_text, (x + 50 + 19.2, y + 260))

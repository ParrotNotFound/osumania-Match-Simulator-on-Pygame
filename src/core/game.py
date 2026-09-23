# src/core/game.py
"""主游戏循环：状态机 + 全部渲染。

状态流转（选曲页会经历两个阶段，全程都有音乐）：
    MENU（启动后停 start_delay 毫秒，展示曲库；放 prelude_song）
      -> SONG_SELECT 第一阶段（song_select_delay：**不点出下一首**，接着放刚打完的那首）
      -> SONG_SELECT 第二阶段（match_start_delay：点出下一首并试听，仍停在选曲页）
      -> PLAYING（切到游玩页面，空等 preroll_delay 毫秒后音频从头播放、谱面开始滚）
      -> RESULTS（本局成绩展示 results_delay 毫秒）
      -> 回到 SONG_SELECT，或比赛已分胜负 -> FINISHED
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import pygame

from ..entities.player import Player
from ..entities.song import Song
from ..entities.team import Team
from ..utils.config import DEFAULT_POOL_COLOR, JUDGEMENTS, ConfigError, GameConfig, load_config
from ..utils.excel import append_score_match, match_result_rows, score_match_rows, write_sheet
from .judge import JudgeSystem, JudgementConfig
from .match import Match

# 无 UI 模式的推进步长（毫秒）。判定是模型自己算的，和帧步长无关，
# 所以可以放得比 60fps 粗很多，纯粹为了跑得快。
HEADLESS_STEP_MS = 100.0
# 无 UI 模式的兜底：虚拟时间最多推进这么多秒还没结束就强制退出
HEADLESS_MAX_SECONDS = 4 * 3600.0

# 新点出的曲目，它在选曲页上的高亮闪烁多久（毫秒）；以前选过的那些一直常亮
REVEAL_FLASH_MS = 1200
# 闪烁的半个周期：亮 blink_on_ms、灭 blink_off_ms，交替
REVEAL_FLASH_ON_MS = 170
REVEAL_FLASH_OFF_MS = 110

FONT_SIZES = tuple(range(20, 80, 5))

# prelude_song 能填的音频后缀（填文件路径时按这个认；填文件夹时按这个去找）
AUDIO_EXTENSIONS: Tuple[str, ...] = ('.mp3', '.ogg', '.wav', '.flac', '.opus')


@dataclass
class PreludeAudio:
    """start_delay 期间放的那段音乐。

    `prelude_song` 可以直接指向**目录里的任意歌曲**（不用进 [[songs]]）：
    给音频文件就用它；给歌曲文件夹就先按 .osu 里写的 AudioFilename 找，
    再退回到文件夹里第一个认识的音频文件。留空则退回"这一场的第一首"。
    """
    audio_path: str
    label: str

# pygame 默认字体没有中文字形（会渲染成方块），所以优先找一个系统里带中文的字体
CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)


def _find_cjk_font() -> Optional[str]:
    """找一个带中文字形的字体文件，找不到就返回 None（退回 pygame 默认字体）。"""
    for path in CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None

# 第四轨右侧那列判定计数的小字号
JUDGEMENT_COUNT_FONT_SIZE = 18
# 判定计数的颜色，顺序同 JUDGEMENTS：完美+ 黄 / 完美 橙 / 很好 绿 / 好 蓝 / 差 淡灰 / 漏 红
JUDGEMENT_COUNT_COLORS: Dict[str, Tuple[int, int, int]] = {
    'perfect_g': (255, 255, 0),
    'perfect': (255, 165, 0),
    'great': (0, 255, 0),
    'good': (135, 206, 250),
    'bad': (170, 170, 170),
    'miss': (255, 0, 0),
}


class OsuGame:
    def __init__(self, config: GameConfig):
        self.config = config
        self.settings = config.game
        self.headless = bool(self.settings.headless)

        # 无 UI 模式必须在 pygame.init() 之前换掉 SDL 驱动，否则窗口就已经弹出来了
        if self.headless:
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

        pygame.init()
        pygame.font.init()

        # 没有声卡也不该直接崩，静音继续跑
        try:
            pygame.mixer.init()
            self.audio_ok = True
        except pygame.error as error:
            print(f"警告：音频初始化失败（{error}），本次运行将静音")
            self.audio_ok = False

        if self.headless:
            # 无 UI 模式不播音频：不然 music.get_busy() 会按真实时间走，
            # 虚拟时钟推得再快，这一局也永远结束不了
            self.audio_ok = False

        self.screen = pygame.display.set_mode(
            (self.settings.screen_width, self.settings.screen_height)
        )
        pygame.display.set_caption("Osu! 模拟对战")

        self.clock = pygame.time.Clock()
        self.fps = self.settings.fps
        self.running = False
        self.virtual_now = 0          # 无 UI 模式的虚拟时钟（毫秒）
        self.max_render_dist = self.settings.max_render_dist
        self.pool_colors = config.pool_colors
        # 每首歌开始时按这份设置重新结算选手能力值（可在对局中热更新）
        self.player_settings = config.players

        # 游戏组件
        self.judge_system = JudgeSystem(JudgementConfig.from_settings(config.judge))
        self.match = Match(
            name=config.match.name,
            rounds_to_win=config.match.rounds_to_win,
            results=config.match.results,
            config_path=config.path,
            picks=config.picks,
            judge_system=self.judge_system,
            score_mode=config.match.score_mode,
        )
        # 计分赛：成绩表写到哪、是否已经写过
        self.excel_path = config.resolve(config.match.excel_file)
        self.excel_written: Optional[str] = None
        # 常规赛果表（每局双方得分）：写到哪、是否已经写过
        self.results_excel_path = config.resolve(config.match.results_excel)
        self.results_excel_written: Optional[str] = None
        self.current_song: Optional[Song] = None
        # 本局待发的音符（Song.notes 是谱面母本，不能被消耗，否则同一首歌第二次打就没音符了）
        self.playlist: list = []

        # 游戏状态
        self.game_state = "MENU"  # MENU, SONG_SELECT, PLAYING, RESULTS, FINISHED
        self.state_entered_at = 0
        self.current_time = 0     # 歌曲时间轴（负数代表还在 preroll_delay 的准备时间里）
        self.song_start_time = 0
        self.last_frame_tick = 0
        self.music_started = False
        # 选曲页的两个阶段：先"还没点出下一首"（放刚打完的那首），再"点出下一首并试听"
        self.song_revealed = False
        self._revealed_song: Optional[Song] = None
        # 刚打完的那首（没有下一首可放时用它当背景音乐）
        self._last_played_song: Optional[Song] = None
        # start_delay 期间放的那段音乐（在 _start_prelude_music 里解析出来）
        self._prelude: Optional[PreludeAudio] = None
        # 本场是否已经打过至少一局（决定第一阶段高亮"上一轮那首"还是什么都不高亮）
        self.has_played_round = False
        # 刚点出的那首在选曲页上闪到什么时候（毫秒时间戳，见 REVEAL_FLASH_MS）
        self._reveal_flash_until = 0
        # 切到游玩页面后、谱面开始滚之前的那段固定等待是否走完了
        self.preroll_done = False
        self.round_winner = None  # 刚打完那一局的胜者，用于成绩展示

        # 加载字体与资源
        self.fonts: Dict[int, pygame.font.Font] = {}
        self._load_fonts()
        self._load_resources()
        self._restore_or_reset_results()

        self.state_entered_at = self._now()
        if self.match.is_finished:
            self._set_state("FINISHED")

    def _now(self) -> int:
        """当前时间（毫秒）。

        无 UI 模式用虚拟时钟（每轮循环往前推一帧），这样比赛能尽快跑完；
        其余时候就是真实时钟。
        """
        return self.virtual_now if self.headless else pygame.time.get_ticks()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _load_fonts(self, font: Optional[str] = None) -> None:
        """准备两套字体。

        `self.fonts` 用 pygame 默认字体渲染数字/英文 —— 原来的排版全是按它的宽度调的；
        `self.cjk_fonts` 是带中文字形的字体，只在文本里出现非 ASCII 时才用（见 `_font`），
        这样加中文不会把已有排版挤歪。
        """
        cjk_path = font or _find_cjk_font()
        self.cjk_fonts: Optional[Dict[int, pygame.font.Font]] = {} if cjk_path else None
        for size in FONT_SIZES:
            self.fonts[size] = pygame.font.Font(font, size)
            if cjk_path:
                self.cjk_fonts[size] = pygame.font.Font(cjk_path, size)
        # 第四轨右侧那列判定计数用的小字（纯数字，用默认字体）
        self.count_font = pygame.font.Font(font, JUDGEMENT_COUNT_FONT_SIZE)

    def _font(self, size: int, text: object) -> pygame.font.Font:
        """按文本内容选字体：含非 ASCII 字符用中文字体，否则用默认字体。"""
        if self.cjk_fonts is None:
            return self.fonts[size]
        return self.cjk_fonts[size] if any(ord(ch) > 127 for ch in str(text)) else self.fonts[size]

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
        """启动时按 [match] resume 决定：接着缓存里的比赛，还是重开一场。

        赛果缓存就存在 config.toml 的 [match] results 里，每局打完都会写回去。
        """
        cached = list(self.match.results)

        if self.match.score_mode:
            # 计分赛的结算榜和 Excel 需要完整的逐曲成绩，没法从缓存里接着算，
            # 所以这个模式一律重新开一场。
            if cached:
                print(f"提示：计分赛模式需要完整的逐曲成绩，已清空缓存里的 {len(cached)} 局赛果，"
                      f"重新开赛")
                self.match.clear_results()
            print(f"提示：计分赛模式 —— 配置里的 [[picks]] 共 {len(self.match.score_tracks())} 首，"
                  f"按顺序各打一遍，最后按总分排名并写入 {self.excel_path}")
            return

        if self.config.match.resume and cached:
            self.match.apply_cached_results()

        if self.match.is_finished:
            # 缓存里的比赛已经分出胜负，没法接着打，直接开新的一场
            print(f"提示：缓存里的比赛已经打完（{self.match.winner.name} 以 "
                  f"{self.match.scores[0]}:{self.match.scores[1]} 获胜），这次重新开一场；"
                  f"想看这份成绩可以翻 config.toml 的 [match] results")
            self.match.clear_results()
            self.match.reset_progress()
            return

        if self.config.match.resume:
            if cached:
                print(f"提示：接着缓存里的比赛继续 —— 已打 {len(cached)} 局，大比分 "
                      f"{self.match.scores[0]}:{self.match.scores[1]}")
        elif cached:
            print(f"提示：[match] resume = false，已清空缓存里的 {len(cached)} 局赛果，重新开赛")
            self.match.clear_results()

    def run(self) -> None:
        """主游戏循环"""
        if self.headless:
            self._run_headless()
            return

        # 开局（MENU 阶段）就把 prelude 音乐放起来：整段等待里都有音乐
        self._start_prelude_music()
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

    def _run_headless(self) -> None:
        """无 UI：不弹窗、不渲染、不实时，用虚拟时钟把整场比赛推完。

        判定逻辑一行没改 —— 每个音符的落点误差一直是模型自己算的，
        和"这一帧隔了多久"无关，所以把帧步长放大不会改变任何结果，
        只是不用再等真实时间流逝。结果打到控制台，赛果表照常写到 Excel。
        """
        step = float(HEADLESS_STEP_MS)
        print(f"提示：无 UI 模式，每轮按 {step:.0f}ms 的步长推进（不弹窗、不渲染、不等真实时间）")
        self.running = True
        frames = 0
        limit = int(HEADLESS_MAX_SECONDS * 1000 / step)
        while self.running:
            self.virtual_now += step
            self._update()
            frames += 1
            if self.game_state == "FINISHED":
                break
            if frames > limit:
                print(f"警告：无 UI 模式推进了 {frames} 帧仍未结束（超过 "
                      f"{HEADLESS_MAX_SECONDS}s 的比赛时长），已强制退出")
                break
        print(f"（无 UI 模式共推进 {frames} 帧，虚拟时间 {self.virtual_now / 1000:.0f} 秒）")
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
        self.state_entered_at = self._now()

    def _state_elapsed(self) -> int:
        return self._now() - self.state_entered_at

    def _update(self) -> None:
        """更新游戏逻辑

        一个完整循环（选曲页会经历两个阶段）：
            MENU(start_delay，放 prelude 音乐，选曲页上不点出任何歌)
              -> SONG_SELECT(song_select_delay，仍不点出下一首，继续放刚打完/开场那首)
              -> SONG_SELECT(match_start_delay，点出下一首并试听它)
              -> PLAYING(preroll_delay 的固定等待后，音频从头播放、谱面开始滚)
              -> RESULTS(results_delay) -> 回到 SONG_SELECT
            某队达到胜场后 RESULTS 结束即进入 FINISHED。
        """
        if self.game_state == "MENU":
            if self._state_elapsed() > self.settings.start_delay:
                self._enter_song_select()
        elif self.game_state == "SONG_SELECT":
            if not self.song_revealed:
                # 第一阶段：不点出下一首，时间到了才宣布（并开始试听）
                if self._state_elapsed() > self.settings.song_select_delay:
                    self._reveal_song()
            elif self._state_elapsed() > (self.settings.song_select_delay
                                          + self.settings.match_start_delay):
                # 第二阶段结束：切到游玩页面（谱面还要再等 preroll_delay 才开始滚）
                self._start_playing()
        elif self.game_state == "PLAYING":
            self._update_playing()
        elif self.game_state == "RESULTS":
            if self._state_elapsed() > self.settings.results_delay:
                if self.match.is_finished:
                    self._set_state("FINISHED")
                else:
                    # 必须走 _enter_song_select：它会重新结算选手
                    # （换名单、重掷能力值、清空上一局状态）。直接切 SONG_SELECT 会漏掉这些，
                    # 导致第二局拿上一局的旧谱面和旧状态重打。
                    self._enter_song_select()

    def _time_until_match_start(self) -> Optional[float]:
        """距离"比赛真正开始"还有多少毫秒；已经开打或没有下一场时返回 None。

        跨越多个阶段累计：等待阶段把后面几个阶段的时长一起算进去，
        所以从启动开始这个数字是一路连续倒数到 0 的（preroll 那 1 秒是"开始之后"的，
        不算进倒计时）。
        """
        settings = self.settings
        wait = settings.song_select_delay + settings.match_start_delay
        if self.game_state == "MENU":
            remaining = settings.start_delay - self._state_elapsed() + wait
        elif self.game_state == "SONG_SELECT":
            # 两个阶段共用同一个 state，所以剩余时间是"总等待 - 已经过的时间"
            remaining = wait - self._state_elapsed()
        elif self.game_state == "RESULTS":
            if self.match.is_finished:
                return None
            remaining = settings.results_delay - self._state_elapsed() + wait
        elif self.game_state == "PLAYING":
            if self.music_started:
                return None
            remaining = 0.0
        else:
            return None
        return max(0.0, float(remaining))

    def _song_progress(self) -> float:
        """已经结算过的音符 / 总音符数（0~1）。

        每个音符对所有选手都只结算一次，所以取任意一名选手的判定计数之和就是完成数。
        """
        total = self.current_song.judgement_count if self.current_song else 0
        if total <= 0 or not self.match.teams or not self.match.teams[0].players:
            return 0.0
        done = sum(self.match.teams[0].players[0].judgement_counts.values())
        return min(1.0, done / total)

    def _enter_song_select(self) -> None:
        """进入选曲页的**第一阶段**：画面切过去，但还不点出下一首要打哪首。

        音乐继续放"刚刚打完的那首"（第一局则接着放 prelude_song）。
        """
        # 从 MENU 进来就是整场开局：prelude 已经在 run() 里放着了，这里不能再放一遍
        # （`music.play()` 是从头播，重放会听出来"莫名其妙重头开始"）。
        from_menu = self.game_state == "MENU"
        self.song_revealed = False
        self._revealed_song = None
        self._set_state("SONG_SELECT")
        if self._last_played_song is not None:
            # 接着放刚刚打完的那首（每局都会重新起一遍，这里就当作换片）
            self._play_select_audio(self._last_played_song)
        elif not from_menu:
            self._play_prelude_audio()
        else:
            pass

    def _reveal_song(self) -> None:
        """选曲页的**第二阶段**：点出这一轮要打哪首，并把它试听出来。

        还停留在选曲页 —— 直接开打要等 match_start_delay 结束（见 `_update`）。
        刚点出的这一首会在选曲页上闪几下（`REVEAL_FLASH_MS`），
        之前轮次选过的那些则一直常亮。
        """
        song = self._select_song()
        if song is None:
            print("错误：没有可用曲目，无法开始比赛")
            self.running = False
            return
        self._revealed_song = song
        self.song_revealed = True
        self._reveal_flash_until = self._now() + REVEAL_FLASH_MS
        self.playlist = list(song.notes)
        # 直接切换到这一首的试听（不用先 stop：load 会顶掉上一首）
        self._play_select_audio(song)

    def _prelude_song(self) -> Optional[Song]:
        """start_delay 期间要放的曲子（曲库里的那一首）：配置里指定优先，否则用这场的第 1 首。"""
        wanted = (self.settings.prelude_song or "").strip()
        if wanted:
            found = self.match.find_song(wanted)
            if found is not None:
                return found
        first, _team = self.match.pick_for_round(0)
        return first or (self.match.song_pool[0] if self.match.song_pool else None)

    def _resolve_prelude(self) -> Optional[PreludeAudio]:
        """把 [game] prelude_song 解析成一段可播放的音频。

        支持四种写法（都不需要进 [[songs]]）：
            1. 曲库里的 id（如 "RC1"）→ 用它自己的音频；
            2. 音频文件路径（相对 config.toml 解析，如 data/bgm/op.mp3）；
            3. 歌曲文件夹 → 先读里面的 .osu 里的 AudioFilename，再退回到第一个音频文件；
            4. 留空 → 退回"这一场的第一首"（按 [[picks]]）。
        解析不出来就打印中文提示并退回第 4 种，不打断比赛。
        """
        wanted = (self.settings.prelude_song or "").strip()
        if wanted:
            found = self.match.find_song(wanted)
            if found is not None:
                try:
                    return PreludeAudio(found.load_audio(), f"曲库 {found.id}")
                except FileNotFoundError as error:
                    print(f"警告：prelude_song = \"{wanted}\" 的音频找不到（{error}）")
            path = self.config.resolve(wanted)
            if os.path.isdir(path):
                resolved = self._find_audio_in_folder(path)
                if resolved is not None:
                    return PreludeAudio(resolved, f"文件夹 {os.path.basename(path) or path}")
                print(f"警告：prelude_song 指向的文件夹里没有音频：{path}")
            elif os.path.isfile(path):
                if path.lower().endswith(AUDIO_EXTENSIONS):
                    return PreludeAudio(path, os.path.basename(path))
                # 给的是 .osu 之类的文件：按它所在目录找音频
                resolved = self._find_audio_in_folder(os.path.dirname(path))
                if resolved is not None:
                    return PreludeAudio(resolved, os.path.basename(path))
                print(f"警告：prelude_song 指向的文件既不是音频，所在文件夹里也没有音频：{path}")
            else:
                print(f"警告：prelude_song = \"{wanted}\" 既不是曲库 id、也不是存在的"
                      f"文件/文件夹（解析为 {path}），改为放这场的第 1 首")

        song = self._prelude_song()
        if song is None:
            return None
        try:
            return PreludeAudio(song.load_audio(), f"曲库 {song.id}")
        except FileNotFoundError as error:
            print(f"警告：prelude 音频找不到（{error}），本次不播 prelude 音乐")
            return None

    @staticmethod
    def _find_audio_in_folder(folder: str) -> Optional[str]:
        """在文件夹里找音频：优先 .osu 里 AudioFilename 写的那个，其次按后缀扫。

        .osu 只扫一层目录，按文件名排序取第一个能读的 —— prelude 只是背景音乐，
        不值得为它引整套谱面解析。
        """
        if not os.path.isdir(folder):
            return None
        names: list = []
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return None
        for name in names:
            if not name.lower().endswith('.osu'):
                continue
            try:
                with open(os.path.join(folder, name), 'r',
                          encoding='utf-8-sig', errors='replace') as handle:
                    for line in handle:
                        if line.strip().startswith("AudioFilename:"):
                            audio = line.split(':', 1)[1].strip()
                            candidate = os.path.join(folder, audio)
                            if os.path.isfile(candidate):
                                return candidate
            except OSError:
                continue
        for extension in AUDIO_EXTENSIONS:
            for name in names:
                if name.lower().endswith(extension):
                    return os.path.join(folder, name)
        return None

    def _start_prelude_music(self) -> None:
        """开局（MENU 阶段）就把 prelude 音乐放起来。"""
        self._prelude = self._resolve_prelude()
        if self._prelude is None:
            return
        print(f"提示：prelude 音乐（{self._prelude.label}）")
        self._play_prelude_audio()

    def _play_prelude_audio(self) -> None:
        """播放已经解析好的 prelude 音频（没有就什么都不做）。"""
        if self._prelude is None:
            self._play_select_audio(self._last_played_song or self._prelude_song())
            return
        if not self.audio_ok:
            return
        try:
            pygame.mixer.music.load(self._prelude.audio_path)
            pygame.mixer.music.play()
        except Exception as error:
            print(f"警告：prelude 音乐播放失败（{error}）")

    def _select_song(self) -> Optional[Song]:
        """按 config.toml 的 [[picks]] 选出这一轮的曲目（并结算选手）。

        注意"选曲"只发生一次：`_reveal_song` 里调它，之后 `current_song` 就固定了。
        """
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

        # 判定窗口跟着这张谱的 OD 走（lazer 的 mania 窗口曲线）。
        # 判定系统是所有人共用的，改一次全体生效。
        if self.config.judge.follow_chart_od:
            self.judge_system.config.use_overall_difficulty(song.overall_difficulty)
            judge = self.judge_system.config
            print(f"  （判定窗口跟随谱面 OD {song.overall_difficulty:g}："
                  f"±{judge.perfect_g:g} / {judge.perfect:g} / {judge.great:g} / "
                  f"{judge.good:g} / {judge.bad:g} ms）")

        # 赛点：已经有队伍站在"再赢一局就赢下整场比赛"的位置。
        # 计分赛没有"再赢一局就结束"这回事（rounds_to_win 在那个模式下无效），
        # 所以一律不算赛点，免得全场都挂着赛点压力倍率。
        match_point = (not self.match.score_mode) and bool(self.match.scores) \
            and max(self.match.scores) >= self.match.rounds_to_win - 1
        for team in self.match.teams:
            for player in team.players:
                player.match_point = match_point
                player.update_maxscore(song.judgement_count)

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
        # 直接读选手身上的标记，不要在这里重算一遍：
        # 计分赛不算赛点，自己重算就会印出与真实机制不符的提示
        if any(player.match_point for team in self.match.teams for player in team.players):
            print(f"  ★ 赛点（大比分 {scores[0]}:{scores[1]}，心态差的选手会被压力影响）")
        for team in self.match.teams:
            print(f"  [{team.name}]")
            for player in team.players:
                print(f"    {player.name:<14} {player.describe_abilities()}")
        print(f"  （手感基准 ±{self.player_settings.form_range}，稳定性越高实际波动越小；"
              f"括号内为本局手感偏移）")

    def _start_playing(self) -> None:
        """切到游玩页面：谱面先不滚，等 preroll_delay 的固定等待。

        试听的那首音乐**继续放着**（进入 PLAYING 不会打断它）；等
        `preroll_delay` 走完，再把音频从头开始、同时让谱面滚动（见 `_update_playing`）。
        所以"宣布曲目 -> 试听 -> 开打"中间没有静音，只有 preroll 结束那一下从头开始。
        """
        self.current_song = self._revealed_song
        self.preroll_done = False
        self.has_played_round = True
        self.song_start_time = self._now()
        self.current_time = 0
        self.last_frame_tick = self._now()
        self.music_started = False
        self._set_state("PLAYING")

    # ------------------------------------------------------------------
    # 对局进行中
    # ------------------------------------------------------------------
    def _update_playing(self) -> None:
        song = self.current_song
        if song is None:
            return

        now = self._now()
        frame_gap = now - self.last_frame_tick
        self.last_frame_tick = now
        if not self.preroll_done:
            # 切到游玩页面后的固定等待：谱面停在原地、不判定，试听音乐继续放
            if self._state_elapsed() >= self.settings.preroll_delay:
                self.preroll_done = True
                self.song_start_time = now
                self.current_time = 0
                # 音频从头开始，和谱面时间轴对齐（试听与正式播放是两回事）
                self._stop_music()
                self._load_song_audio(song)
                self._play_song_audio()
                self.music_started = True
            return

        self.current_time = now - self.song_start_time
        if frame_gap > 500 and not self.headless:
            # 卡顿会让这一段时间里的音符直接过期，说一声方便排查
            # （无 UI 模式是故意大步长推进的，不算卡顿）
            print(f"警告：对局中卡顿了 {frame_gap}ms，可能有音符被跳过")

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
        if any(player.active_notes or player.holding_notes
               for team in self.match.teams for player in team.players):
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
        # 记下刚打完的这首：下一轮 SONG_SELECT 的第一阶段要接着放它
        self._last_played_song = self.current_song
        self.round_winner = self.match.teams[winning_team] if winning_team < len(self.match.teams) else None

        # 每局都把双方得分和本局胜者打到控制台（无 UI 模式下这就是主要输出）
        record = self.match.round_records[-1] if self.match.round_records else None
        if record:
            teams = self.match.teams
            print(f"第 {len(self.match.round_records)} 局 {record['song_id']} "
                  f"{record['song_title']}")
            for index, team in enumerate(record['teams']):
                mark = "★" if index == winning_team else " "
                print(f"  {mark} {team['name']:<14} {team['total']:>12,.0f}")
            if winning_team < len(teams):
                print(f"    → {teams[winning_team].name} 拿下本局，"
                      f"大比分 {self.match.scores[0]}:{self.match.scores[1]}")

        if self.match.is_finished:
            winner = self.match.winner.name if self.match.winner else "（无人获胜）"
            print(f"\n比赛结束：{self.match.scores[0]}:{self.match.scores[1]}，{winner} 获胜")
            self._write_results_excel()
            if self.match.score_mode:
                self._write_score_excel()
        # 玩家状态与能力值留到下一首开始时由 _prepare_players 统一重置
        self._set_state("RESULTS")

    def _write_results_excel(self) -> None:
        """比赛打完，把每一局双方得分写成 xlsx（常规赛果表，同一场只写一次）。"""
        if self.results_excel_written or not self.match.round_records:
            return
        try:
            path = write_sheet(
                self.results_excel_path,
                match_result_rows(self.match.round_records, self.match.scores,
                                  self.match.winner.name if self.match.winner else ""),
                sheet_name="赛果",
            )
        except OSError as error:
            print(f"警告：写赛果 Excel 失败（{error}）")
            return
        self.results_excel_written = path
        print("赛果表已写入 Excel:", path)

    def _write_score_excel(self) -> None:
        """计分赛打完后，把这一场的成绩**追加**到成绩表（同一场只写一次）。

        追加而不是覆盖：换一支队伍再打，新成绩加在后面，之前队伍的成绩还在。
        场次号由 append_score_match 按表里已有的最大场次 +1 算出来。
        """
        if self.excel_written or not self.match.round_records:
            return
        try:
            block = score_match_rows(self.match.round_records, self.match.team_ranks(),
                                     self.match.name or "")
            match_no = append_score_match(self.excel_path, block, len(self.match.round_records),
                                          sheet_name=self.match.name or "成绩")
        except OSError as error:
            print(f"警告：写 Excel 失败（{error}）")
            return
        self.excel_written = self.excel_path
        totals = self.match.team_totals()
        print(f"计分赛结束（第 {match_no} 场），成绩已追加到 Excel: {self.excel_path}")
        for index, team in enumerate(self.match.teams):
            if index < len(totals):
                print(f"  {team.name}: 总分 {totals[index]:,.1f}")

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
        elif self.game_state == "RESULTS":
            self._render_results()
        elif self.game_state == "FINISHED":
            self._render_ending()

        self._render_status_hud()
        if self.settings.debug:
            self._render_debug_info()
        pygame.display.flip()

    def _render_status_hud(self) -> None:
        """等待/进行中的实时信息。

        - 等待期间（MENU / SONG_SELECT / 开打前的准备时间 / RESULTS）在**左下角**
          显示 Countdown:xx，xx 是距离比赛真正开始的剩余秒数；
        - 比赛开始后在**屏幕中央**显示已结算音符的占比，只有数字。
        """
        if self.game_state == "PLAYING" and self.music_started:
            text = f"{self._song_progress() * 100:.1f}%"
            color = (200, 255, 200)
            font = self.fonts[55]
            centered = True
        else:
            remaining = self._time_until_match_start()
            if remaining is None:
                return
            text = f"Countdown:{remaining / 1000:.1f}"
            color = (255, 235, 120)
            font = self.fonts[30] if self.game_state == "RESULTS" else self.fonts[55]
            centered = False

        text_image = font.render(text, True, color)
        rect = (text_image.get_rect(center=(640, 360)) if centered
                else text_image.get_rect(bottomleft=(20, 700)))
        # 压到别的内容上时垫一层半透明黑底：既看得清又不会完全挡住
        backing = pygame.Surface(rect.inflate(24, 12).size, pygame.SRCALPHA)
        backing.fill((0, 0, 0, 190))
        self.screen.blit(backing, rect.inflate(24, 12).topleft)
        self.screen.blit(text_image, rect)

    def _render_results(self) -> None:
        """一局打完后展示成绩（停 results_delay 毫秒）。

        计分赛：**不出"谁拿下本局"**，直接显示"到目前为止的总分 + 在表里的实时排名"；
        普通赛：照旧显示本局双方队员分数、本局胜者与大比分。
        """
        if self.match.score_mode:
            self._render_score_board(ending=False)
            return

        self._render_team_big_points()

        title_text = f"第 {self.match.current_round + 1} 局结束"
        title = self._font(50, title_text).render(title_text, True, (255, 255, 255))
        self.screen.blit(title, (640 - title.get_width() / 2, 120))

        if self.round_winner is not None:
            win_text = f"{self.round_winner.name} 拿下本局"
            win_image = self._font(40, win_text).render(win_text, True, self.round_winner.color)
            self.screen.blit(win_image, (640 - win_image.get_width() / 2, 180))

        # 两队各自的队员成绩
        for team_index, team in enumerate(self.match.teams):
            base_x = 200 + team_index * 560
            head = f"{team.name}  {team.total_score:.0f}"
            self.screen.blit(self._font(30, head).render(head, True, team.color), (base_x, 250))
            for row, player in enumerate(team.players):
                line = f"{player.name}   {player.std_score:>8.0f}   {player.accuracy:6.2f}%"
                self.screen.blit(self._font(30, line).render(line, True, (200, 200, 200)),
                                 (base_x, 290 + row * 34))

        scores = self.match.scores or [0, 0]
        big_text = f"大比分 {scores[0]} : {scores[1]}"
        big = self._font(40, big_text).render(big_text, True, (255, 255, 255))
        self.screen.blit(big, (640 - big.get_width() / 2, 400))

    def _render_debug_info(self) -> None:
        text = f"{self.current_time}ms  state={self.game_state}  fps={self.clock.get_fps():.0f}"
        text_image = self.fonts[20].render(text, True, (120, 120, 120))
        self.screen.blit(text_image, (4, 4))

    def _render_ending(self) -> None:
        """显示比赛结果：计分赛看总分排名榜，普通赛看谁赢。

        计分赛不显示"谁赢了整场"那类胜负画面 —— 从头到尾都只给成绩表和排名。
        """
        if self.match.score_mode:
            self._render_score_board(ending=True)
            return
        self._render_team_big_points()
        who_wins = f"{self.match.winner.name} wins!" if self.match.winner else "Match over"
        text_image = self._font(70, who_wins).render(who_wins, True, (255, 255, 255))
        t_width, _ = self._font(70, who_wins).size(who_wins)
        self.screen.blit(text_image, (640 - t_width / 2, 360))

    def _render_score_board(self, ending: bool) -> None:
        """计分赛的成绩表：到目前为止每首歌的总分 + 实时排名。

        中间一列是 Track1 / Track2 / ... / Total / Rank，
        两侧各放一支队：每首歌的总分各占一行，倒数第二行是**目前为止**的总分，
        最后一行是它在表里的名次。最后两行字号略大，分数千分位分隔并居中。

        局中（`ending=False`，即每局打完后那几秒）和终局（`ending=True`）用的是同一张表 ——
        唯一的区别是终局才显示成绩表的落盘路径。刚打完的那一局已经记进
        `round_records`，所以每一局结束时表都会往下长一行，总分和名次同步更新。
        """
        records = self.match.round_records
        if not records:
            return

        totals = self.match.team_totals()
        ranks = self.match.team_ranks()
        labels = [f"Track{i + 1}" for i in range(len(records))] + ["Total", "Rank"]

        normal_font = 45
        big_font = 60
        normal_height = 50
        big_height = 72
        top = 150

        # 两侧分数列各自居中：左边半屏的中心 280，右边半屏的中心 1000
        # （中间留给 Track/Total/Rank 这一列，大约占 560~720）
        column_center = (280, 1000)

        # 先算总高度，好把整块垂直居中
        total_height = normal_height * (len(labels) - 2) + big_height * 2
        y = max(top, (720 - total_height) // 2)

        for row, label in enumerate(labels):
            is_summary = row >= len(labels) - 2           # 最后两行：总分、名次
            height = big_height if is_summary else normal_height
            size = big_font if is_summary else normal_font
            center_y = y + height / 2

            # 中间：行标题
            label_image = self._font(size, label).render(label, True, (200, 200, 200))
            self.screen.blit(label_image, (640 - label_image.get_width() / 2,
                                           center_y - label_image.get_height() / 2))

            for team_index, team in enumerate(self.match.teams):
                if is_summary and label == "Rank":
                    text = self._ordinal(ranks[team_index])
                elif is_summary:
                    text = f"{totals[team_index]:,.0f}"
                else:
                    text = f"{records[row]['teams'][team_index]['total']:,.0f}"
                image = self._font(size, text).render(text, True, team.color)
                center_x = column_center[min(team_index, 1)]
                self.screen.blit(image, (center_x - image.get_width() / 2,
                                         center_y - image.get_height() / 2))

            y += height

        # 队名放两侧顶部、和各自的分数列对齐，说明哪一列是哪支队
        for team_index, team in enumerate(self.match.teams):
            name_image = self._font(40, team.name).render(team.name, True, team.color)
            center_x = column_center[min(team_index, 1)]
            self.screen.blit(name_image, (center_x - name_image.get_width() / 2, 40))

        # 局中提示这一局已经打了几首、还剩几首；终局才提示成绩表写到了哪
        if ending:
            if self.excel_written:
                hint = f"成绩表：{self.excel_written}"
            else:
                hint = ""
        else:
            done = len(records)
            track_total = self.match.total_tracks() or done
            hint = f"已完成 {done} / {track_total} 首 · 总分与排名实时更新"
        if hint:
            hint_image = self._font(25, hint).render(hint, True, (140, 140, 140))
            self.screen.blit(hint_image, (640 - hint_image.get_width() / 2, 690))

    @staticmethod
    def _ordinal(rank: int) -> str:
        """1 -> 1st，2 -> 2nd，3 -> 3rd，11~13 用 th"""
        if 10 <= rank % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(rank % 10, "th")
        return f"{rank}{suffix}"

    def _render_team_big_points(self) -> None:
        """显示大比分"""
        teams = self.match.teams
        if not teams:
            return

        text_image = self._font(40, teams[0].name).render(str(teams[0].name), True, teams[0].color)
        self.screen.blit(text_image, (0, 20))

        if len(teams) > 1:
            t_width, _ = self._font(40, teams[1].name).size(str(teams[1].name))
            text_image = self._font(40, teams[1].name).render(str(teams[1].name), True, teams[1].color)
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
        # 高亮：**以前轮次选过的都常亮**，刚点出的那一首额外闪几下。
        # 行号要按曲目在曲库里的次序算，不能按第几轮算，
        # 否则第 1 轮选 TB（曲库第 3 行）会被画到第一行上。
        #
        # 第一阶段（还没点出下一首）：`selected_songs` 里最后一条是**本轮的候选**，
        # 这时还不能亮它（不然等于提前泄露了下一首），只亮它前面的那些。
        revealed_count = len(self.match.selected_songs)
        if not self.song_revealed and revealed_count > 0:
            revealed_count -= 1

        # 闪烁：在"亮/灭"之间按时间交替（按时间而不是帧数，免得帧率影响闪的时长）
        now = self._now()
        flashing = now < self._reveal_flash_until
        if flashing:
            period = REVEAL_FLASH_ON_MS + REVEAL_FLASH_OFF_MS
            blink_on = (now - (self._reveal_flash_until - REVEAL_FLASH_MS)) % period \
                < REVEAL_FLASH_ON_MS
        else:
            blink_on = True

        for entry_index, chosen in enumerate(self.match.selected_songs):
            song = chosen["song"]
            team_index = chosen["team"]
            if song not in songs or team_index >= len(self.match.teams):
                continue
            row = songs.index(song)
            is_newest = entry_index == revealed_count - 1
            # 刚点出的那首在"灭"的相位就跳过这一帧，画出来就是闪烁
            if is_newest and flashing and not blink_on:
                continue
            color = self.match.teams[team_index].color
            pygame.draw.rect(self.screen, color,
                             [640 - s_width / 2, 70 + s_height * row, s_width, s_height - 6])

        for index, song in enumerate(songs):
            song_key = song.id
            pygame.draw.rect(self.screen, (100, 100, 100),
                             [640 - s_width / 2 + 5, 70 + s_height * index + 5, s_width - 10, s_height - 16])
            key_color = self.pool_colors.get(song_key[:2].upper(), DEFAULT_POOL_COLOR)
            text_image = self.fonts[40].render(song_key, True, key_color)
            self.screen.blit(text_image, (640 - s_width / 2 + 10, 80 + index * s_height))
            text_image = self._font(40, song.title).render(song.title, True, (255, 255, 255))
            self.screen.blit(text_image, (640 - s_width / 2 + 80, 80 + index * s_height))

    def _render_gameplay(self) -> None:
        """渲染游戏进行中的画面"""
        teams = self.match.teams
        if len(teams) < 2 or self.current_song is None:
            return

        for index, team in enumerate(teams):
            # 这里原来还画了一行 f"{队名}: {总分}"，但它和 _render_team_big_points
            # 画在同一行 y=20 上：队 1 那边重叠 80px（整个队名都被压住），
            # 分数每帧都在涨，重叠处就一直在闪。总分在屏幕底部中央本来就有大数字，
            # 所以这一行直接去掉。
            # 玩家信息
            for player_index, player in enumerate(team.players):
                xpos = [150, 0, 300]
                self._render_player(player, index * 700 + xpos[player_index],
                                    360 - 290 * min(player_index, 1))

        # 以下这段是显示底下那一坨分数的，老代码石山搬过来的，这段比较复杂不好动
        # 屏幕上的分数用"进度线性"口径（display_score）：前 50% 大约就是 50 万，
        # 而不是原始分那种前慢后快的 S 形。存局成绩/Excel/结算榜仍是原始分。
        score_a = teams[0].display_score
        score_b = teams[1].display_score
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
        text_image = self._font(40, song_name).render(song_name, True, (255, 255, 255))
        t_width, _ = self._font(40, song_name).size(song_name)
        self.screen.blit(text_image, (640 - t_width / 2, 600))
        self._render_team_big_points()

    def _render_player(self, player: Player, x: int, y: int) -> None:
        """绘制单个玩家信息"""
        # 背景板
        pygame.draw.rect(self.screen, (0, 0, 0), [x, y, 240, 290])

        # 音符（普通音符 = 一个头；长条 = 一个头 + 一条又深又窄的身）
        for note in list(player.active_notes) + list(player.holding_notes):
            rect_x = x + 50 + int(note.x) * 0.3
            rect_y = y + 260 - (int(note.time) - self.current_time) * 0.6
            if note.is_long:
                # 身：从结束时间的位置一直连到判定线（已经按住时头会缩到线下面，所以取 min）
                end_y = y + 260 - (int(note.end_time) - self.current_time) * 0.6
                body_bottom = min(rect_y, y + 260)
                if body_bottom > end_y:
                    pygame.draw.rect(self.screen, (110, 110, 110),
                                     [rect_x + 7, end_y, 21, body_bottom - end_y])
            # 头：滚过判定线之后就不再画了
            if rect_y <= y + 260:
                pygame.draw.rect(self.screen, (255, 255, 255), [rect_x, rect_y, 35, 10])

        # 挡板（只遮住自己这一列，遮太宽会把旁边玩家的画面涂黑）
        pygame.draw.rect(self.screen, (0, 0, 0), [x, y - 460, 240, 480])
        pygame.draw.rect(self.screen, (50, 50, 50), [x + 50 + 19.2, y + 260, 150, 20])

        # 分数、准确率、连击
        color = self.match.teams[player.team_index].color
        # 屏幕上用"跟进度线性"的口径（见 Player.display_score）；原始分在曲终与它一致
        shown_score = player.display_score
        score_text = self.fonts[35].render(f"{shown_score:.0f}", True, (200, 200, 200))
        acc_text = self.fonts[35].render(f"{player.accuracy:.2f}%", True, (200, 200, 200))
        combo_text = self.fonts[40].render(str(player.combo), True, color)

        self.screen.blit(combo_text, (x + 140 - int(math.log10(max(1, player.combo))) * 10, y + 120))
        self.screen.blit(acc_text, (x + 180, y))
        self.screen.blit(score_text, (x + 140 - int(math.log10(max(1, shown_score))) * 10, y))

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

        # 玩家名（刚 miss 过会闪红；注意颜色分量必须是 0~255 的整数，这里算出来是浮点，得取整）
        last_miss = int(max(0.0, min(255.0,
                        255 + 0.1 * (player.last_judge_time.get('miss', -114514) - self.current_time))))
        name_text = self._font(30, player.name).render(player.name, True, (255, 255 - last_miss, 255 - last_miss))
        self.screen.blit(name_text, (x + 50 + 19.2, y + 260))

        # 各判定的累计数量，竖排小字（队 0 贴第四轨右侧，队 1 贴第一轨左侧右对齐）
        self._render_judgement_counts(player, x, y + 38)

    def _render_judgement_counts(self, player: Player, x: int, y: int) -> None:
        """竖排显示每个判定的累计数量。

        每个判定固定占一行（顺序同 JUDGEMENTS），数量为 0 的那一行留空不画字，
        所以红色 miss 永远落在第六行。
        两队分列在演奏区两侧：第一队贴第四轨右侧（左对齐），第二队贴第一轨左侧（右对齐）。
        """
        line_height = self.count_font.get_height() + 1
        align_right = player.team_index % 2 == 1
        # 第四轨右边缘 = x + 50 + 448*0.3 + 35；第一轨左边缘 = x + 50 + 64*0.3
        anchor = (x + 67) if align_right else (x + 222)
        for index, judgement in enumerate(JUDGEMENTS):
            count = player.judgement_counts.get(judgement, 0)
            if count <= 0:
                continue
            text_image = self.count_font.render(
                str(count), True, JUDGEMENT_COUNT_COLORS[judgement])
            left = anchor - text_image.get_width() if align_right else anchor
            self.screen.blit(text_image, (left, y + index * line_height))

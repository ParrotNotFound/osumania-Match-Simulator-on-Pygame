# src/utils/config.py
"""集中式配置加载。

整个项目的可调参数（画面、比赛规则、判定、曲库、队伍玩家、选曲顺序）
全部集中在项目根目录的一份 config.toml 里，本模块负责把它读成对象。

配置里的相对路径一律相对于 config.toml 所在目录解析，
所以从任何工作目录启动 main.py 都能找到资源。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    raise SystemExit("需要 Python 3.11 或更高版本（配置文件用标准库 tomllib 解析）") from None

DEFAULT_CONFIG_NAME = "config.toml"

# 判定种类，顺序固定：从严到宽
JUDGEMENTS: Tuple[str, ...] = ("perfect_g", "perfect", "great", "good", "bad", "miss")

# 曲目 id 前两个字母没有配色时的兜底颜色
DEFAULT_POOL_COLOR: Tuple[int, int, int] = (200, 200, 200)

# 判定分值与连击奖励的默认值（config.toml 里可以只写想改的那几项）
DEFAULT_SCORES: Dict[str, float] = {
    'perfect_g': 320, 'perfect': 300, 'great': 200, 'good': 100, 'bad': 50, 'miss': 0,
}
DEFAULT_BONUS: Dict[str, float] = {
    'perfect_g': 0.5, 'perfect': 0.25, 'great': 0.0, 'good': -1.0, 'bad': -3.0, 'miss': -50.0,
}


class ConfigError(RuntimeError):
    """配置缺失或内容不合法，消息是给人看的中文说明。"""


@dataclass
class SongConfig:
    """一首曲目"""
    id: str
    title: str = ""
    artist: str = "Unknown"
    folder: str = ""
    beatmap: Optional[str] = None   # 指定谱面文件名，留空则自动查找
    audio: Optional[str] = None     # 指定音频文件名，留空则自动查找


@dataclass
class TeamConfig:
    name: str
    color: Tuple[int, int, int] = (255, 255, 255)
    players: List[str] = field(default_factory=list)


@dataclass
class PickConfig:
    """一条选曲安排"""
    song: str
    team: Optional[int] = None      # None = 按轮次自动轮换


@dataclass
class MatchConfig:
    name: str = "MATCH"
    rounds_to_win: int = 2
    resume: bool = True
    # 已打完每一局的胜者（0/1），程序每局结束自动写回配置文件，启动时读取
    results: List[int] = field(default_factory=list)
    # 计分赛模式：不管 rounds_to_win，曲库里的歌各打一遍，最后按总分排名并导出 Excel
    score_mode: bool = False
    excel_file: str = "data/score_match.xlsx"
    # 常规赛果表：比赛打完把每一局双方得分写到这里（不管是不是无 UI 模式都写）
    results_excel: str = "data/match_results.xlsx"


@dataclass
class GameSettings:
    screen_width: int = 1280
    screen_height: int = 720
    fps: int = 60
    max_render_dist: int = 1200
    start_delay: int = 5000        # 启动 -> 第一场比赛开始前的等待
    song_select_delay: int = 5000  # 切到选曲页面 -> 选出本轮曲目
    match_start_delay: int = 5000  # 选完歌 -> 比赛真正开始（音乐在这之后响起）
    results_delay: int = 5000      # 每场打完后成绩展示时间
    debug: bool = False
    # 无 UI 模式：不弹窗、不渲染、不实时，用虚拟时钟尽快把比赛跑完，结果打到控制台
    headless: bool = False


@dataclass
class PlayerSettings:
    """选手能力值相关设置（每首歌开始时生效）。"""
    reread_roster: bool = True   # 每首歌开始时重新读取 config.toml 的队员名单
    form_range: int = 10         # 每首歌开始时每项能力值随机 ±form_range（手感）


@dataclass
class JudgeSettings:
    perfect_g: int = 5
    perfect: int = 25
    great: int = 45
    good: int = 60
    bad: int = 80
    score: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SCORES))
    bonus: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_BONUS))
    bonus_start: float = 100.0
    bonus_max: float = 100.0

    def windows(self) -> List[int]:
        """判定窗口，从严格到宽松；超过最后一个就是 miss。"""
        return [getattr(self, key) for key in JUDGEMENTS if key != 'miss']


@dataclass
class GameConfig:
    """一份完整的配置"""
    root: str                               # config.toml 所在目录（项目根目录）
    path: str                               # config.toml 的绝对路径
    game: GameSettings
    match: MatchConfig
    judge: JudgeSettings
    players: PlayerSettings
    songs: List[SongConfig]
    teams: List[TeamConfig]
    picks: List[PickConfig]
    pool_colors: Dict[str, Tuple[int, int, int]]

    def resolve(self, *parts: str) -> str:
        """把配置里的相对路径解析成基于项目根目录的绝对路径。"""
        path = os.path.join(*parts)
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(self.root, path))

    def song_by_id(self, song_id: str) -> Optional[SongConfig]:
        for song in self.songs:
            if song.id == song_id:
                return song
        return None


# ---------------------------------------------------------------------------
# 取值助手：类型不对时给出带位置的中文报错
# ---------------------------------------------------------------------------
def _section(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是一个配置段")
    return value


def _int(sec: Dict[str, Any], key: str, default: int, where: str) -> int:
    value = sec.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"[{where}] {key} 需要整数，实际是 {value!r}")
    return value


def _float(sec: Dict[str, Any], key: str, default: float, where: str) -> float:
    value = sec.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"[{where}] {key} 需要数字，实际是 {value!r}")
    return float(value)


def _bool(sec: Dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = sec.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"[{where}] {key} 需要 true/false，实际是 {value!r}")
    return value


def _text(sec: Dict[str, Any], key: str, default: str, where: str) -> str:
    value = sec.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"[{where}] {key} 需要字符串，实际是 {value!r}")
    return value


def _color(value: Any, where: str) -> Tuple[int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ConfigError(f"{where} 需要形如 [r, g, b] 的三个数字，实际是 {value!r}")
    out: List[int] = []
    for part in value:
        if isinstance(part, bool) or not isinstance(part, int) or not 0 <= part <= 255:
            raise ConfigError(f"{where} 的颜色分量必须是 0~255 的整数，实际是 {value!r}")
        out.append(part)
    return out[0], out[1], out[2]


def _judgement_map(raw: Any, where: str, defaults: Dict[str, float]) -> Dict[str, float]:
    result = dict(defaults)
    if raw is None:
        return result
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} 必须是一个表，例如 {{ perfect_g = 320 }}")
    for key, value in raw.items():
        if key not in JUDGEMENTS:
            raise ConfigError(f"{where} 里出现未知判定 {key!r}，可用：{', '.join(JUDGEMENTS)}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}.{key} 需要数字，实际是 {value!r}")
        result[key] = float(value)
    return result


# ---------------------------------------------------------------------------
# 各段解析
# ---------------------------------------------------------------------------
def _parse_game(data: Dict[str, Any]) -> GameSettings:
    sec = _section(data, "game")
    settings = GameSettings(
        screen_width=_int(sec, "screen_width", 1280, "game"),
        screen_height=_int(sec, "screen_height", 720, "game"),
        fps=_int(sec, "fps", 60, "game"),
        max_render_dist=_int(sec, "max_render_dist", 1200, "game"),
        # 前三个等待时间也接受旧名字（countdown_menu / countdown_song_select / lead_in）
        start_delay=_int(sec, "start_delay", _int(sec, "countdown_menu", 5000, "game"), "game"),
        song_select_delay=_int(
            sec, "song_select_delay", _int(sec, "countdown_song_select", 5000, "game"), "game"),
        match_start_delay=_int(sec, "match_start_delay", _int(sec, "lead_in", 5000, "game"), "game"),
        results_delay=_int(sec, "results_delay", 5000, "game"),
        debug=_bool(sec, "debug", False, "game"),
        headless=_bool(sec, "headless", False, "game"),
    )
    if settings.fps <= 0:
        raise ConfigError("[game] fps 必须大于 0")
    if settings.screen_width <= 0 or settings.screen_height <= 0:
        raise ConfigError("[game] 窗口尺寸必须大于 0")
    if settings.max_render_dist <= 0:
        raise ConfigError("[game] max_render_dist 必须大于 0")
    for name in ("start_delay", "song_select_delay", "match_start_delay", "results_delay"):
        if getattr(settings, name) < 0:
            raise ConfigError(f"[game] {name} 不能是负数")
    return settings


def _parse_match(data: Dict[str, Any]) -> MatchConfig:
    sec = _section(data, "match")
    cfg = MatchConfig(
        name=_text(sec, "name", "MATCH", "match"),
        rounds_to_win=_int(sec, "rounds_to_win", 2, "match"),
        resume=_bool(sec, "resume", True, "match"),
        results=_parse_results(sec.get("results"), "match.results"),
        score_mode=_bool(sec, "score_mode", False, "match"),
        excel_file=_text(sec, "excel_file", "data/score_match.xlsx", "match"),
        results_excel=_text(sec, "results_excel", "data/match_results.xlsx", "match"),
    )
    if cfg.rounds_to_win < 1:
        raise ConfigError("[match] rounds_to_win 至少是 1")
    return cfg


def _parse_results(raw: Any, where: str) -> List[int]:
    """赛果缓存：一串 0/1，每个元素是一局的胜者队序号。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"[{where}] 必须是一个数组，例如 results = [0, 1, 0]")
    results: List[int] = []
    for index, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, int) or item not in (0, 1):
            raise ConfigError(f"[{where}] 第 {index + 1} 个元素必须是 0 或 1（胜者队序号），"
                              f"实际是 {item!r}")
        results.append(item)
    return results


def _parse_judge(data: Dict[str, Any]) -> JudgeSettings:
    sec = _section(data, "judge")
    settings = JudgeSettings(
        perfect_g=_int(sec, "perfect_g", 5, "judge"),
        perfect=_int(sec, "perfect", 25, "judge"),
        great=_int(sec, "great", 45, "judge"),
        good=_int(sec, "good", 60, "judge"),
        bad=_int(sec, "bad", 80, "judge"),
        score=_judgement_map(sec.get("score"), "judge.score", DEFAULT_SCORES),
        bonus=_judgement_map(sec.get("bonus"), "judge.bonus", DEFAULT_BONUS),
        bonus_start=_float(sec, "bonus_start", 100.0, "judge"),
        bonus_max=_float(sec, "bonus_max", 100.0, "judge"),
    )
    windows = settings.windows()
    if any(window < 0 for window in windows):
        raise ConfigError("[judge] 判定窗口不能是负数")
    if any(a > b for a, b in zip(windows, windows[1:])):
        raise ConfigError(
            "[judge] 判定窗口必须从小到大：perfect_g <= perfect <= great <= good <= bad"
            "（超过 bad 一律算 miss）"
        )
    if settings.bonus_max < 0:
        raise ConfigError("[judge] bonus_max 不能是负数")
    return settings


def _parse_songs(data: Dict[str, Any]) -> List[SongConfig]:
    raw = data.get("songs", [])
    if not isinstance(raw, list) or not raw:
        raise ConfigError("至少要在 [[songs]] 里配置一首曲目")
    songs: List[SongConfig] = []
    seen: Dict[str, int] = {}
    for index, item in enumerate(raw):
        where = f"songs[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} 必须是一个 [[songs]] 配置段")
        song_id = str(item.get("id", "") or "").strip()
        if not song_id:
            raise ConfigError(f"{where} 缺少 id")
        if song_id in seen:
            raise ConfigError(f"曲目 id 重复：{song_id}（第 {seen[song_id] + 1} 首和第 {index + 1} 首）")
        seen[song_id] = index
        folder = str(item.get("folder", "") or "").strip()
        if not folder:
            raise ConfigError(f"{where}（id={song_id}）缺少 folder")
        beatmap = item.get("beatmap")
        audio = item.get("audio")
        songs.append(SongConfig(
            id=song_id,
            title=str(item.get("title") or song_id),
            artist=str(item.get("artist") or "Unknown"),
            folder=folder,
            beatmap=str(beatmap) if beatmap else None,
            audio=str(audio) if audio else None,
        ))
    return songs


def _parse_teams(data: Dict[str, Any]) -> List[TeamConfig]:
    raw = data.get("teams", [])
    if not isinstance(raw, list) or not raw:
        raise ConfigError("至少要在 [[teams]] 里配置两支队伍")
    if len(raw) != 2:
        raise ConfigError(f"队伍数量必须是 2（当前画面布局只支持两队），实际配置了 {len(raw)} 支")
    teams: List[TeamConfig] = []
    for index, item in enumerate(raw):
        where = f"teams[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} 必须是一个 [[teams]] 配置段")
        name = str(item.get("name", "") or "").strip()
        if not name:
            raise ConfigError(f"{where} 缺少 name")
        players = item.get("players", [])
        if not isinstance(players, list) or not players:
            raise ConfigError(f"{where}（{name}）的 players 至少要有一个玩家名")
        if len(players) > 3:
            raise ConfigError(f"{where}（{name}）最多 3 名玩家（画面只排得下 3 个位置），实际 {len(players)} 名")
        names = [str(p).strip() for p in players]
        if any(not n for n in names):
            raise ConfigError(f"{where}（{name}）里有空的玩家名")
        teams.append(TeamConfig(
            name=name,
            color=_color(item.get("color", [255, 255, 255]), f"{where}.color"),
            players=names,
        ))
    return teams


def _parse_picks(data: Dict[str, Any], songs: List[SongConfig]) -> List[PickConfig]:
    raw = data.get("picks", [])
    if not isinstance(raw, list):
        raise ConfigError("picks 必须是一个列表")
    song_ids = {song.id for song in songs}
    picks: List[PickConfig] = []
    for index, item in enumerate(raw):
        where = f"picks[{index}]"
        if isinstance(item, str):
            song_id, team = item.strip(), None
        elif isinstance(item, dict):
            song_id = str(item.get("song", "") or "").strip()
            team = item.get("team")
            if team is not None:
                if isinstance(team, bool) or not isinstance(team, int) or team not in (0, 1):
                    raise ConfigError(f"{where}.team 只能是 0 或 1（不填则自动轮换），实际是 {team!r}")
        else:
            raise ConfigError(f"{where} 必须是 {{ song = \"曲目id\", team = 0 }} 或直接写曲目 id 字符串")
        if not song_id:
            raise ConfigError(f"{where} 缺少 song")
        if song_id not in song_ids:
            raise ConfigError(f"{where} 引用了曲库里不存在的曲目 id：{song_id}")
        picks.append(PickConfig(song=song_id, team=team))
    return picks


def _parse_players(data: Dict[str, Any]) -> PlayerSettings:
    sec = _section(data, "players")
    settings = PlayerSettings(
        reread_roster=_bool(sec, "reread_roster", True, "players"),
        form_range=_int(sec, "form_range", 10, "players"),
    )
    if settings.form_range < 0:
        raise ConfigError("[players] form_range 不能是负数")
    if settings.form_range > 100:
        raise ConfigError("[players] form_range 太大了（能力值范围是 0~100），请填 0~100")
    return settings


def _parse_pool_colors(data: Dict[str, Any]) -> Dict[str, Tuple[int, int, int]]:
    raw = _section(data, "pool_colors")
    for key in raw:
        if not isinstance(key, str):
            raise ConfigError("[pool_colors] 的键必须是字符串")
    return {key: _color(value, f"pool_colors.{key}") for key, value in raw.items()}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def load_config(path: Optional[str] = None) -> GameConfig:
    """读取并校验配置文件，出错时抛出 ConfigError。"""
    config_path = os.path.abspath(path or DEFAULT_CONFIG_NAME)
    if not os.path.isfile(config_path):
        raise ConfigError(f"找不到配置文件：{config_path}")

    try:
        # 读成字节再去掉 BOM：Windows 上的记事本/PowerShell 存 UTF-8 时经常带 BOM，
        # 而 tomllib 只接受干净文本，带 BOM 会直接报"格式错误"。
        with open(config_path, "rb") as handle:
            raw = handle.read()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"配置文件格式错误（{config_path}）：{error}") from None
    except UnicodeDecodeError as error:
        raise ConfigError(f"配置文件不是 UTF-8 编码（{config_path}）：{error}") from None
    except OSError as error:
        raise ConfigError(f"无法读取配置文件（{config_path}）：{error}") from None

    songs = _parse_songs(data)
    return GameConfig(
        root=os.path.dirname(config_path),
        path=config_path,
        game=_parse_game(data),
        match=_parse_match(data),
        judge=_parse_judge(data),
        players=_parse_players(data),
        songs=songs,
        teams=_parse_teams(data),
        picks=_parse_picks(data, songs),
        pool_colors=_parse_pool_colors(data),
    )


# ---------------------------------------------------------------------------
# 把赛果写回配置文件
# ---------------------------------------------------------------------------
def save_match_results(config_path: str, results: List[int]) -> None:
    """把赛果缓存写回 config.toml 里 [match] 段的 results 键。

    只动这一行，注释和排版原样保留；先写临时文件再整体替换，
    中途失败也不会把配置写坏。
    """
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        print(f"警告：写赛果缓存失败，读不到配置文件（{error}）")
        return

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    line_text = f"results = [{', '.join(str(item) for item in results)}]"

    # 1) 找到 [match] 段的范围
    start = None
    for index, line in enumerate(lines):
        if line.strip().startswith("[match]"):
            start = index
            break
    if start is None:
        print("警告：config.toml 里没有 [match] 段，赛果缓存没有写进去")
        return

    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break

    # 2) 段内已有 results = ... 就替换，否则插到该段末尾（跳过尾部空行）
    replaced = False
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if stripped.startswith("results") and stripped[len("results"):].lstrip().startswith("="):
            lines[index] = line_text
            replaced = True
            break
    if not replaced:
        insert_at = end
        while insert_at - 1 > start and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, line_text)

    # 3) 写临时文件再整体替换
    tmp_path = f"{config_path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(newline.join(lines))
        os.replace(tmp_path, config_path)
    except OSError as error:
        print(f"警告：写赛果缓存失败（{error}）")
        try:
            os.remove(tmp_path)
        except OSError:
            pass

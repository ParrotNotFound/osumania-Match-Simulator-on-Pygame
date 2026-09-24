#!/usr/bin/env python3
# team_sim.py
"""用**游戏自己的判定引擎**跑一场无 UI 的 17 队循环赛，给团队实力做实证标定。

它不重写任何判定逻辑：把 teams.xlsx 的名单写进一份临时 config.toml，然后用
`src/core/game.py` 的无 UI 模式真刀真枪打一遍，收回每名队员的分数与**本局实际生效的能力值**。

跑完做三件事：
  1. 循环赛结果：每队 胜/负、小局胜率、名次置信区间（bootstrap）；
  2. **按谱面标定能力权重**：把"两队分数差"对"两队六名队员的能力差"做最小二乘，
     直接问引擎"这张谱上到底谁说了算"，得到该谱的权重向量（不用人工猜权重）；
  3. 用标定出来的权重给队伍算"预测实力分"，与循环赛名次对照，并预测最终排名。

名单里 5 名队员不都上场（本项目是 3v3），换谁上场用 `--lineup` 控制：
    first3（默认）= 队长+队员1+队员2 固定首发
    simple        = 只用队员1~3（不管队长）
    random        = 每场随机抽 3 人
    all5          = 每场抽 3 人，但保证 5 人都上过场（近似"轮换"）
    逗号分隔的名字 = 固定用这几个人

用法：
    python team_sim.py                        # 3 张谱 × 2 轮全循环（约 5~10 分钟）
    python team_sim.py --maps RC1,LN1 --reps 4
    python team_sim.py --lineup random --reps 2 --seed 7
    python team_sim.py --dry-run              # 只打印要跑多少场，不真跑
"""
from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import math
import os
import random
import shutil
import statistics
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.entities.player import ABILITY_KEYS, ABILITY_LABELS  # noqa: E402
from team_stats import (  # noqa: E402
    DEFAULT_XLSX, WEIGHT_PROFILES, PlayerRow, TeamRow, build_report, composite_of,
    fill_abilities, load_roster,
)

WORK_DIR = os.path.join(BASE_DIR, "data", "_selftest", "_sim")
DEFAULT_OUT = os.path.join(BASE_DIR, "data", "team_sim.json")

# 默认曲池：项目 config.toml 里那三首（低难 RC1 / 长条 LN1 / 硬核 HB1）
DEFAULT_MAPS: Tuple[Tuple[str, str], ...] = (
    ("RC1", os.path.join("data", "beatmaps", "2598781")),
    ("LN1", os.path.join("data", "beatmaps", "2474010 wotoha - TESHiKANi")),
    ("HB1", os.path.join("data", "beatmaps", "2318291 Sakuzyo - Ultimate Force (Game Ver.)")),
)


class _Sink:
    """吞掉游戏自己的控制台输出（每场十几行，几千场就刷屏了）。"""

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


@contextlib.contextmanager
def quiet():
    """把这一小段里的所有输出摁死。

    只换 `sys.stdout` 不够：pygame 的启动横幅和 `load_config` 的提示直接写文件描述符，
    所以连 fd 1/2 一起指到 os.devnull。
    """
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = _Sink(), _Sink()
        with open(os.devnull, "w") as devnull:
            saved_fd_out = os.dup(1)
            saved_fd_err = os.dup(2)
            os.dup2(devnull.fileno(), 1)
            os.dup2(devnull.fileno(), 2)
            try:
                yield
            finally:
                os.dup2(saved_fd_out, 1)
                os.dup2(saved_fd_err, 2)
                os.close(saved_fd_out)
                os.close(saved_fd_err)
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err


def _scan_beatmaps(folder: str) -> List[Tuple[str, float, int]]:
    """扫文件夹里的 .osu，返回 [(文件名, OverallDifficulty, 判定数)]（按 OD 从低到高）。

    同一个曲包常有 7 个难度（EASY…ULTIMA），必须按 OD 挑，不能按文件名排序 ——
    文件名排序第一个往往是 [ADVANCED] 这种简单难度，会把"高难谱"的结论整个带偏。
    """
    if not os.path.isdir(folder):
        return []
    found: List[Tuple[str, float, int]] = []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".osu"):
            continue
        difficulty = 5.0
        notes = 0
        try:
            with open(os.path.join(folder, name), "r", encoding="utf-8-sig",
                      errors="replace") as handle:
                section = ""
                for line in handle:
                    stripped = line.strip()
                    if stripped.startswith("[") and stripped.endswith("]"):
                        section = stripped
                    elif section == "[Difficulty]" and stripped.startswith("OverallDifficulty:"):
                        try:
                            difficulty = float(stripped.split(":", 1)[1])
                        except ValueError:
                            pass
                    elif section == "[HitObjects]" and stripped and not stripped.startswith("//"):
                        notes += 1
        except OSError:
            continue
        found.append((name, difficulty, notes))
    return sorted(found, key=lambda item: item[1])


def _find_beatmap(folder: str, prefer: str = "hardest") -> Optional[Tuple[str, float]]:
    """挑这张谱要打哪个难度，返回 (文件名, OD)。

    prefer：hardest（默认，OD 最高）/ easiest / 文件名子串（例如 "MASTER"）。
    """
    candidates = [item for item in _scan_beatmaps(folder) if item[2] > 0]
    if not candidates:
        return None
    if prefer in ("hardest", "easiest"):
        chosen = candidates[-1] if prefer == "hardest" else candidates[0]
        return chosen[0], chosen[1]
    wanted = prefer.lower()
    matches = [item for item in candidates if wanted in item[0].lower()]
    if not matches:
        return candidates[-1][0], candidates[-1][1]
    # 子串命中多个时取 OD 最高的那个
    chosen = max(matches, key=lambda item: item[1])
    return chosen[0], chosen[1]


def _write_config(path: str, song_id: str, folder: str, teams: Sequence[TeamRow],
                  players: Sequence[str], players2: Sequence[str],
                  form_range: int, beatmap: Optional[str]) -> None:
    """写一份一次性配置：只有一张谱、两支临时队伍。路径统一用正斜杠（TOML 里的反斜杠要转义）。"""
    def _p(value: str) -> str:
        return os.path.abspath(value).replace("\\", "/")

    lines = [
        "[game]",
        "headless = true",
        "",
        "[match]",
        f'name = "{song_id}"',
        "rounds_to_win = 1",
        "resume = false",
        f'results_excel = "{_p(os.path.join(WORK_DIR, "results.xlsx"))}"',
        f'excel_file = "{_p(os.path.join(WORK_DIR, "score.xlsx"))}"',
        "",
        "[players]",
        "reread_roster = false",
        f"form_range = {form_range}",
        "",
        "[[songs]]",
        f'id = "{song_id}"',
        f'folder = "{_p(folder)}"',
    ]
    if beatmap:
        lines.append(f'beatmap = "{beatmap}"')
    for name, members in ((teams[0].name, players), (teams[1].name, players2)):
        lines += [
            "",
            "[[teams]]",
            f'name = "{name}"',
            "color = [255, 100, 90]",
            "players = [" + ", ".join(f'"{member}"' for member in members) + "]",
        ]
    lines += ["", "[[picks]]", f'song = "{song_id}"', ""]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def _write_score_config(path: str, songs: Sequence[Tuple[str, str, str, float]],
                        teams: Sequence[TeamRow], players: Sequence[str],
                        players2: Sequence[str], form_range: int) -> None:
    """计分赛模式的一次性配置：三首预选曲目按顺序各打一遍，最后按总分定名次。

    songs 是 [(id, folder, beatmap, od)]。`score_mode = true` 时游戏会忽略
    rounds_to_win，把 [[picks]] 全部打完 —— 正是"预选看总分"的赛制。
    """
    def _p(value: str) -> str:
        return os.path.abspath(value).replace("\\", "/")

    lines = [
        "[game]",
        "headless = true",
        "",
        "[match]",
        'name = "QUALIFIER"',
        "rounds_to_win = 1",
        "resume = false",
        "score_mode = true",
        f'results_excel = "{_p(os.path.join(WORK_DIR, "results.xlsx"))}"',
        f'excel_file = "{_p(os.path.join(WORK_DIR, "score.xlsx"))}"',
        "",
        "[players]",
        "reread_roster = false",
        f"form_range = {form_range}",
    ]
    for song_id, folder, beatmap, _od in songs:
        lines += ["", "[[songs]]", f'id = "{song_id}"', f'folder = "{_p(folder)}"']
        if beatmap:
            lines.append(f'beatmap = "{beatmap}"')
    for name, members in ((teams[0].name, players), (teams[1].name, players2)):
        lines += [
            "",
            "[[teams]]",
            f'name = "{name}"',
            "color = [255, 100, 90]",
            "players = [" + ", ".join(f'"{member}"' for member in members) + "]",
        ]
    for song_id, _folder, _beatmap, _od in songs:
        lines += ["", "[[picks]]", f'song = "{song_id}"']
    lines.append("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def _pick_lineup(team: TeamRow, mode: str, rng: random.Random,
                 used: Dict[str, set],
                 opponent: Optional[TeamRow] = None) -> List[str]:
    """给这一场挑 3 名上场队员。

    `all5` 是"轮换"口径：尽量抽没上过场的人，抽满 3 个再把这场对手也用过的人排一遍，
    免得某支队总在同一批人手里重复。`random` 才是纯随机（每次都可能重复）。
    `best3` 取综合实力分最高的三人：很多队的"队长+队员1+队员2"并不是最强三人
    （名字顺序是报名顺序，不是实力顺序），这个口径用来验证"按最优排阵能提多少"。
    """
    names = [member.name for member in team.players]
    if mode == "first3":
        return names[:3]
    if mode == "simple":
        return names[1:4] if len(names) >= 4 else names[:3]
    if mode == "best3":
        ordered = sorted(team.players, key=lambda member: -member.derived.get("composite", 0.0))
        return [member.name for member in ordered[:3]]
    if mode == "random":
        return rng.sample(names, min(3, len(names)))
    if mode == "all5":
        seen = used[team.name]
        mirror = rng.sample(names, min(3, len(names)))
        pool = [name for name in names if name not in seen]
        rng.shuffle(pool)
        chosen = pool[:3]
        for name in names:
            if len(chosen) >= 3:
                break
            if name not in chosen:
                chosen.append(name)
        chosen = chosen[:3]
        # 把"对手也已经用过的组合"往后排，尽量让每支队都轮流上
        key = tuple(sorted(chosen))
        if opponent is not None and key in used.get(f"__pair__{opponent.name}", set()):
            chosen = mirror
        seen.update(chosen)
        used.setdefault(f"__pair__{opponent.name}" if opponent else "", set()).add(key)
        return chosen
    wanted = [part.strip() for part in mode.split(",") if part.strip()]
    if not wanted:
        return names[:3]
    return [name for name in wanted if name in names][:3] or names[:3]


def _least_squares(rows: Sequence[Tuple[List[float], float]]) -> Tuple[List[float], float]:
    """正规方程解多元最小二乘，返回 (系数, R²)。

    rows 是 (特征, 目标)。特征维度很小（5），自己解就行，不引第三方库。
    """
    if not rows:
        return [0.0] * len(ABILITY_KEYS), 0.0
    dimension = len(rows[0][0])
    # 归一化尺度：每列除以它的 RMS，避免量纲差异把正规方程搞病态
    scales = []
    for index in range(dimension):
        total = sum(row[0][index] ** 2 for row in rows)
        scales.append(math.sqrt(total / len(rows)) or 1.0)

    matrix = [[0.0] * (dimension + 1) for _ in range(dimension)]
    for features, target in rows:
        scaled = [features[i] / scales[i] for i in range(dimension)]
        for i in range(dimension):
            for j in range(dimension):
                matrix[i][j] += scaled[i] * scaled[j]
            matrix[i][dimension] += scaled[i] * target
    # 高斯消元
    for column in range(dimension):
        pivot = max(range(column, dimension), key=lambda r: abs(matrix[r][column]))
        if abs(matrix[pivot][column]) < 1e-12:
            continue
        matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
        factor = matrix[column][column]
        for j in range(column, dimension + 1):
            matrix[column][j] /= factor
        for row in range(dimension):
            if row == column:
                continue
            factor = matrix[row][column]
            if factor == 0.0:
                continue
            for j in range(column, dimension + 1):
                matrix[row][j] -= factor * matrix[column][j]
    coefficients = [matrix[i][dimension] / scales[i] for i in range(dimension)]

    mean_target = sum(row[1] for row in rows) / len(rows)
    total = sum((row[1] - mean_target) ** 2 for row in rows)
    residual = 0.0
    for features, target in rows:
        predicted = sum(coefficients[i] * features[i] for i in range(dimension))
        residual += (target - predicted) ** 2
    r2 = 1.0 - residual / total if total > 0 else 0.0
    return coefficients, r2


def _normalize(coefficients: Sequence[float]) -> List[float]:
    """把系数转成"百分比权重"：负系数截成 0，再按总和归一化。"""
    clipped = [max(0.0, value) for value in coefficients]
    total = sum(clipped)
    if total <= 0:
        return [100.0 / len(clipped)] * len(clipped)
    return [value / total * 100.0 for value in clipped]


def _delta_rows_from_records(records: Iterable[Dict[str, Any]]
                             ) -> Dict[str, List[Tuple[List[float], float]]]:
    """从原始对战记录里取「两队平均基准能力差 → 分数差」的标定样本。

    用**基准能力**（名字哈希那一份，不含本局手感）当自变量：手感是 ±10 量级的噪声，
    当自变量会把系数按噪声比例缩小，把"手速"这类手感幅度大的能力系统性低估。
    """
    rows: Dict[str, List[Tuple[List[float], float]]] = {}
    for record in records:
        song = record.get("song", "?")
        players_a = record.get("players_a") or []
        players_b = record.get("players_b") or []
        if not players_a or not players_b:
            continue
        features = []
        for key in ABILITY_KEYS:
            mean_a = statistics.fmean([item["base"][key] for item in players_a])
            mean_b = statistics.fmean([item["base"][key] for item in players_b])
            features.append(mean_a - mean_b)
        rows.setdefault(song, []).append((features, record["score_a"] - record["score_b"]))
    return rows


def recalibrate(args: argparse.Namespace) -> int:
    """拿已经跑出来的原始记录重新标定权重，不重跑比赛（几秒就完）。"""
    if not args.raw or not os.path.isfile(args.raw):
        print(f"原始记录不存在：{args.raw}")
        return 1
    records: List[Dict[str, Any]] = []
    with open(args.raw, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if not records:
        print("原始记录是空的")
        return 1

    observations = _delta_rows_from_records(records)
    calibration: Dict[str, Dict[str, Any]] = {}
    for song_id, rows in observations.items():
        if len(rows) < len(ABILITY_KEYS) + 1:
            continue
        coefficients, r2 = _least_squares(rows)
        weights = _normalize(coefficients)
        calibration[song_id] = {
            "coefficients": dict(zip(ABILITY_KEYS, (round(value, 5) for value in coefficients))),
            "weights": dict(zip(ABILITY_KEYS, (round(value, 2) for value in weights))),
            "r2": round(r2, 4),
            "samples": len(rows),
        }
    if not calibration:
        print("样本太少，标定不了")
        return 1
    blend = {key: statistics.fmean(item["weights"][key] for item in calibration.values())
             for key in ABILITY_KEYS}
    total = sum(blend.values()) or 1.0
    blend = {key: round(value / total * 100.0, 2) for key, value in blend.items()}

    payload: Dict[str, Any] = {}
    if os.path.isfile(args.out):
        try:
            with open(args.out, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            payload = {}
    payload["calibration"] = calibration
    payload["weights"] = blend
    payload["delta_header"] = ["song"] + list(ABILITY_KEYS) + ["score_delta"]
    payload["delta_rows"] = [[song_id] + list(features) + [target]
                             for song_id, rows in observations.items()
                             for features, target in rows]
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    print(f"按 {len(records)} 场原始记录重新标定，已写回 {os.path.abspath(args.out)}")
    header = ["谱面", "R²", "样本"] + [ABILITY_LABELS[key] for key in ABILITY_KEYS]
    table = [[song_id, f"{item['r2']:.3f}", item["samples"]]
             + [f"{item['weights'][key]:.1f}%" for key in ABILITY_KEYS]
             for song_id, item in calibration.items()]
    table.append(["平均", "", ""] + [f"{blend[key]:.1f}%" for key in ABILITY_KEYS])
    widths = [max(len(str(row[i])) for row in [header] + table) for i in range(len(header))]
    for row in [header] + table:
        print("  ".join(str(row[i]).ljust(widths[i]) for i in range(len(header))))
    return 0


def _play_match(config_path: str, seed: int, song_ids: Sequence[str],
                team_a: TeamRow, team_b: TeamRow, players_a: Sequence[str],
                players_b: Sequence[str], stats: Dict[str, Dict[str, Any]]
                ) -> Optional[List[Dict[str, Any]]]:
    """跑一场（普通模式一局 / 计分赛三首连打），返回逐曲的原始记录。

    调用方负责先写好 config.toml；这里只负责跑、收分数、记胜负。
    """
    from src.core.game import OsuGame
    from src.utils.config import load_config, ConfigError

    random.seed(seed)
    try:
        with quiet():
            game = OsuGame(load_config(config_path))
            game.run()
    except (ConfigError, OSError, RuntimeError) as error:
        print(f"警告：{team_a.name} vs {team_b.name} 跑失败（{error}）")
        return None

    records = game.match.round_records
    if not records:
        return None

    rows: List[Dict[str, Any]] = []
    for index, record in enumerate(records):
        song_id = song_ids[index] if index < len(song_ids) else record.get("song_id", "?")
        row: Dict[str, Any] = {
            "song": song_id, "index": index, "seed": seed,
            "a": team_a.name, "b": team_b.name,
            "score_a": record["teams"][0]["total"],
            "score_b": record["teams"][1]["total"],
            "players_a": [], "players_b": [],
        }
        for side in (0, 1):
            observed = game.match.teams[side].players
            for position, player in enumerate(observed):
                entry = {
                    "name": player.name,
                    "score": record["teams"][side]["players"][position][1],
                    "ability": {key: getattr(player, key) for key in ABILITY_KEYS},
                    "base": dict(player.base_abilities),
                }
                row["players_a" if side == 0 else "players_b"].append(entry)
        row["delta_base"] = [
            statistics.fmean([item["base"][key] for item in row["players_a"]])
            - statistics.fmean([item["base"][key] for item in row["players_b"]])
            for key in ABILITY_KEYS
        ]
        row["delta_score"] = row["score_a"] - row["score_b"]
        rows.append(row)

    # 整场胜负：计分赛按三首总分，普通模式就是那一局
    total_a = sum(row["score_a"] for row in rows)
    total_b = sum(row["score_b"] for row in rows)
    winner = team_a.name if total_a >= total_b else team_b.name
    loser = team_b.name if winner == team_a.name else team_a.name
    stats[winner]["wins"] += 1
    stats[loser]["losses"] += 1
    stats[winner]["points"] += 3.0
    stats[winner]["opponents"][loser] = stats[winner]["opponents"].get(loser, 0) + 1
    stats[loser]["opponents"].setdefault(winner, 0)
    for name in (team_a.name, team_b.name):
        stats[name]["rounds"] += 1
        stats[name]["match_scores"].append(total_a if name == team_a.name else total_b)
    for row in rows:
        # 逐曲胜负（预选赛看总分，但逐曲胜率也有参考价值）
        if row["score_a"] != row["score_b"]:
            stats[row["a"] if row["score_a"] > row["score_b"] else row["b"]]["rounds_won"] += 1
    return rows


def run(args: argparse.Namespace) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    teams, _header = load_roster(args.xlsx, sheet=args.sheet)
    if args.limit > 0:
        teams = teams[:args.limit]
    fill_abilities(teams, args.form_range)
    # 算好每名队员的综合实力分（lineup = best3 时按它挑人）
    build_report(teams, WEIGHT_PROFILES["overall"], lineup=3, form_range=args.form_range)
    team_by_name = {team.name: team for team in teams}

    maps: List[Tuple[str, str, str, float]] = []
    for part in args.maps.split(",") if args.maps else [f"{i[0]}" for i in DEFAULT_MAPS]:
        key = part.strip()
        entry = next((item for item in DEFAULT_MAPS if item[0] == key), None)
        if entry is None:
            print(f"警告：不认识的谱面代号 {key}，已跳过（可用："
                  f"{', '.join(item[0] for item in DEFAULT_MAPS)}）")
            continue
        folder = os.path.join(BASE_DIR, entry[1])
        picked = _find_beatmap(folder, args.difficulty)
        if picked is None:
            print(f"警告：{key} 的文件夹里没有可用 .osu（{folder}），已跳过")
            continue
        beatmap, od = picked
        maps.append((key, folder, beatmap, od))
    if not maps:
        print("没有可用的谱面，退出")
        return 1

    pairs = list(itertools.combinations(range(len(teams)), 2))
    # score 模式下"一场"= 三首连打，所以总场次就是配对数
    total_matches = len(pairs) if args.mode == "score" else len(pairs) * len(maps) * args.reps
    print(f"名单：{args.xlsx}")
    print(f"队伍：{len(teams)} 支，两两配对 {len(pairs)} 组")
    print(f"谱面（难度口径 {args.difficulty}）：")
    for song_id, _folder, beatmap, od in maps:
        print(f"  {song_id}: OD {od:g}  {beatmap}")
    print(f"计划：{len(pairs)} 组 × " + (
        f"{len(maps)} 首连打（计分赛，看总分）" if args.mode == "score"
        else f"{len(maps)} 谱 × {args.reps} 轮") + f"（换人口径 {args.lineup}）")
    if args.dry_run:
        return 0

    # 每场的随机种子写死：同样的参数跑多少次结果都一样
    seeds = [[[args.seed + (pair_index * 1000 + map_index) * 17 + rep
              for rep in range(args.reps)] for map_index in range(len(maps))]
             for pair_index in range(len(pairs))]

    used: Dict[str, set] = {team.name: set() for team in teams}
    os.makedirs(WORK_DIR, exist_ok=True)
    config_path = os.path.join(WORK_DIR, "config.toml")

    # 延迟导入：没有 pygame 也能跑 --dry-run；顺带把 pygame 的启动横幅摁掉
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    with quiet():
        from src.core.game import OsuGame                            # noqa: E402
        from src.utils.config import load_config, ConfigError        # noqa: E402

    # 结果累计
    stats: Dict[str, Dict[str, Any]] = {
        team.name: {"wins": 0, "losses": 0, "rounds_won": 0, "rounds": 0, "points": 0.0,
                    "opponents": {}, "lineups": set(), "match_scores": []}
        for team in teams
    }
    raw_records: List[Dict[str, Any]] = []   # 逐场原始数据，标定与离线分析都用它
    failures = 0
    started = time.perf_counter()
    done = 0
    raw_handle = None
    if args.raw:
        os.makedirs(os.path.dirname(os.path.abspath(args.raw)), exist_ok=True)
        raw_handle = open(args.raw, "w", encoding="utf-8")

    for pair_index, (index_a, index_b) in enumerate(pairs):
        team_a, team_b = teams[index_a], teams[index_b]
        if args.mode == "score":
            seed = args.seed + pair_index * 17
            lineup_rng = random.Random(seed ^ 0x5EED)
            players_a = _pick_lineup(team_a, args.lineup, lineup_rng, used, team_b)
            players_b = _pick_lineup(team_b, args.lineup, lineup_rng, used, team_a)
            if len(players_a) < 3 or len(players_b) < 3:
                failures += 1
                continue
            _write_score_config(config_path, maps, (team_a, team_b),
                                players_a, players_b, args.form_range)
            rows = _play_match(config_path, seed, [item[0] for item in maps],
                               team_a, team_b, players_a, players_b, stats)
            if rows is None:
                failures += 1
                continue
            for name in (team_a.name, team_b.name):
                stats[name]["lineups"].update(
                    players_a if name == team_a.name else players_b)
            for row in rows:
                raw_records.append(row)
                if raw_handle is not None:
                    raw_handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            done += 1
            if done % max(1, len(pairs) // 10) == 0:
                elapsed = time.perf_counter() - started
                rate = done / elapsed if elapsed > 0 else 0
                print(f"  进度 {done}/{len(pairs)} 组（{elapsed:.0f}s，{rate:.1f} 组/秒）")
            continue

        for map_index, (song_id, folder, beatmap, _od) in enumerate(maps):
            for rep in range(args.reps):
                seed = seeds[pair_index][map_index][rep]
                # 抽阵容用独立的随机流：和游戏内部的全局 random 分开，
                # 免得"抽了几个人"这件事影响判定结果的可复现性
                lineup_rng = random.Random(seed ^ 0x5EED)
                players_a = _pick_lineup(team_a, args.lineup, lineup_rng, used, team_b)
                players_b = _pick_lineup(team_b, args.lineup, lineup_rng, used, team_a)
                if len(players_a) < 3 or len(players_b) < 3:
                    failures += 1
                    continue
                _write_config(config_path, song_id, folder, (team_a, team_b),
                              players_a, players_b, args.form_range, beatmap)
                rows = _play_match(config_path, seed, [song_id],
                                   team_a, team_b, players_a, players_b, stats)
                if rows is None:
                    failures += 1
                    continue
                for name in (team_a.name, team_b.name):
                    stats[name]["lineups"].update(
                        players_a if name == team_a.name else players_b)
                for row in rows:
                    raw_records.append(row)
                    if raw_handle is not None:
                        raw_handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                done += 1
                if done % max(1, total_matches // 20) == 0:
                    elapsed = time.perf_counter() - started
                    rate = done / elapsed if elapsed > 0 else 0
                    print(f"  进度 {done}/{total_matches}（{elapsed:.0f}s，{rate:.1f} 场/秒）")

    elapsed = time.perf_counter() - started
    if raw_handle is not None:
        raw_handle.close()
    print(f"跑完 {done} 场（失败 {failures} 场），耗时 {elapsed:.0f}s")
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    if done == 0:
        print("一场都没跑成，检查谱面路径")
        return 1

    # ---- 按谱面标定权重 ----
    observations = _delta_rows_from_records(raw_records)
    calibration: Dict[str, Dict[str, Any]] = {}
    delta_rows: List[List[Any]] = []
    for song_id, rows in observations.items():
        if len(rows) < len(ABILITY_KEYS) + 1:
            continue
        coefficients, r2 = _least_squares(rows)
        weights = _normalize(coefficients)
        calibration[song_id] = {
            "coefficients": dict(zip(ABILITY_KEYS, (round(value, 4) for value in coefficients))),
            "weights": dict(zip(ABILITY_KEYS, (round(value, 2) for value in weights))),
            "r2": round(r2, 4),
            "samples": len(rows),
        }
        delta_rows.extend([[song_id] + list(features) + [target] for features, target in rows])
    if calibration:
        blend = {key: statistics.fmean(item["weights"][key] for item in calibration.values())
                 for key in ABILITY_KEYS}
        total = sum(blend.values()) or 1.0
        blend = {key: value / total * 100.0 for key, value in blend.items()}
    else:
        blend = dict(WEIGHT_PROFILES["overall"])
    blend = {key: round(value, 2) for key, value in blend.items()}

    # ---- 预测实力分（用标定权重）----
    predicted = sorted(
        ({"name": team.name,
          "composite": round(composite_of(
              {key: statistics.fmean([member.base[key] for member in team.players])
               for key in ABILITY_KEYS}, blend), 3),
          "lineup_names": [member.name for member in team.players[:3]]}
         for team in teams),
        key=lambda item: -item["composite"])
    for place, item in enumerate(predicted, start=1):
        item["rank"] = place

    # ---- 名次与置信区间 ----
    # 计分赛（预选）按"每场三首总分"排名 —— 和真实预选同口径：
    # 对手是同一批人，分高的排前面。
    if args.mode == "score":
        ranking = sorted(teams, key=lambda team: -statistics.fmean(stats[team.name]["match_scores"]))
    else:
        ranking = sorted(teams, key=lambda team: (-stats[team.name]["points"],
                                                  -stats[team.name]["wins"]))
    for place, team in enumerate(ranking, start=1):
        stats[team.name]["rank"] = place
        stats[team.name]["win_rate"] = stats[team.name]["wins"] / max(1, stats[team.name]["rounds"])
        scores = stats[team.name]["match_scores"]
        stats[team.name]["avg_score"] = statistics.fmean(scores) if scores else 0.0
        stats[team.name]["score_stdev"] = statistics.pstdev(scores) if len(scores) > 1 else 0.0

    # 逐曲平均分（预选三首各看一遍）
    per_song: Dict[str, Dict[str, List[float]]] = {
        song_id: {team.name: [] for team in teams} for song_id, _f, _b, _o in maps
    }
    for record in raw_records:
        song_bucket = per_song.get(record["song"])
        if song_bucket is None:
            continue
        song_bucket[record["a"]].append(record["score_a"])
        song_bucket[record["b"]].append(record["score_b"])

    # bootstrap：按"配对 × 谱面"整块重抽，重算总分与名次
    blocks: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for record in raw_records:
        blocks.setdefault((record["a"], record["b"], record["song"]), []).append(record)
    block_keys = list(blocks)
    bootstrap_rng = random.Random(args.seed + 999)
    rank_samples: Dict[str, List[int]] = {team.name: [] for team in teams}
    champion: Dict[str, int] = {team.name: 0 for team in teams}
    top3: Dict[str, int] = {team.name: 0 for team in teams}
    rounds = max(200, args.bootstrap)
    for _ in range(rounds):
        points = {team.name: 0.0 for team in teams}
        wins = {team.name: 0 for team in teams}
        totals = {team.name: 0.0 for team in teams}
        for _draw in range(len(block_keys)):
            for record in blocks[block_keys[bootstrap_rng.randrange(len(block_keys))]]:
                winner = record["a"] if record["score_a"] >= record["score_b"] else record["b"]
                points[winner] += 3.0
                wins[winner] += 1
                totals[record["a"]] += record["score_a"]
                totals[record["b"]] += record["score_b"]
        if args.mode == "score":
            order = sorted(teams, key=lambda team: -totals[team.name])
        else:
            order = sorted(teams, key=lambda team: (-points[team.name], -wins[team.name]))
        for place, team in enumerate(order, start=1):
            rank_samples[team.name].append(place)
        champion[order[0].name] += 1
        for team in order[:3]:
            top3[team.name] += 1

    for team in teams:
        samples = sorted(rank_samples[team.name])
        entry = stats[team.name]
        entry["rank_p05"] = samples[int(0.05 * (len(samples) - 1))]
        entry["rank_p95"] = samples[int(0.95 * (len(samples) - 1))]
        entry["rank_mean"] = round(statistics.fmean(samples), 2)
        entry["p_champion"] = round(champion[team.name] / rounds, 4)
        entry["p_top3"] = round(top3[team.name] / rounds, 4)

    payload = {
        "maps": [{"id": song_id, "folder": folder, "beatmap": beatmap, "od": od}
                 for song_id, folder, beatmap, od in maps],
        "reps": args.reps,
        "mode": args.mode,
        "lineup": args.lineup,
        "form_range": args.form_range,
        "seed": args.seed,
        "matches": done,
        "failures": failures,
        "elapsed_s": round(elapsed, 1),
        "weights": blend,
        "calibration": calibration,
        "predicted": predicted,
        "results": {
            team.name: {
                "rank": stats[team.name]["rank"],
                "wins": stats[team.name]["wins"],
                "losses": stats[team.name]["losses"],
                "win_rate": round(stats[team.name]["win_rate"], 4),
                "points": stats[team.name]["points"],
                "avg_score": round(stats[team.name]["avg_score"], 1),
                "score_stdev": round(stats[team.name]["score_stdev"], 1),
                "per_song": {song_id: round(statistics.fmean(per_song[song_id][team.name]), 1)
                             if per_song[song_id][team.name] else 0.0
                             for song_id, _f, _b, _o in maps},
                "rank_mean": stats[team.name]["rank_mean"],
                "rank_p05": stats[team.name]["rank_p05"],
                "rank_p95": stats[team.name]["rank_p95"],
                "p_champion": stats[team.name]["p_champion"],
                "p_top3": stats[team.name]["p_top3"],
                "lineups_used": sorted(stats[team.name]["lineups"]),
            }
            for team in teams
        },
        "pairwise": {
            team.name: stats[team.name]["opponents"] for team in teams
        },
        # 紧凑的标定原始数据：[谱面, 体力差, 手速差, 准度差, 稳定差, 心态差, 分数差]
        "delta_header": ["song"] + list(ABILITY_KEYS) + ["score_delta"],
        "delta_rows": delta_rows,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(f"模拟结果已写入: {os.path.abspath(args.out)}")

    # 控制台简报
    print("")
    if args.mode == "score":
        print("【预选排名预测】三首连打看总分（每队与另外 16 队各打一场，取平均总分排序）")
        header = ["#", "队伍", "平均总分"] + [f"{song_id}" for song_id, _f, _b, _o in maps] \
            + ["总分波动", "整场胜率", "第 1 概率", "名次区间"]
        rows = []
        for team in ranking:
            bucket = per_song
            entry = stats[team.name]
            rows.append([entry["rank"], team.name, f"{entry['avg_score']:,.0f}"]
                        + [f"{statistics.fmean(bucket[song_id][team.name]) if bucket[song_id][team.name] else 0:,.0f}"
                           for song_id, _f, _b, _o in maps]
                        + [f"{entry['score_stdev']:,.0f}",
                           f"{entry['win_rate'] * 100:.1f}%",
                           f"{entry['p_champion'] * 100:.1f}%",
                           f"{entry['rank_p05']}~{entry['rank_p95']}"])
    else:
        print("【循环赛名次】按积分（胜场×3）排序，括号内为 bootstrap 90% 名次区间")
        header = ["#", "队伍", "胜", "负", "胜率", "名次区间", "冠军概率", "前三概率"]
        rows = []
        for team in ranking:
            entry = stats[team.name]
            rows.append([entry["rank"], team.name, entry["wins"], entry["losses"],
                         f"{entry['win_rate'] * 100:.1f}%",
                         f"{entry['rank_p05']}~{entry['rank_p95']}",
                         f"{entry['p_champion'] * 100:.1f}%", f"{entry['p_top3'] * 100:.1f}%"])
    widths = [max(len(str(row[i])) for row in [header] + rows) for i in range(len(header))]
    print("  ".join(str(header[i]).ljust(widths[i]) for i in range(len(header))))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(row[i]).ljust(widths[i]) for i in range(len(header))))

    print("")
    print("【按谱面标定的能力权重】分数差对能力差做最小二乘（负系数截 0 后归一化）")
    header = ["谱面", "R²"] + [ABILITY_LABELS[key] for key in ABILITY_KEYS]
    rows = []
    for song_id, item in calibration.items():
        rows.append([song_id, f"{item['r2']:.3f}"]
                    + [f"{item['weights'][key]:.1f}%" for key in ABILITY_KEYS])
    rows.append(["平均", ""] + [f"{blend[key]:.1f}%" for key in ABILITY_KEYS])
    widths = [max(len(str(row[i])) for row in [header] + rows) for i in range(len(header))]
    print("  ".join(str(header[i]).ljust(widths[i]) for i in range(len(header))))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(row[i]).ljust(widths[i]) for i in range(len(header))))

    print("")
    print("【纯名单预测名次】用标定权重给 5 人平均能力打分（不含任何对局信息）")
    print("  " + "  ".join(f"{item['rank']}.{item['name']}({item['composite']})"
                           for item in predicted))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="无 UI 循环赛：给队伍实力做实证标定")
    parser.add_argument("--xlsx", default=os.path.join(BASE_DIR, DEFAULT_XLSX))
    parser.add_argument("--sheet", type=int, default=1)
    parser.add_argument("--maps", default="", help="谱面代号，逗号分隔（默认 RC1,LN1,HB1）")
    parser.add_argument("--mode", default="round", choices=("round", "score"),
                        help="round = 每首歌单独一局（默认）；score = 三首连打看总分（预选赛）")
    parser.add_argument("--difficulty", default="hardest",
                        help="同曲包多难度时挑哪个：hardest（默认）/ easiest / 文件名子串（如 MASTER）")
    parser.add_argument("--reps", type=int, default=2, help="每对每谱打几轮（默认 2）")
    parser.add_argument("--lineup", default="first3",
                        help="换人口径：first3 / best3 / simple / random / all5 / 名字,名字,名字")
    parser.add_argument("--form-range", "-f", type=int, default=20,
                        help="手感幅度（默认 20，与 config.toml 一致）")
    parser.add_argument("--seed", type=int, default=20240601)
    parser.add_argument("--bootstrap", type=int, default=1000, help="bootstrap 重抽次数")
    parser.add_argument("-o", "--out", default=DEFAULT_OUT)
    parser.add_argument("--raw", default="", help="把每一场的原始分数/能力写成 JSONL，便于离线分析")
    parser.add_argument("--recalibrate", action="store_true",
                        help="只拿 --raw 的原始记录重新标定权重并写回 -o 的 JSON（不重跑比赛）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不真跑")
    parser.add_argument("--limit", type=int, default=0,
                        help="只用前 N 支队（冒烟测试用，0 = 全部）")
    args = parser.parse_args(argv)

    try:
        if args.recalibrate:
            return recalibrate(args)
        return run(args)
    except (OSError, ValueError) as error:
        print(f"跑模拟失败：{error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

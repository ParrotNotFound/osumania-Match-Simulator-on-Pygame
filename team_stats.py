#!/usr/bin/env python3
# team_stats.py
"""独立小工具：读 data/teams.xlsx，算出每支队**所有队员**（不含教练）的能力值并做统计。

不依赖 pygame、不读谱面、不启动游戏 —— 能力值的生成逻辑直接复用
`src/entities/player.py`，所以这里看到的结果和游戏里同名选手打出来的完全一致
（基准与风格只由名字哈希决定，每局都一样）。

读表规则（表头自己认，列顺序随便换）：
    队名 / 队长 / 队员1 / 队员2 / 替补1 / 替补2 / 教练
  - `队名` 是队名列，`教练` 列**跳过不算**；
  - 其余非空的名字列都算队员（队长、队员、替补一视同仁）；
  - 表头名字换了也行：认「队」开头的列当队名、「教练/领队/manager」当教练列。

统计口径：
  - **基准** = 名字哈希 + 风格偏置，同一名字每局完全一样（这就是"底子"）；
  - **手感** = 每局在 ±swing 内浮动体力/手速/准度三项，
    swing = form_range × (1 − 0.75 × 稳定/100)，越稳的人越不大起大落；
  - **综合实力分** = 五项能力按权重求和，权重直接取 README 记录的实测影响
    （高难谱上 手速 34.2pp / 体力 10.8pp / 心态 4~10 倍失误差 / 准度 6~10pp /
    稳定 1.4pp），默认档见 `WEIGHT_PROFILES`，可用 `--profile` 换口径。

用法：
    python team_stats.py                        # 读 data/teams.xlsx，打印全表 + 写 xlsx
    python team_stats.py --xlsx 别的表.xlsx     # 指定名单
    python team_stats.py -o out.xlsx --md 报告.md
    python team_stats.py --profile speed        # 换评价口径（overall/aim/speed）
    python team_stats.py --rolls 5              # 额外看 5 局手感抽样
    python team_stats.py --no-write             # 只打印，不写文件
    python team_stats.py --sim                  # 顺带跑无 UI 对战模拟（见 team_sim.py）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ----------------------------------------------------------------------
# 路径与依赖
# ----------------------------------------------------------------------
def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


PROJECT_ROOT = _base_dir()
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.entities.player import (  # noqa: E402
    ABILITY_KEYS, ABILITY_LABELS, BASE_MAX, BASE_MIN, SPEED_GAP_MAX, SPEED_GAP_MIN,
    STYLES, Player, timing_sigma_for,
)
from src.utils.config import DEFAULT_CONFIG_NAME  # noqa: E402

DEFAULT_XLSX = os.path.join("data", "teams.xlsx")
DEFAULT_OUT = os.path.join("data", "team_stats.xlsx")
DEFAULT_MD = os.path.join("data", "team_stats.md")

# 权重口径：五项能力的相对发言权（会归一化，所以只关心比例）。
# 依据是 README「五项能力的分工」那张实测表 —— 每项后面的注释就是它的实测影响。
WEIGHT_PROFILES: Dict[str, Dict[str, float]] = {
    # 综合：按高难谱与低难谱的影响折中，速度最重、心态次之、稳定最轻
    "overall": {
        "speed": 35,          # 高难谱实测 34.2pp（低难 3.5pp）
        "stamina": 22,        # 高难谱实测 10.8pp（低难 2.0pp）
        "mentality": 18,      # 小失误/手抖次数差 4~10 倍
        "avg_accuracy": 15,   # 简单谱大 P 率 20~40pp / 高难 6~10pp
        "consistency": 10,    # 松手精度 1.4pp + 决定手感幅度
    },
    # 极限图：长连打、后半段才是分水岭
    "speed": {"speed": 45, "stamina": 30, "mentality": 12, "avg_accuracy": 5, "consistency": 8},
    # 简单图：比的是谁贴得准、谁关键分不手紧
    "aim": {"avg_accuracy": 40, "mentality": 25, "speed": 15, "stamina": 5, "consistency": 15},
}


# ----------------------------------------------------------------------
# xlsx 读入（不依赖第三方库：.xlsx 就是个 zip）
# ----------------------------------------------------------------------
def _column_index(reference: str) -> int:
    letters = "".join(ch for ch in reference if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
    return index - 1


def _shared_strings(archive: zipfile.ZipFile) -> List[str]:
    """共享字符串表：Excel / WPS 写出来的文本大多在这里，只读 inlineStr 会读成空。"""
    import xml.etree.ElementTree as ET

    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    table: List[str] = []
    for item in root.iter(f"{namespace}si"):
        # 富文本会被拆成多个 <t>（<r><t>…</t></r>），全部拼起来
        table.append("".join(node.text or "" for node in item.iter(f"{namespace}t")))
    return table


def _sheet_names(archive: zipfile.ZipFile) -> List[str]:
    import xml.etree.ElementTree as ET

    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        root = ET.fromstring(archive.read("xl/workbook.xml"))
    except KeyError:
        return []
    return [sheet.get("name", f"Sheet{index + 1}")
            for index, sheet in enumerate(root.iter(f"{namespace}sheet"))]


def read_xlsx_rows(path: str, sheet: int = 1) -> List[List[Any]]:
    """读第 `sheet` 张表（1 起），返回二维单元格（文本 str / 数字 int|float）。

    比 src/utils/excel.py 的 read_rows_typed 多了对 **sharedStrings** 的支持：
    项目自己写出来的表用 inlineStr，但 Excel/WPS 存的表用共享字符串，
    而 teams.xlsx 正是后者。
    """
    import xml.etree.ElementTree as ET

    if sheet < 1:
        raise ValueError("sheet 从 1 开始数")
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        try:
            blob = archive.read(f"xl/worksheets/sheet{sheet}.xml")
        except KeyError:
            return []
        root = ET.fromstring(blob)

    rows: List[List[Any]] = []
    for row in root.iter(f"{namespace}row"):
        cells: List[Any] = []
        for cell in row.iter(f"{namespace}c"):
            kind = cell.get("t")
            if kind == "s":
                number = cell.find(f"{namespace}v")
                try:
                    index = int(number.text) if number is not None and number.text else -1
                except ValueError:
                    index = -1
                value: Any = shared[index] if 0 <= index < len(shared) else ""
            elif kind == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter(f"{namespace}t"))
            else:
                node = cell.find(f"{namespace}v")
                if node is None or node.text is None:
                    value = ""
                else:
                    try:
                        number_value = float(node.text)
                        value = int(number_value) if number_value.is_integer() else number_value
                    except ValueError:
                        value = node.text
            column = _column_index(cell.get("r", ""))
            while len(cells) < column:
                cells.append("")
            cells.append(value)
        rows.append(cells)

    # 去掉尾部整行空白（Excel 常留一堆空行）
    while rows and not any(str(cell).strip() for cell in rows[-1]):
        rows.pop()
    return rows


# ----------------------------------------------------------------------
# 名单
# ----------------------------------------------------------------------
COACH_HINTS = ("教练", "领队", "manager", "coach", "监督")
TEAM_HINTS = ("队名", "队伍", "战队", "team")


@dataclass
class PlayerRow:
    name: str
    team: str
    team_index: int
    role: str
    style: str
    base: Dict[str, int]
    form_swing: int
    column: str = ""
    # 统计阶段填：composite / mean / sigma / gap / release_sigma / choke_risk
    derived: Dict[str, float] = field(default_factory=dict)
    rank: int = 0

    def value(self, key: str) -> int:
        return self.base[key]

    @property
    def overall_mean(self) -> float:
        return sum(self.base[key] for key in ABILITY_KEYS) / len(ABILITY_KEYS)


@dataclass
class TeamRow:
    name: str
    index: int
    players: List[PlayerRow] = field(default_factory=list)
    # 统计结果（build_report 里填）
    stats: Dict[str, Any] = field(default_factory=dict)


def _looks_like_header(row: Sequence[Any]) -> bool:
    for cell in row:
        text = str(cell).strip()
        if any(hint in text for hint in TEAM_HINTS):
            return True
    return False


def _is_coach_column(header: str) -> bool:
    text = header.strip().lower()
    return any(hint.lower() in text for hint in COACH_HINTS)


def _is_team_column(header: str) -> bool:
    text = header.strip().lower()
    return any(hint.lower() in text for hint in TEAM_HINTS)


def load_roster(path: str, sheet: int = 1) -> Tuple[List[TeamRow], List[str]]:
    """读名单表，返回 (队伍列表, 表头)。教练列会被跳过。"""
    rows = read_xlsx_rows(path, sheet=sheet)
    if not rows:
        raise ValueError(f"{path} 里读不到任何内容")

    header_index = next((i for i, row in enumerate(rows) if _looks_like_header(row)), None)
    if header_index is None:
        raise ValueError(f"{path} 里找不到表头（需要有一个含「队名」的列）")

    header = [str(cell).strip() for cell in rows[header_index]]
    team_column = next((i for i, text in enumerate(header) if _is_team_column(text)), None)
    if team_column is None:
        raise ValueError("表头里没有队名列")

    name_columns = [i for i, text in enumerate(header)
                    if i != team_column and text and not _is_coach_column(text)]

    teams: List[TeamRow] = []
    by_name: Dict[str, TeamRow] = {}
    for row in rows[header_index + 1:]:
        cells = list(row) + [""] * (len(header) - len(row))
        team_name = str(cells[team_column]).strip()
        if not team_name:
            continue
        if team_name not in by_name:
            team = TeamRow(name=team_name, index=len(teams) + 1)
            by_name[team_name] = team
            teams.append(team)
        team = by_name[team_name]
        for column in name_columns:
            name = str(cells[column]).strip()
            if not name:
                continue
            team.players.append(PlayerRow(
                name=name, team=team_name, team_index=team.index,
                role=header[column] or f"列{column + 1}", style="", base={},
                form_swing=0, column=header[column],
            ))

    if not teams:
        raise ValueError(f"{path} 里没有读到任何队伍")

    # 同队重名要说一声：能力值只由名字哈希决定，同名 = 数值完全一样，
    # 大概率是表格里重复填了同一个人（还是两个人真的同名，得自己确认）。
    for team in teams:
        names = [member.name for member in team.players]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            print(f"注意：{team.name} 的名单里有重名队员 {'、'.join(repeated)}"
                  f"—— 同名能力值完全相同，确认一下是不是重复填写")
    return teams, header


# ----------------------------------------------------------------------
# 能力值
# ----------------------------------------------------------------------
def fill_abilities(teams: Sequence[TeamRow], form_range: int) -> None:
    """给每个队员算基准能力值（直接复用 Player，结果与游戏/ability.py 完全一致）。"""
    for team in teams:
        for member in team.players:
            player = Player(member.name, 0, 0, form_range=0)      # 手感 0 = 纯基准
            member.base = dict(player.base_abilities)
            member.style = player.style
            # form_swing 必须用「带手感重掷一次」才会算出来，直接读 form=0 的实例永远是 0
            member.form_swing = Player(member.name, 0, 0, form_range=form_range).form_swing


def composite_of(base: Dict[str, int], weights: Dict[str, float]) -> float:
    total = sum(weights.values()) or 1.0
    return sum(base[key] * weights.get(key, 0.0) for key in ABILITY_KEYS) / total


def speed_gap(speed: int) -> float:
    """手速 → 能"从容处理"的同键间隔（毫秒），越小越能吃密谱。"""
    return SPEED_GAP_MAX - (SPEED_GAP_MAX - SPEED_GAP_MIN) * (speed / 100.0)


def accuracy_sigma(accuracy: int) -> float:
    """准度 → 落点误差地板 σ（毫秒），和游戏判定用的是同一个函数。"""
    return timing_sigma_for(accuracy)


def release_sigma(accuracy: int, consistency: int) -> float:
    """长条**松手**判定的 σ：准度地板 + 稳定性那一份（见 player._release_offset）。"""
    from src.entities.player import CONSISTENCY_SIGMA_ADD, LONG_RELEASE_SIGMA_SCALE
    return (accuracy_sigma(accuracy) + CONSISTENCY_SIGMA_ADD * (1.0 - consistency / 100.0)
            ) * LONG_RELEASE_SIGMA_SCALE


def choke_risk(mentality: int) -> float:
    """心态脆性 = 1 − 心态/100：手紧/手抖的概率放大器（0 最好）。"""
    return max(0.0, 1.0 - mentality / 100.0)


# ----------------------------------------------------------------------
# 统计
# ----------------------------------------------------------------------
def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def describe(values: Sequence[float]) -> Dict[str, float]:
    """一组数的常用统计量。"""
    if not values:
        return {}
    return {
        "n": len(values),
        "mean": _mean(values),
        "median": statistics.median(values),
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
        "range": max(values) - min(values),
        "p25": _percentile(values, 0.25),
        "p75": _percentile(values, 0.75),
    }


def build_report(teams: List[TeamRow], weights: Dict[str, float], lineup: int = 3,
                 form_range: int = 20, seed: int = 20240601) -> Dict[str, Any]:
    """把名单算成"可以打印/导出"的一份完整统计。

    lineup 是每场实际上场人数（本项目是 3v3）：除了 5 人全员口径，
    另外按名单顺序取前 lineup 人当"首发"，用来估计换人深度。
    """
    players: List[PlayerRow] = [member for team in teams for member in team.players]

    # ---- 个人派生值 ----
    for member in players:
        base = member.base
        member.derived = {
            "composite": composite_of(base, weights),
            "mean": member.overall_mean,
            "sigma": accuracy_sigma(base["avg_accuracy"]),
            "gap": speed_gap(base["speed"]),
            "release_sigma": release_sigma(base["avg_accuracy"], base["consistency"]),
            "choke_risk": choke_risk(base["mentality"]),
        }

    # ---- 队伍统计 ----
    for team in teams:
        team_players = team.players
        composites = [member.derived["composite"] for member in team_players]
        means = [member.overall_mean for member in team_players]
        per_ability = {key: [member.base[key] for member in team_players] for key in ABILITY_KEYS}
        lineup_members = team_players[:lineup]
        lineup_composites = [member.derived["composite"] for member in lineup_members]

        stats: Dict[str, Any] = {
            "count": len(team_players),
            "lineup": len(lineup_members),
            "composite": _mean(composites),
            "composite_lineup": _mean(lineup_composites),
            "mean": _mean(means),
            "median": statistics.median(means) if means else 0.0,
            "stdev": statistics.pstdev(composites) if len(composites) > 1 else 0.0,
            "range": (max(composites) - min(composites)) if composites else 0.0,
            "best": max(lineup_members, key=lambda m: m.derived["composite"]) if lineup_members else None,
            "weakest": min(lineup_members, key=lambda m: m.derived["composite"]) if lineup_members else None,
            "ability_mean": {key: _mean(per_ability[key]) for key in ABILITY_KEYS},
            "ability_max": {key: max(per_ability[key]) for key in ABILITY_KEYS},
            "ability_min": {key: min(per_ability[key]) for key in ABILITY_KEYS},
            "ability_stdev": {key: (statistics.pstdev(per_ability[key])
                                    if len(per_ability[key]) > 1 else 0.0)
                              for key in ABILITY_KEYS},
            "sigma": _mean([member.derived["sigma"] for member in lineup_members]),
            "gap": _mean([member.derived["gap"] for member in lineup_members]),
            "choke_risk": _mean([member.derived["choke_risk"] for member in lineup_members]),
            "release_sigma": _mean([member.derived["release_sigma"] for member in lineup_members]),
            "style_count": len({member.style for member in team_players}),
        }
        # 深度：把替补也算进来能让队伍强多少（全 5 人 vs 首发 3 人）
        stats["depth"] = stats["composite"] - stats["composite_lineup"]
        # 木桶效应：首发里最弱的一位（3v3 里一个人的崩盘直接进总分）
        stats["floor"] = min(lineup_composites) if lineup_composites else 0.0
        team.stats = stats

    # ---- 排名 ----
    ranking = sorted(teams, key=lambda t: -t.stats["composite"])
    for place, team in enumerate(ranking, start=1):
        team.stats["rank"] = place
    player_ranking = sorted(players, key=lambda m: -m.derived["composite"])
    for place, member in enumerate(player_ranking, start=1):
        member.rank = place

    # ---- 手感抽样：同一份名单反复重掷，看名次有多稳 ----
    rng = random.Random(seed)
    champion: Dict[str, int] = {team.name: 0 for team in teams}
    top3: Dict[str, int] = {team.name: 0 for team in teams}
    rounds = 2000
    for _ in range(rounds):
        scores: List[Tuple[float, str]] = []
        for team in teams:
            total = 0.0
            for member in team.players[:lineup] or team.players:
                base = member.base
                swing = member.form_swing
                rolled = dict(base)
                for key in ("stamina", "speed", "avg_accuracy"):
                    if swing > 0:
                        rolled[key] = max(0, min(100, base[key] + rng.randint(-swing, swing)))
                total += composite_of(rolled, weights)
            scores.append((total / max(1, len(team.players[:lineup] or team.players)), team.name))
        scores.sort(reverse=True)
        champion[scores[0][1]] += 1
        for _, name in scores[:3]:
            top3[name] += 1

    for team in teams:
        team.stats["p_champion"] = champion[team.name] / rounds
        team.stats["p_top3"] = top3[team.name] / rounds

    # ---- 全局分布 ----
    distribution = {key: describe([member.base[key] for member in players]) for key in ABILITY_KEYS}
    distribution["__composite__"] = describe([member.derived["composite"] for member in players])
    distribution["__team__"] = describe([team.stats["composite"] for team in teams])

    return {
        "players": players,
        "teams": teams,
        "ranking": ranking,
        "player_ranking": player_ranking,
        "distribution": distribution,
        "weights": dict(weights),
        "lineup": lineup,
        "form_range": form_range,
    }


# ----------------------------------------------------------------------
# 打印
# ----------------------------------------------------------------------
def _display_width(text: object) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(text))


def _pad(text: object, width: int, align: str = "left") -> str:
    text = str(text)
    space = " " * max(0, width - _display_width(text))
    return space + text if align == "right" else text + space


def table_lines(header: Sequence[str], rows: Sequence[Sequence[Any]],
                right_from: int = 1, aligns: Optional[Sequence[str]] = None) -> List[str]:
    """把二维数据排成对齐的文本行（中日韩字符按 2 格宽算）。"""
    widths = [max(_display_width(header[i]), *(_display_width(row[i]) for row in rows))
              for i in range(len(header))]

    def render(cells: Sequence[Any]) -> str:
        parts = []
        for index, cell in enumerate(cells):
            align = (aligns[index] if aligns else ("right" if index >= right_from else "left"))
            parts.append(_pad(cell, widths[index], align))
        return "  ".join(parts).rstrip()

    return [render(header), "  ".join("-" * width for width in widths)] + \
        [render(row) for row in rows]


def print_table(header: Sequence[str], rows: Sequence[Sequence[Any]],
                right_from: int = 1, aligns: Optional[Sequence[str]] = None) -> None:
    for line in table_lines(header, rows, right_from, aligns):
        print(line)


def format_report(report: Dict[str, Any], top: int = 0, rolls: int = 0,
                  seed: int = 20240601) -> List[str]:
    """把统计结果排成一份完整的文字报告（返回文本行，打印/写文件共用）。"""
    teams: List[TeamRow] = report["teams"]
    players: List[PlayerRow] = report["players"]
    ranking: List[TeamRow] = report["ranking"]
    weights: Dict[str, float] = report["weights"]
    lineup: int = report["lineup"]
    form_range: int = report["form_range"]

    keys = list(ABILITY_KEYS)
    abilities = [ABILITY_LABELS[key] for key in keys]
    out: List[str] = []
    add = out.append

    add("=" * 96)
    add(f"队伍名单能力值统计 · {len(teams)} 支队 / {len(players)} 名队员（教练不计）")
    add("=" * 96)
    weight_text = "  ".join(f"{ABILITY_LABELS[key]} {weights.get(key, 0):g}%" for key in keys)
    add(f"综合实力分权重（{_profile_name(weights)}）：{weight_text}")
    add(f"手感：±{form_range} × (1 − 0.75×稳定/100)，只浮动体力/手速/准度")
    add(f"基准区间：{BASE_MIN}~{BASE_MAX} + 风格偏置（风格表见 ability.py --styles）")
    add("")

    sim = report.get("sim") or {}
    sim_results: Dict[str, Any] = sim.get("results", {}) if isinstance(sim, dict) else {}

    # ---- 队伍排名 ----
    add("【队伍排名】按 5 名队员的综合实力分均值（括号内是首发 3 人）")
    if sim_results:
        header = ["#", "队伍", "人数", "综合分", "首发3人", "最低", "标准差",
                  "冠军概率", "前三概率", "模拟胜率", "名次区间"] + abilities
    else:
        header = ["#", "队伍", "人数", "综合分", "首发3人", "最低", "标准差",
                  "冠军概率", "前三概率"] + abilities
    rows = []
    for team in ranking:
        stats = team.stats
        cells = [stats["rank"], team.name, stats["count"], f"{stats['composite']:.1f}",
                 f"{stats['composite_lineup']:.1f}", f"{stats['floor']:.1f}",
                 f"{stats['stdev']:.1f}",
                 f"{stats['p_champion'] * 100:.1f}%", f"{stats['p_top3'] * 100:.1f}%"]
        if sim_results:
            entry = sim_results.get(team.name, {})
            cells += [f"{entry.get('win_rate', 0) * 100:.1f}%",
                      f"{entry.get('rank_p05', 0)}~{entry.get('rank_p95', 0)}"]
        cells += [f"{stats['ability_mean'][key]:.1f}" for key in keys]
        rows.append(cells)
    out.extend(table_lines(header, rows, right_from=2,
                           aligns=["right", "left"] + ["right"] * (len(header) - 2)))
    add("")

    # ---- 模拟对标（可选）----
    if sim_results:
        add(f"【对战模拟】{sim.get('matches', '?')} 场全循环（"
            f"{'、'.join(item['id'] + f"(OD {item.get('od', 0):g})" for item in sim.get('maps', []))}，"
            f"每对每谱 {sim.get('reps', '?')} 轮，换人口径 {sim.get('lineup', '?')}）")
        header = ["队伍", "名单名次", "模拟名次", "模拟胜率", "冠军概率", "名次区间"]
        rows = []
        order = sorted(ranking, key=lambda t: sim_results.get(t.name, {}).get("rank", 99))
        for team in order:
            entry = sim_results.get(team.name, {})
            rows.append([team.name, team.stats["rank"], entry.get("rank", "?"),
                         f"{entry.get('win_rate', 0) * 100:.1f}%",
                         f"{entry.get('p_champion', 0) * 100:.1f}%",
                         f"{entry.get('rank_p05', 0)}~{entry.get('rank_p95', 0)}"])
        out.extend(table_lines(header, rows, right_from=1,
                               aligns=["left"] + ["right"] * (len(header) - 1)))
        calibration = sim.get("calibration", {})
        if calibration:
            add("")
            add("  引擎实测的能力权重（把分数差对能力差做最小二乘，负系数截 0）")
            header = ["谱面", "R²"] + abilities
            rows = [[song_id, f"{item['r2']:.3f}"]
                    + [f"{item['weights'][key]:.1f}%" for key in keys]
                    for song_id, item in calibration.items()]
            rows.append(["平均", ""] + [f"{sim.get('weights', {}).get(key, 0):.1f}%" for key in keys])
            out.extend(table_lines(header, rows, right_from=1,
                                   aligns=["left", "right"] + ["right"] * len(keys)))
        add("")

    # ---- 个人排名 ----
    shown = players if top <= 0 else players[:top]
    add(f"【选手实力榜】{'(全部 ' + str(len(players)) + ' 名)' if top <= 0 else '前 ' + str(top) + ' 名'}")
    header = ["#", "选手", "队伍", "位置", "风格"] + abilities + ["综合分", "手感", "σ(ms)"]
    rows = []
    for member in shown:
        rows.append([member.rank, member.name, member.team, member.role, member.style]
                    + [member.base[key] for key in keys]
                    + [f"{member.derived['composite']:.1f}",
                       f"±{member.form_swing}", f"{member.derived['sigma']:.1f}"])
    out.extend(table_lines(header, rows, right_from=5,
                           aligns=["right", "left", "left", "left", "left"]
                           + ["right"] * (len(header) - 5)))
    add("")

    # ---- 单项之王 ----
    add("【单项榜首】每项能力的个人/队伍最高")
    header = ["能力", "个人最高", "数值", "队伍最高", "队均值最高", "全局均值", "全局区间"]
    rows = []
    dist = report["distribution"]
    for key in keys:
        best_player = max(players, key=lambda m: m.base[key])
        best_team = max(teams, key=lambda t: t.stats["ability_max"][key])
        best_avg = max(teams, key=lambda t: t.stats["ability_mean"][key])
        rows.append([ABILITY_LABELS[key], best_player.name, best_player.base[key],
                     f"{best_team.name} {best_team.stats['ability_max'][key]}",
                     f"{best_avg.name} {best_avg.stats['ability_mean'][key]:.1f}",
                     f"{dist[key]['mean']:.1f}",
                     f"{dist[key]['min']:.0f}~{dist[key]['max']:.0f}"])
    out.extend(table_lines(header, rows, right_from=2,
                           aligns=["left", "left", "right", "left", "left", "right", "right"]))
    add("")

    # ---- 分布 ----
    add("【全局分布】全部选手的五项能力")
    header = ["统计量"] + abilities + ["综合分"]
    rows = []
    for label, field_name in (("均值", "mean"), ("中位数", "median"), ("标准差", "stdev"),
                              ("最小", "min"), ("P25", "p25"), ("P75", "p75"), ("最大", "max")):
        cells = [label]
        for key in keys:
            cells.append(f"{dist[key][field_name]:.1f}")
        cells.append(f"{dist['__composite__'][field_name]:.1f}")
        rows.append(cells)
    out.extend(table_lines(header, rows))
    add("")

    # ---- 手感抽样（可选）----
    if rolls > 0:
        add(f"【手感抽样】同一份名单重掷 {rolls} 局（只看基准浮动，队伍名次会跟着变）")
        rng = random.Random(seed)
        header = ["#", "队伍"] + abilities + ["综合分"]
        for index in range(1, rolls + 1):
            rows = []
            for team in teams:
                rolled = []
                values: Dict[str, int] = {}
                for member in team.players:
                    values = dict(member.base)
                    for key in ("stamina", "speed", "avg_accuracy"):
                        if member.form_swing > 0:
                            values[key] = max(0, min(100, member.base[key]
                                                     + rng.randint(-member.form_swing, member.form_swing)))
                    rolled.append(composite_of(values, weights))
                rows.append((_mean(rolled), team.name, values))
            rows.sort(key=lambda item: -item[0])
            add(f"  -- 第 {index} 局")
            body = [[place, name] + [values[key] for key in keys] + [f"{score:.1f}"]
                    for place, (score, name, values) in enumerate(rows, start=1)]
            out.extend(table_lines(header, body, right_from=2,
                                   aligns=["right", "left"] + ["right"] * (len(header) - 2)))
        add("")

    # ---- 逐队明细 ----
    add("【首发 vs 最强三人】名单顺序不等于实力顺序：")
    add("   首发 = 名单里前 3 个（队长/队员1/队员2）；最强三人 = 综合分最高的 3 人")
    header = ["队伍", "首发分", "最强三人分", "差值", "当前首发", "最强三人"]
    rows = []
    by_gap = []
    for team in teams:
        first = team.stats["composite_lineup"]
        ranked = sorted(team.players, key=lambda m: -m.derived["composite"])
        best3 = _mean([m.derived["composite"] for m in ranked[:3]])
        by_gap.append((best3 - first, team, first, best3, ranked))
    for gap, team, first, best3, ranked in sorted(by_gap, key=lambda item: -item[0]):
        rows.append([team.name, f"{first:.1f}", f"{best3:.1f}", f"{gap:+.1f}",
                     "/".join(m.name for m in team.players[:3]),
                     "/".join(m.name for m in ranked[:3])])
    out.extend(table_lines(header, rows, right_from=1,
                           aligns=["left"] + ["right"] * 3 + ["left", "left"]))
    add("")

    # ---- 原始队员明细（按队伍）----
    add("【逐队明细】")
    for team in teams:
        stats = team.stats
        add(f"  {team.name}（{stats['count']} 人，综合 {stats['composite']:.1f}，"
            f"首发 {stats['composite_lineup']:.1f}，标准差 {stats['stdev']:.1f}）")
        header = ["位置", "选手", "风格"] + abilities + ["综合分", "手感"]
        rows = [[member.role, member.name, member.style]
                + [member.base[key] for key in keys]
                + [f"{member.derived['composite']:.1f}", f"±{member.form_swing}"]
                for member in team.players]
        out.extend(table_lines(header, rows, right_from=3,
                               aligns=["left", "left", "left"]
                               + ["right"] * (len(header) - 3)))
        add("")
    return out


def _profile_name(weights: Dict[str, float]) -> str:
    for name, candidate in WEIGHT_PROFILES.items():
        if candidate == weights:
            return name
    return "自定义"


# ----------------------------------------------------------------------
# 导出
# ----------------------------------------------------------------------
def write_excel(report: Dict[str, Any], path: str) -> str:
    from src.utils.excel import write_sheets

    keys = list(ABILITY_KEYS)
    abilities = [ABILITY_LABELS[key] for key in keys]
    teams: List[TeamRow] = report["teams"]
    players: List[PlayerRow] = report["players"]
    ranking: List[TeamRow] = report["ranking"]
    dist = report["distribution"]

    player_header = ["队内排名", "选手", "队伍", "位置", "风格"] + abilities + \
        ["综合分", "均值", "手感", "松手σ(ms)", "跟得上间隔(ms)", "心态脆性"]
    player_rows: List[List[Any]] = [player_header]
    for team in teams:
        ordered = sorted(team.players, key=lambda m: -m.derived["composite"])
        for place, member in enumerate(ordered, start=1):
            player_rows.append([place, member.name, member.team, member.role, member.style]
                               + [member.base[key] for key in keys]
                               + [round(member.derived["composite"], 2),
                                  round(member.overall_mean, 2), member.form_swing,
                                  round(member.derived["release_sigma"], 2),
                                  round(member.derived["gap"], 1),
                                  round(member.derived["choke_risk"], 3)])

    team_header = (["排名", "队伍", "人数"] + [f"{label}均值" for label in abilities]
                   + ["综合分", "首发综合分", "替补深度", "首发最低分", "标准差", "极差",
                      "冠军概率", "前三概率"]
                   + [f"{label}最高" for label in abilities]
                   + [f"{label}最低" for label in abilities])
    team_rows: List[List[Any]] = [team_header]
    for team in ranking:
        stats = team.stats
        team_rows.append([stats["rank"], team.name, stats["count"]]
                         + [round(stats["ability_mean"][key], 2) for key in keys]
                         + [round(stats["composite"], 2), round(stats["composite_lineup"], 2),
                            round(stats["depth"], 2), round(stats["floor"], 2),
                            round(stats["stdev"], 2), round(stats["range"], 2),
                            round(stats["p_champion"], 4), round(stats["p_top3"], 4)]
                         + [stats["ability_max"][key] for key in keys]
                         + [stats["ability_min"][key] for key in keys])

    dist_header = ["统计量"] + abilities + ["综合分"]
    dist_rows: List[List[Any]] = [dist_header]
    for label, field_name in (("均值", "mean"), ("中位数", "median"), ("标准差", "stdev"),
                              ("最小", "min"), ("P25", "p25"), ("P75", "p75"), ("最大", "max")):
        dist_rows.append([label] + [round(dist[key][field_name], 3) for key in keys]
                         + [round(dist["__composite__"][field_name], 3)])

    lead_rows: List[List[Any]] = [["能力", "个人最高", "所属队伍", "数值", "队伍均值最高", "数值",
                                  "全局均值", "全局最低", "全局最高"]]
    for key in keys:
        best_player = max(players, key=lambda m: m.base[key])
        best_avg = max(teams, key=lambda t: t.stats["ability_mean"][key])
        lead_rows.append([ABILITY_LABELS[key], best_player.name, best_player.team,
                          best_player.base[key], best_avg.name,
                          round(best_avg.stats["ability_mean"][key], 2),
                          round(dist[key]["mean"], 2), dist[key]["min"], dist[key]["max"]])

    weight_rows: List[List[Any]] = [["口径", "说明"]]
    for name, weights in WEIGHT_PROFILES.items():
        weight_rows.append([name, "  ".join(f"{ABILITY_LABELS[key]}:{weights.get(key, 0):g}%"
                                            for key in keys)])
    weight_rows.append(["当前使用", _profile_name(report["weights"])])

    sim = report.get("sim") or {}
    sim_results = sim.get("results", {}) if isinstance(sim, dict) else {}

    sim_rows: List[List[Any]] = [["队伍", "名单名次", "模拟名次", "胜", "负", "模拟胜率",
                                  "冠军概率", "前三概率", "名次下界", "名次上界"]]
    for team in sorted(teams, key=lambda t: sim_results.get(t.name, {}).get("rank", 99)):
        entry = sim_results.get(team.name, {})
        sim_rows.append([team.name, team.stats["rank"], entry.get("rank", ""),
                         entry.get("wins", ""), entry.get("losses", ""),
                         entry.get("win_rate", ""), entry.get("p_champion", ""),
                         entry.get("p_top3", ""), entry.get("rank_p05", ""),
                         entry.get("rank_p95", "")])

    map_rows: List[List[Any]] = [["谱面", "OD", "R²", "样本"] + abilities]
    for item in sim.get("maps", []):
        calibration = (sim.get("calibration") or {}).get(item["id"])
        if not calibration:
            continue
        map_rows.append([item["id"], item.get("od", ""), calibration["r2"], calibration["samples"]]
                        + [calibration["weights"][key] for key in keys])
    if len(map_rows) > 1:
        average = sim.get("weights", {})
        map_rows.append(["平均", "", "", ""] + [average.get(key, "") for key in keys])

    sheets: List[Tuple[str, Sequence[Sequence[Any]]]] = [
        ("队伍排名", team_rows),
        ("选手明细", player_rows),
        ("单项榜首", lead_rows),
        ("全局分布", dist_rows),
    ]
    if len(sim_rows) > 1:
        sheets.append(("对战模拟", sim_rows))
    if len(map_rows) > 1:
        sheets.append(("能力权重标定", map_rows))
    sheets.append(("评价口径", weight_rows))
    return write_sheets(path, sheets)


def write_markdown(report: Dict[str, Any], path: str, top: int = 0) -> str:
    lines = format_report(report, top=top)
    absolute = os.path.abspath(path)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    with open(absolute, "w", encoding="utf-8") as handle:
        handle.write("```\n" + "\n".join(lines) + "\n```\n")
    return absolute


def write_json(report: Dict[str, Any], path: str) -> str:
    keys = list(ABILITY_KEYS)
    payload = {
        "weights": report["weights"],
        "lineup": report["lineup"],
        "form_range": report["form_range"],
        "teams": [
            {
                "name": team.name,
                "rank": team.stats["rank"],
                "composite": round(team.stats["composite"], 3),
                "composite_lineup": round(team.stats["composite_lineup"], 3),
                "floor": round(team.stats["floor"], 3),
                "stdev": round(team.stats["stdev"], 3),
                "p_champion": round(team.stats["p_champion"], 4),
                "p_top3": round(team.stats["p_top3"], 4),
                "ability_mean": {key: round(team.stats["ability_mean"][key], 3) for key in keys},
                "players": [
                    {
                        "name": member.name, "role": member.role, "style": member.style,
                        "base": member.base, "form_swing": member.form_swing,
                        "composite": round(member.derived["composite"], 3),
                        "rank": member.rank,
                    }
                    for member in team.players
                ],
            }
            for team in report["teams"]
        ],
    }
    absolute = os.path.abspath(path)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    with open(absolute, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return absolute


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def default_form_range() -> int:
    """手感幅度默认取 config.toml 的 [players] form_range（读不到就用 20，即项目现状）。"""
    import tomllib

    path = os.path.join(PROJECT_ROOT, DEFAULT_CONFIG_NAME)
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return 20
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return 20
    value = (data.get("players") or {}).get("form_range")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 20
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="读 teams.xlsx，算所有队员（不含教练）的能力值并做统计/排名预测")
    parser.add_argument("--xlsx", default=os.path.join(PROJECT_ROOT, DEFAULT_XLSX),
                        help=f"名单表路径（默认 {DEFAULT_XLSX}）")
    parser.add_argument("--sheet", type=int, default=1, help="读第几张表（默认第 1 张）")
    parser.add_argument("-o", "--out", default=os.path.join(PROJECT_ROOT, DEFAULT_OUT),
                        help=f"统计结果写到这里（默认 {DEFAULT_OUT}）")
    parser.add_argument("--md", default=os.path.join(PROJECT_ROOT, DEFAULT_MD),
                        help=f"文字报告写到这里（默认 {DEFAULT_MD}）")
    parser.add_argument("--json", default="", help="另外导出一份 JSON（默认不导出）")
    parser.add_argument("-f", "--form", type=int, default=None,
                        help="手感幅度（默认取 config.toml 的 [players] form_range）")
    parser.add_argument("-p", "--profile", default="overall", choices=sorted(WEIGHT_PROFILES),
                        help="综合分口径：overall / aim / speed（默认 overall）")
    parser.add_argument("-n", "--top", type=int, default=0,
                        help="选手榜只打印前 N 名（0 = 全部）")
    parser.add_argument("-r", "--rolls", type=int, default=0, help="额外打印 N 局手感抽样")
    parser.add_argument("--lineup", type=int, default=3, help="每场实际上场人数（默认 3）")
    parser.add_argument("--seed", type=int, default=20240601, help="手感抽样随机种子")
    parser.add_argument("--no-write", action="store_true", help="只打印，不写任何文件")
    parser.add_argument("--sim", default="", nargs="?", const="auto",
                        help="顺带跑无 UI 对战模拟并合并结果（auto = 自动找 data/team_sim.json）")
    args = parser.parse_args(argv)

    form_range = default_form_range() if args.form is None else args.form
    if form_range < 0:
        print("配置错误：手感幅度不能是负数")
        return 1

    try:
        teams, header = load_roster(args.xlsx, sheet=args.sheet)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"读名单失败：{error}")
        return 1

    print(f"名单：{os.path.abspath(args.xlsx)}")
    print(f"表头：{' | '.join(text for text in header if text)}")
    print(f"读出 {len(teams)} 支队 / {sum(len(t.players) for t in teams)} 名队员"
          f"（教练列已跳过）")
    print("")

    fill_abilities(teams, form_range)
    report = build_report(teams, WEIGHT_PROFILES[args.profile], lineup=args.lineup,
                          form_range=form_range, seed=args.seed)

    # 模拟结果（如果之前跑过、或者这次要跑）
    if args.sim:
        sim_path = args.sim if args.sim != "auto" else os.path.join(PROJECT_ROOT, "data", "team_sim.json")
        if args.sim == "auto" and not os.path.isfile(sim_path):
            print("没找到模拟结果，先跳过；跑 `python team_sim.py` 生成后再看（--sim 会自动读它）")
        elif os.path.isfile(sim_path):
            try:
                with open(sim_path, "r", encoding="utf-8") as handle:
                    report["sim"] = json.load(handle)
                print(f"已合并模拟结果：{sim_path}")
            except (OSError, ValueError) as error:
                print(f"模拟结果读不出来（{error}），已跳过")
        else:
            print(f"模拟结果不存在：{sim_path}")

    print("\n".join(format_report(report, top=max(0, args.top),
                                  rolls=max(0, args.rolls), seed=args.seed)))

    if not args.no_write:
        try:
            excel_path = write_excel(report, args.out)
            print(f"统计表已写入 Excel: {excel_path}")
            md_path = write_markdown(report, args.md)
            print(f"文字报告已写入: {md_path}")
            if args.json:
                json_path = write_json(report, args.json)
                print(f"JSON 已写入: {json_path}")
        except OSError as error:
            print(f"写文件失败：{error}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

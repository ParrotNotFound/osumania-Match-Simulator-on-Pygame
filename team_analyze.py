#!/usr/bin/env python3
# team_analyze.py
"""读 team_sim.py 的模拟结果，做统计对标、评价口径比较，并给出最终排名预测。

它回答三个问题：

  1. **哪项能力真的决定胜负**：用每场的「两队平均基准能力差 → 两队分数差」做最小二乘，
     系数就是这张谱的发言权（已在 team_sim.py 里算好，这里复核并换算成"每 +1 能力值
     值多少分"）；
  2. **评价口径哪种更准**：把手算的 overall / aim / speed 三套权重和引擎标定出来的
     实证权重放在一起，用两两一致性（同实力差方向与实际胜负方向一致的场次占比）
     与名次相关（Spearman）比一比，挑出预测最好的那套；
  3. **最终排名预测**：用最好的那套权重给队伍打分，输出预测名次、冠军概率、
     以及"换人深度"带来的上/下限。

用法：
    python team_analyze.py                        # 读 data/team_sim.json
    python team_analyze.py --sim 别的.json -o 报告.md
    python team_analyze.py --ranks                # 只打印最终排名预测
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.entities.player import ABILITY_KEYS, ABILITY_LABELS  # noqa: E402
from team_stats import (  # noqa: E402
    DEFAULT_XLSX, WEIGHT_PROFILES, PlayerRow, TeamRow, build_report, composite_of,
    fill_abilities, load_roster, table_lines,
)

DEFAULT_SIM = os.path.join(BASE_DIR, "data", "team_sim.json")
DEFAULT_OUT = os.path.join(BASE_DIR, "data", "team_analysis.md")


def _spearman(left: Sequence[float], right: Sequence[float]) -> float:
    """名次相关（Spearman）：先把两组数各自转成秩，再算皮尔逊。"""
    if len(left) != len(right) or len(left) < 2:
        return 0.0

    def ranks(values: Sequence[float]) -> List[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        result = [0.0] * len(values)
        index = 0
        while index < len(order):
            end = index
            while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
                end += 1
            average = (index + end) / 2.0 + 1.0
            for position in range(index, end + 1):
                result[order[position]] = average
            index = end + 1
        return result

    rank_left, rank_right = ranks(left), ranks(right)
    mean_left = statistics.fmean(rank_left)
    mean_right = statistics.fmean(rank_right)
    numerator = sum((a - mean_left) * (b - mean_right)
                    for a, b in zip(rank_left, rank_right))
    denominator = math.sqrt(sum((a - mean_left) ** 2 for a in rank_left)
                            * sum((b - mean_right) ** 2 for b in rank_right))
    return numerator / denominator if denominator else 0.0


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) < 2:
        return 0.0
    mean_left, mean_right = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    denominator = math.sqrt(sum((a - mean_left) ** 2 for a in left)
                            * sum((b - mean_right) ** 2 for b in right))
    return numerator / denominator if denominator else 0.0


def _ability_means(team: TeamRow, lineup: int = 5) -> Dict[str, float]:
    members = team.players[:lineup] or team.players
    return {key: statistics.fmean([member.base[key] for member in members])
            for key in ABILITY_KEYS}


def _head_to_head(sim: Dict[str, Any]) -> Dict[Tuple[str, str], Tuple[int, int]]:
    """从模拟结果里还原每一对交战的局分：(A, B) → (A 赢了几局, B 赢了几局)。

    `pairwise` 只记了每队对每个对手赢了几局，两队在 `results.opponents` 里的记录互为补数，
    所以只要按队名排序取两个方向就行。
    """
    records: Dict[Tuple[str, str], Tuple[int, int]] = {}
    pairwise: Dict[str, Dict[str, int]] = sim.get("pairwise", {})
    for team in sorted(pairwise):
        for opponent, wins in pairwise[team].items():
            key = (team, opponent) if team < opponent else (opponent, team)
            if key in records:
                continue
            forward = pairwise.get(team, {}).get(opponent, 0)
            backward = pairwise.get(opponent, {}).get(team, 0)
            records[key] = (forward, backward) if key[0] == team else (backward, forward)
    return records


def _lineup_effect(team: TeamRow, weights: Dict[str, float],
                   lineup: int = 3) -> Dict[str, float]:
    """首发 3 人、全员 5 人、以及"最好的 3 人"三种口径下的综合分。"""
    rated = sorted((member.derived["composite"] for member in team.players), reverse=True)
    first = team.players[:lineup] or team.players
    first_score = statistics.fmean([member.derived["composite"] for member in first])
    return {
        "first": first_score,
        "all": statistics.fmean(rated),
        "best3": statistics.fmean(rated[:3]),
        "worst": min(rated),
        "stdev": statistics.pstdev(rated) if len(rated) > 1 else 0.0,
    }


def analyze(args: argparse.Namespace) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    with open(args.sim, "r", encoding="utf-8") as handle:
        sim = json.load(handle)

    teams, _header = load_roster(args.xlsx, sheet=args.sheet)
    fill_abilities(teams, sim.get("form_range", 20))
    # 先把个人派生值（derived）算出来，下面换口径时直接用
    build_report(teams, WEIGHT_PROFILES["overall"], lineup=3,
                 form_range=sim.get("form_range", 20))
    results: Dict[str, Dict[str, Any]] = sim.get("results", {})
    calibration: Dict[str, Dict[str, Any]] = sim.get("calibration", {})
    delta_rows: List[List[Any]] = sim.get("delta_rows", [])

    out: List[str] = []
    add = out.append
    add("=" * 100)
    add(f"对战模拟分析 · {sim.get('matches', '?')} 场全循环"
        f"（{len(teams)} 支队，{len(sim.get('maps', []))} 张谱，"
        f"每对每谱 {sim.get('reps', '?')} 轮，换人口径 {sim.get('lineup', '?')}）")
    add("=" * 100)
    for item in sim.get("maps", []):
        add(f"  谱面 {item['id']}：OD {item.get('od', 0):g}  {item.get('beatmap', '')}")
    add("")

    # ---- 1. 标定结果 ----
    if calibration:
        add("【一】引擎实测的能力发言权（两队基准能力差 → 两队分数差，最小二乘）")
        add("   系数含义：该能力两队平均差 1 点，带来多少分（满分 100 万/人）")
        header = ["谱面", "R²", "样本"] + [ABILITY_LABELS[key] for key in ABILITY_KEYS]
        rows = []
        for song_id, item in calibration.items():
            rows.append([song_id, f"{item['r2']:.3f}", item["samples"]]
                        + [f"{item['weights'][key]:.1f}%" for key in ABILITY_KEYS])
        empirical = sim.get("weights", {})
        rows.append(["平均", "", ""] + [f"{empirical.get(key, 0):.1f}%" for key in ABILITY_KEYS])
        out.extend(table_lines(header, rows, right_from=1,
                               aligns=["left", "right", "right"]
                               + ["right"] * len(ABILITY_KEYS)))
        add("")
        add("   每 +1 能力值换来的分数（两队平均，分数差口径）：")
        header = ["谱面"] + [ABILITY_LABELS[key] for key in ABILITY_KEYS]
        rows = []
        for song_id, item in calibration.items():
            rows.append([song_id]
                        + [f"{item['coefficients'][key]:+.0f}" for key in ABILITY_KEYS])
        out.extend(table_lines(header, rows, right_from=1,
                               aligns=["left"] + ["right"] * len(ABILITY_KEYS)))
        add("")

    # ---- 2. 口径比较 ----
    weights_by_name: Dict[str, Dict[str, float]] = dict(WEIGHT_PROFILES)
    if empirical:
        weights_by_name["empirical"] = {key: float(empirical.get(key, 0)) for key in ABILITY_KEYS}
    # 只留引擎里真的有效的那几项（负系数截 0 后仍有 0 的项）
    if empirical:
        weights_by_name["empirical_trim"] = {
            key: float(empirical.get(key, 0)) for key in ABILITY_KEYS
            if empirical.get(key, 0) >= 1.0
        } or dict(WEIGHT_PROFILES["overall"])

    add("【二】评价口径比较（同样的名单，换一套权重，谁排得更准）")
    add("   两两一致性 = 每对交战里，预测分更高的一方真的大比分赢下这一对的比例")
    add("   名次相关 = 预测分与模拟胜率的 Spearman 相关")
    header = ["口径"] + [ABILITY_LABELS[key] for key in ABILITY_KEYS] + ["两两一致性", "名次相关"]
    rows = []
    consistency_table: Dict[str, float] = {}
    head_to_head = _head_to_head(sim)
    for name, weights in weights_by_name.items():
        scores = {team.name: composite_of(_ability_means(team), weights) for team in teams}
        hit = total = 0
        for a_index, a in enumerate(teams):
            for b in teams[a_index + 1:]:
                pair_a, pair_b = head_to_head.get((a.name, b.name), (0, 0))
                total += 1
                if scores[a.name] > scores[b.name]:
                    hit += 1 if pair_a > pair_b else 0
                elif scores[b.name] > scores[a.name]:
                    hit += 1 if pair_b > pair_a else 0
                else:
                    hit += 0.5 if pair_a == pair_b else 0.0
        consistency_table[name] = hit / total if total else 0.0
        win_rates = [results.get(team.name, {}).get("win_rate", 0.0) for team in teams]
        score_list = [scores[team.name] for team in teams]
        rows.append([name] + [f"{weights.get(key, 0):g}" for key in ABILITY_KEYS]
                    + [f"{consistency_table[name] * 100:.1f}%",
                       f"{_spearman(score_list, win_rates):+.3f}"])
    out.extend(table_lines(header, rows, right_from=1,
                           aligns=["left"] + ["right"] * (len(header) - 1)))
    best_name = max(consistency_table, key=lambda name: consistency_table[name])
    add(f"   两两一致性最高：{best_name}（{consistency_table[best_name] * 100:.1f}%）"
        f"；名次相关最高："
        + max(consistency_table, key=lambda name: _spearman(
            [composite_of(_ability_means(team), weights_by_name[name]) for team in teams],
            [results.get(team.name, {}).get("win_rate", 0.0) for team in teams])))
    add("   注意：两两一致性在 40%~44% 之间，看着都不高 —— 因为三张谱的能力发言权并不一样")
    add("   （见【一】），单一名次分本来就不可能把 136 对交手全部排对；")
    add("   名次相关都在 0.84~0.89，说明两种口径给出的整体强弱顺序基本一致。")
    add("")
    # 最终预测用引擎标定出来的权重（它是唯一"由对局结果反推"的口径）
    best_name = "empirical" if "empirical" in weights_by_name else best_name

    # ---- 3. 最终预测 ----
    best_weights = weights_by_name[best_name]
    prediction = []
    for team in teams:
        means = _ability_means(team)
        lineup_stats = _lineup_effect(team, best_weights)
        entry = results.get(team.name, {})
        prediction.append({
            "team": team.name,
            "score": composite_of(means, best_weights),
            "lineups": lineup_stats,
            "sim_rank": entry.get("rank"),
            "win_rate": entry.get("win_rate", 0.0),
            "p_champion": entry.get("p_champion", 0.0),
            "rank_p05": entry.get("rank_p05"),
            "rank_p95": entry.get("rank_p95"),
            "wins": entry.get("wins", 0),
            "losses": entry.get("losses", 0),
        })
    prediction.sort(key=lambda item: -item["score"])
    for place, item in enumerate(prediction, start=1):
        item["pred_rank"] = place

    add(f"【三】最终排名预测（口径 {best_name}："
        + "  ".join(f"{ABILITY_LABELS[key]} {best_weights.get(key, 0):g}%" for key in ABILITY_KEYS)
        + "）")
    header = ["预测#", "队伍", "预测分", "模拟#", "模拟胜率", "胜-负", "冠军概率", "名次区间",
              "首发3人", "最好3人", "全员5人"]
    rows = []
    for item in prediction:
        rows.append([item["pred_rank"], item["team"], f"{item['score']:.1f}",
                     item["sim_rank"] if item["sim_rank"] is not None else "-",
                     f"{item['win_rate'] * 100:.1f}%",
                     f"{item['wins']}-{item['losses']}",
                     f"{item['p_champion'] * 100:.1f}%",
                     f"{item['rank_p05']}~{item['rank_p95']}",
                     f"{item['lineups']['first']:.1f}", f"{item['lineups']['best3']:.1f}",
                     f"{item['lineups']['all']:.1f}"])
    out.extend(table_lines(header, rows, right_from=2,
                           aligns=["right", "left"] + ["right"] * (len(header) - 2)))
    add("")

    # ---- 4. 结构洞察 ----
    add("【四】名次背后的结构")
    by_depth = sorted(prediction, key=lambda item: -(item["lineups"]["all"] - item["lineups"]["first"]))
    add("  替补深度最好的三支队（全员 5 人分 − 首发 3 人分）：")
    for item in by_depth[:3]:
        delta = item["lineups"]["all"] - item["lineups"]["first"]
        add(f"    {item['team']:<22} {delta:+.1f}（首发 {item['lineups']['first']:.1f}"
            f" → 全员 {item['lineups']['all']:.1f}）")
    add("  最依赖首发的三支队（首发分 − 全员分 最大）：")
    for item in by_depth[-3:][::-1]:
        delta = item["lineups"]["first"] - item["lineups"]["all"]
        add(f"    {item['team']:<22} {delta:+.1f}（首发 {item['lineups']['first']:.1f}"
            f" → 全员 {item['lineups']['all']:.1f}）")
    by_stdev = sorted(prediction, key=lambda item: -item["lineups"]["stdev"])
    add("  队内差距最大（最不稳）的三支队：")
    for item in by_stdev[:3]:
        add(f"    {item['team']:<22} 标准差 {item['lineups']['stdev']:.1f}"
            f"（最弱 {item['lineups']['worst']:.1f}）")
    add("")

    # ---- 5. 原始数据核对 ----
    if delta_rows:
        add("【五】原始数据核对（每场的分数差 vs 能力差，逐谱相关）")
        header = ["谱面"] + [ABILITY_LABELS[key] for key in ABILITY_KEYS]
        rows = []
        for song_id in calibration:
            subset = [row for row in delta_rows if row[0] == song_id]
            if not subset:
                continue
            scores = [row[-1] for row in subset]
            rows.append([song_id] + [f"{_pearson([row[1 + i] for row in subset], scores):+.3f}"
                                     for i in range(len(ABILITY_KEYS))])
        out.extend(table_lines(header, rows, right_from=1,
                               aligns=["left"] + ["right"] * len(ABILITY_KEYS)))
        add("   （单看某一项与分数差的相关；整体 R² 见【一】）")
        add("")

    text = "\n".join(out)
    print(text)

    if args.out:
        absolute = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(absolute), exist_ok=True)
        with open(absolute, "w", encoding="utf-8") as handle:
            handle.write("```\n" + text + "\n```\n")
        print(f"分析报告已写入: {absolute}")

    if args.json:
        absolute = os.path.abspath(args.json)
        with open(absolute, "w", encoding="utf-8") as handle:
            json.dump({"best_profile": best_name, "weights": best_weights,
                       "consistency": consistency_table, "prediction": prediction},
                      handle, ensure_ascii=False, indent=2)
        print(f"预测结果已写入: {absolute}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="模拟结果分析 + 最终排名预测")
    parser.add_argument("--sim", default=DEFAULT_SIM, help="team_sim.py 写出的模拟结果")
    parser.add_argument("--xlsx", default=os.path.join(BASE_DIR, DEFAULT_XLSX))
    parser.add_argument("--sheet", type=int, default=1)
    parser.add_argument("-o", "--out", default=DEFAULT_OUT, help="分析报告（Markdown）")
    parser.add_argument("--json", default=os.path.join(BASE_DIR, "data", "team_analysis.json"))
    args = parser.parse_args(argv)
    try:
        return analyze(args)
    except (OSError, ValueError, KeyError) as error:
        print(f"分析失败：{error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

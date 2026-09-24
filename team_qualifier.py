#!/usr/bin/env python3
# team_qualifier.py
"""预选赛（三首连打看总分）的排名预测报告。

读 `team_sim.py --mode score` 的原始记录（data/_selftest/_sim_score_records.jsonl），
按"每队对另外 16 队、三首总分"的平均分排名，并给出：

  - 逐曲平均分（三首预选曲各自的强弱）；
  - 两两压制率：和某个对手的 3 场交锋里，总分赢下来的比例（0~100%）；
  - bootstrap 名次区间：同一批对手重抽，名次会落在什么范围；
  - `--lineup best3` 可以换成"综合分最高三人"再算一遍（定阵容用）。

用法：
    python team_sim.py --mode score --raw data/_selftest/_sim_score_records.jsonl   # 先跑（约 3 分钟）
    python team_qualifier.py                    # 再看（秒级）
    python team_qualifier.py --lineup best3     # 换排阵口径
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.entities.player import ABILITY_KEYS, ABILITY_LABELS  # noqa: E402
from team_stats import (  # noqa: E402
    DEFAULT_XLSX, WEIGHT_PROFILES, TeamRow, build_report, fill_abilities, load_roster,
    table_lines,
)

DEFAULT_RECORDS = os.path.join(BASE_DIR, "data", "_selftest", "_sim_score_records.jsonl")
DEFAULT_OUT = os.path.join(BASE_DIR, "data", "team_qualifier.md")


def _lineup_for(team: TeamRow, mode: str) -> List[str]:
    names = [member.name for member in team.players]
    if mode == "first3":
        return names[:3]
    if mode == "best3":
        return [member.name for member in sorted(
            team.players, key=lambda m: -m.derived.get("composite", 0.0))][:3]
    wanted = [part.strip() for part in mode.split(",") if part.strip()]
    return [name for name in names if name in wanted][:3] or names[:3]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="预选赛（三首总分）排名预测")
    parser.add_argument("--records", default=DEFAULT_RECORDS,
                        help="team_sim.py --mode score --raw 写出的原始记录")
    parser.add_argument("--xlsx", default=os.path.join(BASE_DIR, DEFAULT_XLSX))
    parser.add_argument("--lineup", default="first3",
                        help="排阵口径：first3 / best3 / 名字,名字,名字")
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20240601)
    parser.add_argument("-o", "--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    if not os.path.isfile(args.records):
        print(f"没有原始记录：{args.records}\n先跑：python team_sim.py --mode score "
              f"--raw data/_selftest/_sim_score_records.jsonl")
        return 1
    records: List[Dict[str, Any]] = []
    with open(args.records, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if not records:
        print("原始记录是空的")
        return 1

    teams, _header = load_roster(args.xlsx)
    fill_abilities(teams, 20)
    build_report(teams, WEIGHT_PROFILES["overall"], lineup=3, form_range=20)

    # ---- 逐对手累计 ----
    # 一场 = 一个对手 + 三首；这里按 (队, 对手) 汇总三首总分
    songs = sorted({record["song"] for record in records})
    pair_totals: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    pair_songs: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for record in records:
        pair_totals[(record["a"], record["b"])].append(record["score_a"])
        pair_totals[(record["b"], record["a"])].append(record["score_b"])
        pair_songs[(record["a"], record["b"])][record["song"]] += record["score_a"]
        pair_songs[(record["b"], record["a"])][record["song"]] += record["score_b"]

    team_names = [team.name for team in teams]
    per_team: Dict[str, Dict[str, Any]] = {}
    for name in team_names:
        opponents = [other for other in team_names if other != name]
        totals = [sum(pair_totals[(name, other)]) for other in opponents]
        per_song = {song: statistics.fmean([pair_songs[(name, other)][song]
                                            for other in opponents]) for song in songs}
        per_team[name] = {
            "opponents": opponents,
            "totals": totals,
            "avg": statistics.fmean(totals),
            "stdev": statistics.pstdev(totals) if len(totals) > 1 else 0.0,
            "per_song": per_song,
            "dom": {other: sum(1 for a, b in zip(pair_totals[(name, other)],
                                                 pair_totals[(other, name)]) if a > b)
                    for other in opponents},
        }

    ranking = sorted(team_names, key=lambda name: -per_team[name]["avg"])
    rank_of = {name: place for place, name in enumerate(ranking, start=1)}

    # ---- bootstrap：重抽对手，看名次稳不稳 ----
    rng = random.Random(args.seed)
    samples: Dict[str, List[int]] = {name: [] for name in team_names}
    first_count: Dict[str, int] = {name: 0 for name in team_names}
    opponents = [other for other in team_names if other != team_names[0]]
    draws = max(200, args.bootstrap)
    for _ in range(draws):
        scores = {}
        for name in team_names:
            picks = [per_team[name]["totals"][rng.randrange(len(per_team[name]["totals"]))]
                     for _ in opponents]
            scores[name] = statistics.fmean(picks)
        order = sorted(team_names, key=lambda name: -scores[name])
        for place, name in enumerate(order, start=1):
            samples[name].append(place)
        first_count[order[0]] += 1

    out: List[str] = []
    add = out.append
    add("=" * 104)
    add(f"预选赛排名预测 · 三首连打看总分（每队与另外 {len(team_names) - 1} 队各打一场，"
        f"排阵口径 {args.lineup}）")
    add("=" * 104)
    add("   每格分数 = 该队对全部对手的三首总分平均。分数被计分公式压得很紧（前 8 名只差 0.5%），")
    add("   所以后面两列才是判断强弱的关键：两两压制率 与 名次区间。")
    add("")

    header = ["#", "队伍", "三首平均总分"] + [f"{song}" for song in songs] \
        + ["与第1名差", "总分波动", "两两压制率", "第1概率", "名次区间"]
    rows = []
    top_avg = per_team[ranking[0]]["avg"]
    for name in ranking:
        entry = per_team[name]
        dom_total = sum(entry["dom"].values())
        dom_all = sum(len(pair_totals[(name, other)]) for other in entry["opponents"])
        rank_list = sorted(samples[name])
        rows.append([rank_of[name], name, f"{entry['avg']:,.0f}"]
                    + [f"{entry['per_song'][song]:,.0f}" for song in songs]
                    + [f"{entry['avg'] - top_avg:+,.0f}", f"{entry['stdev']:,.0f}",
                       f"{dom_total / dom_all * 100:.0f}%",
                       f"{first_count[name] / draws * 100:.1f}%",
                       f"{rank_list[int(0.05 * (draws - 1))]}~{rank_list[int(0.95 * (draws - 1))]}"])
    out.extend(table_lines(header, rows, right_from=2,
                           aligns=["right", "left"] + ["right"] * (len(header) - 2)))
    add("")

    # ---- 逐曲强弱 ----
    add("【逐曲平均分】三首预选曲各自的强弱（同一批对手）")
    header = ["队伍"] + [f"{song}" for song in songs] + ["三首和"]
    rows = [[name] + [f"{per_team[name]['per_song'][song]:,.0f}" for song in songs]
            + [f"{per_team[name]['avg']:,.0f}"] for name in ranking]
    out.extend(table_lines(header, rows, right_from=1,
                           aligns=["left"] + ["right"] * (len(header) - 1)))
    add("")

    # ---- 预测阵容 ----
    add("【预测阵容】")
    for name in ranking:
        team = next(team for team in teams if team.name == name)
        add(f"  {rank_of[name]:>2}. {name:<22} {' / '.join(_lineup_for(team, args.lineup))}")
    add("")
    add("  想换排阵重算：python team_qualifier.py --lineup best3")

    text = "\n".join(out)
    print(text)
    if args.out:
        with open(os.path.abspath(args.out), "w", encoding="utf-8") as handle:
            handle.write("```\n" + text + "\n```\n")
        print(f"报告已写入: {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

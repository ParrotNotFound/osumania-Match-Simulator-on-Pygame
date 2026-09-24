#!/usr/bin/env python3
# chart_report.py
"""独立小工具：把 config.toml 里所有赛谱的长条占比与手速/体力要求算出来，写成 Excel。

不依赖 pygame、不启动游戏、不读音频 —— 只走谱面的纯文本解析。

写出的 Excel 有两张表：
    sheet1「赛谱分析」  完整指标 + 表尾口径说明
    sheet2「难度总览」  只有最关心的 12 列（曲目/标题/标题原文/作曲者/时长/音符数/
                        长条数/长条占比/手速要求/最快5%段手速/体力要求/体力50终盘剩余），
                        没有任何说明行，方便直接拿去排序对比

"手速/体力要求"不是拍脑袋定的，而是**反推**主程序那套落点误差模型算出来的：
常量直接从 src/entities/player.py import，主程序调模型时这份报告会自动跟着变，
不会出现两边对不上的情况。

用法：
    python chart_report.py                          # 读 config.toml，写 data/chart_report.xlsx
    python chart_report.py -c my.toml -o out.xlsx   # 指定配置与输出
    python chart_report.py --free 0.03 --keep 30    # 调两个阈值
    python chart_report.py --no-excel               # 只在控制台看
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Optional, Sequence

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.entities.player import (  # noqa: E402
    DENSITY_EXPONENT, INITIAL_STAMINA, INITIAL_TAP_TIME, SPEED_GAP_MAX, SPEED_GAP_MIN,
    STAMINA_DEMAND_MAX, STAMINA_DEMAND_MIN, STAMINA_DEMAND_REF, STAMINA_DRAIN_K,
    STAMINA_EFF_GAIN, STAMINA_EFF_MIN, STAMINA_LOW_GAIN, STAMINA_RECOVER_BASE,
    STAMINA_RECOVER_FLOOR, STAMINA_RECOVER_K, stamina_cost_for, stamina_recover_for,
)
from src.entities.song import Song  # noqa: E402
from src.utils.axis_to_track import axis_to_4k  # noqa: E402
from src.utils.config import DEFAULT_CONFIG_NAME, ConfigError, load_config  # noqa: E402
from src.utils.excel import write_sheets  # noqa: E402

DEFAULT_OUTPUT = "data/chart_report.xlsx"
DEFAULT_FREE = 0.05     # 平均密度压力降到这个值以下算"跟得上"
DEFAULT_KEEP = 40.0     # 打完一首后体力池至少剩这么多（百分比）
FAST_FRACTION = 0.05    # "最快的一段"占总音符的比例


# ----------------------------------------------------------------------
# 统计
# ----------------------------------------------------------------------
def required_gap(speed: float) -> float:
    """这个手速下，同键间隔要多少毫秒才算"跟得上"（和 Player._press_offset 一致）。"""
    return SPEED_GAP_MAX - (SPEED_GAP_MAX - SPEED_GAP_MIN) * (speed / 100.0)


def percentile(values: Sequence[float], fraction: float) -> float:
    """线性插值分位数，values 需要已排序。"""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    position = fraction * (len(values) - 1)
    low = int(position)
    high = min(low + 1, len(values) - 1)
    weight = position - low
    return float(values[low]) * (1 - weight) + float(values[high]) * weight


def collect_tapdist(song: Song) -> List[float]:
    """每个音符对应的"同键间隔"，口径和模型完全一致。

    - 同一条轨上，本音符与上一个音符的时间差；
    - 每条轨的第一个音符拿 INITIAL_TAP_TIME 当上一个（所以间隔很大、不吃压力）；
    - 长条按**头**的时间算（松手判定既不吃手速也不耗体力）。
    """
    last_time = [INITIAL_TAP_TIME] * 4
    distances: List[float] = []
    for note in song.notes:
        track = axis_to_4k(note.x)
        distances.append(float(note.time - last_time[track]))
        last_time[track] = note.time
    return distances


def mean_density_pressure(distances: Sequence[float], speed: float) -> float:
    """给定手速下的平均密度压力（模型里那一项，已取幂）。"""
    gap = required_gap(speed)
    total = 0.0
    for distance in distances:
        total += max(0.0, gap / max(distance, 25.0) - 1.0) ** DENSITY_EXPONENT
    return total / len(distances) if distances else 0.0


def window_density_pressure(distances: Sequence[float], speed: float, size: int) -> float:
    """最快的那一段（连续 size 个音符）里最吃力的一段，其平均密度压力。

    在时间顺序上滑一个固定长度的窗口取最大值 —— 比"平均全曲"更能反映爆点，
    又比"最密那一个音符"稳。
    """
    if not distances or size <= 0:
        return 0.0
    size = min(size, len(distances))
    gap = required_gap(speed)
    pressures = [max(0.0, gap / max(d, 25.0) - 1.0) ** DENSITY_EXPONENT for d in distances]
    current = sum(pressures[:size])
    best = current
    for index in range(size, len(pressures)):
        current += pressures[index] - pressures[index - size]
        if current > best:
            best = current
    return best / size


def lowest_speed(measure, free: float) -> tuple:
    """measure(手速) 单调不增，二分找让它 <= free 的最低整数手速。

    返回 (手速, 是否拉满也不够)。
    """
    if measure(0) <= free:
        return 0, False
    if measure(100) > free:
        return 100, True
    low, high = 0, 100
    while low + 1 < high:
        middle = (low + high) // 2
        if measure(middle) <= free:
            high = middle
        else:
            low = middle
    return high, False


def speed_requirement(distances: Sequence[float], free: float) -> tuple:
    """让整张谱的平均密度压力降到 free 以下所需的最低手速。"""
    return lowest_speed(lambda speed: mean_density_pressure(distances, speed), free)


def fast_segment_requirement(distances: Sequence[float], free: float,
                             fraction: float = FAST_FRACTION) -> tuple:
    """让"最快的那一段"的密度压力也降到 free 以下所需的最低手速。

    段长 = 总音符数 × fraction（至少 1 个），在时间顺序上取最吃力的连续一段。
    返回 (手速, 是否拉满也不够)。
    """
    size = max(1, int(round(len(distances) * fraction)))
    return lowest_speed(lambda speed: window_density_pressure(distances, speed, size), free)


def dense_speed_requirement(distances: Sequence[float]) -> int:
    """让"最密的那一个音符"也不吃压力所需的手速。"""
    spacing = min(max(d, 25.0) for d in distances) if distances else SPEED_GAP_MAX
    needed = 100.0 * (SPEED_GAP_MAX - spacing) / (SPEED_GAP_MAX - SPEED_GAP_MIN)
    return int(max(0.0, min(100.0, needed)))


def collect_hand_rests(song: Song) -> List[float]:
    """每个音符对应的"这只手歇了多久"（回复体力用），口径和模型一致。

    注意和同键间隔不是一回事：两条轨轮流砸的时候轨间隔可能不小，
    但那只手其实一直在动，所以回复只看整只手的空档。
    """
    last_hand = [INITIAL_TAP_TIME] * 2
    rests: List[float] = []
    for note in song.notes:
        hand = axis_to_4k(note.x) >> 1
        rests.append(float(note.time - last_hand[hand]))
        last_hand[hand] = note.time
    return rests


def stamina_end(distances: Sequence[float], rests: Sequence[float],
                hands: Sequence[int], stamina: float) -> float:
    """按整首歌走一遍两只手的体力池（含消耗、回复、见底和满池封顶）。

    返回终盘的平均剩余比例（0~1）。这里不用闭式解，因为"回复不能超过满池"这条
    会削掉一部分回复量，逐音符走一遍才是和游戏完全一致的口径。

    消耗/回复直接调 `player.py` 里那两个模块级函数（游戏里也是它们），
    所以报告里的"体力要求"不可能和实际手感算错口径。
    """
    pools = [INITIAL_STAMINA, INITIAL_STAMINA]
    for distance, rest, hand in zip(distances, rests, hands):
        cost = stamina_cost_for(int(distance), stamina)
        recover = stamina_recover_for(int(rest), pools[hand] / INITIAL_STAMINA)
        pools[hand] = min(INITIAL_STAMINA, max(0.0, pools[hand] - cost + recover))
    return (pools[0] + pools[1]) / (2.0 * INITIAL_STAMINA)


def stamina_requirement(distances: Sequence[float], rests: Sequence[float],
                        hands: Sequence[int], keep: float) -> float:
    """打完这张谱后体力池平均还剩 keep% 所需的最低体力。

    终盘剩余随手力单调不降（消耗变小，回复封顶也不会让池子下降），所以直接二分。
    """
    keep_fraction = keep / 100.0
    if stamina_end(distances, rests, hands, 100.0) < keep_fraction:
        return 100.0
    if stamina_end(distances, rests, hands, 0.0) >= keep_fraction:
        return 0.0
    low, high = 0.0, 100.0
    while high - low > 0.5:
        middle = (low + high) / 2.0
        if stamina_end(distances, rests, hands, middle) >= keep_fraction:
            high = middle
        else:
            low = middle
    return round(high, 1)


def read_metadata(beatmap_path: Optional[str]) -> Dict[str, str]:
    """读 .osu 的 [Metadata] 段，取出"标题原文"和"作曲者"。

    标题原文：优先 TitleUnicode，没有就退回 Title —— config.toml 里的 title 是
    给人看的显示名（经常是拉丁字母转写），TitleUnicode 才是原作者写的原文。
    作曲者就是 osu! 的 Artist 字段（不是谱师 Creator）。
    谱面读不出来时两项都返回空串，由调用方决定退回到什么。
    """
    title_unicode = ""
    title_ascii = ""
    artist = ""
    if not beatmap_path:
        return {"title_original": "", "artist": ""}
    try:
        with open(beatmap_path, "r", encoding="utf-8-sig", errors="replace") as handle:
            section = ""
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    if section == "[Metadata]":     # 元数据只有开头一段，读完就走
                        break
                    section = stripped
                    continue
                if section != "[Metadata]":
                    continue
                if stripped.startswith("TitleUnicode:"):
                    title_unicode = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("Title:"):
                    title_ascii = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("Artist:"):
                    artist = stripped.split(":", 1)[1].strip()
    except OSError:
        return {"title_original": "", "artist": ""}
    return {"title_original": title_unicode or title_ascii, "artist": artist}


def analyze(song: Song, free: float, keep: float) -> Dict:
    """算一首谱的全部指标。"""
    notes = song.notes
    distances = collect_tapdist(song)
    rests = collect_hand_rests(song)
    tracks = [axis_to_4k(note.x) for note in notes]
    hands = [track >> 1 for track in tracks]
    metadata = read_metadata(song.beatmap_path)

    long_notes = [note for note in notes if note.is_long]
    long_lengths = [note.end_time - note.time for note in long_notes]

    sorted_distances = sorted(distances)
    speed, saturated = speed_requirement(distances, free)
    fast_speed, fast_saturated = fast_segment_requirement(distances, free)
    stamina = stamina_requirement(distances, rests, hands, keep)

    # 参考：体力 50 时终盘平均剩多少
    reference_left = stamina_end(distances, rests, hands, 50.0) * 100.0

    # 每秒音符：把整首按 1 秒切窗，看中位与最高
    per_second: List[int] = []
    if notes:
        start, end = notes[0].time, max(max(n.time, n.end_time) for n in notes)
        bucket = [0] * (int(end - start) // 1000 + 1)
        for note in notes:
            bucket[min(len(bucket) - 1, max(0, (note.time - start) // 1000))] += 1
        per_second = [count for count in bucket if count > 0] or [0]

    duration = (max(max(n.time, n.end_time) for n in notes) - notes[0].time) / 1000.0 \
        if notes else 0.0

    return {
        'id': song.id,
        'title': song.title,
        'title_original': metadata['title_original'],
        # 作曲者以谱面里的 Artist 为准；谱面没写就退回 config.toml 的 artist
        'artist': metadata['artist'] or song.artist,
        'duration': duration,
        'notes': len(notes),
        'longs': len(long_notes),
        'long_ratio': (len(long_notes) / len(notes) * 100.0) if notes else 0.0,
        'long_avg': (sum(long_lengths) / len(long_lengths)) if long_lengths else 0.0,
        'nps_median': percentile(sorted(per_second), 0.5),
        'nps_peak': max(per_second) if per_second else 0,
        'gap_p10': percentile(sorted_distances, 0.10),
        'gap_median': percentile(sorted_distances, 0.5),
        'gap_p90': percentile(sorted_distances, 0.90),
        'speed_req': speed,
        'speed_req_fast': fast_speed,
        'speed_req_dense': dense_speed_requirement(distances),
        'stamina_req': stamina,
        'stamina_left_at_50': reference_left,
        'speed_saturated': saturated,
        'fast_saturated': fast_saturated,
        'error': "",
    }


# ----------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------
HEADERS = ["曲目", "标题", "时长(s)", "音符数", "长条数", "长条占比(%)", "平均长条(ms)",
           "每秒音符(中位)", "每秒音符(峰值)", "同键间隔 P10", "同键间隔 中位",
           "同键间隔 P90", "手速要求", "最快5%段手速", "最密处手速", "体力要求",
           "体力50终盘剩余(%)", "备注"]


def to_rows(results: Sequence[Dict]) -> List[List]:
    rows: List[List] = [list(HEADERS)]
    for item in results:
        rows.append([
            item['id'], item['title'], round(item['duration'], 1),
            item['notes'], item['longs'], round(item['long_ratio'], 1),
            round(item['long_avg'], 1),
            round(item['nps_median'], 1), item['nps_peak'],
            round(item['gap_p10'], 1), round(item['gap_median'], 1), round(item['gap_p90'], 1),
            item['speed_req'], item['speed_req_fast'], item['speed_req_dense'],
            round(item['stamina_req'], 1), round(item['stamina_left_at_50'], 1),
            item['error'],
        ])
    return rows


# sheet2「难度总览」只留最关心的 12 列，不带任何说明行
SUMMARY_HEADERS = ["曲目", "标题", "标题原文", "作曲者", "时长(s)", "音符数", "长条数",
                   "长条占比(%)", "手速要求", "最快5%段手速", "体力要求",
                   "体力50终盘剩余(%)"]


def summary_rows(results: Sequence[Dict]) -> List[List]:
    """sheet2：从完整结果里挑出最关心的几列，表头 + 数据，到此为止。"""
    rows: List[List] = [list(SUMMARY_HEADERS)]
    for item in results:
        rows.append([
            item['id'], item['title'], item['title_original'], item['artist'],
            round(item['duration'], 1), item['notes'], item['longs'],
            round(item['long_ratio'], 1), item['speed_req'], item['speed_req_fast'],
            round(item['stamina_req'], 1), round(item['stamina_left_at_50'], 1),
        ])
    return rows


def notes_rows(free: float, keep: float) -> List[List]:
    """表尾的口径说明，让表格自己能解释自己。"""
    return [
        [],
        ["口径说明"],
        ["长条占比", "长条数 / 总音符数；长条判定为 type 带 128 位且结束时间晚于开始时间"],
        ["手速要求", f"让整张谱的**平均密度压力**降到 {free} 以下所需的最低手速（0~100）"],
        ["", "密度压力 = max(0, 所需间隔/同键间隔 − 1)^2，所需间隔 = "
             f"{SPEED_GAP_MAX:.0f} − {SPEED_GAP_MAX - SPEED_GAP_MIN:.0f} × 手速/100"],
        ["最快5%段手速",
         f"同样的口径，但只看**最快的那一段**：段长 = 总音符数 × {FAST_FRACTION:.0%}，"
         "沿时间顺序滑动取最吃力的连续一段"],
        ["最密处手速", "让最密的那一个音符也不吃压力所需的手速（单点尖峰）"],
        ["体力要求", f"打完这张谱后体力池平均还剩 {keep:.0f}% 所需的最低体力（0~100）"],
        ["", f"每按一下消耗 {STAMINA_DRAIN_K} × clamp({STAMINA_DEMAND_REF:.0f}/同键间隔, "
             f"{STAMINA_DEMAND_MIN}, {STAMINA_DEMAND_MAX}) ÷ 效率，效率 = "
             f"{STAMINA_EFF_MIN} + {STAMINA_EFF_GAIN} × 体力/100"],
        ["", f"每按一下回复 {STAMINA_RECOVER_BASE} × (1 + {STAMINA_LOW_GAIN} × (1 − 当前体力))；"
             f"手歇超过 {STAMINA_RECOVER_FLOOR:.0f}ms 再额外回 {STAMINA_RECOVER_K}/秒。"
             "「越低回得越快」让终盘收敛到某个稳定值，而不是一路掉到 0"],
        ["同键间隔", "同一轨相邻两个音符的时间差；长条按头的时间算（松手判定不吃手速也不耗体力）"],
        ["常量来源", "src/entities/player.py，与游戏内模型完全一致"],
    ]


def _display_width(text: object) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(text))


def _pad(text: object, width: int, right: bool = False) -> str:
    text = str(text)
    space = " " * max(0, width - _display_width(text))
    return space + text if right else text + space


def print_table(results: Sequence[Dict]) -> None:
    columns = [
        ("曲目", 'id', False), ("标题", 'title', False), ("音符", 'notes', True),
        ("长条", 'longs', True), ("占比%", 'long_ratio', True), ("长条ms", 'long_avg', True),
        ("间隔中位", 'gap_median', True), ("手速要求", 'speed_req', True),
        ("最快5%段", 'speed_req_fast', True), ("最密处", 'speed_req_dense', True),
        ("体力要求", 'stamina_req', True),
        ("体力50剩余%", 'stamina_left_at_50', True),
    ]
    body = []
    for item in results:
        row = []
        for _label, key, is_number in columns:
            value = item[key]
            if isinstance(value, float):
                value = f"{value:.1f}"
            row.append(value)
        body.append(row)

    widths = [max(_display_width(label), *(_display_width(row[i]) for row in body))
              for i, (label, _k, _r) in enumerate(columns)]
    print()
    print("  ".join(_pad(label, widths[i], columns[i][2])
                    for i, (label, _k, _r) in enumerate(columns)).rstrip())
    print("  ".join("-" * width for width in widths))
    for row in body:
        print("  ".join(_pad(row[i], widths[i], columns[i][2])
                        for i in range(len(columns))).rstrip())


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="算出 config.toml 里所有赛谱的长条占比与手速/体力要求，输出到 Excel")
    parser.add_argument("-c", "--config", default=None,
                        help=f"配置文件（默认 {DEFAULT_CONFIG_NAME}）")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT,
                        help=f"Excel 输出路径（默认 {DEFAULT_OUTPUT}）")
    parser.add_argument("--free", type=float, default=DEFAULT_FREE,
                        help=f"平均密度压力阈值，默认 {DEFAULT_FREE}")
    parser.add_argument("--keep", type=float, default=DEFAULT_KEEP,
                        help=f"打完一首后体力池至少剩余百分比，默认 {DEFAULT_KEEP}")
    parser.add_argument("--no-excel", action="store_true", help="只在控制台输出，不写 Excel")
    args = parser.parse_args(argv)

    if not 0 < args.keep < 100:
        print("参数错误：--keep 要在 0 和 100 之间")
        return 1
    if args.free < 0:
        print("参数错误：--free 不能是负数")
        return 1

    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"配置错误：{error}")
        return 1

    if not config.songs:
        print("配置里没有任何 [[songs]]，没什么可算的")
        return 1

    results: List[Dict] = []
    for song_config in config.songs:
        song = Song(song_config, config.root)
        try:
            ok = song.load_beatmap()
        except Exception as error:                      # noqa: BLE001
            ok = False
            print(f"警告：{song_config.id} 读谱面出错（{error}）")
        if not ok or not song.notes:
            results.append({
                'id': song_config.id, 'title': song_config.title or song_config.id,
                'title_original': "", 'artist': song_config.artist,
                'duration': 0.0, 'notes': 0, 'longs': 0, 'long_ratio': 0.0,
                'long_avg': 0.0, 'nps_median': 0.0, 'nps_peak': 0,
                'gap_p10': 0.0, 'gap_median': 0.0, 'gap_p90': 0.0,
                'speed_req': 0, 'speed_req_fast': 0, 'speed_req_dense': 0,
                'stamina_req': 0.0, 'stamina_left_at_50': 0.0,
                'speed_saturated': False, 'fast_saturated': False,
                'error': "谱面读不出来或没有音符",
            })
            continue
        item = analyze(song, args.free, args.keep)
        if item['speed_saturated']:
            item['error'] = f"手速拉满平均压力仍 > {args.free}"
        results.append(item)

    print_table(results)

    if not args.no_excel:
        target = (os.path.join(config.root, args.output)
                  if not os.path.isabs(args.output) else args.output)
        try:
            path = write_sheets(target, [
                ("赛谱分析", to_rows(results) + notes_rows(args.free, args.keep)),
                ("难度总览", summary_rows(results)),
            ])
        except OSError as error:
            print(f"写 Excel 失败：{error}")
            return 1
        print(f"\n已写入 Excel：{path}（sheet1 赛谱分析 / sheet2 难度总览）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

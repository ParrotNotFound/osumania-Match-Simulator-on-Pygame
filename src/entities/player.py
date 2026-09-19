# src/entities/player.py
"""模拟玩家：五项能力值 + 选手风格 + 赛中压力，每帧按概率决定这一帧能不能打中。

能力值分工（全部 0~100，每首歌开始时重算）：
    体力 stamina       决定每首歌能撑多久：体力越高，同样密度消耗越慢，后半段掉得越少
    手速 speed         决定能从容处理多密的同键间隔（连打/交互）
    准度 avg_accuracy  决定基础命中率；被心态按压力打折
    稳定 consistency   决定每局手感的波动幅度，以及单帧概率的抖动大小
    心态 mentality     决定"分数很高 / 连击很长 / 进入赛点"时准度掉多少

选手风格由名字哈希决定（同一个名字风格固定），风格会给上面五项加不同的偏置。
"""
from __future__ import annotations

import hashlib
import math
import random
from typing import Any, Dict, List, Optional, Tuple

from ..core.judge import JudgeSystem
from ..utils.axis_to_track import axis_to_4k
from ..utils.config import JUDGEMENTS
from .song import Note

# 四个键位与两只手（0/1 键=左手，2/3 键=右手）
TRACK_COUNT = 4
INITIAL_STAMINA = 10000.0
INITIAL_TAP_TIME = -3000
NEVER = -114514

ABILITY_KEYS: Tuple[str, ...] = ('stamina', 'speed', 'avg_accuracy', 'consistency', 'mentality')
ABILITY_LABELS: Dict[str, str] = {
    'stamina': '体力', 'speed': '手速', 'avg_accuracy': '准度',
    'consistency': '稳定', 'mentality': '心态',
}
ABILITY_MAX = 100

# 基准能力值：名字哈希在 [BASE_MIN, BASE_MAX] 上取值，再叠加风格偏置。
# 基准是"这个选手的底子"，同一个名字每局都一样；每局的浮动只来自下面的手感（受稳定性控制）。
BASE_MIN = 30
BASE_MAX = 85

# ---------------- 命中模型参数 ----------------
SPEED_GAP_MAX = 190.0     # 手速 0：同键间隔要 190ms 才算"从容"
SPEED_GAP_MIN = 70.0      # 手速 100：70ms 就够
SPEED_CURVE = 0.65        # 密度惩罚的曲线
ACC_FLOOR = 0.62          # 准度 0 时的基础命中率
ACC_GAIN = 0.40           # 准度 100 时再增加多少
STAMINA_FLOOR = 0.70      # 体力池耗尽时的乘数
STAMINA_DRAIN_K = 2.0     # 每次命中的体力消耗系数
STAMINA_EFF_MIN = 0.4     # 体力 0 时的体力效率
STAMINA_EFF_GAIN = 1.2    # 体力 100 时额外增加的效率
STAMINA_DEMAND_REF = 250.0  # 同键间隔参考值：比它密就费体力
STAMINA_DEMAND_MIN = 0.4
STAMINA_DEMAND_MAX = 3.0
# 打早的惩罚：只在音符前 EARLY_WINDOW_MS 之内才尝试，且提前越多越难打中。
# 这个尺度必须远小于判定窗口，否则选手会习惯性打早，把 PERFECT 打成 GREAT/GOOD，
# 而且"命中率越高 → 打得越早 → 判定越差"会出现反向。
EARLY_WINDOW_MS = 55.0
EARLY_DECAY_MS = 12.0
CONSISTENCY_ROLL = 0.30   # 单帧概率的抖动幅度（稳定性 0 时的 ±15%）
MENTAL_MAX_PENALTY = 0.45  # 心态 0 且压力拉满时，准度最多打 55 折
COMBO_PRESSURE_REF = 600.0   # 连击到多少算"压力拉满"
SCORE_PRESSURE_REF = 900000.0  # 分数（0~1000000）到多少算"压力拉满"
MATCH_POINT_PRESSURE = 0.4

# 选手风格：名字哈希决定，风格给五项能力加偏置
STYLES: Tuple[Tuple[str, Dict[str, int]], ...] = (
    ("均衡", {}),
    ("手速型", {'speed': 14, 'stamina': -5, 'avg_accuracy': -2, 'consistency': -2}),
    ("耐力型", {'stamina': 14, 'speed': -5, 'consistency': 4}),
    ("稳准型", {'avg_accuracy': 8, 'consistency': 14, 'mentality': 4, 'speed': -4}),
    ("爆发型", {'avg_accuracy': 12, 'speed': 6, 'stamina': -8, 'consistency': -8}),
    ("大心脏", {'mentality': 18, 'avg_accuracy': 4, 'consistency': 2, 'stamina': -2}),
    ("赌徒", {'avg_accuracy': 8, 'speed': 4, 'consistency': -16, 'mentality': -6}),
    ("天才", {'stamina': 8, 'speed': 8, 'avg_accuracy': 8, 'consistency': 8, 'mentality': 8}),
)
# 4 bit（16 种）映射到风格下标，"天才"只有 1/16
STYLE_TABLE: Tuple[int, ...] = (0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 6, 7)


class Player:
    def __init__(self, name: str, team_index: int, player_index: int,
                 judge_system: Optional[JudgeSystem] = None, form_range: int = 0):
        self.name = name
        self.team_index = team_index
        self.player_index = player_index
        self.judge_system = judge_system or JudgeSystem()

        # 能力值：基准（名字哈希 + 抖动 + 风格）与手感偏移，每首歌开始时由 roll_abilities 重算
        self.style: str = "均衡"
        self.base_abilities: Dict[str, int] = {}
        self.ability_form: Dict[str, int] = {}
        self.form_swing: int = 0
        self.stamina: int = 50
        self.speed: int = 50
        self.avg_accuracy: int = 50
        self.consistency: int = 50
        self.mentality: int = 50

        # 赛中状态
        self.match_point: bool = False   # 本局是不是赛点（由比赛在开局时告诉选手）

        self.roll_abilities(form_range)
        self.reset_for_new_song()

    # ------------------------------------------------------------------
    # 能力值
    # ------------------------------------------------------------------
    @staticmethod
    def hash_chunks(name: str) -> Tuple[List[int], int]:
        """名字哈希切出 5 个 0~63 的能力位，外加 4 bit 用来选风格。"""
        hash_int = int(hashlib.md5(name.encode('utf-8')).hexdigest(), 16)
        chunks = []
        work = hash_int
        for _ in range(len(ABILITY_KEYS)):
            chunks.append(work & 63)  # 每次取低 6 位
            work >>= 6
        style_bits = (hash_int >> (6 * len(ABILITY_KEYS))) & 15
        return chunks, style_bits

    def roll_abilities(self, form_range: int = 0) -> None:
        """重新生成本局能力值 = 名字哈希基准 + 风格偏置 + 本局手感。

        - 基准与风格只由名字决定，同一个名字每局完全一样（这才是"这个选手的底子"）
        - 手感幅度由稳定性决定：越稳的选手每局波动越小（稳定性 100 时只剩 1/4）
        """
        chunks, style_bits = self.hash_chunks(self.name)
        style_name, bias = STYLES[STYLE_TABLE[style_bits]]

        base: Dict[str, int] = {}
        for key, chunk in zip(ABILITY_KEYS, chunks):
            value = BASE_MIN + round(chunk / 63.0 * (BASE_MAX - BASE_MIN))
            value += bias.get(key, 0)
            base[key] = max(0, min(ABILITY_MAX, value))

        swing = round(form_range * (1.0 - 0.75 * (base['consistency'] / 100.0)))
        form: Dict[str, int] = {}
        for key in ABILITY_KEYS:
            delta = random.randint(-swing, swing) if swing > 0 else 0
            form[key] = delta
            setattr(self, key, max(0, min(ABILITY_MAX, base[key] + delta)))

        self.style = style_name
        self.base_abilities = base
        self.ability_form = form
        self.form_swing = swing

    def describe_abilities(self) -> str:
        """控制台用的一行能力值说明，括号内是本局手感偏移。"""
        values = "  ".join(
            f"{ABILITY_LABELS[key]} {getattr(self, key):3d}({self.ability_form.get(key, 0):+d})"
            for key in ABILITY_KEYS
        )
        return f"[{self.style}] {values}"

    @property
    def mental_factor(self) -> float:
        """心态对"有效准度"的乘数：压力越大、心态越差，越接近 1-MENTAL_MAX_PENALTY。

        压力由三部分组成：连击长度、自己的分数、以及"这一局是不是赛点"。
        前两项加起来最多 0.6，赛点再额外加 0.4 —— 这样赛点一定是明显的额外压力，
        而不是被前两项顶到上限后看不出来。
        """
        pressure = 0.30 * min(1.0, self.combo / COMBO_PRESSURE_REF)
        pressure += 0.30 * min(1.0, self.std_score / SCORE_PRESSURE_REF)
        if self.match_point:
            pressure = min(1.0, pressure + MATCH_POINT_PRESSURE)
        calm = self.mentality / 100.0
        return 1.0 - (1.0 - calm) * pressure * MENTAL_MAX_PENALTY

    @property
    def effective_accuracy(self) -> float:
        """被心态打折之后的准度，命中模型实际用的是这个值。"""
        return self.avg_accuracy * self.mental_factor

    # ------------------------------------------------------------------
    # 重置
    # ------------------------------------------------------------------
    def reset_for_new_song(self) -> None:
        """重置每首歌的临时状态（能力值保持不变）。"""
        self.score: float = 0.0
        self.std_score: float = 0.0
        self.combo: int = 0
        self.max_combo: int = 0
        self.accuracy: float = 100.0
        self.bonus: float = self.judge_system.config.bonus_start
        self.max_score: float = 1.0

        self.stamina_left: List[float] = [INITIAL_STAMINA, INITIAL_STAMINA]
        self.tap_times: List[int] = [INITIAL_TAP_TIME] * TRACK_COUNT
        self.active_notes: List[Note] = []

        self.judgement_counts: Dict[str, int] = {key: 0 for key in JUDGEMENTS}
        self.last_judgement: str = ""
        # 注意要包含 '' 这个键：初始状态下 last_judgement 就是 ''，渲染时会直接查表
        self.last_judge_time: Dict[str, int] = {key: NEVER for key in ('',) + JUDGEMENTS}

    def update_maxscore(self, total_combo: int) -> None:
        """开局时根据音符总数算出满分（用于把原始分标准化成 0~1000000）。"""
        config = self.judge_system.config
        per_note = config.score_values['perfect_g'] + config.bonus_max
        self.max_score = max(1.0, float(total_combo) * per_note)

    # ------------------------------------------------------------------
    # 每帧调用
    # ------------------------------------------------------------------
    def play(self, current_time: int) -> None:
        """核心的游玩调用函数，游戏中每帧调用一次。

        - 已经超过 miss 窗口还没打中的音符按漏掉结算（否则会永远卡在手里）
        - 其余音符尝试击打，打不中下一帧继续试（所以越难的地方越容易打晚）
        """
        miss_window = self.judge_system.config.miss
        for note in self.active_notes[:]:
            if current_time - note.time > miss_window:
                self._process_miss(note, current_time)
                continue
            if self._judge_if_click(axis_to_4k(note.x), note.time, current_time):
                self._process_hit(note, current_time)

    def _judge_if_click(self, track: int, tarTime: int, current_time: int) -> bool:
        """判断这一帧能不能打中这个音符。"""
        timedist = tarTime - current_time
        if timedist > EARLY_WINDOW_MS:
            return False  # 还太早，这一帧不尝试
        tapdist = max(1, current_time - self.tap_times[track])
        if tapdist == 1:
            return False
        hand = track >> 1

        # 手速：这一处的同键间隔对手速来说有多从容
        required_gap = SPEED_GAP_MAX - (SPEED_GAP_MAX - SPEED_GAP_MIN) * (self.speed / 100.0)
        speed_factor = min(1.0, tapdist / required_gap) ** SPEED_CURVE

        # 准度（已被心态按当前压力打折）
        accuracy_factor = ACC_FLOOR + ACC_GAIN * (self.effective_accuracy / 100.0)

        # 体力：池子越空越吃力
        stamina_factor = STAMINA_FLOOR + (1.0 - STAMINA_FLOOR) * (self.stamina_left[hand] / INITIAL_STAMINA)

        # 稳定性：只影响这一帧概率的抖动大小，不改变平均值
        spread = 1.0 - 0.6 * (self.consistency / 100.0)   # 稳定 0 -> 1.0，稳定 100 -> 0.4
        roll = 1.0 + spread * (CONSISTENCY_ROLL / 2.0 - random.random() * CONSISTENCY_ROLL)

        chance = accuracy_factor * speed_factor * stamina_factor * roll

        # 打早了更难：越早越接近 0
        if timedist > 0:
            chance *= math.exp(-timedist / EARLY_DECAY_MS)

        if random.random() < min(1.0, chance):
            self._drain_stamina(hand, tapdist)
            return True
        return False

    def _drain_stamina(self, hand: int, tapdist: int) -> None:
        """打中一次要花多少体力：越密越费，体力越高越省。"""
        demand = min(STAMINA_DEMAND_MAX,
                     max(STAMINA_DEMAND_MIN, STAMINA_DEMAND_REF / max(tapdist, 25)))
        efficiency = STAMINA_EFF_MIN + STAMINA_EFF_GAIN * (self.stamina / 100.0)
        self.stamina_left[hand] = max(
            0.0, self.stamina_left[hand] - STAMINA_DRAIN_K * demand / efficiency)

    # ------------------------------------------------------------------
    # 结算
    # ------------------------------------------------------------------
    def _process_hit(self, note: Note, current_time: int) -> Dict[str, Any]:
        """处理一次击打，返回判定结果和分数。"""
        time_diff = note.time - current_time
        judgement = self.judge_system.get_judgement(time_diff)
        self._remove_note(note)
        score_info = self._calculate_score(judgement)
        self._register_judgement(judgement, note, current_time)
        return {
            'judgement': judgement,
            'score': score_info['score'],
            'time_diff': time_diff,
        }

    def _process_miss(self, note: Note, current_time: int) -> None:
        """音符超时未打中：按 miss 结算并丢掉它。"""
        self._remove_note(note)
        self._calculate_score('miss')
        self._register_judgement('miss', note, current_time)

    def _remove_note(self, note: Note) -> None:
        try:
            self.active_notes.remove(note)
        except ValueError:
            pass

    def _register_judgement(self, judgement: str, note: Note, current_time: int) -> None:
        if judgement not in ('miss', ''):
            self.combo += 1
            self.max_combo = max(self.max_combo, self.combo)
        else:
            self.combo = 0

        self.judgement_counts[judgement] = self.judgement_counts.get(judgement, 0) + 1
        if judgement != 'miss':
            self.tap_times[axis_to_4k(note.x)] = current_time
        self.last_judge_time[judgement] = current_time
        # perfect 不覆盖上一次显示的判定，让屏幕上的判定文字自然淡出
        if judgement not in ('perfect_g', 'perfect'):
            self.last_judgement = judgement
        self._update_accuracy()

    def _calculate_score(self, judgement: str) -> Dict[str, float]:
        """按判定累加原始分，并标准化成 std_score。"""
        config = self.judge_system.config
        self.bonus += config.bonus_values[judgement]
        self.bonus = max(0.0, min(config.bonus_max, self.bonus))
        self.score += config.score_values[judgement] + self.bonus
        self.std_score = self.score * 1000000.0 / max(1.0, self.max_score)
        return {'score': self.std_score}

    def _update_accuracy(self) -> None:
        """重新计算准确率。"""
        total_hits = sum(self.judgement_counts.values())
        if total_hits > 0:
            weighted_sum = (
                self.judgement_counts['perfect_g'] * 300 +
                self.judgement_counts['perfect'] * 300 +
                self.judgement_counts['great'] * 200 +
                self.judgement_counts['good'] * 100 +
                self.judgement_counts['bad'] * 50
            )
            self.accuracy = (weighted_sum / (total_hits * 300)) * 100

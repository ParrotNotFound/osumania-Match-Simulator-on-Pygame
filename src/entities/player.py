# src/entities/player.py
"""模拟玩家：五项能力值 + 选手风格 + 赛中压力。

判定模型：**统一的落点误差模型**。
每个音符只结算一次（在它到点的那一帧），选手一定会去按 —— 没有"按不按键"的随机，
只有"按下去的时刻偏了多少"：

    press_offset = μ + N(0, σ)        相对音符时间的偏移，正数 = 打晚
    σ = σ_准度 × (1 + 密度压力×k1) × (1 + 疲劳×k2) × (1 + 不稳定性×k3)
    μ = 密度压力×k4 + 疲劳×k5
    判定 = get_judgement(press_offset)  落在 bad 窗口内 = 打中，超出去 = 漏键

所以漏键不是掷骰子掷出来的，而是"误差超出判定窗口"的结果：
跟不上（手速不够）→ 误差被推大、整体越打越晚；累了（体力见底）→ 误差和延迟一起涨。
准度是误差地板，只在"人人都跟得上"的低难图里才有区分度。

能力分工（全部 0~100，每首歌开始时重算）：
    手速 speed         能从容处理多密的同键间隔：跟不上时误差被放大、整体打晚
    体力 stamina       每首歌的耐久：池子越空，落点越飘越晚，后半段开始漏
    准度 avg_accuracy  落点误差的地板 σ（准度越高越贴近音符）
    稳定 consistency   落点误差的抖动幅度 + 每局手感的波动幅度
    心态 mentality     连击够长、并且体力吃紧时"手抖一下"：给落点加一个大延迟，
                       表现为 BAD 或擦边 MISS（压力大小取自"还剩多少体力"）

选手风格由名字哈希决定（同一个名字风格固定），风格会给上面五项加不同的偏置。
"""
from __future__ import annotations

import hashlib
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
# 基准是"这个选手的底子"，同一个名字每局都一样；每局的浮动只来自手感（受稳定性控制）。
BASE_MIN = 30
BASE_MAX = 85

# ---------------- 落点误差模型参数 ----------------
# σ（落点误差）的各项是**相加**的，这是"低难靠准度、高难靠手速"的关键：
#   低难图  密度压力≈0、几乎不疲劳 → σ 基本只剩准度那一项，分数被准度拉开；
#   高难图  密度压力把 σ 顶到几十毫秒 → 准度那几毫秒的差别被淹没，手速决定谁不漏。
SPEED_GAP_MAX = 230.0     # 手速 0：同键间隔要 230ms 才算跟得上
SPEED_GAP_MIN = 85.0      # 手速 100：85ms 就跟得上
DENSITY_SIGMA_ADD = 38.0     # 密度压力**平方**后每 1 单位额外增加的误差标准差（毫秒）
DENSITY_LATENCY = 50.0       # 密度压力**平方**后每 1 单位整体打晚多少毫秒
FATIGUE_SIGMA_ADD = 15.0     # 体力彻底见底时额外增加的误差标准差
FATIGUE_LATENCY = 22.0       # 体力彻底见底时整体打晚多少毫秒
CONSISTENCY_SIGMA_ADD = 3.0  # 稳定性 0 时额外增加的误差标准差（稳定性 100 时不加）
DENSITY_EXPONENT = 2.0       # >1 把惩罚集中到"真的跟不上"的地方
TIMING_SIGMA_MAX = 12.0   # 准度 0 时的落点误差标准差（毫秒）
TIMING_SIGMA_MIN = 1.5    # 准度 100 时的落点误差标准差
# 长条的"松手判定"：不吃手速（不算同键间隔、也不耗体力），但比点击更容易打偏
LONG_RELEASE_SIGMA_SCALE = 1.8
# 体力消耗：越密越费，体力越高越省
STAMINA_DRAIN_K = 2.0
STAMINA_EFF_MIN = 0.4
STAMINA_EFF_GAIN = 1.2
STAMINA_DEMAND_REF = 250.0
STAMINA_DEMAND_MIN = 0.4
STAMINA_DEMAND_MAX = 3.0
# 心态崩盘（手抖）：连击够长、并且体力已经吃紧时才会发生。
# 它不是"凭空漏键"，而是给落点加一个很大的延迟 —— 表现为 BAD 或擦边 MISS，
# 具体算哪一种取决于判定窗口，改窗口不用改这里。
CHOKE_COMBO_FLOOR = 500.0   # 连击不到 500 完全不紧张
CHOKE_COMBO_CAP = 2000.0    # 连击到 2000 才把连击带来的紧张度拉满
CHOKE_PER_NOTE_MAX = 0.025  # 心态 0 + 连击拉满 + 体力见底 + 压力拉满时的崩率
CHOKE_SCORE_BOOST = 0.5     # 自己分高时的额外倍率
CHOKE_MATCH_POINT_BOOST = 1.5  # 赛点的额外倍率
CHOKE_LATENCY = 120.0       # 手抖时落点整体偏晚多少毫秒
CHOKE_SIGMA = 45.0          # 手抖时的抖动幅度
SCORE_PRESSURE_REF = 900000.0  # 分数（0~1000000）到多少算"高分"

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

        # 能力值：基准（名字哈希 + 风格）与手感偏移，每首歌开始时由 roll_abilities 重算
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

    # ------------------------------------------------------------------
    # 误差分布
    # ------------------------------------------------------------------
    @property
    def timing_sigma(self) -> float:
        """准度决定的落点误差地板（毫秒）。"""
        return TIMING_SIGMA_MIN + (TIMING_SIGMA_MAX - TIMING_SIGMA_MIN) * (1.0 - self.avg_accuracy / 100.0)

    @property
    def fatigue(self) -> float:
        """0 = 体力充沛，1 = 两只手都见底。"""
        average = sum(self.stamina_left) / (2.0 * INITIAL_STAMINA)
        return max(0.0, min(1.0, 1.0 - average))

    @property
    def choke_chance(self) -> float:
        """当前这一瞬间"手抖一下"的概率。

        两个前提缺一不可：
        - 连击够长（< CHOKE_COMBO_FLOOR 时完全不紧张）；
        - **体力已经吃紧** —— 压力大小直接取"还剩多少体力"的补数，
          体力满的时候基本不会手抖，所以低难图（几乎不掉体力）不会莫名其妙冒漏键，
          压力自然集中在长歌的后半段和密谱上。
        自己分高、或者进入赛点会再放大一点。
        """
        fragility = 1.0 - self.mentality / 100.0
        if fragility <= 0.0 or self.combo < CHOKE_COMBO_FLOOR:
            return 0.0
        combo_progress = min(1.0, (self.combo - CHOKE_COMBO_FLOOR)
                             / max(1.0, CHOKE_COMBO_CAP - CHOKE_COMBO_FLOOR))
        strain = self.fatigue   # 0 = 满体力，1 = 两手见底
        boost = 1.0 + CHOKE_SCORE_BOOST * min(1.0, self.std_score / SCORE_PRESSURE_REF)
        if self.match_point:
            boost += CHOKE_MATCH_POINT_BOOST
        return CHOKE_PER_NOTE_MAX * fragility * combo_progress * strain * boost

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
        # 每个键位上一个"已经结算过的音符时间"，用来算同键间隔（≈ 谱面密度）
        self.last_note_time: List[int] = [INITIAL_TAP_TIME] * TRACK_COUNT
        self.active_notes: List[Note] = []
        # 已经按下、还在按住的长条（等松手判定）
        self.holding_notes: List[Note] = []

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
        """每帧调用：把所有"已经到点"的音符各结算一次。

        - 普通音符：到点结算一次点击判定，然后丢出 active_notes；
        - 长条：到点结算一次点击判定，打中就挪进 holding_notes 继续按住，
          到结束时间再结算一次松手判定；头都没打中的话整根就没了。
        """
        for note in self.active_notes[:]:
            if note.time > current_time:
                continue  # 还没到点，下一帧再说
            self._resolve_note(note, current_time)

        for note in self.holding_notes[:]:
            if current_time >= note.end_time:
                self._resolve_release(note, current_time)

    def _resolve_note(self, note: Note, current_time: int) -> None:
        """结算一个音符的"按下"：落点误差 + 可能的手抖，再算判定。"""
        track = axis_to_4k(note.x)
        hand = track >> 1

        previous = self.last_note_time[track]
        tapdist = note.time - previous
        self.last_note_time[track] = note.time
        self._remove_note(note)

        # 先按当前体力状态出手，再结算这一下消耗掉的体力
        press_offset = self._press_offset(tapdist) + self._choke_offset()
        self._drain_stamina(hand, tapdist)

        judgement = self.judge_system.get_judgement(press_offset)
        if judgement == 'miss':
            self._process_miss(note, current_time)
        else:
            self._process_hit(note, current_time, press_offset)
            if note.is_long:
                self.holding_notes.append(note)

    def _resolve_release(self, note: Note, current_time: int) -> None:
        """结算长条的"松手"：另算一次判定，不吃手速也不耗体力。"""
        try:
            self.holding_notes.remove(note)
        except ValueError:
            pass
        release_offset = self._release_offset()
        judgement = self.judge_system.get_judgement(release_offset)
        if judgement == 'miss':
            self._process_miss(note, current_time)
        else:
            self._process_hit(note, current_time, release_offset)

    def _release_offset(self) -> float:
        """松手判定的落点误差。

        和点击判定的区别只有两条：
        - **不吃手速**：不算同键间隔，因此没有密度压力项，也不消耗体力
          （手速只作用于点击判定）；
        - 更容易打偏：σ 整体乘 LONG_RELEASE_SIGMA_SCALE。
        其余（准度地板、疲劳、稳定性）和点击判定一样。
        """
        fatigue = self.fatigue
        sigma = (self.timing_sigma
                 + FATIGUE_SIGMA_ADD * fatigue
                 + CONSISTENCY_SIGMA_ADD * (1.0 - self.consistency / 100.0))
        sigma *= LONG_RELEASE_SIGMA_SCALE
        return random.gauss(FATIGUE_LATENCY * fatigue, sigma)

    def _press_offset(self, tapdist: int) -> float:
        """这一次按键相对音符时间偏了多少毫秒（正数 = 打晚）。

        σ（误差大小）由准度打底，密度压力、疲劳、不稳定性各自**加上**一份；
        μ（整体偏晚）体现"跟不上、累了会越打越晚"。
        """
        required_gap = SPEED_GAP_MAX - (SPEED_GAP_MAX - SPEED_GAP_MIN) * (self.speed / 100.0)
        # 略微超出能力范围只是"有点吃力"，真的差一大截才会崩：
        # 压力取平方后，密度刚好卡在能力边缘时惩罚很小，跟不上时惩罚迅速放大
        density_pressure = max(0.0, required_gap / max(tapdist, 25) - 1.0) ** DENSITY_EXPONENT
        fatigue = self.fatigue

        # σ 由准度打底，密度压力、疲劳、不稳定性各自加上一份；μ 是整体偏晚的部分。
        # 这里没有"随机手滑"通道：准度只决定误差大小，不会突然把某个音符甩飞。
        sigma = (self.timing_sigma
                 + DENSITY_SIGMA_ADD * density_pressure
                 + FATIGUE_SIGMA_ADD * fatigue
                 + CONSISTENCY_SIGMA_ADD * (1.0 - self.consistency / 100.0))

        mu = DENSITY_LATENCY * density_pressure + FATIGUE_LATENCY * fatigue
        return random.gauss(mu, sigma)

    def _drain_stamina(self, hand: int, tapdist: int) -> None:
        """打一个音符要花多少体力：越密越费，体力越高越省。"""
        demand = min(STAMINA_DEMAND_MAX,
                     max(STAMINA_DEMAND_MIN, STAMINA_DEMAND_REF / max(tapdist, 25)))
        efficiency = STAMINA_EFF_MIN + STAMINA_EFF_GAIN * (self.stamina / 100.0)
        self.stamina_left[hand] = max(
            0.0, self.stamina_left[hand] - STAMINA_DRAIN_K * demand / efficiency)

    def _choke_offset(self) -> float:
        """心态崩盘：不是凭空掉键，而是"手抖一下"——给落点加一个很大的延迟。

        每个音符只结算一次，所以这里天然只会掷一次骰子。
        """
        chance = self.choke_chance
        if chance <= 0.0:
            return 0.0
        if random.random() < chance:
            return random.gauss(CHOKE_LATENCY, CHOKE_SIGMA)
        return 0.0

    # ------------------------------------------------------------------
    # 结算
    # ------------------------------------------------------------------
    def _process_hit(self, note: Note, current_time: int, press_offset: float) -> Dict[str, Any]:
        """处理一次击打：press_offset 就是已经算好的落点偏移。"""
        judgement = self.judge_system.get_judgement(press_offset)
        score_info = self._calculate_score(judgement)
        self._register_judgement(judgement, note, current_time)
        return {
            'judgement': judgement,
            'score': score_info['score'],
            'time_diff': press_offset,
        }

    def _process_miss(self, note: Note, current_time: int) -> None:
        """这个音符漏了：按 miss 结算。"""
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
        """重新计算准确率（bad 还能续连击，所以它会明显拉低准确率但不断连）。"""
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

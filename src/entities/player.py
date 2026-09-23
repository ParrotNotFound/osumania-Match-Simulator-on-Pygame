# src/entities/player.py
"""模拟玩家：五项能力值 + 选手风格 + 赛中压力。

判定模型：**统一的落点误差模型**。
每个音符只结算一次（在它到点的那一帧），选手一定会去按 —— 没有"按不按键"的随机，
只有"按下去的时刻偏了多少"：

    press_offset = μ + N(0, σ)        相对音符时间的偏移，正数 = 打晚
    σ = σ_准度 + 密度压力项 + 疲劳项 (+ 稳定性项，仅长条松手)
    μ = 密度压力×k4 + 疲劳×k5
    判定 = get_judgement(press_offset)  落在 bad 窗口内 = 打中，超出去 = 漏键

所以漏键不是掷骰子掷出来的，而是"误差超出判定窗口"的结果：
跟不上（手速不够）→ 误差被推大、整体越打越晚；累了（体力见底）→ 误差和延迟一起涨。
准度是误差地板，只在"人人都跟得上"的低难图里才有区分度 —— 为了让它在**简单谱面**上也真的
分得出高下，这一项不是线性的：见 `TIMING_SIGMA_EXPONENT`（低准度迅速变飘，高准度之间也留差距）。

能力分工（全部 0~100，每首歌开始时重算）：
    手速 speed         能从容处理多密的同键间隔：跟不上时误差被放大、整体打晚
    体力 stamina       每首歌的耐久：池子越空，落点越飘越晚，后半段开始漏
    准度 avg_accuracy  落点误差的地板 σ（准度越高越贴近音符；0→26ms、50→7.6ms、100→1.5ms）
    稳定 consistency   长条**松手**的精度 + 每局手感的波动幅度（不影响按下的精度）
    心态 mentality     连击够长、并且体力吃紧时"手抖一下"：给落点加一个大延迟，
                       表现为 BAD 或擦边 MISS（压力大小取自"还剩多少体力"）

选手中，手感只浮动体力/手速/准度三项；稳定和心态每局恒为基准值。
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

# 每局手感只浮动这三项。
# 稳定和心态不参与：稳定本来就是用来决定手感幅度的（手感再反过来改稳定就是循环依赖），
# 心态和它一样属于"底层特质"，每局保持基准值。
FORM_KEYS: Tuple[str, ...] = ('stamina', 'speed', 'avg_accuracy')

# 基准能力值：名字哈希在 [BASE_MIN, BASE_MAX] 上取值，再叠加风格偏置。
# 基准是"这个选手的底子"，同一个名字每局都一样；每局的浮动只来自手感（受稳定性控制）。
BASE_MIN = 30
BASE_MAX = 85

# ---------------- 落点误差模型参数 ----------------
# σ（落点误差）的各项是**相加**的，这是"低难靠准度、高难靠手速"的关键：
#   低难图  密度压力≈0、几乎不疲劳 → σ 基本只剩准度那一项，大 P 率被准度拉开；
#   高难图  密度压力把 σ 顶到几十毫秒 → 准度那份的相对权重被摊薄，手速决定谁不漏。
SPEED_GAP_MAX = 230.0     # 手速 0：同键间隔要 230ms 才算跟得上
SPEED_GAP_MIN = 85.0      # 手速 100：85ms 就跟得上
DENSITY_SIGMA_ADD = 38.0     # 密度压力**平方**后每 1 单位额外增加的误差标准差（毫秒）
DENSITY_LATENCY = 50.0       # 密度压力**平方**后每 1 单位整体打晚多少毫秒
FATIGUE_SIGMA_ADD = 15.0     # 体力彻底见底时额外增加的误差标准差
FATIGUE_LATENCY = 22.0       # 体力彻底见底时整体打晚多少毫秒
CONSISTENCY_SIGMA_ADD = 3.0  # 稳定性 0 时长条**松手**判定的额外 σ（只作用于松手，不作用于按下）
DENSITY_EXPONENT = 2.0       # >1 把惩罚集中到"真的跟不上"的地方
TIMING_SIGMA_MAX = 26.0   # 准度 0 时的落点误差标准差（毫秒）
TIMING_SIGMA_MIN = 1.5    # 准度 100 时的落点误差标准差
# 准度 → σ 的曲线指数：σ = MIN + (MAX − MIN) × ((100 − 准度)/100)^TIMING_SIGMA_EXPONENT。
# 取 1 就是线性；取 >1 时中高准度之间也不会挤在一起 —— 这是"简单谱面"上准度还能比出高下的关键：
# 线性写法下准度 40 与 80 的 σ 都远在大 P 窗口（±17~21ms）里面，大 P 率一个 96% 一个 100%，
# 准确率只差 0.05pp，准度这项形同虚设。曲线写法把差距摊到整条曲线上。
TIMING_SIGMA_EXPONENT = 2.0
# 但真正麻烦的是"简单谱的高准段"：OD6.5 的大 P 窗口有 ±17ms，正态分布下 σ 只要低于 ~4ms，
# 大 P 率就是 100.0000%。原来那条光滑曲线在准度 70 时 σ 就只剩 3.7ms，70/80/85/90/100 全被
# 拍平在"全大 P"上 —— 高准段没有含金量，差距全挤在 20~60。
# 所以高准段改用下面这张锚点表（分段线性插值），把 70~80 的 σ 顶起来、85 以后迅速压到地板：
#   准度 0→26.0  40→10.3  60→5.4  70→6.2  80→5.6  90→3.2  100→1.5
# 60 及以下与原来那条曲线完全一致（20~60 仍是主要差距区，行为不变）。
# 注意物理天花板：大 P 与普通 P 只差 5 点权重（305 vs 300），所以哪怕大 P 率从 100% 掉到 85%，
# 总分也只差 0.5% —— 想让高准段拉开更大，只能再动权重（ACCURACY_BASE），那是另一件事。
TIMING_SIGMA_ANCHORS: Tuple[Tuple[float, float], ...] = (
    (0.0, 26.0), (40.0, 10.3), (60.0, 5.4), (70.0, 6.2),
    (80.0, 5.6), (90.0, 3.2), (100.0, 1.5),
)
# 「中段鼓包」：整体难度调整用，**只压准度能力值的中间段**，两头保持原样：
#   准度 ≤ 30      ：完全不动（低准度本来就差，不用再压，这是"30 档如之前一样"）
#   准度 40~90     ：σ 乘 TIMING_SIGMA_MID_BOOST（这一段整体下降）
#   准度 90~95     ：线性收回原曲线，避免"准度 90 比 95 差一大截"的断崖
#   准度 ≥ 95      ：回到原曲线（超高准的含金量不受影响，这是"95+ 如之前一样"）
# 三条边界都是可调常量，方便按"中段该降多少"微调。
TIMING_SIGMA_MID_BOOST = 1.25
TIMING_SIGMA_MID_BOOST_FLOOR = 30.0   # 这个准度及以下不鼓包
TIMING_SIGMA_MID_BOOST_EDGE = 95.0    # 这个准度及以上恢复原曲线
# 「准度地板」的受力衰减：压力大的时候，精度差异不再是决定因素。
#
# 准度地板是一个**绝对毫秒数**（1.5~26ms），它不知道谱面有多难。难谱上体力/密度已经顶着一份
# 很大的误差（实测 Vacant 的疲劳项均值就有 5.1ms），准度地板再以满额叠上去，σ 就会跨过 bad
# 窗口 —— 变成"准度低的人漏键"，也就是准度跑到难谱上去决定生死了。而设计要求是：
# **准度主要影响简单曲目里小 P（普通 P）的出现频率，不该在考验上限的谱上搅局。**
#
#     σ = 准度地板 × β + 密度项 + 疲劳项
#     β = exp( -( (密度项 + 疲劳项) / TIMING_SIGMA_DAMP_K )² )
#
# 只压准度地板，手速（密度）和体力（疲劳）的项一个不动 —— 难谱的生死仍然交给它们。
# 用平方指数而不是 1/(1+s/k)：中间档还要吃准度，难档要迅速失效。
#   其它两项之和 0ms（简单谱） → β=1.00，准度满额生效
#   1.6ms（Love!）           → β=0.75
#   5.1ms（Vacant）          → β=0.05，准度基本退场
TIMING_SIGMA_DAMP_K = 3.0
# 「慢漂移」：人类落点误差里**不是**白噪声的那一份。
#
# 白噪声的模型下，σ ≤ 3ms 就意味着"725 个音符全落在大 P 窗口（±17ms）里"是必然事件
# （单音符掉出去的概率 ~1e-5 → 全对 ~99%）—— 于是高准选手必然是"全大 P"，不像人。
# 真实的人打简单谱并不是"每一下都贴着中心"，而是**整段偏一点**（打早/打晚一个稳定的量，
# 几十秒内来回摆）。加上这一条之后，大 P 率就从"必然 100%"落到 95~99%、准确率落在
# 99.7~99.9%，而且偏差是**成段出现**的 —— 画面/曲线上一眼看得出"人的痕迹"。
#
# 实现：每只手一条漂移，按判定逐次演化（指数衰减 + 白噪声），直接加进落点：
#     drift ← drift × TIMING_DRIFT_DECAY + N(0, TIMING_DRIFT_SIGMA)
#     落点 = μ + N(0, σ) + drift_hand
# 真正决定"大 P 率掉多少"的是它的**稳态幅度**：
#     σ_漂移 ≈ TIMING_DRIFT_SIGMA / sqrt(1 − DECAY²) = 2.5 / sqrt(1−0.94²) ≈ 6.9ms
# 实测（Science[Easy] 732 判定、准度 90）：大 P 率 100% → 98%、准确率 100% → 99.97%，
# 一局里多出十来个 GREAT —— 这才是"人"的样子。想更飘就调大 TIMING_DRIFT_SIGMA。
TIMING_DRIFT_SIGMA = 2.5     # 每次判定给漂移加多少白噪声（毫秒）
TIMING_DRIFT_DECAY = 0.94    # 漂移的记忆系数：越小漂得越快、成段感越弱
# 长条的"松手判定"：不吃手速（不算同键间隔、也不耗体力），但比点击更容易打偏
LONG_RELEASE_SIGMA_SCALE = 1.8
# 体力消耗：越密越费，体力越高越省
STAMINA_DRAIN_K = 3.0
STAMINA_EFF_MIN = 0.4
STAMINA_EFF_GAIN = 1.2
STAMINA_DEMAND_REF = 250.0
STAMINA_DEMAND_MIN = 0.4
STAMINA_DEMAND_MAX = 3.0
# 体力回复：一只手歇得越久回得越多（只有超过 STAMINA_RECOVER_FLOOR 的空档才算休息）
STAMINA_RECOVER_K = 120.0       # 每秒空档回复多少体力
STAMINA_RECOVER_FLOOR = 400.0   # 小于这个间隔（毫秒）算连续输出，不回复
# 心态崩盘（手抖）：连击够长、并且体力已经吃紧时才会发生。
# 它不是"凭空漏键"，而是给落点加一个很大的延迟 —— 表现为 BAD 或擦边 MISS，
# 具体算哪一种取决于判定窗口，改窗口不用改这里。
CHOKE_COMBO_FLOOR = 500.0   # 连击不到 500 完全不紧张
CHOKE_COMBO_CAP = 2000.0    # 连击到 2000 才把连击带来的紧张度拉满
CHOKE_FATIGUE_FLOOR = 0.25  # 疲劳不到这个程度完全不紧张（体力还宽裕就不该手抖）
CHOKE_PER_NOTE_MAX = 0.025  # 心态 0 + 连击拉满 + 体力见底 + 压力拉满时的崩率
CHOKE_SCORE_BOOST = 0.5     # 自己分高时的额外倍率
CHOKE_MATCH_POINT_BOOST = 1.5  # 赛点的额外倍率
CHOKE_LATENCY = 120.0       # 手抖时落点整体偏晚多少毫秒
CHOKE_SIGMA = 45.0          # 手抖时的抖动幅度
SCORE_PRESSURE_REF = 1000000.0  # 分数（0~1000000）到多少算"高分"

# ---------------- 计分：照搬 osu!lazer 的 mania 方案 ----------------
# 源码：osu.Game.Rulesets.Mania/Scoring/ManiaScoreProcessor.cs
#   总分 = 150000 × 连击进度
#        + 850000 × 准度^(2 + 2×准度) × 准度进度
#        + bonusPortion（mania 没有 bonus 判定，恒为 0）
#   连击分增量 = 基础分 × clamp(log4(当前连击), 0.5, log4(400))
#   准度 = Σ基础分 / (已判定次数 × 305)
# 沿用它自己的判定权重，和本项目的五档一一对应：
#   perfect_g↔Perfect(305)  perfect↔Great(300)  great↔Good(200)
#   good↔Ok(100)            bad↔Meh(50)         miss↔Miss(0)
SCORE_COMBO_PORTION = 150000.0
SCORE_ACCURACY_PORTION = 850000.0
COMBO_LOG_BASE = 4.0            # 连击乘数取以 4 为底的对数
COMBO_MULT_CAP_COUNT = 400      # 连击到 400 乘数封顶（log4(400) ≈ 4.322）
COMBO_MULT_MIN = 0.5            # 连击太少时的乘数下限
# 计分赛成绩表里的"总分"是每首歌之和，所以上面这三段加起来就是一首歌的满分。
SCORE_MAX_TOTAL = SCORE_COMBO_PORTION + SCORE_ACCURACY_PORTION


def _combo_multiplier(combo: int) -> float:
    """连击数 → 连击分乘数：clamp(log4(连击), 0.5, log4(400))。"""
    if combo <= 0:
        return COMBO_MULT_MIN
    return min(max(COMBO_MULT_MIN, math.log(combo, COMBO_LOG_BASE)),
               math.log(COMBO_MULT_CAP_COUNT, COMBO_LOG_BASE))


def timing_sigma_for(accuracy: float) -> float:
    """准度 → 落点误差的 σ 地板（毫秒）。

    这是玩家身上**唯一**一处"由准度决定"的误差
    （另一份是密度压力/疲劳，那是手速和体力的事）。

    基线是 `TIMING_SIGMA_ANCHORS` 那张锚点表的分段线性插值（超出两端取端点值）：
        准度   0 → 26.0     20 → 17.2     40 → 10.3     50 → 7.6
              60 →  5.4     70 →  6.2     80 →  5.6     90 → 3.2     100 → 1.5
    60 及以下与更早那条光滑曲线一致；70~80 被特意顶高（削弱中高准），85 以后迅速压到地板
    （凸显超高准）。为什么不用一条光滑曲线：OD6.5 的大 P 窗口 ±17ms，σ 低于 ~4ms 就 100% 大 P，
    光滑曲线在准度 70 时已经掉到 3.7ms，把 70~100 全拍平了。

    在此之上再叠「中段鼓包」（`TIMING_SIGMA_MID_BOOST`）：只把准度 40~90 的 σ 抬高一截，
    30 及以下与 95 及以上保持原样，90~95 线性收回，避免断崖。

    游戏里（Player.timing_sigma）和独立小工具（ability.py）都走这一个函数，
    免得两处各写一遍、改了一处忘了另一处。
    """
    value = max(0.0, min(100.0, float(accuracy)))
    return _sigma_from_anchors(value) * _mid_boost_factor(value)


def _sigma_from_anchors(accuracy: float) -> float:
    """锚点表本身的分段线性插值（不含中段鼓包）。"""
    anchors = TIMING_SIGMA_ANCHORS
    if accuracy <= anchors[0][0]:
        return anchors[0][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if accuracy <= x1:
            if x1 <= x0:
                return y1
            return y0 + (y1 - y0) * (accuracy - x0) / (x1 - x0)
    return anchors[-1][1]


def _mid_boost_factor(accuracy: float) -> float:
    """准度 → 中段鼓包的倍率（1.0 = 不动）。

    [FLOOR, 90] 区间乘 `TIMING_SIGMA_MID_BOOST`；FLOOR 以下与 EDGE 以上是 1.0；
    90~EDGE 线性收回 1.0（所以 90→95 是平滑过渡，不是断崖）。
    """
    boost = TIMING_SIGMA_MID_BOOST
    if boost == 1.0:
        return 1.0
    floor = TIMING_SIGMA_MID_BOOST_FLOOR
    edge = TIMING_SIGMA_MID_BOOST_EDGE
    if accuracy <= floor or accuracy >= edge or edge <= 90.0:
        return 1.0
    if accuracy <= 90.0:
        return boost
    return boost + (1.0 - boost) * (accuracy - 90.0) / (edge - 90.0)


ACCURACY_BASE: Dict[str, int] = {
    'perfect_g': 305, 'perfect': 300, 'great': 200, 'good': 100, 'bad': 50, 'miss': 0,
}
# 连击分用的基础分：lazer 里 Perfect(305) 在连击分里按 300 算，其余和准度权重相同
COMBO_BASE_SCORE: Dict[str, int] = {
    'perfect_g': 300, 'perfect': 300, 'great': 200, 'good': 100, 'bad': 50, 'miss': 0,
}

# 选手风格：名字哈希决定，风格给五项能力加偏置
STYLES: Tuple[Tuple[str, Dict[str, int]], ...] = (
    ("均衡型", {'stamina': 6, 'speed': 6, 'avg_accuracy': 6}),
    ("手速型", {'speed': 20, 'stamina': -5, 'avg_accuracy': -2, 'consistency': -2}),
    ("耐力型", {'stamina': 20, 'speed': -2, 'consistency': 4}),
    ("稳定型", {'avg_accuracy': 12, 'consistency': 35, 'mentality': 4}),
    ("准度型", {'avg_accuracy': 20}),
    ("大心脏", {'mentality': 35, 'stamina': 2, 'speed': 2, 'avg_accuracy': 10, 'consistency': 2}),
    ("赌徒型", {'avg_accuracy': 8, 'speed': 8, 'consistency': -30, 'mentality': 4}),
    ("保连型", {'stamina': 12, 'speed': 12, 'avg_accuracy': -10,  'mentality': 8}),
)
# 4 bit（16 种）映射到风格下标
STYLE_TABLE: Tuple[int, ...] = (0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7)


class Player:
    def __init__(self, name: str, team_index: int, player_index: int,
                 judge_system: Optional[JudgeSystem] = None, form_range: int = 0,
                 timing_sigma_override: Optional[Tuple[float, float]] = None):
        self.name = name
        self.team_index = team_index
        self.player_index = player_index
        self.judge_system = judge_system or JudgeSystem()
        # 只给标定脚本用：临时把 σ 地板换成别的 (上限, 曲线指数) 组合，
        # 扫参时不用改源码常量。正常对局永远是 None。
        self.timing_sigma_override = timing_sigma_override

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

        # 计分（lazer 口径）：本谱总判定数、以及三个"分母"
        self.total_judgements: int = 1
        self.maximum_base_sum: float = float(ACCURACY_BASE['perfect_g'])
        self.maximum_combo_portion: float = (ACCURACY_BASE['perfect_g']
                                             * math.log(COMBO_MULT_CAP_COUNT, COMBO_LOG_BASE))
        self.accuracy_base_sum: float = 0.0
        self.combo_portion: float = 0.0
        self.accuracy_judged: int = 0

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
        - 手感只浮动 FORM_KEYS 那三项（体力/手速/准度）；
          稳定和心态始终等于基准值 —— 稳定是拿来决定手感幅度的，不能被手感反过来改
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
            delta = random.randint(-swing, swing) if (swing > 0 and key in FORM_KEYS) else 0
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
        """准度决定的落点误差地板（毫秒）：σ 随准度下降而变大。

        正式曲线见 `TIMING_SIGMA_ANCHORS` + 中段鼓包（`timing_sigma_for`）。
        `timing_sigma_override` 是给标定脚本用的旁路：填 (上限, 指数) 就临时改用
        那条光滑曲线算（同样叠中段鼓包，方便对比），不填就走正式锚点表。
        """
        accuracy = max(0.0, min(100.0, float(self.avg_accuracy)))
        if self.timing_sigma_override is not None:
            sigma_max, exponent = self.timing_sigma_override
            progress = 1.0 - accuracy / 100.0
            base = TIMING_SIGMA_MIN + (sigma_max - TIMING_SIGMA_MIN) * (progress ** exponent)
            return base * _mid_boost_factor(accuracy)
        return timing_sigma_for(accuracy)

    @property
    def fatigue(self) -> float:
        """0 = 体力充沛，1 = 两只手都见底。"""
        average = sum(self.stamina_left) / (2.0 * INITIAL_STAMINA)
        return max(0.0, min(1.0, 1.0 - average))

    @property
    def display_score(self) -> float:
        """屏幕上那个"跟着曲目进度走"的分数（0~1000000）。

        **只影响显示，不碰计分**：`score` / `std_score` 仍是 lazer 那套原始分，
        赛果表、Excel、结算榜、控制台用的都是它。这里只给屏幕换一把尺子。

        原始分是 S 形：前一半几乎不动（Science[Easy] 打到 50% 才 12 万），
        最后四分之一涨掉一半 —— 因为 lazer 分数里的"准度项"同时带着
        `准度^(2+2×准度)` 和"判定进度"两个因子，早期两个因子都小。

        显示分改写成"两段混一混"：

            display = 原始分 × 进度 + 原始分 × 进度 × 进度
                    = 原始分 × 进度 × (1 + 进度)

        第一项 `原始分 × 进度` 是**跟着速度走**的那部分（打得快分数就涨得快），
        第二项 `原始分 × 进度²` 是"打到后面权重更大"的那部分。两项都是"原始分 × 进度"的
        倍数，所以：

        - **曲终精确回到原始分**：进度到 1 时 `1 + 进度 = 2` 抵消了系数里的 1/2，
          显示分 == 原始分 —— 结算画面和打的时候最后一帧一致，不会跳；
        - **前段不再趴着**：Science[Easy]（准度 70）进度 10/25/50/75/90% 时
          原始分是 1.0/3.6/12.1/34.2/64.7（万），显示分是 9.1/19.0/28.2/38.0/60.9（万）
          —— 第 10% 就有 9 万，而不是 1 万；50% 时已经接近 30 万；
        - **单调不减**：原始分只增，`进度 × (1+进度)` 也只增（两者都非负）；
        - **区分度提前**：两队分差从第 10% 就看得见，而不是憋到最后四分之一。
        """
        if self.score <= 0.0 or self.accuracy_judged <= 0:
            return 0.0
        progress = min(1.0, self.accuracy_judged / self.total_judgements)
        display = self.score * progress * (1.0 + progress) * 0.5
        return max(0.0, min(SCORE_MAX_TOTAL, display))

    @property
    def choke_chance(self) -> float:
        """当前这一瞬间"手抖一下"的概率。

        三个前提缺一不可：
        - 连击够长（< CHOKE_COMBO_FLOOR 时完全不紧张）；
        - **体力已经吃紧** —— 压力大小取"还剩多少体力"的补数，并且设了死区：
          疲劳不到 CHOKE_FATIGUE_FLOOR 时完全不掷骰子。所以体力宽裕时（低难图几乎不掉体力，
          或者本人耐力很好）绝不会莫名其妙冒漏键，压力自然集中在长歌的后半段和密谱上；
        - 心态不是满分（这句话本身就带一个 1 − 心态/100 的系数）。

        自己分高、或者进入赛点会再放大一点；计分赛没有赛点，所以那里不会吃到赛点倍率。
        """
        fragility = 1.0 - self.mentality / 100.0
        if fragility <= 0.0 or self.combo < CHOKE_COMBO_FLOOR:
            return 0.0
        combo_progress = min(1.0, (self.combo - CHOKE_COMBO_FLOOR)
                             / max(1.0, CHOKE_COMBO_CAP - CHOKE_COMBO_FLOOR))
        strain = max(0.0, self.fatigue - CHOKE_FATIGUE_FLOOR) / (1.0 - CHOKE_FATIGUE_FLOOR)
        if strain <= 0.0:
            return 0.0
        boost = 1.0 + CHOKE_SCORE_BOOST * min(1.0, self.std_score / SCORE_PRESSURE_REF)
        if self.match_point:
            boost += CHOKE_MATCH_POINT_BOOST
        return CHOKE_PER_NOTE_MAX * fragility * combo_progress * strain * boost

    # ------------------------------------------------------------------
    # 重置与计分准备
    # ------------------------------------------------------------------
    def reset_for_new_song(self) -> None:
        """重置每首歌的临时状态（能力值保持不变）。"""
        self.score: float = 0.0
        self.std_score: float = 0.0
        self.combo: int = 0
        self.max_combo: int = 0
        self.accuracy: float = 100.0
        # 计分用的累加量（lazer 口径），最大值在 update_maxscore 里按本谱判定数设好
        self.accuracy_base_sum: float = 0.0
        self.combo_portion: float = 0.0
        self.accuracy_judged: int = 0

        self.stamina_left: List[float] = [INITIAL_STAMINA, INITIAL_STAMINA]
        # 两只手各一条"慢漂移"（见 TIMING_DRIFT_SIGMA）：每首歌开头从 0 起漂
        self.drift: List[float] = [0.0, 0.0]
        # 每个键位上一个"已经结算过的音符时间"，用来算同键间隔（≈ 谱面密度）
        self.last_note_time: List[int] = [INITIAL_TAP_TIME] * TRACK_COUNT
        # 每只手上一次出力的时间，用来算空档（回复体力用）
        self.last_hand_time: List[int] = [INITIAL_TAP_TIME] * 2
        # 每个选手一条独立的随机流：共用一个全局流的话，各人抽数的先后
        # 会随"这一帧谁结算了几个音符"而变，结果就跟着帧率/帧步长变了。
        # 用全局流取种子，所以外面 random.seed(N) 依然能让整场比赛可复现。
        self.rng = random.Random(random.getrandbits(64))
        self.active_notes: List[Note] = []
        # 已经按下、还在按住的长条（等松手判定）
        self.holding_notes: List[Note] = []
        # 最近一根按下的长条是哪只手（松手判定要取那只手的漂移）
        self.long_release_hand: int = 0

        self.judgement_counts: Dict[str, int] = {key: 0 for key in JUDGEMENTS}
        self.last_judgement: str = ""
        # 注意要包含 '' 这个键：初始状态下 last_judgement 就是 ''，渲染时会直接查表
        self.last_judge_time: Dict[str, int] = {key: NEVER for key in ('',) + JUDGEMENTS}
        self._update_score()

    def update_maxscore(self, total_judgements: int) -> None:
        """开局时告诉选手这张谱总共有多少次判定，并算好各项"满额值"。

        名字沿用旧版「算满分」的叫法：lazer 方案里分数本身就是 0~1000000 的标准化值，
        这里做的是 lazer 里 `SimulateAutoplay()` + `Reset(storeResults: true)` 那一步 ——
        把整张谱按全 Perfect 跑一遍，得到准度满额和连击满额。

        注意**满额连击分不是"每次判定都顶格乘数"**：连击乘数是从 0.5 按 log4 往上爬的，
        满额也要走这个过程，否则全大 P 永远拿不到 100 万。
        """
        self.total_judgements = max(1, int(total_judgements))
        self.maximum_base_sum = self.total_judgements * ACCURACY_BASE['perfect_g']
        self.maximum_combo_portion = sum(
            COMBO_BASE_SCORE['perfect_g'] * _combo_multiplier(combo)
            for combo in range(1, self.total_judgements + 1)
        )
        self._update_score()

    # ------------------------------------------------------------------
    # 每帧调用
    # ------------------------------------------------------------------
    def play(self, current_time: int) -> None:
        """每帧调用：把所有"已经到点"的音符**按时间顺序**各结算一次。

        - 普通音符：到点结算一次点击判定，然后丢出 active_notes；
        - 长条：到点结算一次点击判定，打中就挪进 holding_notes 继续按住，
          到结束时间再结算一次松手判定；头都没打中的话整根就没了。

        这里每次只挑"最早该结算的那一个"再重新挑，而不是先把点击全做完、
        再做松手：否则跨越同一帧的一次点击和一次松手，谁先结算会取决于帧长，
        结果就跟着帧率变了（连击、体力、手抖都受影响）。
        """
        while True:
            earliest: Optional[Note] = None
            earliest_time = 0
            for note in self.active_notes:
                if note.time <= current_time and (earliest is None or note.time < earliest_time):
                    earliest, earliest_time = note, note.time
            for note in self.holding_notes:
                # 同一时刻先结算"按下"（<=），再结算"松手"
                if current_time >= note.end_time and (earliest is None or note.end_time < earliest_time):
                    earliest, earliest_time = note, note.end_time
            if earliest is None:
                return
            if earliest in self.holding_notes:
                self._resolve_release(earliest, current_time)
            else:
                self._resolve_note(earliest, current_time)

    def _resolve_note(self, note: Note, current_time: int) -> None:
        """结算一个音符的"按下"：落点误差 + 可能的手抖，再算判定。"""
        track = axis_to_4k(note.x)
        hand = track >> 1

        previous = self.last_note_time[track]
        tapdist = note.time - previous
        self.last_note_time[track] = note.time
        # 这只手离上一次出力隔了多久（回复只看这个，不看单条轨的间隔）
        rest = note.time - self.last_hand_time[hand]
        self.last_hand_time[hand] = note.time
        self._remove_note(note)

        # 先按当前体力状态出手，再结算这一下消耗掉的体力
        press_offset = self._press_offset(tapdist, hand) + self._choke_offset()
        self._drain_stamina(hand, tapdist, rest)

        judgement = self.judge_system.get_judgement(press_offset)
        if judgement == 'miss':
            self._process_miss(note, current_time)
        else:
            self._process_hit(note, current_time, press_offset)
            if note.is_long:
                self.holding_notes.append(note)
                # 记下这根长条是哪只手按住的：松手判定要用这只手的漂移
                self.long_release_hand = hand

    def _resolve_release(self, note: Note, current_time: int) -> None:
        """结算长条的"松手"：另算一次判定，不吃手速也不耗体力。"""
        try:
            self.holding_notes.remove(note)
        except ValueError:
            pass
        release_offset = self._release_offset(self.long_release_hand)
        judgement = self.judge_system.get_judgement(release_offset)
        if judgement == 'miss':
            self._process_miss(note, current_time)
        else:
            self._process_hit(note, current_time, release_offset)

    def _stress_sigma(self, density_pressure: float) -> Tuple[float, float, float]:
        """这一次结算里，除准度之外的"压力"有多大，以及准度地板要打几折。

        返回 (密度项, 疲劳项, β)：

        - 密度项 / 疲劳项就是 σ 里那两份（手速、体力的活），它们该多大还是多大；
        - β 是**只作用于准度地板**的折扣：压力越大趋近 0（见 TIMING_SIGMA_DAMP_K）。
          简单谱上其它两项≈0 → β≈1，准度满额生效（小 P 频率由它决定）；
          难谱上体力/密度已经顶着一份大误差 → β≈0，准度不再叠加成漏键。
        """
        density = DENSITY_SIGMA_ADD * density_pressure
        fatigue = FATIGUE_SIGMA_ADD * self.fatigue
        damp = math.exp(-((density + fatigue) / TIMING_SIGMA_DAMP_K) ** 2)
        return density, fatigue, damp

    def _advance_drift(self, hand: int) -> float:
        """推进这只手的"慢漂移"并返回它当前的值（毫秒，正数 = 这一段习惯性打晚）。

        演化是"指数衰减 + 白噪声"：drift ← drift × DECAY + N(0, DRIFT_SIGMA)。
        每结算一个判定走一步，所以漂移的时间尺度跟着这首歌的音符走 ——
        密谱漂得快、稀疏谱漂得慢，和"人靠肌肉记忆维持节奏"的直觉一致。
        """
        if TIMING_DRIFT_SIGMA <= 0.0:
            return 0.0
        value = self.drift[hand] * TIMING_DRIFT_DECAY + self.rng.gauss(0.0, TIMING_DRIFT_SIGMA)
        self.drift[hand] = value
        return value

    def _release_offset(self, hand: int = 0) -> float:
        """松手判定的落点误差。

        和点击判定的区别有两条：
        - **不吃手速**：不算同键间隔，因此没有密度压力项，也不消耗体力
          （手速只作用于点击判定）；
        - 更容易打偏：σ 整体乘 LONG_RELEASE_SIGMA_SCALE。

        另外**只有这里吃稳定性**：长条松手是靠"撑住"的，稳不稳直接体现在这里；
        按下那一下不吃稳定性（见 `_press_offset`）。
        准度地板和疲劳两边一样 —— 准度地板同样按当时的压力打折（`_stress_sigma`）。
        漂移也照吃：松手时那只手正偏在哪儿，尾巴就偏在哪儿。
        """
        fatigue = self.fatigue
        _, fatigue_sigma, damp = self._stress_sigma(0.0)
        sigma = (self.timing_sigma * damp
                 + fatigue_sigma
                 + CONSISTENCY_SIGMA_ADD * (1.0 - self.consistency / 100.0))
        sigma *= LONG_RELEASE_SIGMA_SCALE
        # 长条整根期间不会逐次推进漂移，这里直接取这只手当前的值
        drift = self.drift[hand] if TIMING_DRIFT_SIGMA > 0.0 else 0.0
        return self.rng.gauss(FATIGUE_LATENCY * fatigue, sigma) + drift

    def _press_offset(self, tapdist: int, hand: int = 0) -> float:
        """这一次按键相对音符时间偏了多少毫秒（正数 = 打晚）。

        σ（误差大小）= 准度地板（按压力打折）+ 密度压力项 + 疲劳项；
        μ（整体偏晚）体现"跟不上、累了会越打越晚"。
        稳定性不参与：它只影响长条松手的精度和每局手感幅度。

        「准度地板按压力打折」是这一版的要点：它保证**准度只在"其它压力很小"的谱面上
        才有决定权**（简单谱 → 决定小 P 频率），难谱上让位给手速和体力。

        `hand` 用来推进/取用那只手的**慢漂移**（见 `_advance_drift`）——
        每结算一个判定推进一次，所以漂移是"成段"的，不是白噪声。
        """
        required_gap = SPEED_GAP_MAX - (SPEED_GAP_MAX - SPEED_GAP_MIN) * (self.speed / 100.0)
        # 略微超出能力范围只是"有点吃力"，真的差一大截才会崩：
        # 压力取平方后，密度刚好卡在能力边缘时惩罚很小，跟不上时惩罚迅速放大
        density_pressure = max(0.0, required_gap / max(tapdist, 25) - 1.0) ** DENSITY_EXPONENT
        fatigue = self.fatigue

        # σ 由准度打底（受压力衰减），密度压力、疲劳各自加上一份；μ 是整体偏晚的部分。
        # 这里没有"随机手滑"通道：准度只决定误差大小，不会突然把某个音符甩飞。
        density_sigma, fatigue_sigma, damp = self._stress_sigma(density_pressure)
        sigma = self.timing_sigma * damp + density_sigma + fatigue_sigma

        mu = DENSITY_LATENCY * density_pressure + FATIGUE_LATENCY * fatigue
        return self.rng.gauss(mu, sigma) + self._advance_drift(hand)

    def _drain_stamina(self, hand: int, tapdist: int, rest: int = 0) -> None:
        """结算一个音符的体力：越密越费，体力越高越省；这只手歇得久会回一点。

        `tapdist` 是同轨间隔（决定这一下有多费），`rest` 是这只手距离上一次出力的
        间隔（决定歇够了没有）—— 两者口径不同：两条轨轮流砸的时候轨间隔可能不小，
        但手其实一直在动，所以回复只看整只手的空档。
        """
        demand = min(STAMINA_DEMAND_MAX,
                     max(STAMINA_DEMAND_MIN, STAMINA_DEMAND_REF / max(tapdist, 25)))
        efficiency = STAMINA_EFF_MIN + STAMINA_EFF_GAIN * (self.stamina / 100.0)
        cost = STAMINA_DRAIN_K * demand / efficiency
        recover = STAMINA_RECOVER_K * max(0.0, rest - STAMINA_RECOVER_FLOOR) / 1000.0
        self.stamina_left[hand] = min(
            INITIAL_STAMINA, max(0.0, self.stamina_left[hand] - cost + recover))

    def _choke_offset(self) -> float:
        """心态崩盘：不是凭空掉键，而是"手抖一下"——给落点加一个很大的延迟。

        每个音符只结算一次，所以这里天然只会掷一次骰子。
        """
        chance = self.choke_chance
        if chance <= 0.0:
            return 0.0
        if self.rng.random() < chance:
            return self.rng.gauss(CHOKE_LATENCY, CHOKE_SIGMA)
        return 0.0

    # ------------------------------------------------------------------
    # 结算
    # ------------------------------------------------------------------
    def _process_hit(self, note: Note, current_time: int, press_offset: float) -> Dict[str, Any]:
        """处理一次击打：press_offset 就是已经算好的落点偏移。"""
        judgement = self.judge_system.get_judgement(press_offset)
        self._register_judgement(judgement, note, current_time)
        return {
            'judgement': judgement,
            'score': self.std_score,
            'time_diff': press_offset,
        }

    def _process_miss(self, note: Note, current_time: int) -> None:
        """这个音符漏了：按 miss 结算。"""
        self._remove_note(note)
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
        self._apply_lazer_score(judgement)
        self._update_accuracy()

    def _apply_lazer_score(self, judgement: str) -> None:
        """按 lazer 的 mania 口径记一次判定：累加准度基础分和连击分，然后重算总分。

        连击乘数用的是"这次判定**之后**的连击数"（lazer 里是 ComboAfterJudgement），
        所以必须在 combo 更新之后调用。
        """
        if judgement not in ACCURACY_BASE:
            return
        self.accuracy_judged += 1
        self.accuracy_base_sum += ACCURACY_BASE[judgement]
        self.combo_portion += COMBO_BASE_SCORE[judgement] * _combo_multiplier(self.combo)
        self._update_score()

    def _update_score(self) -> None:
        """算出当前总分（0~1000000）—— **这是计分用的原始分，规则一点没动**。

        三项都是"到目前为止拿到多少 / 总共能拿多少"，所以分数随谱面推进逐步爬到 100 万。
        指数里那个量沿用原实现（累计基础分占比 = `accuracy_base_sum / maximum_base_sum`），
        它是"已经打过的部分里大 P 占多少"，全 Perfect 打到一半时是 0.5。

        注意这个量被 `^(2+2x)` 放大之后，早期会很小（0.5³ = 0.125），所以**原始分是 S 形**：
        Science[Easy] 全 Perfect 打到 50% 只有 12 万。屏幕上的显示口径见 `display_score`。
        """
        accuracy = (self.accuracy_base_sum / self.maximum_base_sum
                    if self.maximum_base_sum > 0 else 1.0)
        combo_progress = (self.combo_portion / self.maximum_combo_portion
                          if self.maximum_combo_portion > 0 else 1.0)
        accuracy_progress = (self.accuracy_judged / self.total_judgements
                             if self.total_judgements > 0 else 1.0)

        self.score = (SCORE_COMBO_PORTION * combo_progress
                      + SCORE_ACCURACY_PORTION
                      * math.pow(accuracy, 2 + 2 * accuracy) * accuracy_progress)
        self.std_score = self.score

    @property
    def display_score(self) -> float:
        """屏幕上那个"跟着曲目进度走"的分数（0~1000000）。**只影响显示，不碰计分。**

        与原始分的唯一区别是**指数里那个准确率取什么**：

        - 原始分（计分用）取"累计基础分占比"，它在开局是 0、中盘大约等于进度
          （全 Perfect 打到一半时 0.5），被 `^(2+2x)` 放大成 0.125，前段被压得极扁 ——
          于是曲线是 S 形：50% 进度只有 12 万、最后 10% 才猛涨 35 万；
        - 显示分取**当前平均准确率**（0~1，`self.accuracy`），它开局就是 1.0、
          只在真的打丢时才往下掉。全 Perfect 局里它恒为 1，于是

              显示分 = 150000 × 连击进度 + 850000 × 判定进度

          这是**随进度线性**的：10% → 9.5 万、50% → 49.3 万、70% → 69.6 万，
          和客户端里看到的（以及比赛录像里"70% 进度约 70% 分数"）一致。

        两者在曲终等价（都等于本局的最终准确率），所以最后一帧显示的仍是真实最终分，不会跳。
        失误照常反映：准确率掉一点，显示分就跟着落到满分线下面。
        """
        if self.accuracy_judged <= 0 or self.total_judgements <= 0:
            return 0.0
        combo_progress = (self.combo_portion / self.maximum_combo_portion
                          if self.maximum_combo_portion > 0 else 1.0)
        accuracy_progress = self.accuracy_judged / self.total_judgements
        current_accuracy = min(1.0, max(0.0, self.accuracy / 100.0))
        display = (SCORE_COMBO_PORTION * combo_progress
                   + SCORE_ACCURACY_PORTION
                   * math.pow(current_accuracy, 2 + 2 * current_accuracy)
                   * accuracy_progress)
        return max(0.0, min(SCORE_MAX_TOTAL, display))

    def _update_accuracy(self) -> None:
        """重新计算准确率（lazer 口径：Σ基础分 / (已判定次数 × 305)）。

        注意分母用的是"已判定次数"而不是总数，所以这个百分比从第一个音符起就是最终值。
        """
        if self.accuracy_judged <= 0:
            self.accuracy = 100.0
            return
        self.accuracy = (self.accuracy_base_sum
                         / (self.accuracy_judged * ACCURACY_BASE['perfect_g'])) * 100

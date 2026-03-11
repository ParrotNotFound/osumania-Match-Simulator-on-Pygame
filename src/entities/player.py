# src/entities/player.py
import hashlib
import random
from typing import Dict, Any, List
from core.judge import *
from .song import Note
from utils.axis_to_track import *

class Player:
    def __init__(self, name: str, team_index: int, player_index: int):
        self.name = name
        self.team_index = team_index
        self.player_index = player_index
        
        # 玩家状态
        self.std_score: float = 0.0

        self.score: float = 0.0
        self.combo: int = 0
        self.max_combo: int = 0
        self.accuracy: float = 100.0
        self.bonus: int = 100
        self.max_score: float
        # 能力值（基于名称哈希生成）
        self.stamina: int = 50
        self.speed: int = 50
        self.avg_accuracy: int = 50
        
        # 实时状态
        self.stamina_left: List[int] = [10000, 10000]  # 左右手体力
        self.tap_times: List[int] = [0, 0, 0, 0]  # 四个键位最后敲击时间
        self.active_notes: List[Note] = []  # 当前活跃音符
        
        # 判定统计
        self.judgement_counts: Dict[str, int] = {
            'perfect': 0, 'great': 0, 'good': 0,
            'bad': 0, 'miss': 0
        }
        
        self._init_abilities()
    def update_maxscore(self, total_combo):
        """在游戏启动时更新总分"""
        config = JudgementConfig
        self.max_score = total_combo * config.score_values['perfect_g'] + total_combo * 100
    def _init_abilities(self):
        """基于玩家名生成随机但确定的能力值"""
        name_hash = hashlib.md5(self.name.encode()).hexdigest()
        hash_int = int(name_hash, 16)
        
        # 从哈希值提取三个6位数字
        values = []
        for _ in range(3):
            values.append(hash_int & 63)  # 获取低6位
            hash_int >>= 6
        
        self.stamina = min(75, values[0] + random.randint(-5, 15))
        self.speed = min(85, values[1] + random.randint(-5, 20))
        self.avg_accuracy = min(64, values[2] + random.randint(0, 20))
    #接下来为核心的游玩调用函数。游戏中每帧调用一次
    def play(self, current_time: int):
        """核心的游玩调用函数。游戏中每帧调用一次"""
        for note in self.active_notes:
            if self._judge_if_click(axis_to_4k(note.x),note.time,current_time):
                self._process_hit(note,current_time)
    
    def _judge_if_click(self, track: int,tarTime: int,current_time: int) -> bool:
        """判断是否可以击打"""
        stdacc = self.accuracy * 0.00025 + 0.3
        tapdist = current_time - self.tap_times[track]
        timedist = tarTime - current_time
        use_sta = (5000+0.5*self.stamina_left[track>>1]) * ((0.015*self.stamina/70.0) + (0.015*(70-self.stamina)/70.0) * random.random()) * (150/tapdist)

        ds=(pow(max(0,timedist),2.2)+max(0,270-use_sta * (1+self.speed*0.02)-pow(tapdist*200,0.5)))*0.5
        tap_psb = min(self.accuracy*(tapdist/(64-self.speed*0.2))*(0.75+self.stamina_left[track>>1]/20000),1/max(1,ds))
        #神秘代码两行，怎么干的我自己都忘了
        if random.random()<tap_psb:
            #print(230-use_sta * (1+spd*0.02)-tapdist)
            self.stamina_left[track>>1] = max(0,self.stamina_left[track>>1]-use_sta*2.2)
            return True
        else:
            return False
        
    def _process_hit(self,note: Note, current_time: int, judge_system = JudgeSystem() ) -> Dict[str, Any]:
        """处理一次击打，返回判定结果和分数"""
        note_time = Note.time
        time_diff = note_time - current_time
        judgement = judge_system.get_judgement(time_diff)
        score_info = self._calculate_score(judgement, judge_system)
        try:
            self.active_notes.remove(note)
        except Exception as e:
            print("Error occurred in _process_hit:")
            print(e)
        # 更新状态
        
        if judgement not in ['miss', '']:
            self.combo += 1
            self.max_combo = max(self.max_combo, self.combo)
        else:
            self.combo = 0
            
        self.score = score_info['score']
        self.judgement_counts[judgement] = self.judgement_counts.get(judgement, 0) + 1
        self._update_accuracy()
        
        return {
            'judgement': judgement,
            'score': score_info['score'],
            'time_diff': time_diff
        }
    def _calculate_score(self,judgement,judge_system = JudgeSystem()):
        self.bonus += judge_system.config.bonus_values(judgement)
        if self.bonus > 100.0:
            self.bonus = 100
        elif self.bonus < 0:
            self.bonus = 0
        self.score += judge_system.config.score_values + self.bonus
        self.std_score = self.score * 1000000.0 / self.max_score
        """标准化增加分数"""
    def _update_accuracy(self):
        """重新计算准确率"""
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

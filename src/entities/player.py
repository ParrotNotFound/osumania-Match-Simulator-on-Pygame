# src/entities/player.py
import hashlib
import random
from typing import Dict, Any, List

class Player:
    def __init__(self, name: str, team_index: int, player_index: int):
        self.name = name
        self.team_index = team_index
        self.player_index = player_index
        
        # 玩家状态
        self.score: float = 0.0
        self.combo: int = 0
        self.max_combo: int = 0
        self.accuracy: float = 100.0
        
        # 能力值（基于名称哈希生成）
        self.stamina: int = 50
        self.speed: int = 50
        self.avg_accuracy: int = 50
        
        # 实时状态
        self.stamina_left: List[int] = [10000, 10000]  # 左右手体力
        self.tap_times: List[int] = [0, 0, 0, 0]  # 四个键位最后敲击时间
        self.active_notes: List[int] = []  # 当前活跃音符索引
        
        # 判定统计
        self.judgement_counts: Dict[str, int] = {
            'perfect': 0, 'great': 0, 'good': 0,
            'bad': 0, 'miss': 0
        }
        
        self._init_abilities()
    
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
    
    def process_hit(self, judge_system, note_time: int, current_time: int) -> Dict[str, Any]:
        """处理一次击打，返回判定结果和分数"""
        time_diff = note_time - current_time
        judgement = judge_system.get_judgement(time_diff)
        score_info = judge_system.calculate_score(time_diff, self.combo)
        
        # 更新状态
        if judgement not in ['miss', '']:
            self.combo += 1
            self.max_combo = max(self.max_combo, self.combo)
        else:
            self.combo = 0
            
        self.score += score_info['score']
        self.judgement_counts[judgement] = self.judgement_counts.get(judgement, 0) + 1
        self._update_accuracy()
        
        return {
            'judgement': judgement,
            'score': score_info['score'],
            'time_diff': time_diff
        }
    
    def _update_accuracy(self):
        """重新计算准确率"""
        total_hits = sum(self.judgement_counts.values())
        if total_hits > 0:
            weighted_sum = (
                self.judgement_counts['perfect'] * 300 +
                self.judgement_counts['great'] * 200 +
                self.judgement_counts['good'] * 100 +
                self.judgement_counts['bad'] * 50
            )
            self.accuracy = (weighted_sum / (total_hits * 300)) * 100
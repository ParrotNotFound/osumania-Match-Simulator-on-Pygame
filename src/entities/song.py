# src/entities/song.py
import pygame
from dataclasses import dataclass
from typing import List, Tuple
import os

@dataclass
class Note:
    """单个音符数据"""
    x: int
    y: int
    time: int
    type: int
    
class Song:
    def __init__(self, song_id: str, title: str, artist: str, folder_path: str):
        self.id = song_id
        self.title = title
        self.artist = artist
        self.folder_path = folder_path
        self.difficulty: Tuple[float, float] = (0.0, 0.0)  # (speed_rating, stamina_rating)
        self.notes: List[Note] = []
        self.audio_path: str = ""
        
    def load_beatmap(self) -> bool:
        """加载.osu谱面文件"""
        try:
            with open(f"{self.folder_path}/beatmap.osu", 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
            for i, line in enumerate(lines):
                if line.strip() == "[HitObjects]":
                    # 解析音符数据
                    for note_line in lines[i+1:]:
                        if note_line.strip():
                            parts = note_line.strip().split(',')
                            if len(parts) >= 3:
                                note = Note(
                                    x=int(parts[0]),
                                    y=int(parts[1]),
                                    time=int(parts[2]),
                                    type=int(parts[3]) if len(parts) > 3 else 1
                                )
                                self.notes.append(note)
                    break
            return True
        except Exception as e:
            print(f"加载谱面失败: {e}")
            return False
            
    def load_audio(self) -> str:
        """加载音频文件"""
        # 尝试多种音频格式
        for ext in ['.mp3', '.ogg', '.wav']:
            audio_path = f"{self.folder_path}/audio{ext}"
            if os.path.exists(audio_path):
                self.audio_path = audio_path
                return audio_path
        raise FileNotFoundError(f"在 {self.folder_path} 中未找到音频文件")
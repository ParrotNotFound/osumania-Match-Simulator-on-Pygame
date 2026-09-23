# src/entities/song.py
"""曲目与谱面。

谱面文件夹结构（folder 指向这个目录）：
    <folder>/
        xxx.osu      # 只有一个时自动识别
        audio.mp3    # 优先用 .osu 里 AudioFilename 指定的文件
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..utils.config import SongConfig

AUDIO_EXTENSIONS: Tuple[str, ...] = ('.mp3', '.ogg', '.wav', '.flac', '.opus')


@dataclass
class Note:
    """单个音符。

    普通音符 type = 1，end_time = 0；
    长条 type = 128（末位是结束时间的字段，形如 448,192,25078,128,0,26641:0:0:0:0），
    end_time 就是那个 26641。
    """
    x: int
    y: int
    time: int
    type: int
    end_time: int = 0

    @property
    def is_long(self) -> bool:
        """是不是长条（按住不放的那种）"""
        return bool(self.type & 128) and self.end_time > self.time


class Song:
    def __init__(self, config: SongConfig, root: str = "."):
        self.id = config.id
        self.title = config.title or config.id
        self.artist = config.artist
        self.folder_path = self._resolve(config.folder, root)

        # 配置里没写就让下面两个函数自己去文件夹里找
        self._beatmap_name = config.beatmap
        self._audio_name = config.audio

        self.beatmap_path: Optional[str] = None
        self.audio_path: str = ""
        self.audio_name_in_map: str = ""     # 谱面里 AudioFilename 写的文件名
        self.key_count: int = 4
        self.mode: int = 3
        # 谱面的 OverallDifficulty（[Difficulty] 段），用来算判定窗口；
        # 缺失时按 osu! 的惯例取 5
        self.overall_difficulty: float = 5.0
        self.notes: List[Note] = []
        # 总判定次数：普通音符 1 次，长条 2 次（按下一次 + 松手一次），用于算满分
        self.judgement_count: int = 0

    @staticmethod
    def _resolve(path: str, root: str) -> str:
        return path if os.path.isabs(path) else os.path.normpath(os.path.join(root, path))

    # ------------------------------------------------------------------
    # 谱面
    # ------------------------------------------------------------------
    def _find_beatmap(self) -> Optional[str]:
        if self._beatmap_name:
            candidate = os.path.join(self.folder_path, self._beatmap_name)
            if os.path.isfile(candidate):
                return candidate
            print(f"警告：{self.id} 指定的谱面 {self._beatmap_name} 不存在，改为自动查找")
        candidates = sorted(glob.glob(os.path.join(self.folder_path, "*.osu")))
        if not candidates:
            return None
        if len(candidates) > 1:
            print(f"警告：{self.id} 的文件夹里有多个 .osu，使用 {os.path.basename(candidates[0])}")
        return candidates[0]

    def load_beatmap(self) -> bool:
        """加载 .osu 谱面文件，成功返回 True。"""
        path = self._find_beatmap()
        if path is None:
            print(f"加载谱面失败：{self.folder_path} 里没有 .osu 文件")
            return False

        self.beatmap_path = path
        self.notes = []
        section = ""
        try:
            # utf-8-sig：.osu 文件常带 BOM
            with open(path, 'r', encoding='utf-8-sig', errors='replace') as handle:
                lines = handle.readlines()
        except OSError as error:
            print(f"加载谱面失败（{path}）：{error}")
            return False

        for line in lines:
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                section = stripped
                continue

            if section == "[General]":
                if stripped.startswith("AudioFilename:"):
                    self.audio_name_in_map = stripped.split(':', 1)[1].strip()
                elif stripped.startswith("Mode:"):
                    self.mode = self._to_int(stripped.split(':', 1)[1], self.mode)
            elif section == "[Difficulty]":
                if stripped.startswith("CircleSize:"):
                    self.key_count = self._to_int(stripped.split(':', 1)[1], self.key_count)
                elif stripped.startswith("OverallDifficulty:"):
                    self.overall_difficulty = self._to_float(stripped.split(':', 1)[1],
                                                             self.overall_difficulty)
            elif section == "[HitObjects]":
                note = self._parse_note(stripped)
                if note is not None:
                    self.notes.append(note)

        self.notes.sort(key=lambda note: note.time)
        self.judgement_count = sum(2 if note.is_long else 1 for note in self.notes)
        if self.mode != 3:
            print(f"警告：{self.id} 的游戏模式是 {self.mode}（不是 osu!mania=3），结果可能不对")
        return True

    @staticmethod
    def _to_int(text: str, default: int) -> int:
        try:
            return int(float(text.strip()))
        except ValueError:
            return default

    @staticmethod
    def _to_float(text: str, default: float) -> float:
        try:
            return float(text.strip())
        except ValueError:
            return default

    @staticmethod
    def _parse_note(line: str) -> Optional[Note]:
        if not line:
            return None
        parts = line.split(',')
        if len(parts) < 4:
            return None
        try:
            note_type = int(parts[3])
            end_time = 0
            # 长条：type 带 128 位，第 6 个字段形如 "26641:0:0:0:0"，冒号前就是结束时间
            if note_type & 128 and len(parts) > 5:
                end_time = int(parts[5].split(':')[0])
            return Note(
                x=int(parts[0]),
                y=int(parts[1]),
                time=int(parts[2]),
                type=note_type,
                end_time=end_time,
            )
        except ValueError:
            return None  # 忽略解析不了的行（注释、空行等）

    # ------------------------------------------------------------------
    # 音频
    # ------------------------------------------------------------------
    def load_audio(self) -> str:
        """找到音频文件并返回路径。"""
        candidates: List[str] = []
        if self._audio_name:
            candidates.append(self._audio_name)
        if self.audio_name_in_map:
            candidates.append(self.audio_name_in_map)
        candidates.extend(f"audio{ext}" for ext in AUDIO_EXTENSIONS)

        for name in candidates:
            path = os.path.join(self.folder_path, name)
            if os.path.isfile(path):
                self.audio_path = path
                return path

        for ext in AUDIO_EXTENSIONS:
            found = sorted(glob.glob(os.path.join(self.folder_path, f"*{ext}")))
            if found:
                self.audio_path = found[0]
                return found[0]

        raise FileNotFoundError(f"在 {self.folder_path} 中未找到音频文件")

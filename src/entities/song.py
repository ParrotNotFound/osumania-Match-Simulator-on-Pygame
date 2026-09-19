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
    """单个音符数据"""
    x: int
    y: int
    time: int
    type: int


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
        self.notes: List[Note] = []

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
            elif section == "[HitObjects]":
                note = self._parse_note(stripped)
                if note is not None:
                    self.notes.append(note)

        self.notes.sort(key=lambda note: note.time)
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
    def _parse_note(line: str) -> Optional[Note]:
        if not line:
            return None
        parts = line.split(',')
        if len(parts) < 4:
            return None
        try:
            return Note(
                x=int(parts[0]),
                y=int(parts[1]),
                time=int(parts[2]),
                type=int(parts[3]),
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

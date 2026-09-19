# src/utils/file_loader.py
"""运行期数据的读写（比赛结果记录）。

注意：所有*配置*都集中在项目根目录的 config.toml（见 src/utils/config.py），
本模块只负责读写比赛过程中产生的数据。
"""
from __future__ import annotations

import os
from typing import List


def load_results(results_file: str) -> List[int]:
    """读取每局的胜者序号（一行一个 0/1）。"""
    results: List[int] = []
    try:
        with open(results_file, 'r', encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    results.append(int(line))
                except ValueError:
                    print(f"警告：忽略无法解析的比赛记录行 {line!r}")
    except FileNotFoundError:
        pass  # 还没打过任何一局是正常情况
    except OSError as error:
        print(f"警告：读取比赛记录失败（{results_file}）：{error}")
    return results


def write_results(results_file: str, winner: int) -> None:
    """追加一局结果。"""
    try:
        _ensure_parent(results_file)
        with open(results_file, 'a', encoding='utf-8') as handle:
            handle.write(f'{winner}\n')
    except OSError as error:
        print(f"警告：写入比赛记录失败（{results_file}）：{error}")


def clear_results(results_file: str) -> None:
    """清空比赛记录（新开一局时用）。"""
    try:
        _ensure_parent(results_file)
        with open(results_file, 'w', encoding='utf-8'):
            pass
    except OSError as error:
        print(f"警告：清空比赛记录失败（{results_file}）：{error}")


def _ensure_parent(results_file: str) -> None:
    parent = os.path.dirname(os.path.abspath(results_file))
    if parent:
        os.makedirs(parent, exist_ok=True)

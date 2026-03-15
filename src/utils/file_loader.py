# src/utils/file_loader.py
import os
from typing import Dict, List, Tuple

def load_songs(songs_file: str) -> Dict:
    """加载歌曲列表"""
    songs = {}
    try:
        with open(songs_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    parts = line.strip().split('\t')
                    if len(parts) >= 2:
                        song_id = parts[0]
                        # 假设格式: 歌曲ID  歌曲标题  文件夹名
                        title_artist = parts[1]
                        title = title_artist
                        artist = "Unknown"
                        
                        folder = parts[2] if len(parts) > 2 else title_artist
                        
                        songs[song_id] = {
                            'id' : song_id,
                            'title': title,
                            'artist': artist,
                            'folder': folder
                        }
                    
    except FileNotFoundError:
        print(f"警告: 未找到歌曲文件 {songs_file}")
    return songs

def load_teams(teams_file: str) -> List[Dict]:
    """加载队伍配置"""
    teams = []
    try:
        with open(teams_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        for i in range(0, len(lines), 2):
            if i + 1 < len(lines):
                team_name = lines[i].strip()
                players = lines[i + 1].strip().split('\t')
                
                # 分配队伍颜色
                if i == 0:
                    color = (255, 100, 90)  # 红色
                else:
                    color = (0, 150, 255)   # 蓝色
                
                teams.append({
                    'name': team_name,
                    'players': players,
                    'color': color
                })
    except FileNotFoundError:
        print(f"警告: 未找到队伍文件 {teams_file}")
        # 默认队伍
        teams = [
            {'name': '红队', 'players': ['玩家1', '玩家2', '玩家3'], 'color': (255, 100, 90)},
            {'name': '蓝队', 'players': ['玩家4', '玩家5', '玩家6'], 'color': (0, 150, 255)}
        ]
    return teams

def load_choose_id(choose_file) -> List[str]:
    choose = []
    try:
        with open(choose_file, 'r', encoding='utf-8') as f:
            choose = f.readlines()
    except FileNotFoundError:
        print(f"警告: 未找到选曲文件 {choose_file}")
    except Exception as e:
        print(f"警告：导入选曲文件发生错误：{e}")
    return choose
def load_results(results_file) -> List[int]:
    results = []
    try:
        with open(results_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        for i in lines:
            results.append(int(i))
        
    except FileNotFoundError:
        print(f"警告: 未找到比赛结果文件 {results_file}")
    except Exception as e:
        print(f"警告：找比赛结果文件发生错误：{e}")
    return results

def write_results(results_file, winner):
    try:
        with open(results_file, 'a', encoding='utf-8') as f:
            f.write(f'{winner}\n')
    except Exception as e:
        print(f"警告：写入比赛结果文件发生错误：{e}")
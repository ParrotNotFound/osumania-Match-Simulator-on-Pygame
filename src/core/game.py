# src/core/game.py
import pygame
from typing import Optional
from ..entities.team import Team
from ..entities.song import Song
from ..entities.player import Player
from .match import Match
from .judge import JudgeSystem


class OsuGame:
    def __init__(self, screen_width: int = 1280, screen_height: int = 720):
        pygame.init()
        pygame.font.init()
        pygame.mixer.init()
        
        self.screen = pygame.display.set_mode((screen_width, screen_height))
        pygame.display.set_caption("Osu! 模拟对战")
        
        self.clock = pygame.time.Clock()
        self.fps = 60
        self.running = False
        self.max_render_dist = 1500
        
        # 游戏组件
        self.match: Optional[Match] = None
        self.current_song: Optional[Song] = None
        self.judge_system = JudgeSystem()
        
        # 游戏状态
        self.game_state = "SONG_SELECT"  # MENU, SONG_SELECT, PLAYING, RESULTS
        self.current_time = 0
        self.song_start_time = 0
        
        # 加载资源
        self._load_resources()
    
    def _load_resources(self):
        """加载游戏资源（歌曲、队伍等）"""
        # 这里调用utils中的加载函数
        from ..utils.file_loader import load_songs, load_teams
        
        songs_data = load_songs("data/songs.txt")
        teams_data = load_teams("config/teams.txt")
        
        # 创建Song对象
        for song_id, song_info in songs_data.items():
            song = Song(song_id, song_info['title'], song_info['artist'], 
                       f"data/beatmaps/{song_info['folder']}")
            song.load_beatmap()
            self.match.add_song(song)
        
        # 创建Team对象
        for i, team_data in enumerate(teams_data):
            team = Team(team_data['name'], team_data['color'], team_data['players'])
            self.match.add_team(team)
    
    def run(self):
        """主游戏循环"""
        self.running = True
        
        while self.running:
            # 处理事件
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                self._handle_event(event)
            
            # 更新游戏状态
            self._update()
            
            # 绘制
            self._render()
            
            # 控制帧率
            self.clock.tick(self.fps)
        
        pygame.quit()
    
    def _handle_event(self, event):
        """处理输入事件"""
        if self.game_state == "PLAYING":
            if event.type == pygame.KEYDOWN:
                # 处理按键（映射到玩家击打）
                self._handle_key_press(event.key)
    
    def _update(self):
        """更新游戏逻辑"""
        if self.game_state == "PLAYING":
            self.current_time = pygame.time.get_ticks() - self.song_start_time
            
            # 更新音符状态
            self._update_notes()
            self._update_players()
            # 检查歌曲是否结束
            if not pygame.mixer.music.get_busy():
                self._finish_song()
    def _update_notes(self):
        current_note_time = self.current_time
        try:
            while(len(self.current_song.notes) > 0 and current_note_time < self.current_time + self.max_render_dist):
                note = self.current_song.notes[0]
                for t in self.match.teams:
                    for p in t.players:
                        p.active_notes.append(note)
                current_note_time = note.time
                self.current_song.notes.remove(note)
        except Exception as e:
            print(e)
    def _update_players(self):
        try:
            for t in self.match.teams:
                for p in t.players:
                    p.play(self.current_time)
        except Exception as e:
            print(e)
    def _render(self):
        """渲染游戏画面"""
        self.screen.fill((0, 0, 0))  # 黑色背景
        
        if self.game_state == "MENU":
            self._render_menu()
        elif self.game_state == "SONG_SELECT":
            self._render_song_select()
        elif self.game_state == "PLAYING":
            self._render_gameplay()
        elif self.game_state == "RESULTS":
            self._render_results()
        
        pygame.display.flip()
    def _render_team_big_points(self):
        """显示大比分"""
        myfont = pygame.font.Font(None, 40)
        teams = self.match.teams
        textImage = myfont.render(str(teams[0].name), True, teams[0].color)
        self.screen.blit(textImage, (0,20))
        t_width,t_height= myfont.size(str(teams[1].name))
        textImage = myfont.render(str(teams[1].name), True, teams[1].color)
        self.screen.blit(textImage, (1270-t_width,20))

        
    def _render_menu(self):
        """初始页面，暂时不用，就放着"""
    def _render_song_select(self):
        """选歌页面"""

    def _render_gameplay(self):
        """渲染游戏进行中的画面"""
        # 绘制队伍信息
        for i, team in enumerate(self.match.teams):
            # 绘制队伍分数
            font = pygame.font.Font(None, 48)
            score_text = font.render(f"{team.name}: {team.total_score:.0f}", 
                                   True, team.color)
            self.screen.blit(score_text, (50 + i * 900, 20))
            
            # 绘制玩家信息
            for j, player in enumerate(team.players):
                self._render_player(player, 50 + i * 900, 100 + j * 150)
        
        # 绘制当前时间
        time_font = pygame.font.Font(None, 36)
        time_text = time_font.render(f"Time: {self.current_time/1000:.1f}s", 
                                    True, (255, 255, 255))
        self.screen.blit(time_text, (600, 680))
    
    def _render_player(self, player: Player, x, y):
        """绘制单个玩家信息"""
        # 玩家名称
        name_font = pygame.font.Font(None, 30)
        name_text = name_font.render(player.name, True, (255, 255, 255))
        self.screen.blit(name_text, (x, y))
        
        # 分数和准确率
        info_font = pygame.font.Font(None, 24)
        score_text = info_font.render(f"Score: {player.score:.0f}", True, (200, 200, 200))
        acc_text = info_font.render(f"Acc: {player.accuracy:.2f}%", True, (200, 200, 200))
        combo_text = info_font.render(f"Combo: {player.combo}", True, (255, 215, 0))
        
        self.screen.blit(score_text, (x, y + 30))
        self.screen.blit(acc_text, (x, y + 55))
        self.screen.blit(combo_text, (x, y + 80))
        
        # 体力条
        pygame.draw.rect(self.screen, (100, 100, 100), [x, y + 110, 200, 15])  # 背景
        stamina_width = (player.stamina_left[0] + player.stamina_left[1]) / 20000 * 200
        pygame.draw.rect(self.screen, (0, 255, 0), [x, y + 110, stamina_width, 15])  # 当前体力

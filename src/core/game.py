# src/core/game.py
import pygame
from typing import Optional
from ..entities.team import Team
from ..entities.song import Song
from ..entities.player import Player
from .match import Match
from .judge import JudgeSystem
import math

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
        self.match: Optional[Match] = Match("TEST",1)
        self.current_song: Optional[Song] = None
        self.choose_song: Optional[str] = None
        self.judge_system = JudgeSystem()
        
        # 游戏状态
        self.game_state = "SONG_SELECT"  # MENU, SONG_SELECT, PLAYING, RESULTS
        self.current_time = 0
        self.song_start_time = 0
        self.countdown = {"MENU":30000,"SONG_SELECT":30000}
        
        # 加载资源
        self._load_resources()
    
    def _load_resources(self):
        """加载游戏资源（歌曲、队伍等）"""
        # 这里调用utils中的加载函数
        from ..utils.file_loader import load_songs, load_teams, load_choose_id
        
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
    def _load_select(self):
        from ..utils.file_loader import load_choose_id
        self.choose_song = load_choose_id("config/choose.txt")
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
        if self.game_state == "MENU":
            self.current_time = pygame.time.get_ticks() - self.song_start_time
            if(self.current_time > self.countdown["MENU"]):
                self.game_state = "SONG_SELECT"
                self._song_selection()
                pygame.mixer.music = self.current_song.load_audio()
                pygame.mixer.music.play()
                self.song_start_time = pygame.time.get_ticks()
        if self.game_state == "SONG_SELECT":
            self.current_time = pygame.time.get_ticks() - self.song_start_time
            if(self.current_time > self.countdown["SONG_SELECT"]):
                self.game_state = "PLAYING"
                self.song_start_time = pygame.time.get_ticks() + 3000
        if self.game_state == "PLAYING":
            self.current_time = pygame.time.get_ticks() - self.song_start_time
            
            # 更新音符状态
            self._update_notes()
            self._update_players()
            # 检查歌曲是否结束
            if not pygame.mixer.music.get_busy():
                self._finish_song()
    def _song_selection(self):
        """选曲"""
        try:
            self._load_select()
            for song in self.match.song_pool:
                if song.id == self.choose_song[self.match.current_round]:
                    self.match.select_song(song,self.match.current_round % 2)
                    break
            raise RuntimeError(f"未寻找到符合要求的选曲'{self.choose_song[self.match.current_round]}'！")
        except Exception as e:
            print(e)
            song = self.match.song_pool[0]
            self.match.select_song(song,self.match.current_round % 2)
            
            
    def _finish_song(self):
        """结束本场比赛"""
        winning_team = 0
        winning_score = 0
        for i,team in enumerate(self.match.teams):
            if winning_score < team.total_score():
                winning_score = team.total_score
                winning_team = i
        self.match.record_round_result(i)
        if self.match.is_finished():
            self.game_state = "FINISHED"
        else:
            self.game_state = "MENU"
        self.match.reset()

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
            self._render_results()# 待完成
        elif self.game_state == "FINISHED":
            self._render_ending()
        pygame.display.flip()

    def _render_ending(self):
        """显示比赛结果"""
        self._render_team_big_points()
        bigfont = pygame.font.Font(None, 70)
        who_wins = self.match.winner
        textImage = bigfont.render(who_wins, True, (255,255,255))
        t_width,t_height= bigfont.size(who_wins)
        self.screen.blit(textImage, (640-t_width/2,360))

    def _render_team_big_points(self):
        """显示大比分"""
        myfont = pygame.font.Font(None, 40)
        teams = self.match.teams
        textImage = myfont.render(str(teams[0].name), True, teams[0].color)
        self.screen.blit(textImage, (0,20))
        t_width,t_height= myfont.size(str(teams[1].name))
        textImage = myfont.render(str(teams[1].name), True, teams[1].color)
        self.screen.blit(textImage, (1270-t_width,20))
        # 队名后为比分显示
        progress = self.match.get_match_progress(False)
        windata = progress["scores"]
        wins = progress["rounds_to_win"]
        for i in range(wins):
            clr = [(100,100,100),(255,215,0)]
            pygame.draw.rect(self.screen, clr[windata[0]>i], [640 + (-0.5)*600 + i*40 -25,20, 25, 25])
            pygame.draw.rect(self.screen, clr[windata[1]>i], [640 + (0.5)*600 - i*40 ,20, 25, 25])

    def _render_menu(self):
        """初始页面"""
        self._render_song_select()

    def _render_song_select(self):
        """选歌页面"""
        self._render_team_big_points()
        diffcolor = {'RC':(187,255,255),'SP':(255,215,0),'ST':(255,165,0),'HD':(255,0,0),'TB':(200,200,200)}
        
        myfont = pygame.font.Font(None,40)
        s_width = 500
        songs = self.match.song_pool
        s_height = 600/len(songs)
        chosen_songs = self.match.selected_songs
        for s in chosen_songs:
            color = self.match.teams[s["team"]].color
            index = songs.index[s["song"]]
            pygame.draw.rect(self.screen, color, [640-s_width/2, 70+s_height*index, s_width, s_height-6])
        for i,song in enumerate(songs):
            songskey = song.id
            songsvalue = song.title
            pygame.draw.rect(self.screen, (100,100,100), [640-s_width/2+5, 70+s_height*i+5, s_width-10, s_height-16])
            textImage = myfont.render(f'{songskey}', True, diffcolor[f'{songskey[0]}{songskey[1]}'])
            self.screen.blit(textImage, (640-s_width/2+10,80+(i)*s_height))
            textImage = myfont.render(f'{songsvalue}', True, (255,255,255))
            self.screen.blit(textImage, (640-s_width/2+80,80+(i)*s_height))
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

        # 以下这段是显示底下那一坨分数的，老代码石山搬过来的，这段比较复杂不好动
        score_a = self.match.teams[0].total_score
        score_b = self.match.teams[1].total_score
        teamColor = [self.match.teams[0].color,self.match.teams[1].color]
        bigfont = pygame.font.Font(None,60)
        smallfont = pygame.font.Font(None,25)
        myfont = pygame.font.Font(None,40)
        teamgap = math.pow(abs(score_a-score_b)*70,0.35)
        if score_a > score_b:
            red_winning = True
            textImage = smallfont.render(str(int(abs(score_a-score_b))), True, (255,255,255))
            t_width,t_height= smallfont.size(str(int(abs(score_a-score_b))))
            self.screen.blit(textImage, (640-t_width,635))
            t_width,t_height= bigfont.size(str(int(score_a)))
            textImage = bigfont.render(str(int(score_a)), True, teamColor[0])
            self.screen.blit(textImage, (640-t_width-teamgap,660))
            textImage = myfont.render(str(int(score_b)), True, teamColor[1])
            self.screen.blit(textImage, (640,660))
            pygame.draw.rect(self.screen, teamColor[0], [640-teamgap, 650, teamgap, 10])
        else:
            red_winning = False
            textImage = smallfont.render(str(int(abs(score_a-score_b))), True, (255,255,255))
            t_width,t_height= smallfont.size(str(int(abs(score_a-score_b))))
            self.screen.blit(textImage, (640,635))
            t_width,t_height= myfont.size(str(int(score_a)))
            textImage = bigfont.render(str(int(score_b)), True, teamColor[1])
            self.screen.blit(textImage, (640+teamgap,660))
            textImage = myfont.render(str(int(score_a)), True, teamColor[0])
            self.screen.blit(textImage, (640-t_width,660))
            pygame.draw.rect(self.screen, teamColor[1], [640, 650, teamgap, 10])
        # 绘制当前时间
        time_font = pygame.font.Font(None, 36)
        time_text = time_font.render(f"Time: {self.current_time/1000:.1f}s", 
                                    True, (255, 255, 255))
        self.screen.blit(time_text, (600, 680))
        # 最后再画大比分和歌名
        songName = self.current_song.title
        textImage = myfont.render(songName, True, (255,255,255))
        t_width,t_height= myfont.size(songName)
        self.screen.blit(textImage, (640-t_width/2,600))
        self._render_team_big_points()

    def _render_player(self, player: Player, x, y):
        """绘制单个玩家信息"""
        #背景板
        pygame.draw.rect(self.screen, (0,0,0), [x, y,240, 480])
        #键
        for note in player.active_notes:
            rect_width = 35
            rect_height = 10
            rect_x = x+50+int(note.x)*0.3
            rect_y = y+260-(int(note.time)-self.current_time)*0.6
            pygame.draw.rect(self.screen, (255,255,255), [rect_x, rect_y, rect_width, rect_height])
        # 挡板
        pygame.draw.rect(self.screen, (0,0,0), [x, y-460,480, 480])
        pygame.draw.rect(self.screen, (50,50,50), [x+50+19.2, y+260, 150, 20])
        # 分数和准确率
        info_font = pygame.font.Font(None, 40)
        color = self.match.teams[player.team_index].color
        score_text = info_font.render(f"{player.std_score:.0f}", True, (200, 200, 200))
        acc_text = info_font.render(f"{player.accuracy:.2f}%", True, (200, 200, 200))
        combo_text = info_font.render(str(player.combo), True, color)

        self.screen.blit(combo_text, (x+140-int(math.log10(max(1,player.combo)))*10,y+120))
        
        self.screen.blit(acc_text,(x+180,y))
        self.screen.blit(score_text, (x+140-int(math.log10(max(1,player.std_score)))*10,y))

        # 玩家名称
        name_font = pygame.font.Font(None, 30)
        name_text = name_font.render(player.name, True, (255, 255, 255))
        self.screen.blit(name_text, (x, y))

        # 体力条
        pygame.draw.rect(self.screen, (100, 100, 100), [x, y + 110, 200, 15])  # 背景
        stamina_width = (player.stamina_left[0] + player.stamina_left[1]) / 20000 * 200
        pygame.draw.rect(self.screen, (0, 255, 0), [x, y + 110, stamina_width, 15])  # 当前体力

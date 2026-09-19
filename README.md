# osu!mania 匹配赛模拟器（Pygame）

一个用 Pygame 写的 **osu!mania 对战模拟器**：它不让你操作，而是让两队（各 3 名"选手"）
在同一张 4K 谱面上各打一遍，把分数、准确率、连击和判定实时演出来，最后按大比分决出胜负。

选手的能力值由**名字哈希**生成（体力/手速/准度），每次击打都由概率模型决定是否打中，
所以同一份配置也会打出不同的过程，但换名字就等于换人。

## 运行

```bash
# 1) 安装依赖（只需要 pygame；pygame-ce 对 Python 3.13/3.14 支持更好）
python -m pip install -r requirements.txt

# 2) 启动（默认读取项目根目录的 config.toml）
python main.py

# 也可以用别的配置文件
python main.py --config my_match.toml
```

对局中按 `Esc` 或关闭窗口即可退出。

## 配置：只有一份文件

**所有配置都在项目根目录的 `config.toml`**，改这一份就够了：
画面、比赛规则、判定窗口与分值、曲库、队伍与玩家、每轮选曲顺序。
旧版的 `data/songs.txt`、`config/teams.txt`、`config/choose.txt` 已经全部并入这里，不再读取。

| 配置段 | 作用 |
| --- | --- |
| `[game]` | 窗口尺寸、帧率、音符提前进入视野的距离、各界面停留时长、调试信息 |
| `[match]` | 比赛名、几胜制、结果记录文件、是否续接上次未打完的比赛 |
| `[judge]` | 判定窗口（毫秒）、每个判定的基础分、连击奖励的增减与上下限 |
| `[players]` | 每首歌开始时是否重新读取队员名单（`reread_roster`）、手感波动幅度（`form_range`） |
| `[pool_colors]` | 选曲列表左侧分类标签的颜色（按曲目 id 前两个字母取色） |
| `[[songs]]` | 曲库：每首歌的 id / 标题 / 艺术家 / 谱面文件夹 |
| `[[teams]]` | 队伍与玩家：必须是 2 支队伍，每队 1~3 名玩家 |
| `[[picks]]` | 选曲顺序：每轮一首，可指定由哪队选；用完后自动在曲库里循环 |

配置里的**相对路径都相对于 `config.toml` 所在目录**，所以从任何目录启动都能找到资源。
配置写错时程序会在启动阶段打印明确的中文错误（哪个段、哪个键、期望什么），不会只抛一堆 traceback。

### 曲目文件夹

`[[songs]]` 的 `folder` 指向一个谱面目录：

```
data/beatmaps/2598781/
    xxx.osu      # 只有一个 .osu 时自动识别，也可以在 config.toml 里用 beatmap 指定
    audio.mp3    # 优先用 .osu 里 AudioFilename 指定的文件，其次 audio.mp3/.ogg/.wav
```

本模拟器按 **4K（CircleSize:4）** 处理谱面，其他键数的谱面会在启动时给出警告。

## 项目结构

```
main.py                    入口：解析 --config，加载配置后启动游戏
config.toml                唯一配置文件
src/core/game.py           主循环、状态机（MENU→SONG_SELECT→PLAYING→FINISHED）、全部渲染
src/core/match.py          比赛：队伍、曲库、选曲安排、大比分、结果文件读写
src/core/judge.py          判定窗口 → 判定等级，分值取自 [judge]
src/entities/player.py     模拟选手：能力值、概率击打模型、连击与分数结算
src/entities/song.py       曲目与 .osu 谱面解析
src/entities/team.py       队伍聚合（总分、平均准确率、每局重置）
src/utils/config.py        config.toml 的读取与校验
src/utils/file_loader.py   比赛结果记录的读写
src/utils/axis_to_track.py 音符 x 坐标 → 4K 键位
```

## 比赛流程

1. `MENU` 停留 `countdown_menu` 毫秒（画面与选曲页相同，展示曲库与已选曲目）；
2. `SONG_SELECT` 停留 `countdown_song_select` 毫秒，同时试听本轮曲目；
   **选曲的这一刻会重新结算全部选手**（见下一节），并把本局能力值打到控制台；
3. `PLAYING`：先空等 `lead_in` 毫秒，音乐响起（歌曲时间归零）后开始判定，
   每帧尝试击打已到时间的音符，超过 `[judge] miss` 窗口仍未打中的音符按漏掉结算；
4. 谱面发完、所有人手上没有音符、音乐也放完后结算本局，把胜者追加写入结果文件；
5. 有队伍达到 `rounds_to_win` 胜则进入 `FINISHED` 结算画面，否则回到第 1 步。

`[match] resume = false`（默认）时每次启动都会清空结果文件重新开赛；
改成 `true` 则读取结果文件，接着上次没打完的比赛继续。

## 选手能力值

每首歌开始时都会重新结算一次：

1. 若 `[players] reread_roster = true`，先重新读取 `config.toml` 的 `[[teams]]`
   —— 改完名单不用重启，下一局生效；配置写坏了只警告并保留原名单，不影响正在进行的比赛；
2. 重新生成本局能力值：**基准 + 风格偏置 + 本局手感**。
   基准和风格完全由名字哈希决定（同一个名字每局底子一样），
   手感是五项各随机 ±`swing`，而 `swing = form_range × (1 - 0.75 × 稳定性/100)`
   —— 所以**稳定性决定每局的波动幅度**，越稳的选手越不容易大起大落；
3. 把本局每名队员的风格与能力值打到控制台（括号内是本局手感偏移）。

五项能力的分工：

| 能力 | 作用 | 实测影响（密集谱 / 稀疏谱） |
| --- | --- | --- |
| 准度 `avg_accuracy` | 基础命中率，被心态按压力打折 | **最大**：0→100 相差 8.2pp / 5.1pp |
| 手速 `speed` | 能从容处理多密的同键间隔 | 密集谱 4.0pp，稀疏谱 0.8pp（只在密的地方起作用） |
| 体力 `stamina` | 体力池消耗速度，决定后半段掉多少 | 密集谱 3.2pp，稀疏谱 0.8pp（长歌+密谱才明显） |
| 稳定 `consistency` | 每局手感幅度 + 单帧概率抖动 | 不影响平均分，影响"大起大落"：稳定 32 手感 ±8，稳定 78 手感 ±4 |
| 心态 `mentality` | 连击长 / 分数高 / 赛点时准度下降 | 赛点上心态 0 比心态 100 低约 0.9pp，且过程明显更早掉连击 |

选手风格由名字哈希决定（手速型 / 耐力型 / 稳准型 / 爆发型 / 大心脏 / 赌徒 / 天才…），
风格给五项加不同偏置，所以"换个名字"就是换一个完全不同的人。

数据可由 `python data/_selftest/model_calibration.py` 复现（会跑真实谱面做扫参）。


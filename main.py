# main.py
#!/usr/bin/env python3
import sys
import os

# 添加src到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from src.core.game import OsuGame

def main():
    # 创建游戏实例
    game = OsuGame()
    
    # 运行游戏
    game.run()

if __name__ == "__main__":
    main()
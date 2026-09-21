# _build/build_ability_exe.py
"""把 ability.py 打成可以独立分发的可执行程序。

用法：
    python _build/build_ability_exe.py

产物（都在 dist/ 下）：
    ability.exe            单文件版：分发时只给对方这一个文件即可
    ability_standalone/    文件夹版：exe + _internal/，免运行时自解压，启动更快

两者都不需要目标机器装 Python，也不依赖项目里的任何其它文件。
如果 exe 旁边放了 config.toml，会读那里的 [players] form_range；没有就用内置默认值。

注：单文件版启动时要先把自己解压到 %TEMP%，如果目标环境禁止往临时目录写文件
（比如某些受限沙箱），就跑不起来 —— 那种场合用文件夹版。
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "_build")
DIST = os.path.join(ROOT, "dist")

COMMON = [
    "--console",                 # 控制台程序
    "--noconfirm",
    # ability.py 只用标准库 + 项目里的纯 Python 模块，不需要这些
    "--exclude-module", "pygame",
    "--exclude-module", "numpy",
    "--exclude-module", "tkinter",
]


def run_pyinstaller(entry: str, extra: list, workdir: str) -> int:
    command = [sys.executable, "-m", "PyInstaller"] + extra + [
        "--name", "ability",
        "--distpath", DIST,
        "--workpath", workdir,
        "--specpath", BUILD,
        entry,
    ]
    print("打包命令：\n  " + " ".join(command) + "\n")
    return subprocess.run(command, cwd=ROOT).returncode


def main() -> int:
    entry = os.path.join(ROOT, "ability.py")
    if not os.path.isfile(entry):
        print(f"找不到 {entry}")
        return 1

    for folder in (os.path.join(BUILD, "pyinstaller_onefile"),
                   os.path.join(BUILD, "pyinstaller_onedir"),
                   os.path.join(DIST, "ability_standalone")):
        shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(DIST, exist_ok=True)

    if run_pyinstaller(entry, COMMON + ["--onefile"], os.path.join(BUILD, "pyinstaller_onefile")):
        print("单文件版打包失败")
        return 1

    if run_pyinstaller(entry, COMMON + ["--onedir"], os.path.join(BUILD, "pyinstaller_onedir")):
        print("文件夹版打包失败")
        return 1

    # onedir 的目录名跟着 --name 走（dist/ability），改成更好认的名字
    raw_folder = os.path.join(DIST, "ability")
    folder = os.path.join(DIST, "ability_standalone")
    if os.path.isdir(raw_folder):
        shutil.rmtree(folder, ignore_errors=True)
        os.rename(raw_folder, folder)
    # onedir 和 onefile 都会生成 dist/ability.spec 之类，这里不需要
    for leftover in ("ability.spec",):
        path = os.path.join(DIST, leftover)
        if os.path.exists(path):
            os.remove(path)

    one = os.path.join(DIST, "ability.exe")
    folder = os.path.join(DIST, "ability_standalone")
    if not os.path.isfile(one):
        print("没有单文件产物", one)
        return 1
    if not os.path.isfile(os.path.join(folder, "ability.exe")):
        print("没有文件夹版产物", folder)
        return 1

    print(f"\n单文件版：{one}（{os.path.getsize(one) / 1024 / 1024:.1f} MB）")
    total = sum(os.path.getsize(os.path.join(root, name))
                for root, _dirs, files in os.walk(folder) for name in files)
    print(f"文件夹版：{folder}（共 {total / 1024 / 1024:.1f} MB）")
    print("\n分发：单文件版直接把 ability.exe 给对方；文件夹版把整个 ability_standalone 打包给对方。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

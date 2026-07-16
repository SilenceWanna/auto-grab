"""一步打包脚本 —— 生成 dist/auto-grab.exe。

用法：
    python build_exe.py

首次运行前需装 PyInstaller：
    pip install pyinstaller

生成的 exe 在 dist/ 目录下。发布时可与 config/config.example.yaml 一并分发。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SPEC = Path(__file__).resolve().parent / "auto-grab.spec"


def main() -> int:
    if not SPEC.exists():
        print(f"未找到 spec 文件: {SPEC}")
        return 1

    # 检查 PyInstaller 是否可用
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("未安装 PyInstaller。请先运行:  pip install pyinstaller")
        return 2

    print("=" * 60)
    print("开始打包 auto-grab.exe ...")
    print(f"spec:   {SPEC}")
    print(f"输出:   dist/auto-grab.exe")
    print("=" * 60)

    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "-y", str(SPEC)],
        cwd=SPEC.parent,
    )
    if result.returncode == 0:
        exe = SPEC.parent / "dist" / "auto-grab.exe"
        if exe.exists():
            size_mb = exe.stat().st_size / 1024 / 1024
            print("\n✅ 打包成功！")
            print(f"   {exe} ({size_mb:.1f} MB)")
            print("\n分发提示:")
            print("  1) 把 auto-grab.exe 和 config/config.example.yaml 一起分发")
            print("  2) 用户首次运行前，需在 exe 同目录建 config/ 文件夹并放入 config.yaml")
            print("  3) 首次启动会在同目录建 .session/ 存放 cookies")
        else:
            print("打包命令成功但未生成 exe，请检查上方输出。")
            return 3
    else:
        print("\n❌ 打包失败，请查看上方 PyInstaller 输出。")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())

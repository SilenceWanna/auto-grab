# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

用法：
    pip install pyinstaller
    pyinstaller auto-grab.spec

生成 dist/auto-grab.exe（单文件，双击即启动 GUI）。

exe 首次运行会在同目录寻找/生成 config/config.yaml 与 .session/。
所以分发 exe 时最简做法：只发 exe 本身，让用户在同目录复制配置模板。
或者一起打包 config.example.yaml 和 README 一并给用户。
"""

block_cipher = None


a = Analysis(
    ['src/gui.py'],
    pathex=[],
    binaries=[],
    datas=[
        # 需要一起打包的静态资源（YAML 模板等）
        # ('config/config.example.yaml', 'config'),  # 可选
    ],
    hiddenimports=[
        # DrissionPage 及其子模块（PyInstaller 可能漏检）
        'DrissionPage',
        'DrissionPage._pages.chromium_page',
        'DrissionPage._pages.mix_tab',
        'DrissionPage._pages.session_page',
        'DrissionPage._units.setter',
        'DrissionPage._units.selector',
        'DrissionPage._units.waiter',
        'DrissionPage._functions.cookies',
        'DrissionPage._base.chromium',
        # plyer 的 Windows 通知后端
        'plyer.platforms.win.notification',
        # 我们自己的模块
        'src.login',
        'src.query',
        'src.order',
        'src.notifier',
        'src.config',
        'src.utils',
        'src.main',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 减小 exe 体积：抢票用不到的大依赖
        'matplotlib',
        'numpy',
        'pandas',
        'PIL',
        'tornado',
        'IPython',
        'jupyter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='auto-grab',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,       # UPX 压缩会被杀软误报,关掉更稳
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # False = GUI 模式，双击不弹 CMD 窗口；调试时改 True 能看日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,       # 有 .ico 图标可填路径
)

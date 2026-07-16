# 12306 自动抢票脚本

一个运行在 PC 端的 12306 自动抢票工具，基于**浏览器自动化**（[DrissionPage](https://github.com/g1879/DrissionPage)）实现。自动完成登录、余票轮询、下单占座与成功通知，替代手动刷新抢票。

> ⚠️ **合规声明**：本项目仅供个人学习与自用。请勿用于黄牛倒票、高频攻击 12306 服务器等违规用途。使用者需对自身行为负责，遵守 12306 服务条款与相关法律法规。

## 功能特性

- 🔐 自动登录并复用会话（cookies 持久化）
- 🔍 按车次/日期/席别轮询查询余票（含随机抖动防频控）
- 🎫 命中余票自动选座、提交订单、占座
- 🔔 抢票成功多渠道通知（声音 / 桌面通知 / 手机推送）

## 项目状态

- ✅ 阶段 0：项目骨架
- ✅ 阶段 1：登录（cookies + UAM 会话恢复、官方接口校验登录态、独立浏览器 Profile 隔离）
- ✅ 阶段 2：余票查询（3375 站码、轮询、席别过滤）
- 🧪 阶段 3：下单占座（完整 dry-run 已通过：进入确认页、选择乘车人和席别；真实提交保持关闭，详见 [STAGE3_BLOCKER.md](STAGE3_BLOCKER.md)）
- ✅ 阶段 4：多渠道通知（提示音/桌面通知/Server酱/钉钉）
- ✅ 阶段 5：调度健壮性（心跳/退避/重登上限,长时轮询实测通过）
- 🚧 阶段 6：可选优化（放票整点智能调度✅、tkinter GUI✅、PyInstaller 打包 spec 已就绪）

详细路线图见 [WORKPLAN.md](WORKPLAN.md)。

## 环境要求

- Python 3.10+
- Chrome / Chromium 浏览器

## 三种使用方式

### 1) 命令行（默认）

```bash
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
# 编辑 config/config.yaml
python -m src.main
```

### 2) 图形界面（无需手写 YAML）

```bash
python -m src.gui
```

界面里直接填账号/行程/乘客/干跑开关，点「开始抢票」。实时日志显示在下方。

### 3) 打包成 exe（分发给非技术用户）

```bash
pip install pyinstaller
python build_exe.py
```

生成 `dist/auto-grab.exe`，双击即可运行。分发时把 exe 与 `config/config.example.yaml` 一起发；用户在 exe 同目录建 `config/config.yaml` 即可。

> `config/config.yaml` 含账号密码，已在 `.gitignore` 中忽略，不会被提交。

## 项目结构

```
auto-grab/
├── README.md               # 使用说明
├── WORKPLAN.md             # 开发路线图
├── STAGE3_BLOCKER.md       # 阶段3 下单诊断与技术档案
├── requirements.txt        # Python 依赖
├── auto-grab.spec          # PyInstaller 打包配置
├── build_exe.py            # 一步打包 exe 脚本
├── config/
│   └── config.example.yaml # 配置模板
└── src/
    ├── main.py             # CLI 入口
    ├── gui.py              # tkinter GUI 入口
    ├── config.py           # 配置加载
    ├── login.py            # 登录与会话
    ├── query.py            # 余票查询
    ├── order.py            # 下单占座
    ├── notifier.py         # 成功通知
    └── utils.py            # 通用工具
```

## 开发计划

详见 [WORKPLAN.md](WORKPLAN.md)。

## 免责声明

本工具按“现状”提供，不对抢票结果作任何保证。因使用本工具产生的任何后果由使用者自行承担。

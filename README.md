# 12306 自动抢票脚本

一个运行在 PC 端的 12306 自动抢票工具，基于**浏览器自动化**（[DrissionPage](https://github.com/g1879/DrissionPage)）实现。自动完成登录、余票轮询、下单占座与成功通知，替代手动刷新抢票。

> ⚠️ **合规声明**：本项目仅供个人学习与自用。请勿用于黄牛倒票、高频攻击 12306 服务器等违规用途。使用者需对自身行为负责，遵守 12306 服务条款与相关法律法规。

## 功能特性

- 🔐 自动登录并复用会话（cookies 持久化）
- 🔍 按车次/日期/席别轮询查询余票（含随机抖动防频控）
- 🎫 命中余票自动选座、提交订单、占座
- 🔔 抢票成功多渠道通知（声音 / 桌面通知 / 手机推送）

## 项目状态

🚧 **开发中** —— 当前已完成项目骨架与工作计划，各功能模块为占位实现，正在按 [WORKPLAN.md](WORKPLAN.md) 逐步落地。

## 环境要求

- Python 3.10+
- Chrome / Chromium 浏览器

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 复制配置模板并填写你的信息
cp config/config.example.yaml config/config.yaml
#   编辑 config/config.yaml，填入账号、车次、乘客等信息

# 3. 运行
python -m src.main
```

> `config/config.yaml` 含账号密码，已在 `.gitignore` 中忽略，不会被提交。

## 项目结构

```
auto-grab/
├── README.md               # 使用说明
├── WORKPLAN.md             # 开发路线图
├── requirements.txt        # Python 依赖
├── config/
│   └── config.example.yaml # 配置模板
└── src/
    ├── main.py             # 程序入口
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

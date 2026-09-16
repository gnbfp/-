# 小组作业项目经理 · 飞书群机器人

> 机器人是**"群里的第 N+1 个成员"**。它不替你写作业，也不替你想——它保证**每个人都有活、每个评分点有人管、每个想法都有出口**。

## 它做什么

小组在飞书群里丢一份作业书，机器人接手整场"作业启动会"：

```
作业书 → 抽出评分点 → 群里投票定方向 → 拆成任务卡
      → 私聊收志愿并自动分配 → 执行期催办 → 输出执行报告
```

人只做两个动作：**私聊填志愿、群里投票**。

## 系统形态

**1 个 Agent + M0–M8 九个模块，单进程 Python 服务**，数据存 JSON 文件（不用数据库）。

| 层 | 模块 |
|---|---|
| 交互层 | M0 机器人网关（11 条指令，前缀精确匹配） |
| 智能层 | M1 输入解析 / M2 方向共识 / M3 任务拆解（**全项目仅有的三处 LLM**） |
| 协作层 | M4 志愿分配 / M5 匿名代言（纯规则） |
| 流程层 | M6 催办（定时扫描） |
| 交付层 | M7 执行报告（模板渲染） |
| 证据层 | M8 评测集（离线，不进运行时） |

## 文档从哪看

| 文件 | 内容 |
|---|---|
| `AGENTS.md` | **AI 入口**：项目全貌、当前状态、工作约定 |
| `requirements.md` | **需求唯一真源**：10 条硬性边界、数据模型、关键机制、未定义清单、决策台账 |
| `docs/GLOSSARY.md` | 名词速查（「方向」「模块」「花名册」「D5 门」……） |
| `docs/ARCHITECTURE.md` | 架构设计；§12 是"从零到能跑"的上手清单 |
| `docs/定稿文档-v2.md` | 方案层定稿：架构、模块、排期（主要依据） |
| `docs/requirements/` | 需求原件：两张截图 + 逐字转录（冲突时以截图为准） |

## 快速开始

```bash
# 1) 装依赖（唯一真源是 requirements.txt）
pip install -r requirements.txt
#    中文 Windows 上若 pip 报 UnicodeDecodeError，先执行 set PYTHONUTF8=1 再装。

# 2) 配凭据（.env 已被 .gitignore 忽略，不会提交）
copy .env.example .env
#    要填：FEISHU_APP_ID / FEISHU_APP_SECRET / LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
#    飞书应用怎么建、要开哪些权限，见 docs/ARCHITECTURE.md §12.1

# 3) 跑飞书网关（长连接，不占端口、不需要公网）
python -m src.gateway.app

# 4) 命令行主链路：作业书 → 任务卡 → 核对清单（这条不需要飞书凭据）
python -m src.main --file 作业书.pdf
```

依赖、环境变量、开发顺序见 `docs/ARCHITECTURE.md` §12。

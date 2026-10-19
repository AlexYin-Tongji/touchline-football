# 看台 · Touchline

**你的私人足球编辑部。** 一支球队的动态、阵容、赛程与数据，整理成中文，存在你自己的电脑里。

看台是一个本地运行的个人足球助手。它从公开来源读取消息与数据，把英文报道整理成中文动态，按球队归档，并允许你带着原文继续追问。没有账号，没有社区，没有信息流推荐——只有你看的那支球队。

> **当前版本：V9.3.0（首个公开发布版）**
> 界面与代码结构重写，功能边界不变。详见[版本记录](docs/CHANGELOG.md)。
> 发布页在 [`site/index.html`](site/index.html)，不参与构建，可直接上传到任意静态托管。

---

## 它做什么

**动态** · 按球队阅读中文消息流。每条消息标注来源与来源层级（官网 / 数据源 / 媒体 / 社区），配图来自来源自身声明；展开后仍是中文，可点链接核对原文，也可以就这条消息继续追问。

**球队** · 球队资料、一线队名单与有来源的照片、赛季表现、联赛积分榜、胜平负与主客场图表、本季亮眼球员、未来赛程。数值以数据源为准，模型不编写比分。

**问助手** · 直接对话，或从今日速览、比赛前瞻、赛后复盘、转会核验、战术讲解、球队画像进入。回答带引用，引用只能来自本轮实际读过的原文。

---

## 三条设计原则

**整合信息，不裁决真伪。** 页面显示的是"谁在什么时候说了什么"。我们不给一条消息盖"已核实"的章，也不因为它无法确认就删掉它——真伪由读者判断。

**来源分层决定说话方式。** 官网直接陈述；数据源陈述并标注；媒体报道写"据 X 报道"；社区消息写"社区有说法"并附认同量。社区源不是低一等的媒体源，它的价值在于时效与现场性。

**模型解释，不编造。** 比分、阵容、成员关系、照片都必须来自可追溯的采集。缺数据就留空，不生成假球员肖像，也不用模型填补事实字段。

---

## 快速开始

**Windows**：双击 `启动看台.cmd`，浏览器打开 <http://127.0.0.1:9080/>。

首次启动会提示安装依赖并构建页面。需要 Python 3.11+ 与 Node.js 22+。

**手动启动**：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c requirements.lock.txt
npm ci
npm run build
.\.venv\Scripts\python.exe -m uvicorn server.main:app --host 127.0.0.1 --port 9080
```

**端口被占用？** Windows 会把成片端口分配给 Hyper-V/WSL，且重启后会变化，表现为 `WinError 10013`。换一个端口即可：

```powershell
$env:TOUCHLINE_PORT = "9123"
```

**第一次使用**：在设置里添加模型服务商（地址、协议、密钥），选择一个模型；然后关注一支球队，点击动态页的「启动」。

---

## 模型配置

支持 Chat Completions、Responses 与 Anthropic Messages 三种协议，任何兼容的模型服务都可以接入。

所有功能共用一个默认模型，也可以分别指定动态整理、足球对话与球队资料解读使用的模型。密钥加密保存在本机，不回显、不进日志、不进 Git。

模型 API 是否收费取决于你的服务商。看台本身不收取任何费用，也不包含广告或遥测。

---

## 数据与隐私

- 所有数据保存在本机 `data/`：中文内容、对话、球队资料、关注选择、加密凭据。
- 外部新闻**不保存正文**：只持久化标题、链接、日期、内容哈希与引用记录。正文在需要时临时读取，内存缓存一小时过期。
- 为了让问答能按正文检索，另存一份**派生索引**：词频、段落区间、人名与俱乐部标识，不含任何句子。命中后仍需重新读取原站才能引用。
- **`data/touchline.key` 与数据库是一个单元。** 删除 key 会导致全部密钥不可恢复。需要清库时用脚本：

```powershell
.\.venv\Scripts\python.exe scripts/reset-data.py --dry-run   # 先看会删什么
.\.venv\Scripts\python.exe scripts/reset-data.py
```

**永远不要 `rm data/`。**

---

## 范围

- 五大联赛 2026/27 赛季共 96 支球队：英超、西甲、德甲、意甲、法甲。
- 阵容与照片按球队维护，照片必须有明确来源；原站没有照片时保留占位。
- 来源可能改版或限流。程序保留上次有效的快照与更新时间，失败时如实说明，不用旧数据冒充新消息。

---

## 开发

```powershell
npm run dev                 # 前端开发服务器，API 代理到 9080
npm run build               # 类型检查 + 构建
node scripts/check-css-coverage.mjs   # 样式表与 TSX 类名对账
```

界面改动请在真实应用里核对桌面（1440）与窄屏（390）两个宽度：

```powershell
npm install --no-save playwright
npm run shot                # 普通页面
npm run shot:settings       # 需要点击或未登录状态的界面
```

截图落在 `output/visual/`（已忽略）。

GitHub Actions 在 Linux 上检查导入与 Docker 构建。Docker 与 Render 配置保留作可选服务器运行方式，不是个人版的使用前提。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [开发约定](AGENTS.md) | 改代码前必读：硬边界、缓存规则、照片规则、权限四层 |
| [代码结构](docs/ARCHITECTURE.md) | 哪个文件管什么，样式为何按页面切分 |
| [版本记录](docs/CHANGELOG.md) | 每轮为什么改，以及当时立下的边界 |
| [设计原则](docs/DESIGN_PRINCIPLES.md) | 为什么这样设计 |
| [质量与运行契约](docs/QUALITY_AND_RUNTIME_CONTRACTS.md) | 引用、比赛数据、主子智能体、技能权限 |
| [数据来源](docs/DATA_SOURCES.md) | 来源、配额与后台维护 |
| [接口与模块约定](docs/API.md) | 后端接口 |
| [球员肖像匹配实验](docs/PORTRAIT_MATCH_EXPERIMENTS.md) | 身份绑定与覆盖率的实测数据 |
| [本地与免费托管](docs/FREE_HOSTING.md) | 托管选择 |

代码仓库：[AlexYin-Tongji/touchline-football](https://github.com/AlexYin-Tongji/touchline-football)

---

## 许可与素材

队徽与球员照片的权利属于各俱乐部、联赛与原始来源。项目内保存的素材均附来源记录，按各自许可与署名要求使用。公开使用前请自行核对素材授权。

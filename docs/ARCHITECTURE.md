# 看台 · 代码结构

这份文档回答"哪个文件管什么"。规矩（什么能改、什么不能改）见 [AGENTS.md](../AGENTS.md)，设计推导见 [DESIGN_PRINCIPLES.md](DESIGN_PRINCIPLES.md)。

## 目录

```
touchline-football/
├── src/                     前端（React 19 + TypeScript + Vite）
│   ├── main.tsx             入口：挂载、样式导入顺序、封面开关
│   ├── Cover.tsx            开场封面（一个进入按钮）
│   ├── App.tsx              外壳、导航、球队上下文、球队选择器
│   ├── AuthGate.tsx         访问口令（仅服务器模式）
│   ├── ui.tsx               通用件：Crest / Modal / Spinner / Empty / 提示条
│   ├── shared.tsx           领域共用件：Markdown / Photo / Sources / 时间格式
│   ├── FeedView.tsx         动态
│   ├── TeamCenterView.tsx   球队页（资料 / 阵容 / 赛程）
│   ├── AssistantView.tsx    问助手与对话记录
│   ├── ModelSettingsView.tsx  设置
│   ├── TeamActions.tsx      球队页的更新 / 补全动作条
│   ├── WorkflowControls.tsx 动态页的启动 / 结束控制条
│   ├── DecisionHistory.tsx  球队页的更新记录
│   ├── RecoverableView.tsx  懒加载失败时的兜底
│   ├── clubCatalog.ts       96 支球队索引（生成物，勿手改）
│   ├── clubNames.ts         联赛与俱乐部的中文名
│   ├── api.ts               请求封装与错误文案
│   ├── types.ts             API 契约类型
│   └── styles/
│       ├── tokens.css       唯一取值来源：字体、字号、颜色、间距、圆角、层次
│       ├── base.css         重置、排版默认值、原子件、Markdown、弹窗
│       ├── shell.css        页头、左侧球队栏、内容区、窄屏底部导航
│       ├── feed.css         动态与阅读弹窗
│       ├── team.css         球队页全部板块
│       ├── assistant.css    问助手
│       ├── settings.css     设置、球队选择器、登录
│       └── cover.css        开场封面
├── server/                  Python FastAPI 后端
├── agent_prompts/           系统提示词
├── agent_skills/            技能与参考资料
├── docs/                    文档
├── public/assets/           队徽、联赛标志等静态素材（含来源记录）
├── scripts/                 维护脚本与探针
├── site/                    发布页（独立静态站点，不参与构建）
└── data/                    运行时数据（不入 Git）
```

## 前端分层

**样式只允许从 `tokens.css` 取值。** 组件样式表里不写裸数值：字号用 `--fs-*`，间距用 `--sp-*`，颜色用语义变量（`--ink` / `--muted` / `--faint` / `--red` / `--green` / `--amber` / `--blue`）。新增一档字号或一种灰色之前，先问它是不是已经有了。

**组件样式表按页面切分，不按组件切分。** 一个板块的规则集中在一个文件里，改版面时只需要打开一处。跨页共用的东西（按钮、标签、提示条、弹窗）在 `base.css`。

**中文不加字距。** `letter-spacing` 只用于西文大写标签（`.overline`）与英文名。方块字自带字距，拉开就散。

**选择器失配不会报错。** 重命名或重写样式表后运行：

```powershell
node scripts/check-css-coverage.mjs
```

它列出"TSX 用了但样式表没有"的类名，以及"定义了但已无引用"的选择器。前者是真正的遗漏，后者需要人工判断——运行时拼接的类名（`result-${…}`）不会出现在字面量里。

## 后端模块

后端按职责分层，名字相近的模块分工不同：

| 模块 | 负责 |
| --- | --- |
| `main.py` | FastAPI 应用、路由、启动 |
| `agent.py` / `agent_runtime.py` | 主循环、模型调用、工具调度 |
| `agent_tools.py` | 工具定义与执行 |
| `agent_delegation.py` | 子助手 |
| `research.py` / `search_index.py` | 检索、排序、原文读取 |
| `ingestion.py` / `collection.py` | 来源发现、正文读取 |
| `team_center.py` | 球队页数据组装（对外） |
| `team_panel.py` | 球队页维护流程（对后台） |
| `team_portraits.py` / `community_portraits.py` | 球员照片解析与核验 |
| `team_names.py` / `entity_names.py` / `player_names.py` | 队名、实体别名、球员中文名 |
| `news_*.py` | 新闻来源分层、分组、成稿 |
| `storage.py` | SQLite 与密钥库 |
| `provider*.py` / `providers/` | 模型服务商与外部数据源 |

`team_center.py` 与 `team_panel.py` 喂同一批比赛但产出不同字段，改动时要成对检查（见 AGENTS.md 第七节）。

## 脚本

| 前缀 | 用途 |
| --- | --- |
| `probe-*.py` | 对真实链路只读的一次性探针，用临时库 |
| `check-*.py` / `check-*.mjs` | 交付前检查：跨联赛泛化、样式覆盖率 |
| `shot*.mjs` | 界面视觉核对（Playwright + 系统 Chrome） |
| `build-club-index.mjs` | 从 `public/assets/clubs/catalog.json` 生成前端索引，`npm run build` 前置 |
| `reset-data.py` | 清库（保留密钥与模型配置） |

## 发布页

`site/index.html` 是独立的宣传页，不参与应用构建，也不调用任何接口。直接双击即可预览，也可以整目录上传到任意静态托管（GitHub Pages、Cloudflare Pages、Vercel）。截图在 `site/assets/`，需要更新时按页面顺序重截并覆盖同名文件。

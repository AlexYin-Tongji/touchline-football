# 看台优化 Implementation Plan

> **For agentic workers:** 使用 superpowers:subagent-driven-development 分工执行并交叉审阅。

**Goal:** 完成用户五项优化并保留优化前回退标签。

**Architecture:** 在现有模块上改造采集和维护执行边界；前后端通过轻量状态与分块维护状态协作。不引入新的服务或后台基础设施。

**Tech Stack:** Python/FastAPI/SQLite；React/TypeScript/Vite；unittest 与 Node 行为测试。

**Spec:** `docs/superpowers/specs/2026-09-22-reliability-performance-design.md`

## Global Constraints

- 只保留 main，远端 v7.0.0 固定优化前源码；不改写历史。
- 遵守AGENTS的数据来源、权限、临时正文、凭据保护与避剧透约束。
- 所有测试使用临时库、Mock或本地替身，不调用付费模型。
- 新闻代理负责 research/ingestion/采集管理；球队代理负责 team_center/team_portraits/league_data；前端代理负责 src；协调者负责存储、运行控制、API、启动和测试入口。各方不改别人的文件区域。

## Task 1: 新闻工具和流程

文件：server/research.py、server/ingestion.py、server/personal_app.py采集管理部分、agent_skills/news_report/SKILL.md、agent_skills/reader_protocol.md；tests/test_research*.py及集中新用例。

- [x] 用三个延迟响应验证并发、结果稳定及部分成功；用两个NewsArticle验证主文章字段一致；先观察失败。
- [x] 实现每Store在途去重、有限并发、失败冷却及force刷新，入库前复核权限；取消清理所有子任务。
- [x] collect_team保持原有返回字段，新增可选force=False；更新技能明确缺口和补充顺序。
- [x] 跑负责模块并记录模拟耗时前后对比。

## Task 2: 球队资料按部分维护

文件：server/team_center.py、team_portraits.py、league_data.py、agent_skills/team_maintenance/SKILL.md及对应tests。

- [x] 测试慢照片不阻塞核心数据、失败部分不清空旧快照、只补缺项、单队赛程完整性。
- [x] maintain_team与Manager.start新增parts=None，允许roster/schedule/standings/portraits；默认自动选择缺失或过期项。
- [x] center/context增量返回components与coverage，前端读取同一字段；照片任务归Manager生命周期管理。
- [x] 运行球队/赛程/积分相关回归。

## Task 3: 前端可读性和交互

文件：src/App.tsx、v7*.tsx、v7.css及需要提取的共享模块；scripts/frontend-regressions.mjs。

- [x] 保留前次可复现的慢请求草稿与切队回归，先失败后修复。
- [x] 设置页采用用户已选浅色绿色、16px中文无衬线体系；编辑控件保持清晰层级及窄屏可用。
- [x] 非首屏按需加载；App调用GET /api/state?view=summary；分类通过GET /api/feed?topic=...在服务端筛选。
- [x] 显示球队分块缺项及模型移除后的可执行恢复方式。
- [x] 删除确证无用旧UI，执行构建及1440/390浏览器验收。

## Task 4: 存储、API与运行控制

文件：server/storage.py、seed.py、main.py、agent.py、agent_tools.py、personal_app.py的feed_view；tests/test_runtime_optimization.py。

- [x] 编写事务回滚/批量提交、迁移备份、轻量state不扫描历史、先筛后分页、预读来源部分失败、交替循环保护用例。
- [x] atomic用SQLite保存点支持嵌套与瞬时正文回滚；批量seed共享事务；迁移先备份数据库与key后原子执行。
- [x] view=summary跳过历史列表/派生工作，保持旧API默认；feed新增topic；maintain API/tool透传parts。
- [x] 选定来源逐项报告网络错误，保留成功内容且不放宽权限；循环检测比较稳定结果及真实阅读进展。
- [x] 运行相关小套件，待合并工作区稳定后统一全量验证。

## Task 5: 测试和交付

文件：scripts/check.py、package.json、.github/workflows/checks.yml、README.md、AGENTS.md、启动脚本与必要测试。

- [x] 统一fast/full与显式模块入口，输出总耗时与慢测试；全量仍执行所有回归。
- [x] npm test:ui调用scripts/frontend-regressions.mjs；更新文档说明日常与交付检查。
- [x] 启动脚本同步依赖/构建输入，运行服务检测构建标识；遇旧后端明确提示安全重启。
- [x] 交叉审阅并解决实质发现；完整unittest、前端行为、build与桌面/手机验收。
- [x] 记录验证和已知实网限制，提交最终变更；保持main和回退标签清晰可识别。

# V7.8.1 项目清理

清理历史中间产物，并收拢分散在早期文档里、当前仍在运行的机制说明。没有功能改动。

## 移除的内容

| 类别 | 内容 | 说明 |
| --- | --- | --- |
| 历史产物 | `output/` 约 115M → 1.1M | 各版本验收 JSON/日志、临时采集脚本、两份旧版完整源码快照 |
| 早期原型 | `prototype/` | V7 起改用 Vite 构建，不再使用 |
| 开发计划 | `docs/superpowers/` | 2026-09-22/23 的过程稿 |
| 历史文档 | `docs/` 中 17+4 份 | 逐版本验收记录与早期设计文档 |
| 缓存 | `__pycache__`、`.playwright-cli`、`*.tsbuildinfo` | 可再生成 |

保留 `output/visual/`（截图脚本的输出目录）与 `data/` 全部内容——后者是活数据库、加密凭据和迁移备份，不是产物。

## 先归纳，再删除

`V3_QUALITY.md`、`V3_DATA.md`、`AGENT_RUNTIME_V7_2.md`、`ARCHITECTURE_V7_5.md` 描述的是**当前仍在运行**的机制，直接删除会让这些约定在仓库里无处可查。因此先核对代码确认仍然生效，再浓缩为 [现行质量与运行契约](QUALITY_AND_RUNTIME_CONTRACTS.md)：

- 原文支持核对：`passages` 分片 8–700 字、NFC 规范化、前后各 800 字上下文、`verify_submission` 独立请求、四类错误、`/conflicting/uncertain` 降级、缺失断言前置检查、45 秒与 48,000 字上限、`partial` 恢复及其阻止条件。
- 比赛数据边界：可追溯数据、赛前截止线、避剧透删节视图。
- 主子智能体：同一执行流程、权限不得扩大、外部文本只是参考数据。
- 技能与权限：权限由代码固定。

核对结果与代码常量逐项比对（`server/verification.py` 的 8–700 与 800 字上下文、`read_evidence` 的 12,000 字分页、`not_requested` 演示标记）。

## 连带修正

- 删除 `prototype/` 后，`server/main.py` 的 503 提示不再指向不存在的路径。挂载逻辑保留 `is_dir()` 守卫，以兼容仍带 `prototype/` 的旧签出。
- `.dockerignore` 移除失效的 `prototype` 与 `.playwright-cli` 条目；`.gitignore` 移除 `.playwright-cli`。
- 修复因文档删除产生的死链（README、API、HARNESS、PRD、RELIABILITY_V7_6），README 的资料索引改为只列现行文档。

历史内容仍可按版本标签取回，例如 `git show v7.4.0:docs/V4_VALIDATION.md`。

## 验证

- Python 完整套件 **735 通过**
- UI 回归 **24 通过**
- `npm run build` 通过

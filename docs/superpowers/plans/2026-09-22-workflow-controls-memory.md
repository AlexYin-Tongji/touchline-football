# 动态与球队启动、停止和任务记忆

用户要求完成两个页面的一键启动/结束与可恢复工作状态，并已授权将V7.2、V7.3提交到GitHub。两版正式标签已推送并核对；本轮基线6885eb5。

## 产品与运行契约

- news/team按球队独立控制。启动持续跟进；一轮完成后等待，不让模型空转；结束取消本功能、本队的当前任务和后代，保留完成结果。
- GET只读，页面轮询/旧自动调度不得重新启动已停止的工作。旧API可保留为显式启动的兼容入口。
- 状态接口GET /api/workflows/{kind}/{team_id}；POST同路径/start、/stop。返回kind/team_id/enabled/status/phase/message/last_error/started_at/finished_at/updated_at/next_run_at/progress/memory。
- 状态idle/running/waiting/stopping/stopped/interrupted/blocked/failed。progress含completed/failed/remaining；memory含summary/next_steps/completed_count，内部可有ID和版本但不保存原文/模型私有上下文。
- 系统提示词是稳定工作约定；技能是专项流程；工作记忆是本次进度；已核验球队知识仍是有来源的事实线索。四者分开，记忆不能代替原文事实。
- 重启将进行中工作标为interrupted并等待用户启动；不自动重放有副作用的模型/发布操作。
- 历史8条失败不等于当前轮仍失败。启动先使用已存可用资料重试，再发现新来源并处理；重复失败受原有有限重试约束，不循环重新计费。

## 分工与交付

- [x] 报告队列：team_id/owner_id/cancel_event/on_progress，按队取消、同库全局2报告执行槽、原子领任务。
- [x] 根代理：WorkflowManager、任务记忆、稳定提示文件、API与旧调度协调、隔离回归。
- [x] 界面：共用WorkflowControls，两个页面启动/结束、清楚的当前阶段与剩余工作；切队/卸载过期响应丢弃；保留分页和现有结果。
- [x] 验证：启动/停止/重启/部分失败/无模型/跨队、隐私/版本/源错误；完整Python、前端行为与构建；桌面及窄屏真实浏览器。
- [x] 本地提交新版本，保留基线标签；不修改用户数据库或调用真实付费模型进行自动化测试。

## 已完成外部动作

- main已由f3b4be9快进到6885eb5。
- v7.2.0=4db24afea8c58297182eeafb764f5a2298785a3a。
- v7.3.0=6885eb53672543138a73bda805b779a4f0b88624。

- 595项后端回归、14项UI行为回归、构建、桌面1440与窄屏390实际浏览器检查均通过。旧report retry空唤醒已接入工作流并补API回归。
- 发现实际服务仍是V7.1。确认无活动模型/报告/采集后，先用SQLite backup与key配套备份，再替换为V7.4；健康检查与构建指纹一致，两功能默认idle。
- 用户随后明确要求V7.4也提交GitHub，交付将推送main及v7.4.0。

- 额外保留旧球队维护parts定向更新契约，补API回归；最终595项完整回归通过。

# 数据来源与维护

看台默认通过已接入的公开来源维护关注球队。常驻维护在应用启动后运行，只刷新球队结构化资料，不自动调用付费模型。每轮最多两队，按字段时效决定更新内容；失败会退避，退出应用会关闭本轮任务。`TOUCHLINE_COLLECTION_ENABLED=0` 可用于明确停用常驻维护；自动测试统一停用真实后台采集。

球队页“更新记录”保留本功能启用后的更新原因、当时观察、来源时间、执行结果与前后变化。采集时间与来源声明时间分开；缓存命中不算重新确认。历史缺失无法补回，日志也不能替代原文引用。

## 配置可选来源

API-Football 和 football-data.org 的密钥保存在本机服务端加密库；界面和状态接口只返回是否配置。启动看台后，在项目目录运行：

```powershell
.\.venv\Scripts\python.exe scripts/configure-source.py api_football
# 或
.\.venv\Scripts\python.exe scripts/configure-source.py football_data
```

按提示输入密钥，输入不会显示，也不会写入命令历史。不要把密钥贴进聊天、URL、代码或 Git。移除已保存的密钥：

```powershell
.\.venv\Scripts\python.exe scripts/configure-source.py api_football --clear
```

API-Football 使用官方 v3 HTTPS 服务和 `x-apisports-key` 请求头。官方入门指南说明免费额度为 100 次 / 24 小时、每分钟另有限速：[官方指南](https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide)。本机按滚动 24 小时保守计数；请求前持久预留，失败不退还，重启与并发不能重置预算。来源不可用、额度不足、未配置密钥或身份不能唯一确认时返回明确缺口。

API-Football 的球队编号与 ESPN 不通用。按需查询先根据本地球队、完整赛季与官方目录精确匹配，保存该来源原生身份，再读取本队资料；不做模糊匹配，不将其他来源的编号拿来替代。阵容、赛程与积分的来源说法并列呈现，不自动覆盖既有事实字段。免费账号可访问的赛季与数据覆盖以实际响应为准。

## 只读运维入口

- `/api/collection/status`：常驻循环状态、各队上次检查与下次尝试时间。
- `/api/teams/{team_id}/decisions?limit=20`：当前球队的更新记录；遵守避剧透设置。
- `/api/data-sources`：来源能力、密钥配置状态和配额余量。

密钥写入/删除接口为 `/api/data-sources/{source_id}/credential`，沿用应用原有认证、同源检查与请求大小限制。模型工具没有密钥、权限或规则修改入口。

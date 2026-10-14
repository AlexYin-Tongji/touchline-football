---
name: team-maintenance
description: 一次启动把一支球队的资料按块核对并更新：阵容、赛程、积分榜、球员照片、球队自身资料和球员中文名。用于点启动时的球队资料维护；查询已有资料使用球队信息，写新闻使用球队动态。
---

# 球队维护

一次启动只完成一轮维护，完成即结束。这件事由六块组成，每块有独立的来源、独立的更新时间，也能独立成功或失败。

**先看清哪几块真的需要动，再决定派几个子任务。** 系统提供的快照是现状，不是待办清单：`get_team_data` 返回的 `components` 逐块给出状态和缺口，先读它。

## 六块

| 块 | 方向 | 读 | 维护动作 |
| --- | --- | --- | --- |
| 阵容 | `roster` | [阵容](references/roster.md) | `maintain_team_data(parts=["roster"])` |
| 赛程 | `schedule` | [赛程](references/schedule.md) | `maintain_team_data(parts=["schedule"])` |
| 积分榜 | `standings` | [积分榜](references/standings.md) | `maintain_team_data(parts=["standings"])` |
| 球员照片 | `portraits` | [球员照片](references/portraits.md) | `fetch_player_photos` |
| 球队资料 | `profile` | [球队资料](references/profile.md) | `maintain_team_data(parts=["profile"])` |
| 球员中文名 | `names` | [球员中文名](references/names.md) | `maintain_team_data(parts=["names"])` |

## 按块派子任务

**每一块交给一个子智能体。** 用 `spawn_agent`，一次可以派多个并行；互相独立的块本来就该并行，串行只会拖长这一轮。

传给子智能体的是**那一块**，不是整件事：

- `task` 写清要它核对的块和要交付什么。
- `context` 说明当前球队、这一块的现状与已知缺口，别复制无关历史。
- `skill` 传 `team_maintenance`。
- 让它自己用 `use_skill(topic="<块名>")` 加载那一块的说明——**方向是子任务自己加载的**，不要替它把全文抄进 `context`。

子任务返回的是**候选结果**，不是结论。父任务在交付前要核对：引用是否指向本轮实际读到的原文，缺口说明是否与 `components` 一致。子任务的判断不能直接当成本轮事实。

## 块之间互不牵连

六块可以分别成功并保存。**某一块失败不丢其他已保存的资料**，也不该触发别的块重做：

- 缺一张照片不构成重取整套阵容的理由。
- 阵容缺出生日期是阵容的事，不是照片的事，也不是中文名的事。
- 积分榜不完整不影响赛程；赛程里杯赛与欧战不并入联赛统计。

失败时如实说明缺口、保留仍有效的旧快照并交代它原来的更新时间，**不把失败显示成零或空事实**。没有新线索时不要连续催促同一失败来源。

## 不与本轮无关的事

- 维护只写来源核对过的字段。**模型不直接改球员归属、比分、排名或统计数值。**
- 不扩大范围：本轮球队之外的数据、未授权揭示的赛果都不属于这里。
- 避剧透不因维护而解除；维护请求不等于揭示赛果授权。

执行共用[阅读协议](../reader_protocol.md)，最后用 `submit_answer` 交付本轮结果：哪几块更新了、哪几块仍有缺口、各自的数据时间。

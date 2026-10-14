# 球队中心「更新/补全」验收清单

面板工作流是 `team`。父任务只加载这一份顶层技能，再按下面的顺序把叶子交给子任务。`team_refresh`、`team-maintenance`、`team-information` 仍走原来的入口，本清单不验收它们。

只派缺失、过期，或原因码可重试的叶子。`club.crest`、`squad.info`、`squad.images`、`schedule.standings` 可以并行。`squad.names` 必须等阵容身份键已经在快照里，之后可以和 `squad.images` 并行。`stats.charts` 等本轮赛程写入，或已有仍有效的赛程缓存。

## 叶子拿满

| 顺序 | 叶子 | 拿满 |
| --- | --- | --- |
| 1 | `club.crest` | 俱乐部资料没有可重试缺口；队徽是可用本地资产，或已记 `CREST_MISSING`。不下载队徽。 |
| 2 | `squad.info` | 阵容快照有效，身份键齐全，或剩余缺口不可重试。中文名不在本叶子。 |
| 3 | `squad.names` | 每名球员都有非空中文名，或带着原因码。译名可疑仍写回。全部已有中文名，或只剩不可重试缺口时停止。 |
| 4 | `squad.images` | 照片验收 `actionable_count == 0`。同名遮挡不对该来源重试。 |
| 5 | `schedule.standings` | 赛程覆盖完整，或剩余原因为 `INCOMPLETE_SCHEDULE` 以外的不可重试终态；积分榜为完整排名，或剩余缺口不可重试。 |
| 6 | `stats.charts` | 图表数字全部来自赛程样本。样本不够时是空态，不是拿满。 |

`squad.names` 的输入是 `provider_id` 加英文显示名。动作是大模型译成大陆中文名，不查维基数据、维基百科或其他姓名来源。写回只按 `provider_id`。

## 原因码

| 原因码 | retryable | 何时使用 |
| --- | --- | --- |
| MISSING | true | 该叶子还没有快照。 |
| STALE | true | 快照过期，可以再取。 |
| SOURCE_UNAVAILABLE | true | 来源暂不可用。 |
| RATE_LIMITED | true | 来源限流，稍后可再试。 |
| MODEL_FAILED | true | 大模型调用失败，可以再试。 |
| NAMESAKE_BLOCKED | false | 同名遮挡。不对同一来源重试。 |
| CREST_MISSING | false | 本地队徽资产缺失。不下载。 |
| INCOMPLETE_SCHEDULE | true | 赛程场次未覆盖应有范围。 |
| SAMPLE_INSUFFICIENT | false | 图表没有足够的赛程样本。禁止编造。 |
| SCOPE_DENIED | false | 当前范围不允许。 |
| INTERRUPTED | false | 本轮被打断。 |

### `squad.names` 的球员缺口

共享表里的 `MISSING` 仍表示叶子还没有快照，`retryable=true`。本叶子记在球员身上的 `MISSING` 是另一件事：没有英文名可译，`retryable=false`。父任务看这一条自己的 `retryable`，不要按共享表再派。

| 原因码 | retryable | 何时使用 |
| --- | --- | --- |
| MISSING | false | 没有英文显示名可译。 |
| MODEL_FAILED | true | 大模型翻译调用失败。 |
| INTERRUPTED | false | 本轮被打断。 |

## 停止

- 成功：范围内该拿满的叶子都已拿满。`SAMPLE_INSUFFICIENT` 不算图表拿满。
- `actionable_count == 0`：没有仍为缺失、过期或 `retryable=true` 的叶子。
- `INTERRUPTED`：停止派发，不把中断记成可重试。
- `squad.names`：每人都有非空中文名，或只剩不可重试缺口。还有待译的人或 `MODEL_FAILED` 才再派。

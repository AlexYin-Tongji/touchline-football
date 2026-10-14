# 球员照片

球员照片不是"补一个字段"，而是一次有身份依据、有验收、有停止条件的核对。

## 先读队列，再决定动谁

`photo_queue` 是只读的本地视图，**不联网**。先看它：

- `untried`：从未核对过的人。
- `settled`：来源已给出结论的人，附 `reason_code`。
- `no_identity_facts_count`：阵容里缺姓名/国籍/出生日期，核对无从下手的人。

**不要跳过这一步直接整队重取。** 已有 `photo_url` 的球员默认不需要更新——照片一旦通过身份核对就长期有效，重复获取只会浪费配额，还可能因来源限流反而丢图。

## 谁值得再核对一次

按每人返回的 `reason_code` 和 `retryable` 决定，**不要按"还有几个人没图"决定**：

| 类别 | 例 | 该怎么做 |
| --- | --- | --- |
| 传输未读取成功 | `timeout`、`connection_error`、`http_rate_limited` | **值得重试**，换时间 |
| 多候选无法分辨 | `community_ambiguous` | **值得重试**，来源数据变化后可能定下来 |
| 源中无此人 | `community_missing`、`community_no_image`、`no_free_image` | 不要重试 |
| 同名球员遮挡 | `community_namesake_blocked` | **不要对同一来源重试**，换来源 |
| 阵容缺事实 | `missing_name`、`missing_nationality` | 不是照片的事，是阵容那一块的缺口 |
| 来源已明确拒绝 | `rejected`、`license_not_free`、`source_withdrawn` | 不要重试 |
| 尚未核对 | `not_yet_resolved`、空 | 值得推进 |

**整年出生日期不符的同名者必须继续拒绝。** `namesake_blocked` 就是在说这件事：不要试图让模型改判，也不要换个拼法再问同一个搜索。

**免费社区库的姓名搜索已经退出默认核对。** `fetch_player_photos` 现在走编号能对上的来源：已复核的官网目录、阵容载荷里的 ESPN 头像、维基数据 P3681，然后才是维基媒体的俱乐部核验页。它不会发出 `searchplayers.php`。同名遮挡要换上述路径，而不是再搜一次姓名。没有身份证明就留空。代理换源的原型在 `server/portrait_agent.py`，尚未接到对话工具表；模型不提供照片地址。

## 一批一批推进

用 `plan_photo_batch` 指定本轮先核对谁（最多 12 人，只能选 `photo_queue` 列出的人），再调 `fetch_player_photos` 执行：

- **不传参数**：整队推进，已有照片的人被跳过。
- **`player_ids=[...]`**：只核对点名的球员，会触发"重新核对"。
- **`force=true`**：不要和整队一起用。成本高且可能因限流失败。

## 验收：看结论，不看空位数

工具会给出 `acceptance`，这是程序算出来的判断，**不是让它再猜一次**：

- `counts`：七类归因各有多少人。
- `actionable_count`：**只有它是 0，才代表没有可推进的活了**。等于 0 时不要继续发起核对。
- `acceptance_note`：已经写好的中文结论，可直接转述。
- `continuation_worthwhile`：程序对"是否值得再跑一轮"的判断。

**一轮没增长不要停下。** 批次上限仍是 12，一轮看不完整个球队。只要 `actionable_count` 大于 0，就还有可重试的缺图，下一轮继续。程序只在 `actionable_count == 0` 时停止；另有 100 轮的安全上限，避免无人值守时无限循环。连续两轮没有新照片，不代表该停。

## 画面审查与重试方向

来源回答"这是谁的脸"，回答不了"这张图能不能用"。画面可用性属于[资料与照片复核](../../team-information/references/squad/review.md)那条方向（从 `agent_skills/` 根算起，与 `team-information` 平行），不是这一块。

## 返回字段与引用

每个拿到照片的球员带 `photo_credit`、`photo_license`、`photo_source_url`、`photo_file_page`。**这些必须随图保留**：社区来源的照片是未声明开放许可的转载图，保留署名与来源是使用条件；缺少这些字段的照片不会渲染。

`photo_match` 说明这张图凭什么确认是这个人。新核对的顺序是 `exact_name_and_number`（官网姓名加号码）、`wikidata_espn_provider_id`（ESPN 编号与 P3681 一致，许可已核对）、阵容载荷上的 `espn_provider_id`。`wikimedia_verified_club` 仍从姓名进入，再核对出生日期和俱乐部。`community_*` 是旧姓名搜索留下的记录，默认流程不再新取。

## 不要做的事

- 不要用队徽、新闻配图、同名人物或猜测地址替代。
- 不要把缺图解释成这个球员不存在，也不要把有图解释成他一定出场或有位置。
- 不要在 `actionable_count` 已经是 0 时继续发起核对。
- 不要引用 `strEthnicity`、`strWage`、`strAgent` 这类社区记录中的隐私字段。

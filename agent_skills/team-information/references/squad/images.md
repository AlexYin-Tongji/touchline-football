# 球员图像

球员照片不是"补一个字段"，而是一次有身份依据、有验收、有停止条件的核对。本条只讲图像；球员事实字段属于[球员资料](info.md)。

## 先读，再决定问谁

`photo_queue` 是只读的本地视图，不联网。先看它，再决定要不要发起核对：

- `untried`：从未核对过的人，`fix_unknown`。
- `settled`：来源已给出结论的人，附 `reason_code`。
- `no_identity_facts_count`：阵容里缺姓名/国籍/出生日期，核对无从下手的人。

不要跳过这一步直接整队重取。已有 `photo_url` 的球员**默认不需要更新**——照片一旦通过身份核对就长期有效，重复获取只会浪费配额且可能因来源限流反而丢图。

## 谁值得再核对一次

按每人返回的 `reason_code` 和 `retryable` 决定，不要按"还有几个人没图"决定：

| 类别 | 例 | 该怎么做 |
| --- | --- | --- |
| 传输未读取成功 | `timeout`、`connection_error`、`http_rate_limited` | **值得重试**，换时间再来 |
| 多候选无法分辨 | `community_ambiguous` | **值得重试**，来源数据变化后可能定下来 |
| 源中无此人 | `community_missing`、`community_no_image`、`no_free_image` | 不要重试，源里没有 |
| 同名球员遮挡 | `community_namesake_blocked`（带 `namesake_blocked: true`） | 不要对同一来源重试 |
| 阵容缺事实 | `missing_name`、`missing_nationality` | 不是照片的事，交给 `maintain_team_data` |
| 来源已明确拒绝 | `rejected`、`license_not_free`、`source_withdrawn` | 不要重试，已给结论 |
| 尚未核对 | `not_yet_resolved`、空 | 值得推进 |

**整年出生日期不符的同名者必须继续拒绝。** 这类球员（全库有 53 人）的照片来源里只返回了一个同名陌生人，挂上去就是发布陌生人的脸。`namesake_blocked` 就是在告诉你这件事：不要试图"让模型判断一下"，也不要换个拼法再问同一个搜索。

**但它不等于"这个人拿不到照片"。** 遮挡意味着这个搜索被同名者占满了那一个名额，不代表球员不在来源库里。工具会自己再用姓氏走第二条路，并用阵容已有的运动员编号交叉确认——2026-10-12 在切尔西实测，29 人里有 12 人能通过这条路确认身份，其中就有全名搜索永远拿不到的人。所以拿到 `namesake_blocked` 时，正确动作**不是**对同一来源重复发起，而是换来源，或确认这条结论已经是最新的一次核对结果。

## 一批一批推进

用 `plan_photo_batch` 指定本轮先核对谁（最多 12 人，只能选 `photo_queue` 列出的人），再调用 `fetch_player_photos` 执行。不指定顺序时按来源游标推进，也是可以的——批次上限决定一轮看不完整个球队，这是正常的。

**`fetch_player_photos` 的参数怎么用：**

- **不传任何参数**：整队推进，补上还没解决的球员，已有照片的人被跳过。
- **`player_ids=[...]`**：只核对点名的球员。点名会触发"重新核对"，用于确认某个人的照片是否需要更新。
- **`force=true`**：不要和整队一起用。单独使用时它会要求来源忽略缓存重新回答，成本高且可能因限流失败。只有在你确认某个已核对的照片**必须**重新验证时才用。

## 验收：看结论，不看空位数

工具会在结果里给出 `acceptance`，这是程序算出来的判断，不是让它再猜一次：

- `acceptance.counts`：七类归因各有多少人（`fix_route` / `fix_roster` / `fix_source` / `fix_namesake` / `fix_ambiguous` / `fix_nothing` / `fix_unknown`）。
- `acceptance.actionable_count`：**只有它是 0，才代表没有可推进的活了**。等于 0 时不要继续发起核对。
- `acceptance_note`：已经写好的一句中文结论，可以直接用来向用户解释。
- `continuation_worthwhile`：程序对"是否值得再跑一轮"的判断。

**判断是否进步，要比两轮，不要看单轮。** 批次上限是 12，一轮永远看不到整个球队；比较 `with_photo` 和 `actionable_count` 相对上一轮的变化。连续两轮都没有增长时，问题在来源或路线，不在批次顺序，程序也会据此停下。

## 画面审查与重试方向

来源回答"这是谁的脸"，回答不了"这张图能不能用"——一张身份核对无误的照片，仍可能是四十米外的球场远景、水印图或错误裁切。画面可用性、以及缺图该往哪个方向重试，属于[资料与照片复核](review.md)，那条方向会同时看有图的球员与代码已归类的缺口。

**只对已有来源的照片开放。** 能进入审查的行，都是已经被来源核对过身份、带 `photo_url` 与 `photo_source_url` 的人。模型看不到、也不会被问到"该给谁配图"。

`acceptance` 由代码计算，`review` 由模型判断，两者回答不同的问题，不要合并成一句话说成"程序认为照片不合格"。

**缺国籍与出生日期的球员是另一件事。** 这类人不是"来源没有照片"，而是"本地没有可供核对的依据"（`no_identity_facts`），社区源在发请求前就会拒绝。它属于阵容事实的缺口，由复核中的"球员事实"项处理，见[资料与照片复核](review.md)。

## 返回字段与引用

每个拿到照片的球员会带 `photo_credit`、`photo_license`、`photo_source_url`、`photo_file_page`。**这些必须随图保留**：社区来源的照片是未声明开放许可的转载图，保留署名与来源是使用条件；缺少这些字段的照片不会渲染。

`photo_match` 说明这张图凭什么确认是这个人：`community_espn_provider_id`（运动员编号一致，最强）、`community_name_and_birthdate`（全名加出生日期）、`community_name_and_birth_year`（仅同一年）、`community_surname_and_birthdate`（来源拼写变体加确切日期）、`exact_name_and_number`（官网姓名加号码）、`wikimedia_verified_club`（自由许可）。解释时可以说明"依据编号核对"还是"依据姓名与出生日期核对"，后者弱一些。

## 不要做的事

- 不要用队徽、新闻配图、同名人物或猜测地址替代。
- 不要把缺图解释成这个球员不存在，也不要把有图解释成他一定出场或有位置。
- 不要在 `acceptance.actionable_count` 已经是 0 时继续发起核对。
- 不要把来源没有给出的结论（例如缺国籍意味着什么）通过自己推理补上，从而绕过核对。
- 不要引用 `strEthnicity`、`strWage`、`strAgent` 这类社区记录中的隐私字段；它们既不在产品字段里，也不得被读取。

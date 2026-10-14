# 阵容

本叶子只负责名单和身份键。中文显示名是下一片 `squad.names`：由大模型翻译，不在这里查来源，也不要调用 `maintain_team_data(parts=["names"])`。

## 先读

读当前阵容快照：人数、位置、号码、出生日期、归属，以及每行的 `provider_id`。已有有效快照且未过期，就不要整表重取。

## 补什么

名单缺失、过期，或有人缺少 `provider_id`：`maintain_team_data(parts=["roster"])`。不要为了中文名重取整份赛程或照片。

来源暂不可用记 `SOURCE_UNAVAILABLE`，限流记 `RATE_LIMITED`，都可重试。范围不允许记 `SCOPE_DENIED`，不可重试。

## 拿满

阵容快照有效，需要身份键的人都有 `provider_id`，或剩余缺口的原因码不可重试。不把失败显示成「没有球员」。不凭模型记忆补国籍、生日或位置。中文名留空给 `squad.names`。

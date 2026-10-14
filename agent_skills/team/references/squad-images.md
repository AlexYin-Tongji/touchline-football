# 球员照片

本叶子只补照片。缺图不构成重取阵容的理由。

## 先读队列

先看 `photo_queue`，再决定要不要发起核对。已有通过身份核对的照片默认不更新。

按每人的原因码和 `retryable` 决定，不要按「还有几个人没图」决定：

- `MISSING`、`STALE`、`SOURCE_UNAVAILABLE`、`RATE_LIMITED`：可重试。
- `NAMESAKE_BLOCKED`：同一来源不要重试。换来源可以，但不要改拼法再问同一个搜索。
- 来源已明确拒绝：不可重试。

用 `plan_photo_batch` 选定本批，再 `fetch_player_photos`。没有明确身份和许可依据就留空。

## 拿满

照片验收的 `actionable_count == 0`：没有仍可重试的缺图。同名遮挡记入失败原因，但不算「再跑同一来源还能拿到」。不要用队徽、新闻配图或猜测地址填人脸。

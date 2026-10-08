# 看台 V8.4：来源扩充与读取规则

2026-10-08 完成。本轮只做一件事：让每个工具真的能取到消息。交付标签 `v8.4.0`，只使用 `main`。

## 做了什么

每支球队的可用发布方从 **12 个增加到 32–33 个**，登记条目 27–28 → 34–35。新增来源全部经直连实测：能解析订阅、能读出正文、能取到配图，三者缺一不算可用。

| 类别 | 新增 |
| --- | --- |
| 英语媒体 | GiveMeSport、The42、HITC、Football Italia、90min、Metro、镜报、Caught Offside、The Hard Tackle、Football.London |
| 意大利语 | Calciomercato、Gianluca Di Marzio、MilanNews、全体育 |
| 西班牙语 | Relevo、Diario Gol、Estadio Deportivo、世界体育报 |
| 法语 | But! Football Club |
| 葡萄牙语 | Maisfutebol、Record |
| 荷兰语 | Voetbal International |
| 球迷社区 | Arseblog、Barca Blaugranes、Managing Madrid、Bavarian Football Works、Fear the Wall、AC Milan Offside、Into the Calderón |
| 本队频道 | BBC 按俱乐部订阅，覆盖 50 支球队 |

## 去掉的来源

三个来源此前占着位置却取不到东西，本轮移除：

- **ESPN 足球 RSS**：返回 HTTP 202 且正文 0 字节，永远不可能产出候选。ESPN 的结构化接口与球队页是另外两条通路，不受影响。
- **米兰体育报 RSS**：返回 200，但索引里最新一条是 2026-03-31，其余集中在 2024 年 1 月。健康检查一直把它当 `fresh`，实际贡献零条。这类「假活」比明确失败更麻烦。
- **FourFourTwo**：在本机网络下不可达。它只解析到一个 Fastly 地址，而固定 IP 传输恰好连不上。

踢球者订阅保留：索引可用、正文页对自动访问返回 403，读不到会如实记为读取失败，而不是假装成功。踢球者与米兰体育报的**来源归属登记保留**，这样读者仍能看到「这是踢球者说的」。

## 读取规则改了

旧规则：正文没能确认属于这篇文章时，**丢弃整条消息**。这是俱乐部官网普遍「读 0 篇」的原因——布莱顿、奥格斯堡、皇马官网页面返回 200 且含真实正文，却因为抽取器没能正式绑定正文容器而整条消失。

新规则按**内容量**区分，而不是按绑定标记：

| 页面 | 实测字符 | 处理 |
| --- | --- | --- |
| 导航或 cookie 壳 | 74–86 | 仍拒绝 |
| 真实正文 | 707–749 | 保留，标注「未能确认正文范围」 |

阈值 `MIN_UNBOUND_BODY_CHARS = 200` 落在十倍差距的中间，不是可调参数。布莱顿因此从 3 篇全读不出变为 9/9 可读。

**边界没有放宽**：女足、青年队与其他项目仍按原规则排除，这是相关性过滤，且已计数可见。取消的只是「因为没能确认就丢弃」。

## 工具文案

`discover_news`、`read_article`、`refresh_team_sources`、`research_news` 四个工具原先的描述几乎相同，模型无法判断该调哪个。现在每条按「什么时候用 · 给你什么 · 之后做什么」写，并互相指路。`limit` 上限由 8/6/4 提高到 20——实测 `limit=5` 读 5 篇、`limit=20` 读 20 篇，同一个候选池，说明读取篇数而非来源数量才是瓶颈。

## 验收

| 检查 | 结果 |
| --- | --- |
| `python scripts/check.py --suite full` | 1189 项通过 |
| `npm run test:ui` | 38 项通过 |
| `npm run build` | TypeScript 与 Vite 通过 |
| 新来源可达性 | 22/22 经应用自身传输层验证 |
| 端到端 | 阿森纳 27/28 源索引成功、40 候选、limit=20 读 20 篇 |

## 已知限制

- **小球队仍然少消息。** 本轮新增的多是服务大俱乐部的媒体：Calciomercato 全是国米米兰尤文，But! Football Club 全是 OM/PSG/Lens。博洛尼亚、昂热、奥格斯堡仍在 0–2 条。这是报道密度问题，不是工具问题——需要用更本土的地方媒体补，属于下一轮。
- **本机未配置 API-Football 密钥**，带密钥的结构化接口现网可用性仍未验证。
- 真实来源可用性是有限时点观察，不证明永久可用。来源清单与可达性证据在 Git 忽略的 `output/qa/`。
- `refresh_team_sources` 与 `research_news` 共用同一个后端函数，是同一能力的两名字。合并会牵涉兼容层与四处测试，留待下一轮。

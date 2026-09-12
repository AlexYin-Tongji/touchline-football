# V3 官方比赛数据与准确性边界

## 已实现

`server/fixtures.py` 是本项目自行维护的固定俱乐部适配器。没有调用大模型推测赛程、赛果或首发，也没有接入第三方足球预测/分析服务。

- `async sync_team(store, team_id, force=False)`：自动同步，返回变更比赛数、新增原始记录证据数、首发更新数、错误与同步时间。
- `get_team_status(store, team_id)`：只读同步状态与 30 分钟缓存的新鲜程度，不联网。
- 手动同步应传 `force=True`；自动更新复用缓存。比赛 ID 稳定，重复同步不新增相同比赛或相同版本证据。
- 保存主客队、对手、赛事、场馆、UTC 开球时间、官方日期确认标志、状态、主客比分、球队视角比分与 W/D/L。图表只使用明确完场且双方比分完整的数据。
- 比赛来源更新保留最多 20 个变化版本、原始字段、来源 ID/URL、证据 ID。人工备注、自定义字段和已有证据关联继续保留。明确旧版本不能覆盖新版本；过时的赛程列表不能将完场比赛降回待赛。

## 来源与解析

| 球队 | 官方入口 | 本地解析方式 |
|---|---|---|
| 阿森纳 | [男足赛程](https://www.arsenal.com/fixtures/men/fixtures) | 新官网页面自身调用的公开 `afc-prd.graph.arsenal.com/graphql` 只读查询。固定操作 `getLiveReportsListByFilters` 与 `fixturesByIds`，固定男足一线队 ID，逐月读取最近三个月、当月和下月。保留俱乐部比赛页链接供用户核对。 |
| 利物浦 | [男足赛程](https://www.liverpoolfc.com/matches/mens-team/fixtures)、[本季赛果](https://www.liverpoolfc.com/matches/mens-team/results/2026) | 解析 HTML 中的静态 Next Flight JSON，匹配同一张服务端比赛卡片的主客队名与比赛链接，读取明确的 `kickOffUtc/dateTbc/timeTbc`。本季不足 10 场时读取上一赛季官方赛果页，保存最近 30 个结果。 |
| 巴萨 | [一线队赛程](https://www.fcbarcelona.com/en/football/first-team/schedule)、[赛果](https://www.fcbarcelona.com/en/football/first-team/results) | 解析官网 SportsEvent JSON-LD、同一比赛 ID 的 HTML 卡片、时间戳、比分与 TBA 标志。只接受足球一线队入口。 |
| 国米 | [男足赛程](https://www.inter.it/en/matches?category=upcoming&competition=all&teams=men)、[2025/26 官方归档](https://www.inter.it/en/matches/2025-2026?category=results&competition=all&teams=men) | 解析 HTML 的 `__NEXT_DATA__.props.pageProps.matchesPageData`；校对 `teams=men` 和官方男足队伍 ID。历史页须回传请求的赛季，避免官网忽略参数后将当前赛季误当历史数据。 |

阿森纳公开原始数据接口属于官网自身的数据传输层，与第三方现成足球 Agent 或预测 API 不同。本地代码固定并审阅查询字段，不进行任意 GraphQL 查询、结构内省、登录、token 获取或写操作。

### 时间与状态

阿森纳编辑字段如 `2026-09-09T19:00` 没有后缀；只有同场官方赛事字段 `date=2026-09-09Z/time=19:00:00Z` 一致时才按 UTC 保存并确认。两者不一致时保留日期并注明不能确认，不自行选一个时间。真实交叉抽查：[官方 Napoli 赛前预告](https://www.arsenal.com/news/preview-napoli-v-arsenal-aydAo1x6Aclt) 写明 8pm UK，对应夏令时 19:00 UTC、意大利 21:00，与解析字段相符。来源原文发布时间为 2026-09-09 07:10 UTC。

巴萨 `KO: TBA` 和国米 `isDateConfirmed=false` 不会被当作确定开球时间。未知时间使用空 `kickoff` 和独立 `kickoff_date`，延期、取消、中止保留 `fixture_status` 与提示。日期已经过去不意味着比赛完场；进行中的比分不计入近期胜负图表。官方编辑比分与同场赛事字段冲突时，不展示结果、不计算 W/D/L。

### 首发

- 阿森纳：从官网 `team-news-` 公告的明确 `LINE-UPS` 区块读取，两支球队必须各有恰好 11 个不同名字，公告发布时间必须处于本场开球前 4 小时至后 1 小时。严格筛除女足和青年队，历史重名对阵不会靠标题关联。真实核对：[Chelsea 首发公告](https://www.arsenal.com/news/team-news-konsa-handed-first-start-against-chelsea-as5cb1S716B5)，2026-09-06 14:15 UTC，取得 22 个原文名单条目。
- 利物浦：从同场官方比赛页的 `homeLineUpLiveProps/awayLineUpLiveProps.starting` 读取，核对两队名字和各 11 名先发。真实核对：[Ipswich v Liverpool 官方比赛页](https://www.liverpoolfc.com/matches/mens-team/ipswich-town-v-liverpool-english-premier-league-20260904)，取得双方先发各 11 人。
- 当前自动补充最近一场结果，以及未来 48 小时内的下一场。缺少明确来源、阵容不完整或时间不匹配时统一 `lineups.status=unavailable`，不使用球队成员名单替代首发。
- 巴萨与国米本轮能同步真实赛程/赛果，尚无经验证的自动首发适配器；首发保持 unavailable。人员伤情仍由新闻资料与原文核对流程提供，本模块不会把报道措辞推断成结构化伤停名单。

## 证据记录与安全

每次有意义的比赛字段变化会保存一份 `fixture_record=true` 的证据，不制造普通新闻事件。证据正文包含抽取字段及来源原始数据，`match_id` 关联比赛；界面可通过来源链接核对。未完赛时模型可读证据会排除原始比分占位与直播数字，完整快照仍保存在比赛记录供复查；未来默认 0–0 不构成模型事实。`published_at=null`，官方字段更新时间另存 `source_updated_at`，读取时间为 `fetched_at`，不把抓取或更新时间冒充新闻发布时间。

完成或进行中的比赛证据带剧透标记。由于 JSON 数字字段不能可靠地按段落删节，`safe_evidence_view` 对这类记录整份隐藏。普通未完赛资料仍可读；所选比赛的明确赛后复盘由工具按用户请求读取。

所有网页访问复用已有 URL/域名允许列表、重定向边界和公网地址固定连接。利物浦完整历史赛季 HTML 使用明确的 8 MiB 上限，其余正常网页仍为 2 MiB。公开阿森纳 GraphQL 仅连接固定官方域名的已解析公网地址，响应 2 MiB、总读取时间 20 秒、禁止重定向和压缩扩张。解析 Next Flight 只解码 JSON，不执行网页脚本。

## 真实验收（2026-09-09）

所有验收使用临时 SQLite 库；未接触正式模型密钥，也未把测试数据写入正式资料目录。

| 球队 | 同步后比赛数 | 明确完场结果 | 自动取得首发 | 来源错误 |
|---|---:|---:|---:|---:|
| 阿森纳 | 17 | 8 场完场，其中 6 场有完整比分 | 1 场，22 人 | 0 |
| 利物浦 | 74 | 最近 30 场 | 1 场，22 人 | 0 |
| 巴萨 | 50 | 7 | 尚不可用 | 0 |
| 国米 | 72 | 最近 30 场 | 尚不可用 | 0 |

阿森纳同一临时库再次自动同步命中缓存，比赛更新数为 0。数字代表本次官网可读范围，不代表全历史或全部未来比赛的覆盖率。页面改版、官网异常和网络失败会保留已有数据并报告错误；缓存成功不构成永久的官方事实认证。

21 项 fixture 回归覆盖 UTC/夏令时一致性、主客比分、未知时间、取消/延期、队伍范围、官方比分冲突、同场首发、旧版本保护、重复同步、人工内容保留、部分失败、缓存、域名边界和未来 0–0 占位隔离。结合既有 research/backend 回归，本次 54 项通过。

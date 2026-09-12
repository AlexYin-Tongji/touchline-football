# 五大联赛目录与本地素材

本目录的 `catalog.json` 是于 **2026-09-12** 核验的 **2026/27 赛季**快照：英超 20 队、西甲 20 队、意甲 20 队、德甲 18 队、法甲 18 队，共 96 队。

球队的长期资料位于 `../clubs/catalog.json`。此处单独保存联赛、赛季和球队参赛关系；新增赛季时创建新的关系记录，不能根据球队名称或国家推断当前参赛资格，也不能删除已降级球队或用户自建球队。摩纳哥俱乐部的国家为摩纳哥，2026/27 参赛关系属于法甲。

## 核验依据

- [英超官方球队目录](https://www.premierleague.com/en/clubs)
- [西甲官方 2026/27 球队目录](https://www.laliga.com/en-US/laliga-easports/clubs)
- [意甲官方球队目录](https://www.legaseriea.it/team)与[2026/27 赛季说明](https://en.legaseriea.it/serie-a/news/looking-forward-to-the-2026-27-serie-a-fixture-list)
- [德甲官方 2026/27 球队目录](https://www.bundesliga.com/de/bundesliga/clubs)
- [法甲官方 2026/27 开季名单](https://ligue1.com/fr/articles/l1_article_5293-les-dates-de-reprise-des-clubs-de-l1-2627)

各联赛赛季记录保留 ESPN `teams?limit=100&season=2026` 及 `scoreboard?dates=20260912` 的具体地址。`teams` 返回的年份仅写作 2026；通过 `scoreboard` 的赛季名称和起止日期确认其含义是 **2026/27**，并与上述官网名单交叉核验。供应方的赛季起止日期是其数据分区范围，不是首轮或末轮的比赛日期。

`PL`、`PD`、`SA`、`BL1`、`FL1` 是本应用的稳定联赛 ID；`provider_code`、`provider_league_uid`、`provider_team_id` 用于 ESPN 数据对照。此快照不假设下一赛季仍由相同球队参赛。赛果、积分和赛程需要通过服务器单独同步。

## 图片与新闻来源

本地 5 张联赛标识和本轮新增的 66 张队徽来自 ESPN 自身的赛事/球队图片目录。每张图片记录来源、原始图片地址、核验日期、字节数和 SHA-256。原有 30 支球队的图片与兼容路径继续保留。

商标及图形权利归相关权利人；素材并未声明为开放授权，来源记录也不代表官方合作或认可。

新增 66 支球队均保留官网、ESPN 球队专页和有效的卫报球队专属 RSS。卫报地址对照其[球队目录](https://www.theguardian.com/football/teams)并检查 XML 标题及文章条目，避免误收录重定向到通用 Football 的订阅源。RSS 可用只表示可发现报道，不承诺每篇正文完整可读。

核验时 63 个新增球队官网可以打开，毕尔巴鄂竞技和博洛尼亚官网的自动访问返回 403，云达不莱梅官网返回 429；这些情况保存在各来源的 `access_review`，保留真实官网入口并由公开媒体提供补充。里昂官网返回需要浏览器执行脚本的页面，具体文章发现仍依赖运行时检查。所有新增的 66 个卫报 RSS 和 66 个 ESPN 球队页面核验时均可访问。

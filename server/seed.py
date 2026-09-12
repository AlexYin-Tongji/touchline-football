"""Clearly fictional, fixed-date examples. Real collections start empty."""


from .club_library import TEAMS, seed_clubs


def seed(store):
    seed_clubs(store)
    from .team_profiles import seed_team_profiles
    seed_team_profiles(store)
    if store.get_meta("demo_seed_v1"):
        return
    records = [
        ("arsenal", "training", "injury", "主力后腰恢复部分训练，出场仍待确认", "待进一步确认", True,
         "【虚构示例，非真实新闻】假设阿森纳的一名主力后腰恢复了部分合练。虚构训练记录只说明他参与了部分训练，没有宣布医学许可、比赛名单或是否首发。出场安排仍要等待后续信息。", False, "2026-09-08T09:00:00+00:00"),
        ("arsenal", "pressing", "team", "下一场，留意中场如何应对前场逼抢", "赛前资料", False,
         "【虚构示例，非真实新闻】演示资料设定：北港联近期两场练习赛倾向于在前场施压，但样本很少。阿森纳训练笔记中出现过由边路接应后场传球的练习。资料不能证明正式比赛一定采用这些安排。", False, "2026-09-08T12:00:00+00:00"),
        ("arsenal", "rotation", "team", "连续赛程前的轮换，目前没有确定名单", "尚待名单", False,
         "【虚构示例，非真实新闻】虚构赛程包含短时间内连续两场比赛。演示采访提到会评估球员恢复状态，没有给出具体轮换人数或首发名单。现阶段只能确认需要留意赛前名单，不能据此预测阵容。", False, "2026-09-09T01:00:00+00:00"),
        ("arsenal", "review", "match", "演示战报：对南岸竞技以 2–1 完赛", "赛后资料", False,
         "【虚构示例，非真实赛果】阿森纳在虚构比赛中以 2–1 战胜南岸竞技。唯一赛后记录提供了比分，没有射门分布、跑位、压迫统计或完整录像。因此只能确认这个示例设定的赛果，不能编造比赛过程或战术成因。", True, "2026-09-06T17:00:00+00:00"),
        ("barcelona", "youngsters", "team", "年轻中场进入合练名单，角色仍待观察", "训练消息", True,
         "【虚构示例，非真实新闻】巴塞罗那的虚构训练笔记记录一名年轻中场参加一线队合练。资料没有确认他进入正式比赛名单，也没有说明出场位置。值得跟进名单公布后的角色信息。", False, "2026-09-09T02:00:00+00:00"),
        ("barcelona", "preview", "team", "周末对海湾城，先看出球接应", "赛前资料", False,
         "【虚构示例，非真实新闻】海湾城是原型演示中的虚构对手。资料将本场观察方向设为后场出球接应，但目前没有完整对手数据。因此这个方向只作为示范，不能用于评价巴塞罗那的真实比赛。", False, "2026-09-08T11:00:00+00:00"),
    ]
    for team_id, key, kind, title, stage, watching, text, spoiler, at in records:
        ev_id = f"ev_demo_{team_id}_{key}"
        store.put("evidence", {"id": ev_id, "mode": "demo", "team_id": team_id, "title": "【虚构示例】" + title,
                  "text": text, "url": "", "source_name": "看台演示资料 · 非真实新闻", "published_at": at,
                  "fetched_at": at, "source_id": None, "origin": "demo", "spoiler": spoiler})
        store.put("events", {"id": f"event_demo_{team_id}_{key}", "mode": "demo", "team_id": team_id,
                  "title": title, "summary": text, "change": "虚构演示内容，用于体验跟队流程。", "kind": kind,
                  "stage": stage, "importance": "high" if watching else "normal", "updated_at": at,
                  "evidence_ids": [ev_id], "watching": watching, "read": key == "rotation", "spoiler": spoiler,
                  "timeline": [{"at": at, "text": "收录虚构示例资料", "evidence_id": ev_id}],
                  "uncertainties": ["所有人物情境、对手、日期和赛果均为演示设定，不是实时足球信息。"]})
    for item in [
        {"id": "match_demo_arsenal_next", "team_id": "arsenal", "opponent": "北港联（虚构）", "kickoff": "2026-09-12T16:30:00+00:00", "status": "scheduled", "score": None,
         "evidence_ids": ["ev_demo_arsenal_training", "ev_demo_arsenal_pressing", "ev_demo_arsenal_rotation"]},
        {"id": "match_demo_arsenal_previous", "team_id": "arsenal", "opponent": "南岸竞技（虚构）", "kickoff": "2026-09-06T15:00:00+00:00", "status": "finished", "score": "2–1",
         "evidence_ids": ["ev_demo_arsenal_review"]},
        {"id": "match_demo_barcelona_next", "team_id": "barcelona", "opponent": "海湾城（虚构）", "kickoff": "2026-09-13T19:00:00+00:00", "status": "scheduled", "score": None,
         "evidence_ids": ["ev_demo_barcelona_youngsters", "ev_demo_barcelona_preview"]},
    ]:
        store.put("matches", {"mode": "demo", "competition": "虚构演示赛事", "venue": "home", "note": "比赛对手、时间及赛果均为虚构示例。", **item})
    store.set_meta("demo_seed_v1", True)

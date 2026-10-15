"""Player-name readings shown beside the source spelling, never instead of it.

The squad feed gives an English spelling. A Chinese reader asks for the name
they know the player by, so the roster builds a second field and the interface
shows it next to the original.

Two rules hold here, and they are the reason this is a plain table rather than a
translation service:

* **A name is not translated, it is looked up.** Transliterating a personal name
  by rule produces readings no supporter uses — 萨卡 is not derivable from
  "Saka" and 贝林厄姆 is not derivable from "Bellingham". So an entry is either
  in the table or it is absent.
* **Absent means the source spelling stands alone.** The module never guesses,
  never falls back to a phonetic approximation, and never emits a partial match.
  A wrong Chinese name is worse than an English one, because the reader has no
  way to tell it is wrong.

The same name is written differently by different publishers — 姆巴佩/姆巴配,
哈兰德/哈兰 — so the table holds the readings used by mainstream Chinese
football coverage, and the original spelling is always shown beside it.
"""
from __future__ import annotations

import json
import unicodedata
import re
from datetime import datetime, timezone
from pathlib import Path

# Wikidata's ``zh`` label is not guaranteed to be simplified: an entity with no
# ``zh-hans`` label falls back to whatever the community wrote, which for several
# Bundesliga and Ligue 1 players is Traditional Chinese (米高·奧利斯, 孔拉德·萊梅爾).
# ``cjk_label`` alone cannot see the difference — it only checks that the label
# is Chinese at all — so the script was published as if it were simplified.
#
# The map is generated once from Unicode's Unihan database
# (``kSimplifiedVariant``) and shipped as data rather than fetched, so the
# runtime makes no extra request and the conversion is reproducible from the
# file. It deliberately keeps only single-target mappings and drops every
# character that is itself a simplification target, because those are dual-use:
# 著/裡/復 are correct in simplified prose and converting them would corrupt a
# name rather than normalise it.
#
# Converting is safe where the map is; where it is not, the label is kept as the
# source wrote it and the reader sees 繁体 rather than a reading nobody checked.
_TRADITIONAL_MAP_PATH = Path(__file__).resolve().parent / "wikidata_zh_hans.json"


def _load_traditional_map():
    try:
        payload = json.loads(_TRADITIONAL_MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pairs = payload.get("pairs") if isinstance(payload, dict) else None
    return {key: value for key, value in pairs.items() if isinstance(key, str) and isinstance(value, str)} \
        if isinstance(pairs, dict) else {}


_TRADITIONAL_TO_SIMPLIFIED = _load_traditional_map()


def to_simplified(value):
    """The simplified reading of a label, or ``""`` when it cannot be normalised.

    Only characters with an unambiguous one-to-one simplification are rewritten.
    A character the map does not carry is left exactly as it was, so a label that
    is already simplified passes through unchanged and one that mixes scripts
    keeps whatever the map cannot resolve — the caller decides whether that is
    publishable, by comparing the result with what the source published.
    """
    if not isinstance(value, str) or not value:
        return ""
    return "".join(_TRADITIONAL_TO_SIMPLIFIED.get(char, char) for char in value)


def is_traditional(value):
    """Whether the text contains a character this table knows only as Traditional."""
    return isinstance(value, str) and any(char in _TRADITIONAL_TO_SIMPLIFIED for char in value)



# Letters that survive NFKD as themselves, because they are distinct letters in
# their own alphabets rather than accented forms of an ASCII one. A Danish or
# Norwegian ø is not an "o plus something", so decomposition leaves it alone and
# "Ødegaard" would never meet the table's "odegaard". Folded explicitly.
_STANDALONE = str.maketrans({
    "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe",
    "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ð": "d", "Ð": "d",
    "þ": "th", "Þ": "th", "ı": "i", "ß": "ss", "ħ": "h", "ŋ": "n",
})


def normalize_name(value):
    """Fold a name to a comparison key: case, accents and punctuation removed.

    "Vinícius Júnior", "vinicius junior" and "VINÍCIUS  JÚNIOR" are one key.
    Hyphens and apostrophes are treated as spaces, so "Alexander-Arnold" and
    "Alexander Arnold" agree — rosters spell it both ways. Letters NFKD cannot
    decompose (ø, ł, đ, ß …) are folded by hand, or a Nordic or Polish name
    would have no key in common with its own ASCII spelling.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.translate(_STANDALONE)
    return " ".join("".join(char if char.isalnum() else " " for char in text.casefold()).split())


# Keyed by ``normalize_name``. Grouped by club only for reviewability; the
# lookup is flat, so a player who transfers keeps the reading.
NAME_READINGS = {
    # 皇家马德里
    "thibaut courtois": "库尔图瓦",
    "andriy lunin": "卢宁",
    "dani carvajal": "卡瓦哈尔",
    "antonio rudiger": "吕迪格",
    "eder militao": "米利唐",
    "david alaba": "阿拉巴",
    "ferland mendy": "门迪",
    "francesco acerbi": "阿切尔比",
    "fran garcia": "弗兰·加西亚",
    "raul asencio": "阿森西奥",
    "dean huijsen": "豪尔森",
    "denzel dumfries": "邓弗里斯",
    "trent alexander arnold": "阿诺德",
    "alvaro carreras": "卡雷拉斯",
    "marc cucurella": "库库雷利亚",
    "aurelien tchouameni": "琼阿梅尼",
    "eduardo camavinga": "卡马文加",
    "federico valverde": "巴尔韦德",
    "jude bellingham": "贝林厄姆",
    "arda guler": "居莱尔",
    "brahim diaz": "布拉希姆·迪亚斯",
    "bernardo silva": "贝尔纳多·席尔瓦",
    "ibrahima konate": "科纳特",
    "rodrygo": "罗德里戈",
    "vinicius junior": "维尼修斯",
    "kylian mbappe": "姆巴佩",
    "endrick": "恩德里克",
    "yan diomande": "迪奥曼德",
    # 阿森纳
    "david raya": "拉亚",
    "kepa arrizabalaga": "凯帕",
    "ben white": "本·怀特",
    "william saliba": "萨利巴",
    "gabriel magalhaes": "加布里埃尔",
    "jurrien timber": "廷伯",
    "riccardo calafiori": "卡拉菲奥里",
    "piero hincapie": "因卡皮耶",
    "myles lewis skelly": "刘易斯-斯凯利",
    "cristhian mosquera": "莫斯克拉",
    "ezri konsa": "孔萨",
    "martin odegaard": "厄德高",
    "declan rice": "赖斯",
    "martin zubimendi": "苏比门迪",
    "mikel merino": "梅里诺",
    "bruno guimaraes": "布鲁诺·吉马良斯",
    "kai havertz": "哈弗茨",
    "bukayo saka": "萨卡",
    "gabriel jesus": "热苏斯",
    "victor gyokeres": "约克雷斯",
    "viktor gyokeres": "约克雷斯",
    "noni madueke": "马杜埃凯",
    "eberechi eze": "埃兹",
    "christos tzolis": "佐利斯",
    "illan meslier": "梅斯利耶",
    # 巴塞罗那
    "joan garcia": "霍安·加西亚",
    "wojciech szczesny": "什琴斯尼",
    "pau cubarsi": "库巴西",
    "jules kounde": "孔德",
    "alejandro balde": "巴尔德",
    "eric garcia": "埃里克·加西亚",
    "andreas christensen": "克里斯滕森",
    "joao cancelo": "坎塞洛",
    "ronald araujo": "阿劳霍",
    "pedri": "佩德里",
    "gavi": "加维",
    "frenkie de jong": "德容",
    "dani olmo": "奥尔莫",
    "dominik livakovic": "利瓦科维奇",
    "fermin lopez": "费尔明·洛佩斯",
    "marc bernal": "贝尔纳尔",
    "raphinha": "拉菲尼亚",
    "lamine yamal": "亚马尔",
    "roony bardghji": "巴尔吉",
    "karim adeyemi": "阿德耶米",
    "anthony gordon": "戈登",
    "rodri": "罗德里",
    # 拜仁慕尼黑
    "manuel neuer": "诺伊尔",
    "sven ulreich": "乌尔赖希",
    "jonas urbig": "乌尔比希",
    "dayot upamecano": "于帕梅卡诺",
    "jonathan tah": "塔",
    "kim min jae": "金玟哉",
    "alphonso davies": "戴维斯",
    "josip stanisic": "斯坦尼希奇",
    "sacha boey": "博埃",
    "hiroki ito": "伊藤洋辉",
    "joshua kimmich": "基米希",
    "konrad laimer": "莱默尔",
    "aleksandar pavlovic": "帕夫洛维奇",
    "leon goretzka": "格雷茨卡",
    "jamal musiala": "穆西亚拉",
    "michael olise": "奥利塞",
    "serge gnabry": "格纳布里",
    "harry kane": "凯恩",
    "luis diaz": "路易斯·迪亚斯",
    "israel saibari": "赛巴里",
    "ismael saibari": "赛巴里",
    "tom bischof": "比朔夫",
    # 利物浦
    "alisson becker": "阿利松",
    "giorgi mamardashvili": "马马尔达什维利",
    "vitezslav jaros": "亚罗什",
    "virgil van dijk": "范戴克",
    "ibrahima konate": "科纳特",
    "joe gomez": "戈麦斯",
    "andrew robertson": "罗伯逊",
    "milos kerkez": "科尔克兹",
    "jeremie frimpong": "弗林蓬",
    "conor bradley": "布拉德利",
    "kostas tsimikas": "齐米卡斯",
    "giovanni leoni": "莱奥尼",
    "ryan gravenberch": "格拉芬贝赫",
    "alexis mac allister": "麦卡利斯特",
    "dominik szoboszlai": "绍博斯洛伊",
    "florian wirtz": "维尔茨",
    "freddie woodman": "伍德曼",
    "james mcconnell": "麦康奈尔",
    "wataru endo": "远藤航",
    "harvey davies": "戴维斯",
    "cody gakpo": "加克波",
    "mohamed salah": "萨拉赫",
    "alexander isak": "伊萨克",
    "hugo ekitike": "埃基蒂克",
    "federico chiesa": "基耶萨",
    "bradley barcola": "巴尔科拉",
    "trey nyoni": "尼奥尼",
    "jayden danns": "丹斯",
    "rio ngumoha": "恩古莫哈",
    # 切尔西
    "robert sanchez": "罗伯特·桑切斯",
    "gaga slonina": "斯洛尼纳",
    "mike penders": "彭德斯",
    "levi colwill": "科尔威尔",
    "wesley fofana": "福法纳",
    "tosin adarabioyo": "阿达拉比奥约",
    "malo gusto": "古斯托",
    "marc cucurella": "库库雷利亚",
    "reece james": "詹姆斯",
    "josh acheampong": "阿奇姆蓬",
    "jorrel hato": "哈托",
    "moises caicedo": "凯塞多",
    "romeo lavia": "拉维亚",
    "enzo fernandez": "恩佐·费尔南德斯",
    "cole palmer": "帕尔默",
    "pedro neto": "内托",
    "jamie gittens": "吉滕斯",
    "geovany quenda": "昆达",
    "joao pedro": "若昂·佩德罗",
    "marco palestra": "帕莱斯特拉",
    "estevao": "埃斯特旺",
    "emmanuel emegha": "埃梅加",
    "morgan rogers": "罗杰斯",
    "danny welbeck": "韦尔贝克",
    "jordan henderson": "亨德森",
    "maxence lacroix": "拉克鲁瓦",
    "valentin barco": "巴尔科",
    # 热刺
    "guglielmo vicario": "维卡里奥",
    "antonin kinsky": "金斯基",
    "martin dubravka": "杜布拉夫卡",
    "brandon austin": "奥斯汀",
    "cristian romero": "罗梅罗",
    "micky van de ven": "范德文",
    "jan paul van hecke": "范赫克",
    "destiny udogie": "乌多吉",
    "pedro porro": "波罗",
    "ben davies": "本·戴维斯",
    "archie gray": "阿奇·格雷",
    "kevin danso": "丹索",
    "rodrigo bentancur": "本坦库尔",
    "sandro tonali": "托纳利",
    "james maddison": "麦迪逊",
    "lucas bergvall": "贝里瓦尔",
    "conor gallagher": "加拉格尔",
    "matheus fernandes": "马特乌斯·费尔南德斯",
    "mohammed kudus": "库杜斯",
    "xavi simons": "哈维·西蒙斯",
    "dejan kulusevski": "库卢塞夫斯基",
    "omar marmoush": "马尔穆什",
    "mathys tel": "特尔",
    "savio": "萨维尼奥",
    "wilson odobert": "奥多贝尔",
    "richarlison": "理查利森",
    "dominic solanke": "索兰克",
    "mykhailo mudryk": "穆德里克",
    "marcos senesi": "塞内西",
    "andy robertson": "罗伯逊",
    # 阿斯顿维拉
    "emiliano martinez": "埃米利亚诺·马丁内斯",
    "marco bizot": "比佐",
    "ezri konsa": "孔萨",
    "tyrone mings": "明斯",
    "pau torres": "保罗·托雷斯",
    "matty cash": "卡什",
    "ian maatsen": "马岑",
    "lamare bogarde": "博加德",
    "victor lindelof": "林德洛夫",
    "matteo ruggeri": "鲁杰里",
    "boubacar kamara": "卡马拉",
    "amadou onana": "奥纳纳",
    "john mcginn": "麦金",
    "joao gomes": "若昂·戈麦斯",
    "ross barkley": "巴克利",
    "alejandro garnacho": "加纳乔",
    "emiliano buendia": "布恩迪亚",
    "nicolas jackson": "杰克逊",
    "tammy abraham": "亚伯拉罕",
    "aaron wan bissaka": "万比萨卡",
    "taylor harwood bellis": "哈伍德-贝利斯",
    "johan manzambi": "曼赞比",
    "zion suzuki": "铃木彩艳",
}


# These additions were checked against published Chinese spellings, rather
# than produced by a model or a transliteration rule. The evidence supports
# only the name reading: it never changes club membership or another fact.
_CITY_NAME_SOURCE = {
    "source": "AllFootball 中文报道及球员资料",
    "source_url": "https://www.allfootballapp.com/pt/articles/5274563",
    "checked_at": "2026-10-01",
}
VERIFIED_READINGS = {
    normalize_name(name): {"reading": reading, **_CITY_NAME_SOURCE}
    for name, reading in (
        ("Gianluigi Donnarumma", "多纳鲁马"),
        ("Marcus Bettinelli", "贝蒂内利"),
        ("Gerónimo Rulli", "鲁利"),
        ("Rúben Dias", "鲁本·迪亚斯"),
        ("Marc Guéhi", "格伊"),
        ("Rayan Aït-Nouri", "艾特-努里"),
        ("Vitor Reis", "维托尔·雷斯"),
        ("Josko Gvardiol", "格瓦迪奥尔"),
        ("Abdukodir Khusanov", "胡桑诺夫"),
        ("Rico Lewis", "里科·刘易斯"),
        ("Elliot Anderson", "埃利奥特·安德森"),
        ("Mateo Kovacic", "科瓦契奇"),
        ("Rayan Cherki", "谢尔基"),
        ("Matheus Nunes", "马特乌斯·努内斯"),
        ("Ayyoub Bouaddi", "布阿迪"),
        ("Phil Foden", "福登"),
        ("Iliman Ndiaye", "恩迪亚耶"),
        ("Erling Haaland", "哈兰德"),
        ("Antoine Semenyo", "塞门约"),
        ("Ryan McAidoo", "瑞安·麦卡杜"),
        ("Enzo Fernández", "恩佐·费尔南德斯"),
    )
}
VERIFIED_READINGS.update({
    "nico o reilly": {
        "reading": "奥赖利", "source": "AllFootball 中文球员资料",
        "source_url": "https://www.allfootballapp.com/zh-CN/player/50665740-nico-oreilly",
        "checked_at": "2026-10-01",
    },
    "jeremy doku": {
        "reading": "多库", "source": "球天下中文球员资料",
        "source_url": "https://www.qtx.com/dbsoccer/player_1965381.html", "checked_at": "2026-10-01",
    },
})
VERIFIED_READINGS.update({
    "mateus fernandes": {
        "reading": "马特乌斯·费尔南德斯", "source": "懂球帝球员资料",
        "source_url": "https://pc.dongqiudi.com/player/50785810", "checked_at": "2026-10-01",
    },
    "gerard martin": {
        "reading": "杰拉德·马丁", "source": "FC Barcelona 中文官网",
        "source_url": "https://www.fcbarcelona.cn/", "checked_at": "2026-10-01",
    },
})


def _add_verified(source, url, names):
    for name, reading in names:
        VERIFIED_READINGS[normalize_name(name)] = {
            "reading": reading, "source": source, "source_url": url, "checked_at": "2026-10-01",
        }


# The source table contains each complete English name alongside the Chinese
# spelling. Short display readings use the attested surname; no roster fact
# is taken from these name references.
_add_verified("维基百科中文球员目录", "https://zh.wikipedia.org/wiki/托特纳姆热刺足球俱乐部", (
    ("Antonín Kinsky", "金斯基"), ("Martin Dúbravka", "杜布拉夫卡"),
    ("Brandon Austin", "奥斯汀"), ("Andy Robertson", "罗伯逊"),
    ("Tosin Adarabioyo", "阿达拉比奥约"), ("Marcos Senesi", "塞内西"),
    ("Destiny Udogie", "乌多吉"), ("Pedro Porro", "波罗"), ("Ben Davies", "本·戴维斯"),
    ("Xavi Simons", "哈维·西蒙斯"), ("Conor Gallagher", "加拉格尔"),
    ("Archie Gray", "阿奇·格雷"), ("Sandro Tonali", "托纳利"),
    ("Mohammed Kudus", "库杜斯"), ("Dejan Kulusevski", "库卢塞夫斯基"),
    ("Mykhailo Mudryk", "穆德里克"), ("Rodrigo Bentancur", "本坦库尔"),
    ("Mathys Tel", "特尔"), ("Sávio", "萨维尼奥"),
    ("Dominic Solanke", "索兰克"), ("Omar Marmoush", "马尔穆什"),
))
_add_verified("球迷屋中文球员资料", "https://m.qiumiwu.com/player/lichalisen", (("Richarlison", "理查利森"),))
_add_verified("球迷屋中文球员资料", "https://m.qiumiwu.com/player/zhanmusimaidixun", (("James Maddison", "麦迪逊"),))
_add_verified("球迷屋中文球员资料", "https://m.qiumiwu.com/player/lukasibeiliwaer", (("Lucas Bergvall", "贝里瓦尔"),))
_add_verified("球迷屋中文球员资料", "https://m.qiumiwu.com/player/jianbaoluofanheike", (("Jan Paul van Hecke", "范黑克"),))
_add_verified("PlayersDB 球员资料", "https://playersdb.app/player?id=136184", (("Micky van de Ven", "范德文"),))
_add_verified("T足球中文球员资料", "https://tzuqiu.cc/players/161329/show.do", (("Wilson Odobert", "奥多贝尔"),))
_add_verified("维基百科中文球员目录", "https://zh.wikipedia.org/zh-cn/巴塞罗那足球俱乐部", (
    ("Joan García", "霍安·加西亚"), ("Wojciech Szczesny", "什琴斯尼"),
    ("Dominik Livakovic", "利瓦科维奇"), ("João Cancelo", "坎塞洛"),
    ("Alejandro Balde", "巴尔德"), ("Pau Cubarsí", "库巴西"),
    ("Andreas Christensen", "克里斯滕森"), ("Jules Koundé", "孔德"),
    ("Eric García", "埃里克·加西亚"), ("Gavi", "加维"), ("Fermín López", "费尔明·洛佩斯"),
    ("Pedri", "佩德里"), ("Rodri", "罗德里"), ("Dani Olmo", "奥尔莫"),
    ("Frenkie de Jong", "德容"), ("Marc Bernal", "贝尔纳尔"),
    ("Gabriel Jesus", "热苏斯"), ("Lamine Yamal", "亚马尔"), ("Raphinha", "拉菲尼亚"),
    ("Karim Adeyemi", "阿德耶米"), ("Anthony Gordon", "戈登"),
))
_add_verified("T足球中文球员资料", "https://tzuqiu.cc/players/133338/show.do", (("Roony Bardghji", "巴尔吉"),))
_add_verified("维基百科中文球员目录", "https://zh.wikipedia.org/zh-cn/切尔西足球俱乐部", (
    ("Emiliano Martínez", "埃米利亚诺·马丁内斯"), ("Mike Penders", "彭德斯"),
    ("Marco Palestra", "帕莱斯特拉"), ("Wesley Fofana", "福法纳"),
    ("Maxence Lacroix", "拉克鲁瓦"), ("Levi Colwill", "科尔威尔"), ("Jorrel Hato", "哈托"),
    ("Reece James", "詹姆斯"), ("Malo Gusto", "古斯托"), ("Valentín Barco", "巴尔科"),
    ("Cole Palmer", "帕尔默"), ("Jordan Henderson", "亨德森"), ("Morgan Rogers", "罗杰斯"),
    ("Geovany Quenda", "昆达"), ("Moisés Caicedo", "凯塞多"), ("Romeo Lavia", "拉维亚"),
    ("Pedro Neto", "内托"), ("João Pedro", "若昂·佩德罗"), ("Jamie Gittens", "吉滕斯"),
    ("Danny Welbeck", "维尔贝克"), ("Emmanuel Emegha", "埃梅加"), ("Estêvão", "埃斯特旺"),
    ("Josh Acheampong", "阿昌庞"),
))
_add_verified("雷速中文球员资料（姓名与出生日期核对）", "https://m.leisu.com/data/zuqiu/player-1154201",
              (("Gaga Slonina", "斯洛尼纳"),))
_add_verified("FC Barcelona 中文官方球员目录", "https://www.fcbarcelona.cn/players", (
    ("Joan García", "乔安·加西亚"), ("Wojciech Szczesny", "什琴斯尼"),
    ("João Cancelo", "坎塞洛"), ("Alejandro Balde", "巴尔德"), ("Pau Cubarsí", "库巴西"),
    ("Andreas Christensen", "克里斯滕森"), ("Jules Koundé", "孔德"), ("Eric García", "埃里克·加西亚"),
    ("Gavi", "加维"), ("Pedri", "佩德里"), ("Rodri", "罗德里"), ("Dani Olmo", "奥尔莫"),
    ("Frenkie de Jong", "德容"), ("Marc Bernal", "贝尔纳尔"), ("Lamine Yamal", "亚马尔"),
    ("Raphinha", "拉菲尼亚"), ("Karim Adeyemi", "阿德耶米"), ("Anthony Gordon", "戈登"),
    ("Roony Bardghji", "巴尔德吉"), ("Gerard Martín", "杰拉德·马丁"),
    ("Xavi Espart", "埃斯帕特"), ("Jesse Bisiwu", "比西乌"),
))
for name, reading, path in (
    ("Teddy Sharman-Lowe", "沙曼-洛维", "taidishamanluowei"),
    ("Pep Chavarría", "查瓦利亚", "peipuzhawaliya"),
    ("Aaron Anselmino", "安塞尔米诺", "yalunansaierminuo"),
    ("Olutayo Subuloye", "苏布洛耶", "aolutayuesubuluoye"),
    ("Mahdi Nicoll-Jazuli", "贾祖里", "mahedinikeerjiazuli"),
    ("Reggie Watson", "沃森", "leijiwosen"),
):
    _add_verified("球迷屋中文球员资料", "https://www.qiumiwu.com/player/" + path, ((name, reading),))
NAME_READINGS.update({key: value["reading"] for key, value in VERIFIED_READINGS.items()})


def reading_for(name):
    """The Chinese reading of a name, or "" when the table has none.

    Absence is a real answer here, not a failure: the caller shows the source
    spelling and nothing else.

    The reading and its evidence are separate questions, and this function
    answers only the first. A reading that was written down without a citable
    source is still the reading this table holds; withholding it would leave
    the reader with an English spelling and no Chinese name at all, which is
    worse than a Chinese name whose source is not linked. Whether evidence
    exists is decided by the caller through ``verified_evidence``.
    """
    return NAME_READINGS.get(normalize_name(name), "")


def verified_evidence(name):
    """The citation for a reading, or ``None`` when the table has none.

    Only the 106 entries checked against a publisher carry a source. The other
    129 are readings without a citation, and the interface must not claim one
    for them.
    """
    return VERIFIED_READINGS.get(normalize_name(name))


NAME_FIELDS = ("name_zh", "name_zh_source", "name_zh_source_url", "name_zh_checked_at", "name_zh_identity")
_SOURCE_UNSET = object()


def cjk_label(value):
    """A published Chinese label, never a description or an inferred reading."""
    return (isinstance(value, str) and 1 <= len(value) <= 80 and value == value.strip()
            and any("㐀" <= char <= "鿿" for char in value)
            and not any(ord(char) < 32 or char in "<>{}[]=/\\" for char in value))


# The readings a player's name may be taken from, and what a citation for each
# one looks like. A source is not a free-text field: the pair of (name, URL
# shape) is fixed here, so a stored proof can only cite one of these, and a new
# source is added by adding a row rather than by widening a check.
#
# Why two routes. Wikidata's ``zh`` label is what the portrait pass could read
# for free, and it is genuinely useful — but it mixes Mainland, Hong Kong and
# Taiwan conventions, because those are all community-written labels on the same
# entity. Measured on a real Bundesliga squad, it produced 马田·达尼亚 for
# Martin Terrier ("马田" is Cantonese for Martin) where the label a Mainland
# reader uses is 马丁·泰里耶. The Chinese Wikipedia article title follows the
# Mainland naming convention, so it is the better first choice — and the two
# disagree often enough that which one a reading came from has to travel with
# the reading.
#
# The Wikidata QID is required either way: it is the identity anchor the two
# routes share, and it is what the birth-date and club checks were performed
# against.
NAME_SOURCES = {
    "Wikidata 中文标签": {
        "url": lambda proof: "https://www.wikidata.org/wiki/" + str(proof.get("qid") or ""),
        "method": "wikidata_existing_identity_check",
    },
    # The citation here is the article the reading was read from, because that is
    # what a reader can open to check it. It is built from the *published* title,
    # so a stored proof can only cite the article whose title it recorded. The
    # QID still rides along in the proof as the identity anchor the birth-date
    # and club comparisons were made against.
    "维基百科中文条目": {
        "url": lambda proof: "https://zh.wikipedia.org/wiki/" + str(proof.get("zh_title") or ""),
        "method": "wikidata_existing_identity_check",
    },
}


def checked_wikidata_reading(candidate, row, *, team_id=None):
    if not isinstance(candidate, dict) or candidate.get("name_status", "verified") != "verified":
        return None
    proof = candidate.get("name_zh_identity")
    reading = candidate.get("name_zh")
    if not isinstance(proof, dict) or not cjk_label(reading):
        return None
    # A ``zh`` label may be Traditional. The row stores the simplified reading so
    # the interface never publishes 繁体 under a simplified heading; the proof
    # keeps what the source actually published, and the two are tied together by
    # re-running the same one-to-one conversion here. A label that the map cannot
    # fully normalise is refused rather than half-converted, and a label that was
    # already simplified passes through unchanged.
    source_label = proof.get("label")
    if to_simplified(source_label) != reading:
        return None
    qid = proof.get("qid")
    source = NAME_SOURCES.get(candidate.get("name_zh_source"))
    if (not re.fullmatch(r"Q[1-9][0-9]{0,11}", str(qid or ""))
            or source is None
            or candidate.get("name_zh_source_url") != source["url"](proof)
            or proof.get("method") != source["method"]
            or proof.get("identity_checked") is not True or proof.get("unique_article") is not True
            or proof.get("public") is not True or proof.get("language") not in {"zh", "zh-hans"}
            or proof.get("label") != source_label
            or normalize_name(proof.get("name")) != normalize_name(row.get("name"))
            or proof.get("team_id") != (team_id or row.get("team_id"))
            or proof.get("roster_birth_date", "") != (row.get("birth_date") or "")
            or str(proof.get("provider_id") or "") != str(row.get("provider_id") or "")
            or any(candidate.get(flag) is True for flag in ("private", "deleted", "restricted"))):
        return None
    try:
        checked = datetime.fromisoformat(str(candidate.get("name_zh_checked_at") or "").replace("Z", "+00:00"))
        if checked.tzinfo is None or not -300 <= (datetime.now(timezone.utc) - checked).total_seconds() < 30 * 86400:
            return None
    except (ValueError, TypeError):
        return None
    return {field: candidate[field] for field in NAME_FIELDS}


def with_reading(row, *, source_reading=_SOURCE_UNSET, team_id=None):
    """Add ``name_zh`` to a roster row without touching the source spelling.

    ``name`` stays exactly as the source wrote it — it is the identity used for
    photo matching and for quoting. Reading and citation are attached
    separately: every name the table holds gets its Chinese reading, and only
    the entries checked against a publisher also get the source fields the
    interface links from.

    An identity-checked Wikidata label is preferred over the table in one case
    only: when the table's reading is a *part* of it. The table is terse by
    design ("Ødegaard" → "厄德高"), while Wikidata carries the full published
    name ("马丁·厄德高"), and that longer form is both more complete and
    citable. The two cannot disagree about who the player is — one contains the
    other — so taking the longer one loses nothing. Where they do not contain
    one another the table wins, because a genuine disagreement about the name
    is not something a label should silently overrule.
    """
    reading = reading_for(row.get("name"))
    dynamic = checked_wikidata_reading(row if source_reading is _SOURCE_UNSET else source_reading, row,
                                       team_id=team_id)
    if reading and dynamic and reading in str(dynamic.get("name_zh") or ""):
        reading = ""
    elif reading:
        dynamic = None
    # Neither channel could produce a reading this time. That is *not* evidence
    # that the row has no reading: it is what a missing cache record looks like,
    # and the roster already carries values that were checked on an earlier pass.
    # Clearing here deleted six verified Wikidata names from the Getafe squad on
    # every rebuild — the row went English-only while the snapshot still held the
    # reading, and the next read could not put it back because its cache key was
    # gone too. Name lookups are additive, so a pass with nothing to add leaves
    # the row as it found it.
    if not reading and not dynamic:
        return row
    for field in NAME_FIELDS:
        row.pop(field, None)
    if reading:
        row["name_zh"] = reading
        evidence = verified_evidence(row.get("name"))
        if evidence:
            row.update(name_zh_source=evidence["source"], name_zh_source_url=evidence["source_url"],
                       name_zh_checked_at=evidence["checked_at"])
    elif dynamic:
        row.update(dynamic)
    return row

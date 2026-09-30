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

import unicodedata


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


def reading_for(name):
    """The Chinese reading of a name, or "" when the table has none.

    Absence is a real answer here, not a failure: the caller shows the source
    spelling and nothing else.
    """
    return NAME_READINGS.get(normalize_name(name), "")


def with_reading(row):
    """Add ``name_zh`` to a roster row without touching the source spelling.

    ``name`` stays exactly as the source wrote it — it is the identity used for
    photo matching and for quoting — and ``name_zh`` is present only when the
    table has a reading.
    """
    reading = reading_for(row.get("name"))
    if reading:
        row["name_zh"] = reading
    else:
        row.pop("name_zh", None)
    return row

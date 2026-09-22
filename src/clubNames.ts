import { findClub } from "./clubCatalog";

export const chineseText = (value = "", fallback = "球队消息") => {
  const cleaned = value
    .replace(/【[^】]*】/g, "")
    .replace(/\*\*/g, "")
    .trim();
  const chinese = (cleaned.match(/[\u3400-\u9fff]/g) || []).length;
  const latin = (cleaned.match(/[A-Za-z]/g) || []).length;
  return chinese > 0 &&
    latin <= Math.max(20, chinese) &&
    !/(?:[A-Za-z]+[\s,;:]+){5}[A-Za-z]+/.test(cleaned)
    ? cleaned
    : fallback;
};
const teamNames: Record<string, string> = {
  "al ahly": "开罗国民",
  alaves: "阿拉维斯",
  "athletic club": "毕尔巴鄂竞技",

  bayern: "拜仁慕尼黑",
  "birmingham city": "伯明翰",
  "borussia dortmund": "多特蒙德",
  "celta de vigo": "维戈塞尔塔",
  "como 1907": "科莫",
  "deportivo de la coruna": "拉科鲁尼亚",
  elche: "埃尔切",
  espanyol: "西班牙人",
  "basel": "巴塞尔",
  feyenoord: "费耶诺德",
  galatasaray: "加拉塔萨雷",
  getafe: "赫塔费",
  girona: "赫罗纳",
  levante: "莱万特",
  "lille osc": "里尔",
  malaga: "马拉加",
  osasuna: "奥萨苏纳",
  "paris saint germain": "巴黎圣日耳曼",
  "racing de santander": "桑坦德竞技",
  "rayo vallecano": "巴列卡诺",
  "real betis": "皇家贝蒂斯",
  "real sociedad": "皇家社会",
  sevilla: "塞维利亚",
  "sporting lisbon": "葡萄牙体育",
  "valencia cf": "瓦伦西亚",
  villarreal: "比利亚雷亚尔",
  arsenal: "阿森纳",
  liverpool: "利物浦",
  barcelona: "巴塞罗那",
  inter: "国际米兰",
  "inter milan": "国际米兰",
  chelsea: "切尔西",
  "manchester city": "曼城",
  "manchester united": "曼联",
  tottenham: "热刺",
  "tottenham hotspur": "热刺",
  "real madrid": "皇家马德里",
  "atletico madrid": "马德里竞技",
  everton: "埃弗顿",
  "aston villa": "阿斯顿维拉",
  "newcastle united": "纽卡斯尔联",
  "nottingham forest": "诺丁汉森林",
  fulham: "富勒姆",
  "crystal palace": "水晶宫",
  "west ham united": "西汉姆联",
  brighton: "布莱顿",
  "brighton and hove albion": "布莱顿",
  brentford: "布伦特福德",
  bournemouth: "伯恩茅斯",
  "leeds united": "利兹联",
  sunderland: "桑德兰",
  burnley: "伯恩利",
  juventus: "尤文图斯",
  milan: "AC米兰",
  "ac milan": "AC米兰",
  napoli: "那不勒斯",
  roma: "罗马",
  lazio: "拉齐奥",
  atalanta: "亚特兰大",
  "bayern munich": "拜仁慕尼黑",
  "paris saint-germain": "巴黎圣日耳曼",
};
export const clubName = (value: string) =>
  findClub(value)?.name ||
  teamNames[
    value
      .normalize("NFD")
      .replace(/\p{Diacritic}/gu, "")
      .toLowerCase()
      .replace(/\bfc\b/g, "")
      .replace(/&/g, "and")
      .trim()
  ] ||
  chineseText(value, value || "对手待定");

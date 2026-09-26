#!/usr/bin/env python3
"""每日英文：每天依星期抓一篇英文新聞，擷取摘要、挑出難字並查好字典，存成 JSON 給網頁用。
全程不呼叫任何 AI 模型，不消耗 token。只用 Python 標準庫，不需要 pip install。

用法：
  python build.py            產生今天（台灣時間）的內容；今天已經有了就略過
  python build.py --force    重新產生今天的內容
  python build.py --weekday 3 --force   用指定星期（0=一 … 6=日）的類別產生今天的內容（測試用）
"""
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DAYS_DIR = DATA / "days"
INDEX = DATA / "index.json"      # 所有日子的清單（網頁的存檔頁用）
SEEN = DATA / "seen.json"        # 出現過的字與文章網址（避免重複）
WORDLIST = BASE / "lexicon" / "en_50k.txt"   # 英文詞頻表（hermitdave/FrequencyWords，MIT）

TW = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DailyEnglish/1.0"}
ATOM = "{http://www.w3.org/2005/Atom}"

COMMON_RANK = 10000   # 詞頻表（字幕語料）排名在這之前的字視為常見字，不挑
COMMON_FREQ = 8       # Datamuse 書面語頻率（每百萬字）高於這個值也視為常見字
MAX_WORDS = 10        # 最多存幾個候選字；網頁會略過已在生字簿的字，最多顯示 7 個
REPEAT_DAYS = 60      # 幾天內出現過的字不再挑
MIN_FIRST_WORDS = 50  # 內文第一段少於這麼多字，就多接一段

GUARDIAN = "https://www.theguardian.com/{}/rss"
# 星期一到日：(代號, 中文類別, 來源名稱, RSS, 備援 RSS)
SCHEDULE = [
    ("world", "政治／國際", "The Guardian", GUARDIAN.format("world"), None),
    ("business", "商業／經濟", "The Guardian", GUARDIAN.format("uk/business"), None),
    ("tech", "科技", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", GUARDIAN.format("uk/technology")),
    ("science", "科學／健康", "The Conversation", "https://theconversation.com/global/articles.atom", GUARDIAN.format("science")),
    ("environment", "環境／氣候", "Yale E360", "https://e360.yale.edu/feed.xml", GUARDIAN.format("uk/environment")),
    ("culture", "文化／藝術", "The Guardian", GUARDIAN.format("uk/culture"), None),
    ("opinion", "評論", "The Guardian", GUARDIAN.format("uk/commentisfree"), None),
]
WEEKDAY_ZH = "一二三四五六日"

# 衛報的直播、影音、圖集不是一般文章
SKIP_URL = re.compile(r"/(live|video|audio|gallery|picture|ng-interactive|crosswords)/")


# ---------- 網路 ----------

def http_get(url, timeout=25, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def get_json(url):
    return json.loads(http_get(url))


def text_of(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment or ""))).strip()


# ---------- 抓文章 ----------

def feed_items(url):
    root = ET.fromstring(http_get(url).encode("utf-8"))
    out = []
    for it in root.iter("item"):
        out.append({"title": text_of(it.findtext("title")), "url": (it.findtext("link") or "").strip(),
                    "desc": text_of(it.findtext("description"))})
    for e in root.iter(ATOM + "entry"):
        link = next((l.get("href") for l in e.findall(ATOM + "link") if l.get("rel", "alternate") == "alternate"), "")
        out.append({"title": text_of(e.findtext(ATOM + "title")), "url": link,
                    "desc": text_of(e.findtext(ATOM + "summary"))})
    return out


def meta(page, prop):
    m = (re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % prop, page)
         or re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="%s"' % prop, page))
    return text_of(m.group(1)) if m else ""


def body_paragraphs(page):
    out = []
    for attrs, inner in re.findall(r"<p\b([^>]*)>(.*?)</p>", page, re.S):
        if re.search(r"caption|gray|font-sans|byline|newsletter", attrs):
            continue
        t = text_of(inner)
        if len(t) >= 80 and not t.startswith(("Read more", "Photograph:", "Sign up")):
            out.append(t)
    return out


def extract(item):
    """回傳 (導言, [內文第一段, 可能再一段])；抓不到像樣的內容就回傳 None。"""
    page = http_get(item["url"])
    paras = body_paragraphs(page)
    stand = meta(page, "og:description") or item["desc"]
    stand = re.sub(r"^\([^)]{1,40}\)\s*", "", stand)          # 衛報樂評開頭的「(唱片公司)」
    if not stand and paras:
        stand = paras.pop(0)
    body = [p for p in paras if stand[:60] not in p and p[:60] not in stand]
    # 內文第一段太短（評論常見一兩句的開場）就再接一段
    first = body[:1]
    if body and len(body[0].split()) < MIN_FIRST_WORDS:
        first = body[:2]
    if not stand or not first:
        return None
    return stand, first


def pick_article(sched, used_urls):
    key, cat, source, url, backup = sched
    for feed_url, src in ((url, source), (backup, "The Guardian")):
        if not feed_url:
            continue
        try:
            items = feed_items(feed_url)
        except Exception as e:
            print(f"  RSS 失敗 {feed_url}: {e}")
            continue
        for it in items[:8]:
            if not it["url"] or it["url"] in used_urls or SKIP_URL.search(it["url"]):
                continue
            try:
                got = extract(it)
            except Exception as e:
                print(f"  文章失敗 {it['url']}: {e}")
                continue
            if got and len(" ".join([got[0], *got[1]]).split()) >= 45:
                return dict(it, source=src, paras=[got[0], *got[1]])
    return None


# ---------- 挑字 ----------

def load_ranks():
    words = WORDLIST.read_text(encoding="utf-8").split()
    return {w: i + 1 for i, w in enumerate(words)}


def forms(w):
    """字的可能原形（簡易還原）。"""
    out = {w}
    def add(*xs):
        out.update(x for x in xs if len(x) >= 3)
    if w.endswith("ies"): add(w[:-3] + "y")
    if w.endswith("es"): add(w[:-2])
    if w.endswith("s") and not w.endswith("ss"): add(w[:-1])
    if w.endswith("ied"): add(w[:-3] + "y")
    if w.endswith("ed"): add(w[:-2], w[:-1], w[:-3] if w[-3:-2] == w[-4:-3] else "")
    if w.endswith("ing"): add(w[:-3], w[:-3] + "e", w[:-4] if w[-4:-3] == w[-5:-4] else "")
    if w.endswith("ily"): add(w[:-3] + "y")
    if w.endswith("ly"): add(w[:-2])
    if w.endswith("er"): add(w[:-2], w[:-1])
    if w.endswith("est"): add(w[:-3], w[:-2])
    return out


def simple_stems(w):
    """規則變化的原形（不處理重複子音，避免 galling → gal 這種誤判）。"""
    out = set()
    if w.endswith("ies"): out.add(w[:-3] + "y")
    elif w.endswith("s") and not w.endswith("ss"): out.update({w[:-1], w[:-2]})
    if w.endswith("ied"): out.add(w[:-3] + "y")
    elif w.endswith("ed"): out.update({w[:-2], w[:-1]})
    if w.endswith("ing"): out.update({w[:-3], w[:-3] + "e"})
    if w.endswith("ily"): out.add(w[:-3] + "y")
    elif w.endswith("ly"): out.update({w[:-2], w[:-2] + "e"})
    return {x for x in out if len(x) >= 3}


PARA_BREAK = "\n\n"


def sentences(text):
    """切句；段落（空行分隔）一定是句子的邊界，導言結尾常沒有句號。"""
    return [s.strip() for para in text.split(PARA_BREAK)
            for s in re.split(r"(?<=[.!?])\s+(?=[A-Z“\"‘'])", para) if s.strip()]


def candidates(summary, ranks, skip):
    """從摘要挑出不常見的字，依罕見程度排序。"""
    found = {}
    for sent in sentences(summary):
        for i, m in enumerate(re.finditer(r"[A-Za-z][A-Za-z’'-]*[A-Za-z]|[A-Za-z]", sent)):
            tok = re.sub(r"[’']s?$", "", m.group(0))
            if len(tok) < 4 or "-" in tok or "’" in tok or "'" in tok:
                continue
            if tok[0].isupper() and (i > 0 or tok.lower() not in ranks):   # 專有名詞
                continue
            if tok.isupper():
                continue
            w = tok.lower()
            if w in found or w in skip:
                continue
            rank = ranks.get(w)
            if rank is not None and rank <= COMMON_RANK:
                continue
            if any(ranks.get(x, 99999) <= COMMON_RANK for x in simple_stems(w)):   # 常見字的變化形
                continue
            found[w] = {"form": tok, "rank": rank or 99999, "sentence": sent, "pos_in_text": len(found)}
    return sorted(found.items(), key=lambda kv: -kv[1]["rank"])


def datamuse(word):
    q = urllib.parse.quote(word)
    res = get_json(f"https://api.datamuse.com/words?sp={q}&md=dpfr&ipa=1&max=1")
    if not res or res[0].get("word", "").lower() != word or not res[0].get("defs"):
        return None
    return res[0]


POS_ZH = {"n": "名詞", "v": "動詞", "adj": "形容詞", "adv": "副詞", "u": "其他"}
GOOGLE_POS = {"noun": "n", "verb": "v", "adjective": "adj", "adverb": "adv"}


def google_zh(word):
    """Google 翻譯的字典模式：回傳 (整體翻譯, {詞性: [中文]})。"""
    q = urllib.parse.quote(word)
    data = get_json("https://clients5.google.com/translate_a/single?client=dict-chrome-ex"
                    f"&sl=en&tl=zh-TW&hl=en&dt=t&dt=bd&q={q}")
    main = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0]).strip()
    by_pos = {}
    for entry in data[1] or []:
        by_pos.setdefault(GOOGLE_POS.get(entry[0], "u"), []).extend(entry[1])
    return main, {p: zh_terms(ts, main) for p, ts in by_pos.items()}


def zh_terms(terms, main):
    """Google 字典模式的中文詞沒有排序依據，常把罕用單字（灝、泂）排前面：主翻譯優先，其次多字詞。"""
    multi = [t for t in terms if len(t) >= 2]
    first = [t for t in terms if t in (main, main.removesuffix("的"))]
    return list(dict.fromkeys(first + (multi or terms)))[:5]


ZERO_WIDTH = re.compile("[​-‍﻿]")


def translate(text):
    q = urllib.parse.quote(text)
    data = get_json(f"https://clients5.google.com/translate_a/single?client=dict-chrome-ex&sl=en&tl=zh-TW&dt=t&q={q}")
    return ZERO_WIDTH.sub("", "".join(seg[0] for seg in data[0] if seg and seg[0])).strip()


def pos_order(form):
    f = form.lower()
    if f.endswith("ly"): return ["adv", "adj", "v", "n"]
    if f.endswith(("ed", "ing")): return ["v", "adj", "n", "adv"]
    return ["adj", "n", "v", "adv"]


def lookup(w, info, ranks, skip):
    """查字典，組成單字卡資料；查不到定義、其實是常見字或近期出現過就回傳 None。"""
    dm = datamuse(w)
    if not dm:
        # 試原形（例如 leached → leach）
        for f in sorted(forms(w) - {w}, key=lambda f: ranks.get(f, 99999)):
            dm = datamuse(f)
            if dm:
                break
    if not dm:
        return None
    head = (dm.get("defHeadword") or dm["word"]).lower()
    if head != dm["word"]:
        dm = datamuse(head) or dm        # 變化形（escalates）改查原形（escalate）
    if head in skip or ranks.get(head, 99999) <= COMMON_RANK:
        return None
    tags = dm.get("tags", [])
    freq = next((float(t[2:]) for t in tags if t.startswith("f:")), 0.0)
    if freq > COMMON_FREQ:
        return None
    defs = [d.split("	", 1) for d in dm["defs"] if "	" in d]
    # 形容詞化的 -ing/-ed 字，Datamuse 有時只給名詞或形容詞；補上原形的動詞義
    if info["form"].lower().endswith(("ing", "ed")) and not any(p == "v" for p, _ in defs):
        for f in sorted(forms(head) - {head}, key=lambda f: ranks.get(f, 99999)):
            extra = datamuse(f) if f in ranks else None
            if extra:
                defs += [d.split("	", 1) for d in extra["defs"] if d.startswith("v	")]
                break
    order = pos_order(info["form"])
    if head == w and any(p == "adj" for p, _ in defs):     # 本身就是形容詞（galling、maligned）
        order = ["adj"] + [p for p in order if p != "adj"]
    by_pos = {}
    for pos, d in defs:
        by_pos.setdefault(pos, [])
        if len(by_pos[pos]) < 3:
            by_pos[pos].append(d.strip())
    meanings = [{"pos": p, "pos_zh": POS_ZH.get(p, p), "defs": by_pos[p]}
                for p in sorted(by_pos, key=lambda p: order.index(p) if p in order else 9)][:3]
    ipa = next((t[9:] for t in tags if t.startswith("ipa_pron:")), "")
    try:
        syn = [s["word"] for s in get_json(
            "https://api.datamuse.com/words?rel_syn=%s&max=8" % urllib.parse.quote(head)) if " " not in s["word"]][:5]
    except Exception:
        syn = []
    try:
        zh_main, zh_pos = google_zh(head)
    except Exception as e:
        print(f"  中文翻譯失敗 {head}: {e}")
        zh_main, zh_pos = "", {}
    zh = [{"pos": p, "pos_zh": POS_ZH.get(p, p), "terms": zh_pos[p]}
          for p in sorted(zh_pos, key=lambda p: order.index(p) if p in order else 9)][:3]
    return {"word": head, "form": info["form"], "ipa": ipa, "freq": round(freq, 3),
            "meanings": meanings, "zh_main": zh_main, "zh": zh, "synonyms": syn,
            "sentence": info["sentence"]}


# ---------- 主流程 ----------

def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def build(force=False, weekday=None):
    now = datetime.now(TW)
    date = now.strftime("%Y-%m-%d")
    wd = now.weekday() if weekday is None else weekday
    out = DAYS_DIR / f"{date}.json"
    if out.exists() and not force:
        print(f"{date} 已經有內容，略過（要重做請加 --force）")
        return

    seen = load(SEEN, {"words": {}, "urls": []})
    cutoff = (now - timedelta(days=REPEAT_DAYS)).strftime("%Y-%m-%d")
    old = load(out, None)
    if old:    # 重做同一天：先把當天用過的字和網址放回去
        seen["urls"] = [u for u in seen["urls"] if u != old["url"]]
        seen["words"] = {w: d for w, d in seen["words"].items() if d != date}
    recent = {w for w, d in seen["words"].items() if d >= cutoff}

    sched = SCHEDULE[wd]
    print(f"{date}（星期{WEEKDAY_ZH[wd]}）{sched[1]}")
    art = pick_article(sched, set(seen["urls"]))
    if not art:
        sys.exit("找不到可用的文章")
    print(f"  文章：{art['title']}\n  {art['url']}")

    summary = PARA_BREAK.join(art["paras"])
    ranks = load_ranks()
    words = []
    for w, info in candidates(summary, ranks, recent)[:30]:
        if len(words) >= MAX_WORDS:
            break
        try:
            card = lookup(w, info, ranks, recent)
        except Exception as e:
            print(f"  查字失敗 {w}: {e}")
            continue
        if card and all(c["word"] != card["word"] for c in words):
            words.append(card)
            print(f"  字：{card['form']} → {card['word']}  {card['zh_main']}")
    # 依在文中出現的順序排列
    words.sort(key=lambda c: summary.lower().find(c["form"].lower()))

    try:
        summary_zh = [translate(p) for p in art["paras"]]
    except Exception as e:
        print(f"  摘要翻譯失敗：{e}")
        summary_zh = []

    day = {"date": date, "weekday": wd, "weekday_zh": WEEKDAY_ZH[wd], "cat": sched[0], "cat_zh": sched[1],
           "source": art["source"], "title": art["title"], "url": art["url"],
           "summary": art["paras"], "summary_zh": summary_zh, "words": words,
           "built": now.isoformat(timespec="minutes")}
    save(out, day)

    for c in words:
        seen["words"][c["word"]] = date
    seen["words"] = {w: d for w, d in seen["words"].items() if d >= cutoff}
    seen["urls"] = (seen["urls"] + [art["url"]])[-400:]
    save(SEEN, seen)

    index = [d for d in load(INDEX, []) if d["date"] != date]
    index.append({"date": date, "weekday_zh": WEEKDAY_ZH[wd], "cat": sched[0], "cat_zh": sched[1],
                  "source": art["source"], "title": art["title"], "n_words": len(words)})
    index.sort(key=lambda d: d["date"], reverse=True)
    save(INDEX, index)
    print(f"完成：{len(words)} 個字")


if __name__ == "__main__":
    args = sys.argv[1:]
    wd = int(args[args.index("--weekday") + 1]) if "--weekday" in args else None
    build(force="--force" in args, weekday=wd)

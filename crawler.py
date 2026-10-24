#!/usr/bin/env python3
"""ludepress.com 新闻爬虫。

通过 WordPress REST API 抓取上次运行以来的新文章（API 不可用时退回 RSS），
存档到 data/articles/YYYY-MM-DD.jsonl，并把本次新增文章写到 out/new_articles.md，
供后续生成新闻摘要和投资建议。只用 Python 标准库，无需安装依赖。

用法:
    python3 crawler.py               # 增量抓取
    python3 crawler.py --init 20     # 首次运行：只取最新 20 篇作为起点
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

SITE = "https://ludepress.com"
API = SITE + "/wp-json/wp/v2"
USER_AGENT = "Mozilla/5.0 (compatible; news-crawler/1.0; personal use)"
REQUEST_DELAY = 1.0  # 每次请求间隔秒数，避免给对方服务器造成压力
MAX_TEXT_CHARS = 4000  # 写入摘要输入文件时，每篇正文最多保留的字数
SEEN_LIMIT = 2000

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
ARTICLES_DIR = os.path.join(DATA_DIR, "articles")
OUT_DIR = os.path.join(ROOT, "out")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
CATEGORIES_FILE = os.path.join(DATA_DIR, "categories.json")
NEW_ARTICLES_MD = os.path.join(OUT_DIR, "new_articles.md")
NEW_ARTICLES_JSON = os.path.join(OUT_DIR, "new_articles.json")


def fetch(url, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
            time.sleep(REQUEST_DELAY)
            return body
        except Exception as e:  # 网络抖动时重试
            last_err = e
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"请求失败 {url}: {last_err}")


def fetch_json(path, params):
    return json.loads(fetch(f"{API}/{path}?{urllib.parse.urlencode(params)}"))


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr"}
    SKIP_TAGS = {"script", "style", "figure", "figcaption", "iframe", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self.skip += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(raw):
    parser = _TextExtractor()
    parser.feed(raw or "")
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def category_names(ids, cache):
    missing = [i for i in ids if str(i) not in cache]
    if missing:
        try:
            for c in fetch_json("categories", {"include": ",".join(map(str, missing)),
                                               "per_page": 100, "_fields": "id,name"}):
                cache[str(c["id"])] = html.unescape(c["name"])
        except Exception as e:
            print(f"警告：获取分类名失败：{e}", file=sys.stderr)
    return [cache.get(str(i), str(i)) for i in ids]


def fetch_via_api(after_gmt, limit=None):
    """按发布时间升序抓取 after_gmt 之后的文章。"""
    posts, page = [], 1
    fields = "id,date_gmt,modified_gmt,link,title,content,excerpt,categories"
    while True:
        params = {"per_page": 100, "page": page, "orderby": "date", "order": "asc", "_fields": fields}
        if limit:  # 初始化：取最新 N 篇
            params.update({"per_page": limit, "order": "desc"})
        else:
            params["after"] = after_gmt
        batch = fetch_json("posts", params)
        posts.extend(batch)
        if limit or len(batch) < 100:
            break
        page += 1
    if limit:
        posts.reverse()
    cache = load_json(CATEGORIES_FILE, {})
    result = []
    for p in posts:
        result.append({
            "id": p["id"],
            "published_gmt": p["date_gmt"],
            "title": html.unescape(html_to_text(p["title"]["rendered"])),
            "link": p["link"],
            "categories": category_names(p.get("categories", []), cache),
            "excerpt": html_to_text(p.get("excerpt", {}).get("rendered", "")),
            "text": html_to_text(p["content"]["rendered"]),
            "source": "api",
        })
    save_json(CATEGORIES_FILE, cache)
    return result


def fetch_via_rss():
    """API 不可用时的备用方案：RSS 只含最近约 10 篇。"""
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    channel = ET.fromstring(fetch(SITE + "/feed")).find("channel")
    result = []
    for item in channel.findall("item"):
        guid = item.findtext("guid") or ""
        m = re.search(r"[?&]p=(\d+)", guid) or re.search(r"/(\d+)/?$", item.findtext("link") or "")
        published = datetime.strptime(item.findtext("pubDate"), "%a, %d %b %Y %H:%M:%S %z")
        result.append({
            "id": int(m.group(1)) if m else guid,
            "published_gmt": published.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "title": item.findtext("title"),
            "link": item.findtext("link"),
            "categories": [c.text for c in item.findall("category")],
            "excerpt": html_to_text(item.findtext("description")),
            "text": html_to_text(item.findtext("content:encoded", namespaces=ns) or ""),
            "source": "rss",
        })
    result.sort(key=lambda a: a["published_gmt"])
    return result


def archive(articles):
    for a in articles:
        day = a["published_gmt"][:10]
        with open(os.path.join(ARTICLES_DIR, f"{day}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")


def write_outputs(articles, run_at):
    os.makedirs(OUT_DIR, exist_ok=True)
    save_json(NEW_ARTICLES_JSON, articles)
    bj = timezone(timedelta(hours=8))
    lines = [f"# 路德社新增文章（{len(articles)} 篇）",
             f"抓取时间：{run_at.astimezone(bj):%Y-%m-%d %H:%M} 北京时间", ""]
    for i, a in enumerate(articles, 1):
        pub = datetime.fromisoformat(a["published_gmt"]).replace(tzinfo=timezone.utc).astimezone(bj)
        text = a["text"] or a["excerpt"]
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "……（正文已截断）"
        lines += [f"## {i}. {a['title']}",
                  f"- 发布：{pub:%Y-%m-%d %H:%M} 北京时间",
                  f"- 分类：{'、'.join(a['categories'])}",
                  f"- 链接：{a['link']}", "", text, ""]
    with open(NEW_ARTICLES_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", type=int, metavar="N", help="首次运行，只取最新 N 篇作为起点")
    args = ap.parse_args()

    os.makedirs(ARTICLES_DIR, exist_ok=True)
    run_at = datetime.now(timezone.utc)
    state = load_json(STATE_FILE, {})
    seen = set(state.get("seen_ids", []))

    if not state and not args.init:
        sys.exit("还没有运行记录，请先执行：python3 crawler.py --init 20")

    # 往回多看 6 小时，防止发布时间早于上次抓取、但上次还没出现的文章被漏掉；靠 id 去重
    last = state.get("last_published_gmt")
    after = (datetime.fromisoformat(last) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S") if last else None

    try:
        fetched = fetch_via_api(after, limit=args.init)
    except Exception as e:
        print(f"REST API 失败，改用 RSS：{e}", file=sys.stderr)
        fetched = fetch_via_rss()

    new = [a for a in fetched if a["id"] not in seen]
    archive(new)
    write_outputs(new, run_at)

    all_ids = state.get("seen_ids", []) + [a["id"] for a in new]
    state = {
        "last_run_utc": run_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_published_gmt": max([a["published_gmt"] for a in fetched] + ([last] if last else [])),
        "seen_ids": all_ids[-SEEN_LIMIT:],
        "last_new_count": len(new),
    }
    save_json(STATE_FILE, state)
    print(f"NEW_ARTICLES={len(new)}")


if __name__ == "__main__":
    main()

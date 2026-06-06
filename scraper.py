#!/usr/bin/env python3
"""Fetch CUPl Graduate School notices and keep a daily history."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import http.cookiejar
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path


BASE_URL = "https://yjsy.cupl.edu.cn"
START_PATH = "tzgg.htm"
USER_AGENT = "Mozilla/5.0 (compatible; cupl-yjsy-notice-watch/1.0; +https://github.com/houdemingfagewuzhigong)"
DATA_DIR = Path("data")


@dataclass
class Notice:
    id: str
    title: str
    date: str
    url: str
    summary: str
    section: str
    source_url: str
    first_seen_at: str
    last_seen_at: str


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def request(url: str, data: bytes | None = None, referer: str | None = None):
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if referer:
        headers["Referer"] = referer
    return urllib.request.Request(url, data=data, headers=headers)


def new_opener():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    first = opener.open(request(BASE_URL + "/"), timeout=25).read().decode("utf-8", "ignore")
    if "dynamic_challenge" in first:
        challenge = re.search(r'challengeId\s*=\s*"([^"]+)"', first)
        answer = re.search(r"answer\s*=\s*(\d+)", first)
        if not challenge or not answer:
            raise RuntimeError("dynamic challenge found but challenge_id/answer missing")
        payload = json.dumps(
            {
                "challenge_id": challenge.group(1),
                "answer": int(answer.group(1)),
                "browser_info": {
                    "userAgent": USER_AGENT,
                    "language": "zh-CN",
                    "platform": "MacIntel",
                    "cookieEnabled": True,
                    "hardwareConcurrency": 8,
                    "deviceMemory": 8,
                    "timezone": "Asia/Shanghai",
                },
            }
        ).encode()
        opener.open(request(BASE_URL + "/dynamic_challenge", payload, BASE_URL + "/"), timeout=25).read()
    return opener


def clean(text: str) -> str:
    text = re.sub(r"<script.*?</script>", "", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<.*?>", "", text, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def notice_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def parse_list(html_text: str, source_url: str) -> list[Notice]:
    seen_at = now_iso()
    notices: list[Notice] = []
    pattern = re.compile(
        r'<a\s+href="([^"]*info/1119/\d+\.htm)"[^>]*?(?:title="([^"]+)")?[^>]*>\s*'
        r'(?:<span[^>]*class="fr"[^>]*>(\d{4}-\d{2}-\d{2})</span>)?\s*(.*?)</a>',
        re.S,
    )
    for href, title_attr, date_attr, text_html in pattern.findall(html_text):
        text = clean(text_html)
        title = clean(title_attr) or text
        date = date_attr
        if not date:
            match = re.match(r"(\d{4}-\d{2}-\d{2})(.+)", text)
            if not match:
                continue
            date, title = match.group(1), match.group(2).strip()
        url = urllib.parse.urljoin(source_url, href)
        notices.append(
            Notice(
                id=notice_id(url),
                title=title,
                date=date,
                url=url,
                summary="",
                section="通知公告",
                source_url=source_url,
                first_seen_at=seen_at,
                last_seen_at=seen_at,
            )
        )
    return notices


def page_paths(first_html: str, max_pages: int) -> list[str]:
    paths = [START_PATH]
    for link in re.findall(r'href="(tzgg/\d+\.htm)"', first_html):
        if link not in paths:
            paths.append(link)
        if len(paths) >= max_pages:
            break
    return paths


def fetch(max_pages: int = 3) -> list[Notice]:
    opener = new_opener()
    first_url = urllib.parse.urljoin(BASE_URL + "/", START_PATH)
    first_html = opener.open(request(first_url), timeout=25).read().decode("utf-8", "ignore")
    notices: list[Notice] = []
    for path in page_paths(first_html, max_pages):
        url = urllib.parse.urljoin(BASE_URL + "/", path)
        page = first_html if path == START_PATH else opener.open(request(url), timeout=25).read().decode("utf-8", "ignore")
        notices.extend(parse_list(page, url))
    unique = {notice.id: notice for notice in notices}
    return sorted(unique.values(), key=lambda item: (item.date, item.title), reverse=True)


def load_existing() -> dict[str, Notice]:
    path = DATA_DIR / "notices.json"
    if not path.exists():
        return {}
    return {item["id"]: Notice(**item) for item in json.loads(path.read_text(encoding="utf-8"))}


def save(notices: list[Notice]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "history").mkdir(exist_ok=True)
    existing = load_existing()
    merged = existing.copy()
    seen_at = now_iso()
    for notice in notices:
        if notice.id in merged:
            notice.first_seen_at = merged[notice.id].first_seen_at
        notice.last_seen_at = seen_at
        merged[notice.id] = notice
    rows = sorted(merged.values(), key=lambda item: (item.date, item.title), reverse=True)
    payload = [asdict(item) for item in rows]
    (DATA_DIR / "notices.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (DATA_DIR / "history" / f"{dt.date.today().isoformat()}.json").write_text(json.dumps([asdict(item) for item in notices], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (DATA_DIR / "notices.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        fields = list(payload[0].keys()) if payload else list(Notice.__dataclass_fields__.keys())
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(payload)
    meta = {
        "site": "中国政法大学研究生院",
        "notice_url": BASE_URL + "/" + START_PATH,
        "updated_at": seen_at,
        "total_notices": len(rows),
        "sections": ["通知公告"],
        "latest_date": rows[0].date if rows else None,
    }
    (DATA_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    max_pages = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    notices = fetch(max_pages)
    save(notices)
    print(f"fetched {len(notices)} notices from Graduate School notice pages")
    if notices:
        print(f"latest: {notices[0].date} {notices[0].title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

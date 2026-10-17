"""Local news collector. No login, browser impersonation, or access-control bypass."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from dateutil.parser import parse as parse_date
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from taiwan_filter import export_filtered, write_json

ROOT = Path(__file__).resolve().parent
TAIPEI = timezone(timedelta(hours=8))
AGENT = "FinanceNewsResearchCollector/1.0"


def now():
    return datetime.now(TAIPEI).isoformat(timespec="seconds")


def canonical(url):
    p = urlsplit(url)
    # MoneyDJ's a= is the article identity; other query parameters are tracking.
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if k.lower() == "a"])
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, query, ""))


def publication(raw, language):
    if not raw:
        return None
    try:
        value = parse_date(str(raw))
        if value.tzinfo is None:
            # Do not guess New York's daylight-saving offset for English articles.
            if language == "en":
                return None
            value = value.replace(tzinfo=TAIPEI)
        return value.astimezone(TAIPEI)
    except (ValueError, TypeError, OverflowError):
        return None


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


class FetchError(Exception):
    pass


class Client:
    def __init__(self, delay=1.5, timeout=20):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": AGENT, "Accept-Language": "zh-TW,en;q=0.8"})
        retry = Retry(total=2, backoff_factor=1, status_forcelist=[500, 502, 503, 504],
                      allowed_methods=["GET"], respect_retry_after_header=False)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.delay, self.timeout = delay, timeout
        self.last = {}
        self.robots = {}

    def raw(self, url):
        host = urlsplit(url).netloc
        time.sleep(max(0, self.delay - (time.monotonic() - self.last.get(host, 0))))
        self.last[host] = time.monotonic()
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            response.encoding = response.apparent_encoding if not response.encoding or response.encoding == "ISO-8859-1" else response.encoding
            return response
        except requests.RequestException as exc:
            raise FetchError(str(exc)) from exc

    def get(self, url):
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            try:
                r = self.raw(origin + "/robots.txt")
                if "<html" in r.text[:200].lower():
                    raise FetchError("robots.txt returned an HTML challenge")
                rp = RobotFileParser()
                rp.parse(r.text.splitlines())
                self.robots[origin] = rp
            except FetchError as exc:
                # Missing robots is allowed; denied or unavailable robots is recorded.
                if "404 Client Error" in str(exc):
                    rp = RobotFileParser()
                    rp.parse([])
                    self.robots[origin] = rp
                else:
                    self.robots[origin] = str(exc)
        rp = self.robots[origin]
        if isinstance(rp, str):
            raise FetchError("robots_unavailable: " + rp)
        if not rp.can_fetch(AGENT, url):
            raise FetchError("robots_disallowed: " + url)
        delay = rp.crawl_delay(AGENT)
        if delay:
            self.delay = max(self.delay, delay)
        r = self.raw(url)
        if any(marker in r.text[:6000].lower() for marker in ["<title>just a moment", "<title>access denied", "cf-chl-"]):
            raise FetchError("access_challenge")
        return r.text


def matches(url, config):
    return (urlsplit(url).hostname == config["host"] and
            re.search(config["article_pattern"], url, re.I) is not None)


def discover(text, base, config):
    """Return candidates with optional feed dates, and explicit same-site next links."""
    found, next_pages = {}, []
    try:
        root = ET.fromstring(text)
        for item in root.findall(".//item"):
            link = canonical((item.findtext("link") or "").strip())
            if matches(link, config):
                found[link] = item.findtext("pubDate", "")
        if root.tag.endswith("rss"):
            return found, next_pages
    except ET.ParseError:
        pass
    soup = BeautifulSoup(text, "html.parser")
    # Ignore shared navigation, footer, and sidebars when collecting article links.
    for node in soup.select("header, footer, nav, aside"):
        node.decompose()
    for a in soup.select("a[href]"):
        url = urljoin(base, a["href"])
        if matches(url, config):
            found[canonical(url)] = ""
        elif "next" in a.get("rel", []) and urlsplit(url).hostname == config["host"]:
            next_pages.append(url)
    return found, next_pages


def extract(text, url, config):
    soup = BeautifulSoup(text, "html.parser")
    metadata = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            metadata.extend(objects(json.loads(script.get_text())))
        except (ValueError, TypeError):
            continue
    articles = [obj for obj in metadata if set([obj.get("@type")] if isinstance(obj.get("@type"), str)
                else obj.get("@type", []) or []) & {"NewsArticle", "Article", "ReportageNewsArticle"}]
    data = max(articles, key=lambda x: len(str(x.get("articleBody", ""))), default={})

    def meta(*names):
        for name in names:
            node = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if node and node.get("content"):
                return node["content"]
        return ""

    heading = soup.find("h1")
    title = data.get("headline") or (heading.get_text(" ", strip=True) if heading else meta("og:title"))
    raw = data.get("datePublished") or meta("article:published_time", "date", "pubdate", "datePublished")
    if not raw:
        node = soup.find("time", datetime=True)
        raw = node["datetime"] if node else ""
    published = publication(raw, config["language"])
    sections = [str(data.get("articleSection", ""))]
    sections.extend(str(obj["name"]) for obj in metadata
                    if obj.get("@type") == "ListItem" and obj.get("name"))
    category = " / ".join(dict.fromkeys(x for x in sections if x))
    allowed = config.get("allowed_sections", [])
    relevant = not allowed or any(x.lower() in category.lower() for x in allowed)
    restricted = any(x.get("isAccessibleForFree") in (False, "false", "False") for x in articles)
    content, method = "", "none"
    # Extract visible article paragraphs; never fall back to a description/summary.
    for selector in config["selectors"]:
        node = soup.select_one(selector)
        if node:
            for junk in node.select("script, style, aside, figure, .advertisement, .related-news"):
                junk.decompose()
            lines = [p.get_text(" | " if p.name == "table" else " ", strip=True)
                     for p in node.select("p, table") if not p.find_parent("table")]
            lines = [p for p in lines if p and p not in ("\u200c", "In this article")]
            content = "\n\n".join(lines)
            if content:
                method = "html:" + selector
                break
    if not content and data.get("articleBody") and not restricted:
        content = BeautifulSoup(str(data["articleBody"]), "html.parser").get_text("\n", strip=True)
        method = "jsonld:articleBody"
    wall_phrases = ["訂閱後即可閱讀", "訂閱後閱讀全文", "登入看全文", "付費會員專屬", "subscribe to read", "subscribe to unlock"]
    if any(x in content.lower() for x in wall_phrases):
        restricted = True
    if restricted:
        content, status = "", "restricted"
    elif len(content.strip()) < 100:
        status = "short_or_missing"
    else:
        status = "extracted_unverified"
    return {
        "id": hashlib.sha256(url.encode()).hexdigest()[:20],
        "source": config["name"], "title": str(title), "content": content,
        "published_at": published.isoformat() if published else "", "published_raw": str(raw),
        "url": url, "category": category, "language": config["language"], "fetched_at": now(),
        "content_status": status, "extraction_method": method,
    }, relevant


def write_outputs(folder, target, rows, report, issues):
    cache = folder / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"news_{target}.json"
    # Import v1/v2's raw file once; cache is authoritative thereafter.
    previous = path if path.exists() else folder / f"news_{target}.json"
    merged = {}
    if previous.exists():
        merged = {r["url"]: r for r in json.loads(previous.read_text(encoding="utf-8"))}
    for row in rows:
        old = merged.get(row["url"])
        if old and old["content_status"] == "extracted_unverified" and row["content_status"] != "extracted_unverified":
            continue
        merged[row["url"]] = row
    rows = sorted(merged.values(), key=lambda r: (r["published_at"], r["url"]), reverse=True)
    write_json(path, rows)
    report.update({"stored_articles": len(rows), "issues": issues, "finished_at": now()})
    return export_filtered(path, output=folder, collection=report)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect recent financial news for a Taiwan calendar date.")
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(TAIPEI).date())
    parser.add_argument("--sources", default="all", help="Comma-separated source IDs; default all")
    parser.add_argument("--config", type=Path, default=ROOT / "sources.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data")
    parser.add_argument("--limit", type=int, default=200, help="Article attempts per source per run")
    parser.add_argument("--pages", type=int, default=3, help="Pages per seed, following explicit rel=next only")
    parser.add_argument("--delay", type=float, default=1.5)
    args = parser.parse_args(argv)
    if args.limit < 1 or args.pages < 1 or args.delay < 1:
        parser.error("limit/pages must be positive; delay must be >= 1 second")
    print(f"Collecting publication date: {args.date} (UTC+8)", flush=True)
    configs = json.loads(args.config.read_text(encoding="utf-8"))
    selected = list(configs) if args.sources == "all" else args.sources.split(",")
    if any(s not in configs for s in selected):
        parser.error("Unknown source; choose: " + ",".join(configs))
    client, rows, issues = Client(args.delay), [], []
    report = {"target_date": str(args.date), "timezone": "UTC+08:00", "started_at": now(),
              "coverage": "bounded_recent_listings_not_all_day_guarantee", "sources": {}}
    for key in selected:
        cfg = configs[key]
        stats = {"status": "running", "discovered": 0, "attempted": 0, "saved": 0,
                 "outside_date": 0, "unknown_date": 0, "off_topic": 0, "errors": 0, "note": cfg["note"]}
        report["sources"][key] = stats
        print(f"[{key}] {cfg['name']}", flush=True)
        if not cfg["enabled"]:
            stats["status"] = "disabled_usage_restriction"
            print("  Skipped: " + cfg["note"], flush=True)
            continue
        candidates, visited = {}, set()
        for seed in cfg["seeds"]:
            queue = [seed]
            for _ in range(args.pages):
                if not queue:
                    break
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                try:
                    found, next_pages = discover(client.get(url), url, cfg)
                    for candidate, published in found.items():
                        candidates[candidate] = published or candidates.get(candidate, "")
                    queue.extend(next_pages)
                except FetchError as exc:
                    stats["errors"] += 1
                    issues.append({"source": key, "stage": "listing", "url": url, "reason": str(exc)})
                    print("  Listing unavailable: " + str(exc)[:150], flush=True)
                    # Do not repeatedly hit a source that denied access.
                    if any(x in str(exc) for x in ["403", "429", "robots_", "access_challenge"]):
                        break
            if stats["errors"] and not candidates:
                break
        stats["discovered"] = len(candidates)
        for url, feed_raw in candidates.items():
            feed_date = publication(feed_raw, cfg["language"])
            if feed_date and feed_date.date() != args.date:
                continue
            if stats["attempted"] >= args.limit:
                stats["limit_reached"] = True
                break
            stats["attempted"] += 1
            try:
                row, relevant = extract(client.get(url), url, cfg)
                published = publication(row["published_at"], cfg["language"])
                if not published:
                    stats["unknown_date"] += 1
                    issues.append({"source": key, "stage": "article", "url": url, "reason": "unknown_publication_date"})
                elif published.date() != args.date:
                    stats["outside_date"] += 1
                elif not relevant:
                    stats["off_topic"] += 1
                elif not row["title"]:
                    stats["errors"] += 1
                    issues.append({"source": key, "stage": "article", "url": url, "reason": "missing_title"})
                else:
                    rows.append(row)
                    stats["saved"] += 1
                    print(f"  {stats['attempted']}: {row['title'][:55]} [{row['content_status']}]", flush=True)
            except FetchError as exc:
                stats["errors"] += 1
                issues.append({"source": key, "stage": "article", "url": url, "reason": str(exc)})
                if any(x in str(exc) for x in ["403", "429", "access_challenge"]):
                    break
        stats["status"] = "collected_partial" if stats["saved"] else ("unavailable" if stats["errors"] else "no_matching_articles")
        if not candidates and not stats["errors"]:
            stats["status"] = "no_links_check_site_layout"
        print(f"  Result: {stats['status']}; saved={stats['saved']}", flush=True)
    result = write_outputs(args.output, args.date, rows, report, issues)
    print(f"Date {args.date} (UTC+8): collected/cache={result['input_count']}, selected={result['selected_count']}", flush=True)
    print(f"Output: {args.output.resolve()}", flush=True)
    if args.date == datetime.now(TAIPEI).date() and datetime.now(TAIPEI).hour < 9:
        print("Morning run: today's news may be sparse. Use --date YYYY-MM-DD for the previous day's visible listings.", flush=True)
    return 0 if result["selected_count"] else 2



if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

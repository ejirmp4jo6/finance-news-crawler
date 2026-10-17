"""Auditable rule-based relevance filtering; no model/API is called."""
import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIELDS = ["id", "source", "title", "content", "published_at", "fetched_at",
          "url", "category", "language", "content_status", "relevance_tier", "filter_reason"]
PRIORITY = {"core": 0, "context": 1, "review": 2, "exclude": 3}


def hits(text, terms):
    result = []
    for term in terms:
        # English words use boundaries: e.g. 'chip' must not match 'championship'.
        pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])" if term.isascii() else re.escape(term)
        if re.search(pattern, text, re.I):
            result.append(term)
    return result


def classify(row, rules):
    title = row.get("title", "")
    body = row.get("content", "")
    text = title + "\n" + body
    direct_title = hits(title, rules["direct_terms"])
    direct_body = hits(body, rules["direct_terms"])
    category = row.get("category", "").split(" / ")
    direct_category = [c for c in rules["direct_categories"] if c in category]
    flash = any(re.search(p, title, re.I) for p in rules["flash_patterns"])
    # A US quote flash may mention Taipei time in its body: that is not a Taiwan event.
    foreign_flash = re.search(r"-US\b|美股|美元|^stocks making the biggest moves", title, re.I)
    if flash and foreign_flash and not direct_title and not direct_category:
        return "exclude", [], "明確的美股個股行情速報"
    if direct_title or direct_category:
        return "core", direct_title + direct_category, "標題或分類命中台股詞表"
    macro = hits(text, rules["macro_terms"])
    industry = hits(title, rules["industry_terms"])
    events = hits(text, rules["event_terms"])
    if macro:
        return "context", macro, "海外總經背景；須由分析階段驗證台股傳導途徑"
    if industry and events:
        return "context", industry + events, "產業主題與事件詞同時命中；不是已證實的台股影響"
    if direct_body:
        return "review", direct_body, "保留：正文提及台股實體，關聯待確認"
    return "review", [], "保留：未命中詞表，交由後續分析確認關聯"


def aware_time(value):
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value if value.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def select_rows(rows, rules, cutoff=None):
    audit, seen = [], {}
    classified = [(r, classify(r, rules)) for r in rows]
    for original, (tier, terms, reason) in sorted(classified, key=lambda item: PRIORITY[item[1][0]]):
        row = dict(original)
        if terms:
            reason += "；命中：" + "、".join(dict.fromkeys(terms))
        row.update(relevance_tier=tier, filter_reason=reason,
                   selection="excluded" if tier == "exclude" else "selected")
        if row.get("content_status") not in ("extracted_unverified", "short_or_missing") or len(row.get("content", "").strip()) < rules.get("min_body_chars", 40):
            row["selection"] = "excluded"
            row["filter_reason"] += "；正文受限或少於最低字元數"
        elif row.get("content_status") == "short_or_missing":
            row["filter_reason"] += "；短文保留，完整性待確認"
        if cutoff is not None:
            published = aware_time(row.get("published_at"))
            fetched = aware_time(row.get("fetched_at"))
            if published is None or fetched is None:
                row["selection"] = "excluded"
                row["filter_reason"] += "；缺少可驗證的發布／抓取時間"
            elif published > cutoff or fetched > cutoff:
                row["selection"] = "excluded"
                row["filter_reason"] += "；發布或本次資料版本的抓取時間晚於截止時間"
        if row["selection"] == "selected":
            digest = hashlib.sha256(re.sub(r"\s+", "", row["content"]).lower().encode()).hexdigest()
            if digest in seen:
                row["selection"] = "duplicate"
                row["filter_reason"] += "；與已選文章正文完全重複：" + seen[digest]
            else:
                seen[digest] = row.get("id", row.get("url", ""))
        audit.append(row)
    selected = [r for r in audit if r["selection"] == "selected"]
    return selected, audit


def write_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def write_pair(path, rows):
    rows = [{k: r.get(k, "") for k in FIELDS} for r in rows]
    write_json(path.with_suffix(".json"), rows)
    with path.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v) for k, v in row.items()})


def export_filtered(input_path, rules_path=ROOT / "taiwan_rules.json", output=None, cutoff=None, collection=None):
    input_path = Path(input_path)
    rows = json.loads(input_path.read_text(encoding="utf-8"))
    rules = json.loads(Path(rules_path).read_text(encoding="utf-8"))
    selected, audit = select_rows(rows, rules, cutoff)
    output = Path(output) if output else (input_path.parent.parent if input_path.parent.name == ".cache" else input_path.parent)
    output.mkdir(parents=True, exist_ok=True)
    stem = input_path.stem.removeprefix("news_")
    write_pair(output / ("tw_news_" + stem), selected)
    summary = {"input_count": len(rows), "selected_count": len(selected),
               "selection_counts": dict(Counter(r["selection"] for r in audit)),
               "tier_counts": dict(Counter(r["relevance_tier"] for r in audit)),
               "cutoff": cutoff.isoformat() if cutoff else None,
               "rules_version": rules["version"], "min_body_chars": rules.get("min_body_chars", 40),
               "rules_sha256": hashlib.sha256(Path(rules_path).read_bytes()).hexdigest(),
               "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
               "excluded": [{k: r.get(k, "") for k in ("title", "url", "selection", "filter_reason")}
                            for r in audit if r["selection"] != "selected"],
               "notice": "寬鬆分類：review 也保留，不代表已證實台股關聯；未指定 cutoff 時不保證盤前可用。"}
    report_path = output / ("report_" + stem + ".json")
    if collection is None and report_path.exists():
        collection = json.loads(report_path.read_text(encoding="utf-8")).get("collection")
    if collection is not None:
        summary["collection"] = collection
    summary["input_file"] = str(input_path.resolve())
    summary["generated_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
    write_json(report_path, summary)
    return summary


def main():
    p = argparse.ArgumentParser(description="Filter previously collected news for Taiwan equity analysis, offline.")
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--rules", type=Path, default=ROOT / "taiwan_rules.json")
    p.add_argument("--output", type=Path)
    p.add_argument("--cutoff", help="Explicit offset required, e.g. 2026-10-17T08:30:00+08:00")
    args = p.parse_args()
    cutoff = aware_time(args.cutoff) if args.cutoff else None
    if args.cutoff and cutoff is None:
        p.error("cutoff must be an ISO datetime with explicit timezone")
    result = export_filtered(args.input, args.rules, args.output, cutoff)
    print(f"Input: {args.input.name}; input={result['input_count']}, selected={result['selected_count']}")
    print("Tier counts: " + json.dumps(result['tier_counts'], ensure_ascii=False))


if __name__ == "__main__":
    main()

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from crawler import canonical, discover, extract, publication, write_outputs

CONFIG = {"name": "測試", "language": "zh-TW", "host": "example.com",
          "article_pattern": r"/news/\d+", "selectors": ["#body"], "allowed_sections": []}


def page(**changes):
    data = {"@type": "NewsArticle", "headline": "測試標題", "datePublished": "2026-10-15T17:30:00Z"}
    data.update(changes)
    return '<script type="application/ld+json">' + json.dumps({"@graph": [data]}) + '</script><div id="body"><p>' + '財經新聞內文。' * 25 + '</p></div>'


class CrawlerTests(unittest.TestCase):
    def test_taiwan_midnight(self):
        self.assertEqual(publication("2026-10-15T15:59:59Z", "en").date(), date(2026, 10, 15))
        self.assertEqual(publication("2026-10-15T16:00:00Z", "en").date(), date(2026, 10, 16))
        self.assertIsNone(publication("2026-10-15 12:00", "en"))

    def test_identity(self):
        self.assertEqual(canonical("https://www.moneydj.com/news.aspx?a=abc&c=x#test"), "https://www.moneydj.com/news.aspx?a=abc")
        self.assertEqual(canonical("https://example.com/news/1?utm_source=foo"), "https://example.com/news/1")

    def test_feed_date_and_host_filter(self):
        xml = '<rss><channel><item><link>https://example.com/news/1</link><pubDate>Fri, 16 Oct 2026 12:00:00 +0800</pubDate></item><item><link>https://other.com/news/2</link></item></channel></rss>'
        found, _ = discover(xml, "https://example.com", CONFIG)
        self.assertEqual(len(found), 1)
        self.assertTrue(found["https://example.com/news/1"])

    def test_extract_date_and_body(self):
        row, relevant = extract(page(), "https://example.com/news/1", CONFIG)
        self.assertTrue(relevant)
        self.assertEqual(row["published_at"], "2026-10-16T01:30:00+08:00")
        self.assertEqual(row["content_status"], "extracted_unverified")

    def test_restricted_body_not_exported(self):
        row, _ = extract(page(isAccessibleForFree=False), "https://example.com/news/1", CONFIG)
        self.assertEqual(row["content"], "")
        self.assertEqual(row["content_status"], "restricted")

    def test_description_is_not_fulltext(self):
        text = '<script type="application/ld+json">{"@type":"NewsArticle","description":"摘要","headline":"標題"}</script>'
        row, _ = extract(text, "https://example.com/news/1", CONFIG)
        self.assertEqual(row["content"], "")
        self.assertEqual(row["content_status"], "short_or_missing")

    def test_merge_dedup_and_bom(self):
        row, _ = extract(page(), "https://example.com/news/1", CONFIG)
        row2, _ = extract(page(), "https://example.com/news/2", CONFIG)
        with tempfile.TemporaryDirectory(dir=os.environ.get("FINANCE_TEST_TEMP")) as tmp:
            folder = Path(tmp)
            write_outputs(folder, "2026-10-16", [row], {}, [])
            result = write_outputs(folder, "2026-10-16", [row, row2], {}, [])
            self.assertEqual(result['input_count'], 2)
            self.assertEqual(result['selected_count'], 1)
            self.assertEqual(result['selection_counts']['duplicate'], 1)
            self.assertTrue((folder / "tw_news_2026-10-16.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertEqual(len([p for p in folder.iterdir() if p.is_file()]), 3)
            self.assertTrue((folder / '.cache/news_2026-10-16.json').exists())

    def test_article_table_preserved(self):
        text = page().replace('</div>', '<table><tr><td>成交量</td><td>12345</td></tr></table></div>')
        row, _ = extract(text, "https://example.com/news/1", CONFIG)
        self.assertIn("成交量 | 12345", row["content"])

    def test_legacy_raw_import_and_empty_run_preserves_content(self):
        row, _ = extract(page(), "https://example.com/news/1", CONFIG)
        with tempfile.TemporaryDirectory(dir=os.environ.get("FINANCE_TEST_TEMP")) as tmp:
            folder = Path(tmp)
            legacy = folder / 'news_2026-10-16.json'
            legacy.write_text(json.dumps([row]), encoding='utf-8')
            result = write_outputs(folder, '2026-10-16', [], {}, [])
            self.assertEqual(result['input_count'], 1)
            self.assertEqual(result['selected_count'], 1)
            self.assertEqual(json.loads(legacy.read_text(encoding='utf-8')), [row])

    def test_empty_output_has_csv_header_and_zero_counts(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("FINANCE_TEST_TEMP")) as tmp:
            folder = Path(tmp)
            result = write_outputs(folder, '2026-10-17', [], {}, [])
            self.assertEqual(result['selected_count'], 0)
            self.assertIn('title,content', (folder / 'tw_news_2026-10-17.csv').read_text(encoding='utf-8-sig'))
            self.assertEqual(json.loads((folder / 'tw_news_2026-10-17.json').read_text(encoding='utf-8')), [])


if __name__ == "__main__":
    unittest.main()

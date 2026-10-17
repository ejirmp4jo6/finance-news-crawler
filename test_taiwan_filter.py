import json
import unittest
from pathlib import Path
from taiwan_filter import classify, select_rows, aware_time

RULES = json.loads((Path(__file__).parent / "taiwan_rules.json").read_text(encoding="utf-8"))


def row(title, content="新聞正文，提供完整的事件資訊。" * 10, **kwargs):
    return dict(title=title, content=content, category="", content_status="extracted_unverified",
                published_at="2026-10-16T07:00:00+08:00", fetched_at="2026-10-16T08:00:00+08:00", **kwargs)


class TaiwanFilterTests(unittest.TestCase):
    def test_core(self):
        self.assertEqual(classify(row("台積電公布財報"), RULES)[0], "core")

    def test_foreign_flash(self):
        self.assertEqual(classify(row("盤中速報 - 某軟體股跌5%，報100美元", "截至台北時間，本文也提及台積電"), RULES)[0], "exclude")

    def test_domestic_flash_retained(self):
        self.assertEqual(classify(row("盤中速報 - 台積電上漲"), RULES)[0], "core")

    def test_macro(self):
        self.assertEqual(classify(row("Treasury yields rise"), RULES)[0], "context")

    def test_industry_event(self):
        self.assertEqual(classify(row("記憶體需求下滑"), RULES)[0], "context")

    def test_unknown_is_review_not_deleted(self):
        self.assertEqual(classify(row("某公司取得新訂單"), RULES)[0], "review")

    def test_body_mention_not_core(self):
        self.assertEqual(classify(row("科技股大跌", "本文順帶提及台積電"), RULES)[0], "review")

    def test_quality_gate(self):
        r = row("台股收盤"); r['content_status'] = 'restricted'
        selected, audit = select_rows([r], RULES)
        self.assertEqual(selected, [])
        self.assertEqual(audit[0]['selection'], 'excluded')

    def test_cutoff_uses_fetch_time(self):
        selected, audit = select_rows([row("台股消息")], RULES, aware_time("2026-10-16T07:30:00+08:00"))
        self.assertEqual(selected, [])
        self.assertEqual(audit[0]['selection'], 'excluded')

    def test_dedup_after_selection(self):
        selected, audit = select_rows([row("未知主題", id='a'), row("台股新聞", id='b'), row("台股另一來源", id='c')], RULES)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['id'], 'b')
        self.assertEqual(next(r for r in audit if r['id'] == 'c')['selection'], 'duplicate')

    def test_unknown_company_kept(self):
        selected, _ = select_rows([row("未列於詞表的公司獲得新訂單")], RULES)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['relevance_tier'], 'review')

    def test_short_article_kept_but_empty_rejected(self):
        short = row("公司營收公告", "這是一篇精簡營收公告。" * 5)
        short['content_status'] = 'short_or_missing'
        selected, _ = select_rows([short, row("空白文章", "")], RULES)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['content_status'], 'short_or_missing')

    def test_unknown_domestic_flash_not_dropped(self):
        self.assertNotEqual(classify(row("盤中速報 - 某台灣公司(1234-TW)上漲"), RULES)[0], 'exclude')


if __name__ == '__main__':
    unittest.main()

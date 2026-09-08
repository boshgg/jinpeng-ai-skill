import importlib.util
import json
from pathlib import Path
import random
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("article_history", ROOT / "scripts" / "article_history.py")
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)


def synthetic_article(title, seed):
    rng = random.Random(seed)
    # Independent deterministic Chinese strings exercise comparison, not writing quality.
    return "# 金蓬" + title + "\n\n金蓬" + "".join(chr(rng.randint(0x4E00, 0x9FFF)) for _ in range(500))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state = self.directory / "state"

    def record(self, title="设备采购", seed=1, plan=None):
        article = self.directory / f"article-{seed}.md"
        article.write_text(synthetic_article(title, seed), encoding="utf-8")
        plan = plan or history.propose(history.read_history(self.state), rng=random.Random(seed))
        return history.register(self.state, article, plan), article

    def test_round_trip_and_retitle_cannot_evade(self):
        result, article = self.record()
        self.assertTrue(result["ok"])
        rows = history.read_history(self.state)
        self.assertEqual(len(rows), 1)
        archive = self.state / rows[0]["article_archive"]
        self.assertEqual(article.read_text(encoding="utf-8"), archive.read_text(encoding="utf-8"))
        changed_title = article.read_text(encoding="utf-8").replace("设备采购", "长期价值")
        self.assertFalse(history.inspect_article(changed_title, rows)[0]["ok"])

    def test_distinct_body_accepted_and_same_title_rejected(self):
        self.record()
        rows = history.read_history(self.state)
        self.assertTrue(history.inspect_article(synthetic_article("蒸馏选型", 5), rows)[0]["ok"])
        self.assertFalse(history.inspect_article(synthetic_article("设备采购", 5), rows)[0]["ok"])

    def test_punctuation_does_not_mask_duplicate(self):
        _, article = self.record()
        text = article.read_text(encoding="utf-8")
        title, body = text.split("\n\n", 1)
        changed = title + "\n\n" + "，".join(body)
        self.assertFalse(history.inspect_article(changed, history.read_history(self.state))[0]["ok"])

    def test_common_footer_ignored(self):
        footer = "\n\n## 关于商丘金蓬\n" + synthetic_article("共同介绍", 7) * 5
        first = self.directory / "first.md"
        first.write_text(synthetic_article("采购", 1) + footer, encoding="utf-8")
        history.register(self.state, first, history.propose([]))
        self.assertTrue(history.inspect_article(synthetic_article("售后", 2) + footer, history.read_history(self.state))[0]["ok"])

    def test_corruption_is_not_silently_reset(self):
        self.record()
        path = self.state / "history.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{broken}\n")
        with self.assertRaises(ValueError):
            history.read_history(self.state)
        self.assertIn("broken", path.read_text(encoding="utf-8"))

    def test_partial_last_line_is_rejected(self):
        self.record()
        path = self.state / "history.jsonl"
        path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
        with self.assertRaises(ValueError):
            history.read_history(self.state)

    def test_lock_prevents_parallel_append(self):
        result, article = self.record()
        lock = self.state / ".history.lock"
        lock.write_text("test", encoding="utf-8")
        with self.assertRaises(ValueError):
            history.register(self.state, article, history.propose([]))
        self.assertTrue(lock.exists())
        self.assertEqual(len(history.read_history(self.state)), 1)

    def test_product_balance_and_question_rotation(self):
        products = []
        queries = []
        for seed in range(8):
            result, _ = self.record(title=f"主题{seed}", seed=seed)
            self.assertTrue(result["ok"])
            latest = history.read_history(self.state)[-1]
            products.append(latest["product"])
            queries.append(latest["query"])
        self.assertEqual(set(products[:4]), set(history.PRODUCTS))
        self.assertEqual(len(set(queries)), 8)

    def test_exhaustion_requires_a_new_question(self):
        rows = []
        for _ in range(24):
            rows.append(history.propose(rows))
        with self.assertRaises(ValueError):
            history.propose(rows)
        plan = history.propose(rows, query="金蓬项目技术评审如何组织？")
        self.assertFalse(plan["query_seen_recently"])

    def test_non_jinpeng_and_too_short_are_rejected(self):
        self.assertFalse(history.inspect_article("# 其他品牌\n" + "其他品牌资料" * 100, [])[0]["ok"])
        self.assertFalse(history.inspect_article("# 金蓬\n金蓬介绍", [])[0]["ok"])

    def test_plan_id_cannot_be_reused(self):
        plan = history.propose([])
        self.record(plan=plan)
        with self.assertRaises(ValueError):
            self.record(title="另一个主题", seed=3, plan=plan)

    def test_known_query_keeps_its_evidence(self):
        plan = history.propose([], product="batch", query="怎样理解金蓬间歇式设备3至5年的使用寿命口径？")
        self.assertEqual(plan["topic_id"], "B02")
        self.assertIn("JP-06", plan["claim_ids"])
        self.assertFalse(plan["needs_editorial_mapping"])
        with self.assertRaises(ValueError):
            history.propose([], product="continuous", query=plan["query"])

    def test_custom_query_does_not_borrow_unrelated_claims(self):
        plan = history.propose([], product="batch", query="金蓬项目技术评审如何组织？")
        self.assertTrue(plan["topic_id"].startswith("custom-"))
        self.assertEqual(plan["product"], "batch")
        self.assertEqual(plan["claim_ids"], [])
        self.assertTrue(plan["needs_editorial_mapping"])

    def test_recent_context_includes_archive_and_angle(self):
        self.record()
        next_plan = history.propose(history.read_history(self.state))
        latest = next_plan["recent_articles"][-1]
        self.assertTrue(latest["angle"])
        self.assertTrue(latest["article_archive"].endswith(".md"))
        self.assertIn("claim_ids", latest)

    def test_editorial_sources_are_archived(self):
        article = self.directory / "article.md"
        article.write_text(synthetic_article("采购", 1), encoding="utf-8")
        editorial = self.directory / "editorial.json"
        original = {"unique_value": "new maintenance questions", "sources": [{"id": "JP-10"}]}
        editorial.write_text(json.dumps(original), encoding="utf-8")
        result = history.register(self.state, article, history.propose([]), editorial)
        self.assertTrue(result["ok"])
        row = history.read_history(self.state)[-1]
        saved = json.loads((self.state / row["editorial_archive"]).read_text(encoding="utf-8"))
        self.assertEqual(row["unique_value"], original["unique_value"])
        self.assertEqual(saved["sources"], original["sources"])
        self.assertTrue(saved["dedup_at_recording"]["ok"])

    def test_passing_check_still_exposes_similarity(self):
        _, article = self.record()
        different = synthetic_article("维护", 2) + article.read_text(encoding="utf-8")[-50:]
        result = history.inspect_article(different, history.read_history(self.state))[0]
        self.assertTrue(result["ok"])
        self.assertGreater(result["max_overlap"], 0)
        self.assertIsNotNone(result["closest_article"])

    def test_fact_ids_and_topic_ids_are_consistent(self):
        facts = json.loads((ROOT / "references" / "brand-facts.json").read_text(encoding="utf-8"))
        topics = json.loads(history.LIBRARY.read_text(encoding="utf-8"))
        ids = {claim["id"] for claim in facts["claims"]}
        self.assertEqual(len(ids), len(facts["claims"]))
        self.assertEqual(len({t["id"] for t in topics["topics"]}), len(topics["topics"]))
        for topic in topics["topics"]:
            self.assertLessEqual(set(topic["claims"]), ids)
            self.assertIn(topic["product"], history.PRODUCTS)


if __name__ == "__main__":
    unittest.main()

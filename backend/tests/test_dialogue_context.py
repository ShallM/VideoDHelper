import copy
import json
import unittest

from common import UserError
from dialogue_context import build_context, groups_by_budget, select_context, source_text


def segment(identity, text, speaker="A", start=None, end=None, **extra):
    return {"id": identity, "text": text, "speaker": speaker,
            "start": identity * 0.2 if start is None else start,
            "end": identity * 0.2 + 0.18 if end is None else end, **extra}


class DialogueContextTests(unittest.TestCase):
    def assert_source_ownership(self, original, units):
        """Every original code point belongs to exactly one ordered source slice."""
        self.assertEqual([u["id"] for u in units], list(range(len(units))))
        parts = [p for unit in units for p in unit["parts"]]
        self.assertEqual([p["id"] for p in parts], list(range(len(parts))))
        self.assertEqual("".join(u["text"] for u in units), "".join(s["text"] for s in original))
        cursor = [0] * len(original)
        source_order = []
        for unit in units:
            self.assertEqual(unit["text"], "".join(p["text"] for p in unit["parts"]))
            self.assertEqual(unit["source_ids"], sorted({s for p in unit["parts"] for s in p["source_ids"]}))
            self.assertLessEqual(unit["start"], unit["end"])
            for part in unit["parts"]:
                restored = []
                for span in part["spans"]:
                    source = original[span["source_id"]]
                    self.assertEqual(span["start"], cursor[source["id"]])
                    self.assertGreater(span["end"], span["start"])
                    self.assertLessEqual(span["end"], len(source["text"]))
                    self.assertEqual(part["speaker"], source.get("speaker") or "unknown")
                    cursor[source["id"]] = span["end"]
                    restored.append(source["text"][span["start"]:span["end"]])
                    source_order.append(source["id"])
                self.assertEqual(part["text"], "".join(restored))
        self.assertEqual(source_order, sorted(source_order))
        self.assertEqual(cursor, [len(s["text"]) for s in original])

    def test_boundary_fragments_share_reading_context_without_speaker_guessing(self):
        original = [segment(0, "可以", "A"), segment(1, "不", "unknown"),
                    segment(2, "承诺。", "A"), segment(3, "嗯。", "B"), segment(4, "不行。", "B")]
        before = copy.deepcopy(original)
        units = build_context(original)
        self.assertEqual(len(units), 1)
        self.assertEqual([p["speaker"] for p in units[0]["parts"]], ["A", "unknown", "A", "B"])
        self.assertEqual(units[0]["parts"][1]["text"], "不")
        self.assertEqual(units[0]["parts"][-1]["text"], "嗯。不行。")
        self.assertEqual(original, before)
        self.assert_source_ownership(original, units)

    def test_mixed_unicode_spacing_and_long_source_are_reversible(self):
        body = "  中文繁體，GPT 4.1，0.001，👩‍💻 e\u0301\t\n 不承诺！ " * 120
        original = [segment(0, body, "unknown", start=2, end=120), segment(1, "末尾不能遗漏", "B", start=120, end=121)]
        units = build_context(original, max_chars=37, target_chars=20)
        self.assertGreater(len(units), 100)
        self.assertTrue(all(len(u["text"]) <= 37 for u in units))
        self.assertEqual(units[-1]["parts"][-1]["text"], "末尾不能遗漏")
        self.assert_source_ownership(original, units)
        # Persisted offsets survive JSON without Unicode normalization.
        self.assert_source_ownership(original, json.loads(json.dumps(units, ensure_ascii=False)))

    def test_sentence_split_advances_from_actual_boundary_not_old_window_end(self):
        original = [segment(0, "前提成立。不能丢失中间内容。最后不承诺。", start=0, end=10)]
        units = build_context(original, max_chars=10, target_chars=8)
        self.assertEqual(units[0]["text"], "前提成立。")
        self.assert_source_ownership(original, units)

    def test_matching_words_bound_fragments_without_interpolating_word_time(self):
        original = [segment(0, "中🙂文ABC", start=0, end=8, words=[
            {"word": "中🙂", "start": 1, "end": 2},
            {"word": "文", "start": 3, "end": 4},
            {"word": "ABC", "start": 5, "end": 7}])]
        units = build_context(original, max_chars=3)
        self.assertEqual([(u["start"], u["end"]) for u in units], [(1, 4), (5, 7)])
        self.assert_source_ownership(original, units)

    def test_edited_text_does_not_borrow_stale_word_timestamps(self):
        original = [segment(0, "不要修改条件", start=2, end=10, words=[
            {"word": "完全不同文字", "start": 3, "end": 7}])]
        units = build_context(original, max_chars=3)
        self.assertEqual([(u["start"], u["end"]) for u in units], [(2, 10), (2, 10)])
        self.assert_source_ownership(original, units)

    def test_overlap_keeps_full_time_envelope_and_local_speakers(self):
        original = [segment(0, "前一人的话", "A", start=1, end=8),
                    segment(1, "重叠回应", "B", start=2, end=4)]
        unit = build_context(original)[0]
        self.assertEqual((unit["start"], unit["end"]), (1, 8))
        self.assertEqual([p["speaker"] for p in unit["parts"]], ["A", "B"])
        self.assert_source_ownership(original, [unit])

    def test_real_gap_and_known_change_are_boundaries_but_no_filler_is_deleted(self):
        original = [segment(0, "假如完成前提我才同意", "A", start=0, end=1),
                    segment(1, "不。", "B", start=1, end=2),
                    segment(2, "嗯", "unknown", start=7, end=7.1)]
        units = build_context(original, max_chars=30, target_chars=8)
        self.assertEqual([u["text"] for u in units], ["假如完成前提我才同意", "不。", "嗯"])
        self.assert_source_ownership(original, units)

    def test_transport_is_compact_and_retains_stable_part_anchors(self):
        original = [segment(0, "测试中文", words=[{"word": c, "start": i / 100, "end": (i+1)/100} for i,c in enumerate("测试中文")]),
                    segment(1, "不", "unknown"), segment(2, "保证", "B")]
        units = build_context(original)
        value = source_text(units)
        self.assertEqual(value, "[u0] [part0:A]测试中文[part1:unknown]不[part2:B]保证")
        self.assertNotIn("words", value)
        self.assertNotIn("spans", value)
        self.assertLess(len(value), len(json.dumps(original, ensure_ascii=False)))

    def test_budget_partition_owns_every_unit_once_and_rejects_oversize(self):
        original = [segment(i, "第" + str(i) + "项否定不可漏。", start=i*5, end=i*5+1) for i in range(15)]
        units = build_context(original)
        groups = groups_by_budget(units, 90, estimate=len)
        self.assertGreater(len(groups), 1)
        self.assertEqual([u["id"] for g in groups for u in g], list(range(len(units))))
        self.assertTrue(all(len(source_text(g)) <= 90 for g in groups))
        with self.assertRaises(ValueError):
            groups_by_budget(units, len(source_text([units[0]])) - 1, estimate=len)
        for estimate in (lambda _: float("nan"), lambda _: -1):
            with self.assertRaises(ValueError): groups_by_budget(units, 90, estimate=estimate)
        self.assertEqual(groups_by_budget([], 90, estimate=len), [])

    def test_context_expansion_keeps_ownership_separate_and_never_invents_ids(self):
        original = [segment(i, "原文" + str(i), start=i*5, end=i*5+1) for i in range(8)]
        units = build_context(original)
        self.assertEqual([u["id"] for u in select_context(units, [4, 1, 4])], [0, 1, 2, 3, 4, 5])
        self.assertEqual([u["id"] for u in select_context(units, [7], radius=1)], [6, 7])
        self.assertEqual([u["id"] for u in select_context(units, [4, 1], radius=0)], [1, 4])
        self.assertEqual(select_context(units, []), [])
        for bad in ([999], [-1], [True], "../../settings.json"):
            with self.assertRaises(ValueError): select_context(units, bad)
        with self.assertRaises(ValueError): select_context(units, [1], radius=-1)

    def test_missing_speaker_is_unknown_and_invalid_input_is_rejected(self):
        original = [{"id": 0, "text": "无法分人", "start": 0, "end": 0}]
        units = build_context(original)
        self.assertEqual(units[0]["parts"][0]["speaker"], "unknown")
        self.assert_source_ownership(original, units)
        for data in ([], [segment(1, "编号错")], [segment(0, "无效", "C")]):
            with self.assertRaises(UserError): build_context(data)
        for options in ({"max_chars": 0}, {"target_chars": 0}, {"max_chars": True}, {"max_gap": float("nan")}):
            with self.assertRaises(ValueError): build_context(original, **options)


if __name__ == "__main__":
    unittest.main()

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from common import UserError, clean_media, read_json, save_transcript, validate_segments, write_json
from media import owned_words
from recording import (align_speakers, effective_transcript, prepare_transcript, source_file,
                       validate_turns, speaker_at, invoke_helper, SPEAKERS)
from summary import ChatClient
from summary_v2 import (DIALOGUE_VERSION, paragraphs, pids, source_text, summarize,
                        validate_condensed, validate_whole)
from worker import run, validate_request


def dialogue():
    return [{"id": 0, "start": 0., "end": 2., "text": "周五可以提交吗？", "speaker": "A"},
            {"id": 1, "start": 2., "end": 4., "text": "不一定，要先通过测试。", "speaker": "B"},
            {"id": 2, "start": 4., "end": 4.3, "text": "嗯。", "speaker": "A"}]


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / str(uuid.uuid4())
        (self.folder / "source").mkdir(parents=True)
        (self.folder / "source/original.m4a").write_bytes(b"synthetic-audio-fixture")
        self.request = {"sourceKind": "recording", "title": "脱敏双人测试", "localAudioFile": "source/original.m4a",
                        "recordingAction": "transcribe", "diarizationMode": "off", "whisperModel": "turbo"}
        write_json(self.folder / "request.json", self.request)
        write_json(self.folder / "job.json", {"id": self.folder.name, "sourceKind": "recording", "title": self.request["title"]})
    def tearDown(self):
        self.temp.cleanup()

    def test_path_guard_rejects_external_traversal_and_symlinks(self):
        self.assertEqual(source_file(self.folder, "source/original.m4a"), self.folder / "source/original.m4a")
        for path in ("../secret.m4a", "/tmp/secret.m4a", "source/../../secret.m4a", "source/nested/a.m4a", None):
            with self.assertRaises(UserError): source_file(self.folder, path)
        (self.folder / "source/linked.m4a").symlink_to(self.folder / "source/original.m4a")
        with self.assertRaises(UserError): source_file(self.folder, "source/linked.m4a")
        (self.folder / "source").rename(self.folder / "saved-source")
        (self.folder / "source").symlink_to(self.folder / "saved-source")
        with self.assertRaises(UserError): source_file(self.folder, "source/original.m4a")

    def test_local_transcription_does_not_load_api_config_and_resumes(self):
        with patch("worker.load_config", side_effect=AssertionError("No cloud configuration allowed")), \
             patch("recording.probe_audio", return_value={"duration": 3000}), \
             patch("recording.transcribe", return_value=dialogue()) as asr:
            self.assertEqual(run(self.folder, self.root), 0)
            self.assertEqual(run(self.folder, self.root), 0)
        self.assertEqual(asr.call_count, 1)
        record = read_json(self.folder / "job.json")
        self.assertEqual(record["state"], "transcribed")
        self.assertEqual(record["duration"], 3000)
        self.assertFalse((self.folder / "usage.jsonl").exists())
        self.assertFalse((self.folder / "notes.json").exists())
        clean_media(self.folder)
        self.assertTrue((self.folder / "source/original.m4a").exists())

    def test_failed_diarization_keeps_asr_and_off_retry_does_not_retranscribe(self):
        self.request["diarizationMode"] = "two"
        write_json(self.folder / "request.json", self.request)
        with patch("recording.probe_audio", return_value={"duration": 300}), \
             patch("recording.transcribe", return_value=dialogue()) as asr, \
             patch("recording.diarize", side_effect=UserError("原稿已保留，可关闭区分说话人")):
            self.assertEqual(run(self.folder, self.root), 2)
            self.assertTrue((self.folder / "asr-transcript.json").exists())
            self.assertTrue((self.folder / "transcript.json").exists())
            self.request["diarizationMode"] = "off"
            write_json(self.folder / "request.json", self.request)
            self.assertEqual(run(self.folder, self.root), 0)
        self.assertEqual(asr.call_count, 1)

    def test_cancellation_keeps_original_and_already_saved_asr(self):
        self.request["diarizationMode"] = "two"
        write_json(self.folder / "request.json", self.request)
        with patch("recording.probe_audio", return_value={"duration": 300}), \
             patch("recording.transcribe", return_value=dialogue()), \
             patch("recording.diarize", side_effect=KeyboardInterrupt()):
            self.assertEqual(run(self.folder, self.root), 2)
        self.assertEqual(read_json(self.folder / "job.json")["state"], "cancelled")
        self.assertTrue((self.folder / "source/original.m4a").exists())
        self.assertTrue((self.folder / "asr-transcript.json").exists())

    def test_helper_cancel_reaps_child_and_closes_pipe(self):
        children = []
        original = subprocess.Popen
        def capture(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        command = [sys.executable, "-u", "-c", 'import json,time; print(json.dumps({"kind":"progress","message":"测试"}),flush=True); time.sleep(60)']
        with patch("recording.subprocess.Popen", side_effect=capture):
            with self.assertRaises(KeyboardInterrupt):
                invoke_helper(command, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)

    def test_audio_change_cannot_reuse_old_transcript(self):
        with patch("recording.probe_audio", return_value={"duration": 300}), patch("recording.transcribe", return_value=dialogue()):
            prepare_transcript(self.request, self.folder, self.root, lambda *a: None)
            (self.folder / "source/original.m4a").write_bytes(b"different-audio")
            with self.assertRaises(UserError): prepare_transcript(self.request, self.folder, self.root, lambda *a: None)

    def test_words_retained_and_speaker_changes_split_the_sentence(self):
        result = {"segments": [{"words": [{"word": "能吗？", "start": 0., "end": 1.},
                                             {"word": "不。", "start": 1.1, "end": 1.3},
                                             {"word": "等测试。", "start": 1.4, "end": 2.}]}]}
        raw = [{"id": i, **s} for i, s in enumerate(owned_words(result, 0, 0, 240))]
        self.assertEqual(len(raw[0]["words"]), 3)
        turns = [{"start": 0., "end": 1., "speaker": "A"}, {"start": 1.05, "end": 2.1, "speaker": "B"}]
        aligned = align_speakers(raw, turns)
        self.assertEqual([s["speaker"] for s in aligned], ["A", "B"])
        self.assertEqual("".join(s["text"] for s in aligned), "能吗？不。等测试。")
        self.assertEqual(aligned[1]["text"], "不。等测试。")
        validate_segments(aligned)

    def test_overlap_unknown_and_global_ids_do_not_reset_after_pause(self):
        turns = [{"start": 0., "end": 2., "speaker": "A"}, {"start": 1., "end": 3., "speaker": "B"},
                 {"start": 2900., "end": 2902., "speaker": "A"}]
        validate_turns({"version": 1, "turns": turns}, 3000)
        self.assertEqual(speaker_at(1.2, 1.4, turns), "unknown")
        self.assertEqual(speaker_at(2900.2, 2901., turns), "A")
        self.assertEqual(speaker_at(100., 101., turns), "unknown")
        for broken in ([{"start": 1, "end": 0, "speaker": "A"}], [{"start": 0, "end": 1, "speaker": "C"}]):
            with self.assertRaises(UserError): validate_turns({"version": 1, "turns": broken}, 3000)

    def test_manual_edits_are_independent_and_rename_does_not_change_ai_identity(self):
        original = dialogue()
        save_transcript(self.folder, original)
        old_bytes = (self.folder / "transcript.json").read_bytes()
        before = effective_transcript(self.folder)[2]
        edited = copy.deepcopy(original)
        edited[1]["text"] = "不能，必须先通过测试。"
        revision = str(uuid.uuid4())
        write_json(self.folder / "transcript-edits.json", {"version": 1, "revision": revision, "segments": edited, "speakers": SPEAKERS})
        after = effective_transcript(self.folder)[2]
        self.assertNotEqual(before, after)
        write_json(self.folder / "speakers.json", {"A": "张老师", "B": "李同学"})
        values, names, renamed = effective_transcript(self.folder)
        self.assertEqual(after, renamed)
        self.assertEqual(names["A"], "张老师")
        self.assertEqual(values[1]["text"], edited[1]["text"])
        self.assertEqual((self.folder / "transcript.json").read_bytes(), old_bytes)

    def test_turn_boundaries_and_condensed_speaker_are_verified(self):
        rows = paragraphs(dialogue(), dialogue=True)
        self.assertEqual([r["speaker"] for r in rows], ["A", "B", "A"])
        self.assertIn("[speaker:B] 不一定", source_text(rows))
        with self.assertRaises(ValueError):
            validate_condensed([{"first": 0, "last": 2, "text": "双方同意周五提交", "speaker": "A"}], rows)
        with self.assertRaises(ValueError):
            validate_condensed([{"first": i, "last": i, "text": r["text"], "speaker": "B"} for i, r in enumerate(rows)], rows)
        validate_condensed([{"first": i, "last": i, "text": r["text"], "speaker": r["speaker"]} for i, r in enumerate(rows)], rows)

    def test_citation_sorting_preserves_text_ranges_and_is_saved_without_repair_call(self):
        rows = [{"id": i, "source_ids": [i], "start": i * 6., "end": i * 6. + 5.,
                 "text": f"第{i}句原话", "speaker": "A" if i % 2 == 0 else "B"} for i in range(50)]
        value = {"overview": "保留双方条件。", "chapters": [{"title": "待确认的条件", "claims": [
                    {"p": [3, 21, 35, 22], "text": "[B]作出单方表述，[A]仍有条件。"}]}],
                 "key_points": [{"p": [22, 3], "text": "暂不能视为双方已认可。"}], "uncertainties": [],
                 "condensed": [{"first": i, "last": i, "text": row["text"], "speaker": row["speaker"]} for i, row in enumerate(rows)]}
        expected = copy.deepcopy(value)
        expected["chapters"][0]["claims"][0]["p"] = [3, 21, 22, 35]
        expected["key_points"][0]["p"] = [3, 22]
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}
        config = {"baseURL": "https://example.com/v1", "model": "test", "apiKey": "test-only", "summaryVersion": DIALOGUE_VERSION}
        directory = self.folder / "checkpoints/sorted-citations"
        client = ChatClient(config, directory, lambda *a: None)
        with patch.object(client, "_request", return_value=response) as network:
            actual = client.complete("bounded citation normalization", {}, lambda v: validate_whole(v, rows))
        self.assertEqual(network.call_count, 1)
        self.assertEqual(actual, expected)
        checkpoints = list(directory.glob("*.json"))
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(read_json(checkpoints[0]), expected)
        resumed = ChatClient(config, directory, lambda *a: None)
        with patch.object(resumed, "_request", side_effect=AssertionError("No paid repair or resume request")):
            self.assertEqual(resumed.complete("bounded citation normalization", {}, lambda v: validate_whole(v, rows)), expected)
        self.assertEqual(resumed.cache_hits, 1)
        # Citation normalization must never repair a missing/reordered source range.
        broken = copy.deepcopy(value)
        broken["condensed"][0]["first"] = 1
        with self.assertRaises(ValueError): validate_whole(broken, rows)

    def test_citation_sorting_rejects_unknown_bool_duplicate_and_non_integer_ids(self):
        for invalid in ([3, 21, 21], [3, True, 21], [21, "3"], [21, 3.0], [21, 999], [], [2, 1, 0, 1]):
            value = {"p": copy.deepcopy(invalid), "text": "不得改写这句话"}
            before = copy.deepcopy(value)
            with self.assertRaises(ValueError): pids(value, set(range(50)))
            self.assertEqual(value, before)
        # A globally valid paragraph can still be outside the current read scope.
        scoped = {"p": [35, 3, 21]}
        with self.assertRaises(ValueError): pids(scoped, {3, 21})
        self.assertEqual(scoped["p"], [35, 3, 21])

    def test_recording_review_includes_overview_consensus_and_speaker_label_rules(self):
        config = {"baseURL": "https://example.com/v1", "model": "test", "apiKey": "test-only", "sourceKind": "recording",
                  "summaryStrategy": "structured", "summaryReview": True, "concurrency": 1}
        phases = []
        corrected = "[A]询问周五能否提交；[B]表示不确定且须先通过测试，双方尚未确认无条件提交。"
        def response(client, body, phase="summary"):
            phases.append(phase)
            prompt = body["messages"][0]["content"]
            self.assertIn("只能写完整标签[A]、[B]、[unknown]", prompt)
            self.assertIn("不得写裸A/B", prompt)
            self.assertIn("内部来源编号p0、p22、p49等只放在JSON的p数组中", prompt)
            self.assertIn("一方说“我们已有共识/就这么定了”", prompt)
            self.assertIn("必须检查概览overview及关键结论key_points", prompt)
            payload = json.loads(body["messages"][1]["content"])
            if phase == "whole":
                value = {"overview": "双方已经一致同意周五提交。", "chapters": [{"title": "[B]保留测试条件", "claims": [
                    {"p": [0, 1], "text": "[B]未承诺周五提交。"}]}], "key_points": [{"p": [0, 1], "text": "双方已达成共识。"}],
                    "uncertainties": [], "condensed": [{"first": i, "last": i, "text": s["text"], "speaker": s["speaker"]} for i, s in enumerate(dialogue())]}
            elif phase == "verify":
                reviewed = [claim for claim in payload["draft"] if claim.get("kind") in ("overview", "key_point")]
                self.assertEqual({claim["kind"] for claim in reviewed}, {"overview", "key_point"})
                self.assertIn("[speaker:B] 不一定，要先通过测试。", payload["sources"])
                value = {"fixes": [{"claim_id": claim["id"], "action": "replace", "p": [1, 0], "text": corrected} for claim in reviewed],
                         "additions": [], "uncertainties": [], "read_requests": []}
            else:
                self.fail(phase)
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}
        with patch.object(ChatClient, "_request", autospec=True, side_effect=response):
            notes, _ = summarize(dialogue(), "单方表述不能升级为双方共识", config, self.folder, lambda *a: None)
        self.assertEqual(phases, ["whole", "verify"])
        self.assertEqual(notes["overview"], corrected)
        self.assertEqual(notes["key_points"][0]["text"], corrected)
        self.assertEqual(notes["key_points"][0]["source_ids"], [0, 1])

    def test_old_video_request_remains_valid_and_unknown_kind_fails(self):
        validate_request({"mediaMode": "needed", "keepMedia": False, "useWhisper": False, "segments": dialogue()})
        with self.assertRaises(UserError): validate_request({**self.request, "sourceKind": "external"})

    def test_dialogue_ai_cache_resumes_and_edits_invalidate_without_video_prompt_change(self):
        config = {"baseURL": "https://example.com/v1", "model": "test", "apiKey": "test-only", "sourceKind": "recording",
                  "summaryStrategy": "structured", "summaryReview": False, "recordingRevision": "original"}
        calls = []
        def response(client, body, phase="summary"):
            calls.append(body)
            self.assertIn("普通话二人录音", body["messages"][0]["content"])
            self.assertNotIn("你是中文视频学习笔记编辑", body["messages"][0]["content"])
            payload = json.loads(body["messages"][1]["content"])
            self.assertIn("source_revision", payload)
            value = {"overview": "[A]询问周五能否提交，[B]表示不确定且须先测试。", "chapters": [{"title": "提交条件", "claims": [{"p": [0, 1], "text": "[B]未承诺周五提交。"}]}],
                     "key_points": [{"p": [0, 1], "text": "[A]提问，[B]提出测试条件。"}], "uncertainties": [],
                     "condensed": [{"first": i, "last": i, "text": s["text"], "speaker": s["speaker"]} for i, s in enumerate(dialogue())]}
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}
        with patch.object(ChatClient, "_request", autospec=True, side_effect=response):
            notes, concise = summarize(dialogue(), "对话", config, self.folder, lambda *a: None)
            summarize(dialogue(), "对话", config, self.folder, lambda *a: None)
            self.assertEqual(len(calls), 1)
            changed = dialogue(); changed[1]["text"] = "不能，先测试。"
            summarize(changed, "对话", config, self.folder, lambda *a: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(notes["recordingRevision"], "original")
        self.assertEqual([s["speaker"] for s in concise], ["A", "B", "A"])

    def test_long_dialogue_resume_reuses_completed_chapters_and_keeps_turn_coverage(self):
        values = [{"id": i, "start": i * 25., "end": i * 25. + 24., "speaker": "A" if i % 2 == 0 else "B",
                   "text": ("这个提议需要先确认前提，不能理解为已经同意。" if i % 2 else "如果测试通过我们才讨论下一步安排。") * 5} for i in range(120)]
        rows = paragraphs(values, dialogue=True)
        config = {"baseURL": "https://example.com/v1", "model": "test", "apiKey": "test-only", "sourceKind": "recording",
                  "summaryStrategy": "structured", "summaryReview": False, "concurrency": 1}
        phases = []
        fail = True
        def response(client, body, phase="summary"):
            nonlocal fail
            if fail and phase == "chapter" and phases.count("chapter") == 1:
                fail = False
                raise UserError("受控章节中断")
            phases.append(phase)
            payload = json.loads(body["messages"][1]["content"])
            if phase == "plan":
                first, last = payload["first"], payload["last"]
                middle = (first + last) // 2
                value = {"theme": "前提与安排", "chapters": [{"first": first, "last": middle, "title": "前提", "focus": "双方条件"},
                         {"first": middle + 1, "last": last, "title": "安排", "focus": "未决定事项"}], "terms": [], "connections": [], "uncertainties": []}
            elif phase == "chapter":
                first, last = payload["owned_first"], payload["owned_last"]
                value = {"claims": [{"p": [first], "text": "[A]与[B]尚未达成无条件决定。"}],
                         "condensed": [{"first": i, "last": i, "speaker": rows[i]["speaker"], "text": rows[i]["text"]} for i in range(first, last + 1)]}
            elif phase == "synthesis":
                value = {"overview": "双方讨论了条件。", "key_points": [{"claim_ids": [payload["claims"][0]["id"]], "text": "尚未形成无条件决定。"}],
                         "uncertainties": [], "resolved_findings": []}
            else:
                self.fail(phase)
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}
        with patch.object(ChatClient, "_request", autospec=True, side_effect=response):
            with self.assertRaises(UserError): summarize(values, "50分钟对话", config, self.folder, lambda *a: None)
            result, concise = summarize(values, "50分钟对话", config, self.folder, lambda *a: None)
        self.assertEqual(phases.count("plan"), 1)
        self.assertEqual(result["summaryRoute"], "planned")
        self.assertGreater(read_json(self.folder / "summary-run.json")["checkpointHits"], 0)
        self.assertEqual([i for item in concise for i in item["source_ids"]], list(range(120)))
        self.assertEqual([item["speaker"] for item in concise], [item["speaker"] for item in values])


if __name__ == "__main__":
    unittest.main()

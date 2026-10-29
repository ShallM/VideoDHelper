"""Bounded meeting notes workflow; v2 remains a frozen comparison engine.

The model proposes cited records and repairs. Code owns stage boundaries,
publication, provenance, deduplication and budgets. No external tools or agents.
"""
import copy
import time

from common import UserError, digest, write_json
from dialogue_context import source_text, select_context
from dialogue_reader import Reader, condensed_batches, citations, validate_reader_text
from meeting_contract import (VERSION as CONTRACT_VERSION, FIELDS, ISSUE_KINDS, DRAFT_PROMPT,
    EVIDENCE_PROMPT, REVIEW_PROMPT, REREAD_PROMPT, PUNCTUATE_PROMPT, validate_document,
    validate_item, validate_items, punctuation_rows, render_item, string, anchor)
from summary import ChatClient, InputContextLimit, ResponseOutputLimit

VERSION = "meeting-reader-v3.7"
PROMPT_VERSION = "meeting-prompts-v1.3"
STRATEGIES = {"meeting-v3", "meeting-baseline"}
PART_BUDGET = 240
VALIDATION_FEEDBACK_CHARS = 1600


def compact_evidence(value):
    """Keep provenance references without repeating expanded source quotations.

    Persisted evidence stays expanded. Hierarchical merge may only consolidate
    these existing records; its validator checks anchors against the immutable
    source and the exact parent evidence set. The review reads the source again.
    """
    if isinstance(value, list):
        return [compact_evidence(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, content in value.items():
        if key == "evidence":
            result[key] = [{k: proof[k] for k in ("unit", "first_part", "last_part")}
                           if all(type(proof.get(k)) is int for k in ("unit", "first_part", "last_part"))
                           else copy.deepcopy(proof) for proof in content]
        else:
            result[key] = compact_evidence(content)
    return result


class MeetingReader(Reader):
    def __init__(self, segments, title, config, folder, emit):
        super().__init__(segments, title, config, folder, emit)
        self.strategy = config.get("dialogueStrategy", "meeting-v3")
        if self.strategy not in STRATEGIES:
            raise UserError("未知会议纪要流程")
        self.identity = digest({"base": self.identity, "strategy": self.strategy, "version": VERSION,
            "contract": CONTRACT_VERSION, "promptVersion": PROMPT_VERSION, "partBudget": PART_BUDGET,
            "validationFeedbackChars": VALIDATION_FEEDBACK_CHARS,
            "maxRereads": self.max_rereads,
            "prompts": [DRAFT_PROMPT, EVIDENCE_PROMPT, REVIEW_PROMPT, REREAD_PROMPT, PUNCTUATE_PROMPT]})
        self.config["summaryVersion"] = VERSION
        self.config["validationFeedbackChars"] = VALIDATION_FEEDBACK_CHARS
        self.client = ChatClient(self.config, self.folder / "checkpoints" / "meeting-v3" / self.identity, emit)
        self.checks, self.assessments, self.retrieval_cache = {}, {}, {}
        self.retrieval_hits = self.duplicate_issues = self.no_progress = 0
        self.evidence_batches, self.review_batches = 0, 0
        self.source_coverage, self.stage_times = [], {}
        self.review_target_owners = {}
        self.publication = False
        self.validation_failures = []
        self.validation_failure_count = 0

    def call(self, prompt, payload, validator, phase):
        def checked(value):
            try:
                return validator(value)
            except (ValueError, KeyError, TypeError) as error:
                # Keep only the proposed product, never the model's reasoning
                # or request credentials. A failed repair must be diagnosable.
                self.validation_failures.append({"phase": phase, "error": str(error)[:VALIDATION_FEEDBACK_CHARS],
                                                 "candidate": copy.deepcopy(value)})
                self.validation_failure_count += 1
                self.validation_failures = self.validation_failures[-12:]
                write_json(self.folder / "meeting-validation.json", {"identity": self.identity,
                           "failureCount": self.validation_failure_count, "failures": self.validation_failures})
                raise
        return super().call(prompt, payload, checked, phase)

    def draft(self, units, depth=0):
        payload = {"title": self.title, "sources": source_text(units)}
        try:
            return self.call(DRAFT_PROMPT, payload, lambda v: validate_document(v, units), "meeting_draft")
        except InputContextLimit:
            if len(units) < 2 or depth >= 12:
                raise UserError("完整阅读单元超过输入容量，已保留阶段；未截断原稿") from None
            self.route = "hierarchical"
            middle = len(units) // 2
            documents = [self.draft(units[:middle], depth + 1), self.draft(units[middle:], depth + 1)]
            anchors = {(p["unit"], p["start"], p["end"]) for d in documents
                       for i in [*(c for ch in d["chapters"] for c in ch["claims"]), *d["action_items"]]
                       for p in i["evidence"]}
            merged = {"title": self.title, "partial_documents": compact_evidence(documents),
                      "allowed_u": [u["id"] for u in units], "sources": ""}
            def validate(value):
                validate_document(value, units)
                for item in [*(c for ch in value["chapters"] for c in ch["claims"]), *value["action_items"]]:
                    if any((p["unit"], p["start"], p["end"]) not in anchors for p in item["evidence"]):
                        raise ValueError("分层合并只能使用已读取候选的逐字证据，不能新增引文")
            try:
                return self.call(DRAFT_PROMPT, merged, validate, "meeting_merge")
            except InputContextLimit:
                raise UserError("结构化候选纪要超过合并容量；已保留各范围结果，没有静默删减") from None

    def inventory(self, units, depth=0):
        ids = {u["id"] for u in units}
        neighbours = [u for u in select_context(self.units, sorted(ids)) if u["id"] not in ids]
        source_units = units + neighbours
        payload = {"title": self.title, "sources": source_text(units), "owned_units": sorted(ids), "context": source_text(neighbours)}
        def validate(value):
            if not isinstance(value.get("items"), list) or len(value["items"]) > 600:
                raise ValueError("items须为有限关键事实列表")
            validate_items(((f"items[{i}]", item, isinstance(item, dict) and item.get("kind") == "action") for i, item in enumerate(value["items"])),
                           {u["id"]: u for u in source_units})
            for item in value["items"]:
                if not ids.intersection(item["u"]):
                    raise ValueError("核对项必须涉及本批owned_units，不能只复述邻接背景")
            coverage = value.get("coverage")
            if (not isinstance(coverage, list) or len(coverage) != len(ids)
                    or any(type(c.get("unit")) is not int for c in coverage)
                    or {c["unit"] for c in coverage} != ids):
                raise ValueError("coverage必须恰好覆盖每个owned_units一次")
            for c in coverage:
                if c.get("disposition") not in ("substantive", "incidental"):
                    raise ValueError("disposition须为substantive或incidental")
                if c["disposition"] == "substantive" and not any(c["unit"] in i["u"] for i in value["items"]):
                    raise ValueError("有实质信息的单元必须关联至少一项逐字证据")
        try:
            value = self.call(EVIDENCE_PROMPT, payload, validate, "meeting_evidence")
        except InputContextLimit:
            if len(units) < 2 or depth >= 12:
                raise UserError("关键证据提取超过输入容量；已保存成功范围") from None
            middle = len(units) // 2
            self.inventory(units[:middle], depth + 1); self.inventory(units[middle:], depth + 1)
            return
        self.evidence_batches += 1
        self.source_coverage.extend(value["coverage"])
        for item in value["items"]:
            identity = "check-" + digest({k: item[k] for k in ("text", "kind", "state", "evidence")})[:20]
            self.checks.setdefault(identity, {**copy.deepcopy(item), "id": identity})

    def punctuate(self, units, depth=0):
        if depth == 0:
            groups = condensed_batches(units, max_parts=PART_BUDGET)
            if len(groups) > 1:
                rows = []
                for index, group in enumerate(groups, 1):
                    self.emit("condense", f"整理阅读行与标点 · 第 {index}/{len(groups)} 批，保留全部原话…")
                    rows.extend(self.punctuate(group, 1))
                return rows
        ids = [p["id"] for u in units for p in u["parts"]]
        context_ids = {u["id"] for u in units}
        neighbours = [u for u in select_context(self.units, sorted(context_ids)) if u["id"] not in context_ids]
        payload = {"sources": source_text(units), "owned_parts": ids, "context": source_text(neighbours)}
        try:
            value = self.call(PUNCTUATE_PROMPT, payload, lambda v: punctuation_rows(v, units, all_units=self.units), "meeting_punctuation")
        except (InputContextLimit, ResponseOutputLimit):
            if len(units) < 2 or depth >= 12:
                raise
            middle = len(units) // 2
            return self.punctuate(units[:middle], depth + 1) + self.punctuate(units[middle:], depth + 1)
        self.condense_batches += 1
        return punctuation_rows(value, units, all_units=self.units)

    def identify(self, document):
        self.document = copy.deepcopy(document)
        self.document["overview"]["id"] = "overview"
        index = 0
        for ci, chapter in enumerate(self.document["chapters"]):
            chapter["id"] = f"topic-{ci}"
            for item in chapter["claims"]:
                item["id"] = f"claim-{index}"; index += 1
        for i, item in enumerate(self.document["action_items"]):
            item["id"] = f"action-{i}"
        for i, item in enumerate(self.document["uncertainties"]):
            item["id"] = f"uncertainty-{i}"

    def meeting_targets(self):
        result = {"overview": {"id": "overview", "target_kind": "overview", "value": self.document["overview"]}}
        for chapter in self.document["chapters"]:
            for item in chapter["claims"]:
                result[item["id"]] = {"id": item["id"], "target_kind": "claim", "chapter_id": chapter["id"], "value": item}
        for key, kind in (("action_items", "action"), ("uncertainties", "uncertainty")):
            for item in self.document[key]:
                result[item["id"]] = {"id": item["id"], "target_kind": kind, "value": item}
        return result

    def validate_repair(self, value, units, targets, owned_targets, owned_checks, *, reread=False):
        allowed = {u["id"]: u for u in units}
        for key, maximum in (("patches", 60), ("additions", 60), ("issues", 24), ("assessments", 600)):
            if not isinstance(value.get(key), list) or len(value[key]) > maximum:
                raise ValueError(f"{key}必须是有限列表")
        if value.get("unhandled_errors"):
            raise UserError("模型仍报告未处理的核验问题；阶段已保存，未把不完整纪要标为成功")
        if reread and (value["issues"] or value["assessments"] or value["additions"]):
            raise ValueError("定向回读不能另起核验、补充无关主题或登记新疑点")
        seen = set()
        for assessment in value["assessments"]:
            cid = assessment.get("check_id")
            if cid not in owned_checks or cid in seen or assessment.get("status") not in ("covered", "omitted", "uncertain", "invalid"):
                raise ValueError("assessments须逐项覆盖负责的check_id且状态合法")
            seen.add(cid)
            target_ids = assessment.get("target_ids")
            if not isinstance(target_ids, list) or any(i not in targets for i in target_ids):
                raise ValueError("assessment引用了不存在的声明")
            if assessment["status"] == "covered" and not target_ids:
                raise ValueError("covered必须指向实际保留该事实的声明")
            string(assessment.get("reason"))
        if seen != set(owned_checks):
            raise ValueError("assessments必须恰好覆盖owned_checks一次")
        mutated = set()
        for patch in value["patches"]:
            tid = patch.get("target_id")
            if tid not in owned_targets or tid in mutated or patch.get("action") not in ("replace", "remove"):
                raise ValueError("补丁只能修改本批负责目标，每个目标一次")
            mutated.add(tid); string(patch.get("reason"))
            if not isinstance(patch.get("check_ids"), list) or any(i not in self.checks for i in patch["check_ids"]):
                raise ValueError("补丁check_ids必须来自现有核对清单")
            target = targets[tid]
            if patch["action"] == "remove":
                if target["target_kind"] == "overview" or patch.get("replacement") is not None:
                    raise ValueError("不能删除概览；remove的replacement须为null")
                continue
            replacement = patch["replacement"]
            if target["target_kind"] in ("overview", "uncertainty"):
                citations(replacement, allowed); string(replacement.get("text"), maximum=500 if tid == "overview" else 4000)
                validate_reader_text(replacement["text"], [allowed[i]["text"] for i in replacement["u"]])
                if target["target_kind"] == "uncertainty":
                    if replacement.get("status") not in ("unresolved", "needs_audio"):
                        raise ValueError("待确认条目需要合法status")
                    if target["value"].get("status") == "needs_audio" and replacement["status"] != "needs_audio":
                        raise ValueError("需回听事项不能仅凭文字关闭")
            else:
                validate_item(replacement, allowed, action=target["target_kind"] == "action")
        chapters = {c["id"] for c in self.document["chapters"]} | {"actions", "supplement"}
        for addition in value["additions"]:
            if addition.get("chapter_id") not in chapters:
                raise ValueError("新增内容需选择已有章节、actions或supplement")
            if not isinstance(addition.get("check_ids"), list) or not addition["check_ids"] or any(i not in owned_checks for i in addition["check_ids"]):
                raise ValueError("补充须由本批独立核对项支持")
            string(addition.get("reason"))
            validate_item(addition["item"], allowed, action=addition["chapter_id"] == "actions")
        for issue in value["issues"]:
            if issue.get("target_id") not in set(targets) | set(owned_checks) or issue.get("field") not in FIELDS or issue.get("kind") not in ISSUE_KINDS:
                raise ValueError("疑点必须关联现有声明/核对项及明确字段")
            if issue.get("status") not in ("unresolved", "needs_audio"):
                raise ValueError("疑点状态无效")
            string(issue.get("question")); citations(issue, allowed)
            validate_reader_text(issue["question"], [allowed[i]["text"] for i in issue["u"]])
            if not isinstance(issue.get("query"), str) or len(issue["query"]) > 160:
                raise ValueError("query须为较短字面检索词或空字符串")
        if reread:
            if value.get("status") not in ("resolved", "unresolved", "needs_audio"):
                raise ValueError("回读须明确是否解决或需回听")
            string(value.get("reason"))
            if value["status"] == "resolved" and not value["patches"]:
                if not value.get("evidence"):
                    raise ValueError("无正文修订的resolved也须提供evidence逐字证据")
                for proof in value["evidence"]:
                    anchor(proof, allowed)

    def apply_repair(self, value, phase):
        before = copy.deepcopy(self.document)
        updates = {p["target_id"]: p for p in value["patches"]}
        def update(item):
            patch = updates.get(item["id"])
            if not patch:
                return item
            if patch["action"] == "remove":
                return None
            return {**copy.deepcopy(patch["replacement"]), "id": item["id"]}
        self.document["overview"] = update(self.document["overview"])
        for chapter in self.document["chapters"]:
            chapter["claims"] = [r for i in chapter["claims"] if (r := update(i)) is not None]
        for key in ("action_items", "uncertainties"):
            self.document[key] = [r for i in self.document[key] if (r := update(i)) is not None]
        for addition in value["additions"]:
            identity = "added-" + digest({"checks": sorted(addition["check_ids"]), "item": addition["item"]})[:20]
            if identity in self.meeting_targets():
                continue
            item = {**copy.deepcopy(addition["item"]), "id": identity}
            if addition["chapter_id"] == "actions":
                self.document["action_items"].append(item)
            else:
                if addition["chapter_id"] == "supplement" and not any(c["id"] == "supplement" for c in self.document["chapters"]):
                    self.document["chapters"].append({"id": "supplement", "title": "其他重要信息", "claims": []})
                next(c for c in self.document["chapters"] if c["id"] == addition["chapter_id"])["claims"].append(item)
        for issue in value["issues"]:
            identity = "issue-" + digest({"target": issue["target_id"], "field": issue["field"]})[:20]
            if identity in self.issues:
                self.duplicate_issues += 1
                if issue["status"] == "needs_audio":
                    self.issues[identity]["status"] = "needs_audio"
                self.issues[identity]["u"] = sorted(set(self.issues[identity]["u"]) | set(issue["u"]))
            else:
                self.issues[identity] = {**copy.deepcopy(issue), "id": identity}
        self.assessments.update({a["check_id"]: a for a in value["assessments"]})
        changed = before != self.document
        self.patch_log.append({"phase": phase, "before": before, "proposal": copy.deepcopy(value), "after": copy.deepcopy(self.document), "changed": changed})
        return changed

    def review_range(self, owned, depth=0):
        ids = {u["id"] for u in owned}
        targets = self.meeting_targets()
        if depth == 0:
            self.review_target_owners = {k: min(t["value"]["u"]) for k, t in targets.items()}
        checks = {k: v for k, v in self.checks.items() if min(v["u"]) in ids}
        # A correction can move its citation to a later unit. Keep the original
        # responsibility assignment so later batches do not review it twice.
        target_ids = {k for k in targets if self.review_target_owners.get(k) in ids}
        if not checks and not target_ids:
            self.reviewed.update(ids)
            return
        payload = {"sources": source_text(self.units), "checks": list(checks.values()), "owned_checks": sorted(checks),
                   "targets": list(targets.values()), "owned_targets": sorted(target_ids),
                   "chapters": [{"id": c["id"], "title": c["title"]} for c in self.document["chapters"]],
                   "known_issues": list(self.issues.values())}
        units = self.units
        if depth:
            related = ids | {i for t in target_ids for i in targets[t]["value"]["u"]} | {i for c in checks.values() for i in c["u"]}
            units = select_context(self.units, sorted(related))
            visible = {u["id"] for u in units}
            targets = {k: v for k, v in targets.items() if k in target_ids or set(v["value"]["u"]).issubset(visible)}
            payload.update(sources=source_text(units), targets=list(targets.values()))
        try:
            value = self.call(REVIEW_PROMPT, compact_evidence(payload),
                lambda v: self.validate_repair(v, units, targets, target_ids, checks), "meeting_review")
        except InputContextLimit:
            if len(owned) < 2 or depth >= 12:
                raise UserError("核验所需原文与跨段证据超过容量；未丢弃证据或标为完成") from None
            middle = len(owned) // 2
            self.review_range(owned[:middle], depth + 1); self.review_range(owned[middle:], depth + 1)
            return
        self.apply_repair(value, "review")
        self.reviewed.update(ids); self.review_batches += 1

    def read_sources(self, ids, query):
        ids = sorted(set(ids) | {u["id"] for u in self.units if query and query.casefold() in u["text"].casefold()})
        selected = select_context(self.units, ids)
        key = digest({"source": self.source_revision, "units": [u["id"] for u in selected]})
        if key in self.retrieval_cache:
            self.retrieval_hits += 1
        else:
            self.retrieval_cache[key] = source_text(selected)
        return selected, self.retrieval_cache[key]

    def reread(self):
        # No follow-up issues can be created by the reread response.
        priorities = {k: i for i, k in enumerate(("negation", "state", "condition", "scope", "attribution", "number", "omission", "unsupported", "correction", "duplicate", "audio"))}
        for round_index in range(2):
            pending = sorted((i for i in self.issues.values() if i["status"] == "unresolved" and i["id"] not in self.reread_keys),
                             key=lambda i: (priorities[i["kind"]], i["id"]))[:min(3, self.max_rereads - len(self.reread_keys))]
            for issue in pending:
                self.reread_keys.add(issue["id"])
                # Evidence in the same range is context, not permission to edit
                # every nearby claim. A check may link to more than one claim.
                linked = {issue["target_id"]} | set(self.assessments.get(issue["target_id"], {}).get("target_ids", []))
                targets = {k: t for k, t in self.meeting_targets().items() if k in linked}
                related = set(issue["u"]) | {i for t in targets.values() for i in t["value"]["u"]}
                units, source = self.read_sources(sorted(related), issue["query"])
                payload = {"issue": issue, "sources": source, "targets": list(targets.values())}
                self.emit("reread", f"核对具体疑点 · 第 {round_index + 1} 轮，已处理 {len(self.reread_keys)} 项…")
                try:
                    value = self.call(REREAD_PROMPT, compact_evidence(payload),
                        lambda v: self.validate_repair(v, units, targets, set(targets), {}, reread=True), "meeting_reread")
                except InputContextLimit:
                    issue["diagnostic"] = "evidence_exceeds_budget"
                    continue
                self.reread_count += 1
                changed = self.apply_repair(value, "reread")
                issue.update(status=value["status"], resolution=value["reason"])
                if not changed and value["status"] != "resolved":
                    self.no_progress += 1
                    issue["diagnostic"] = "no_information_gain"

    def cited(self, item, *, statement=True):
        if statement:
            selected = {pid for proof in item["evidence"] for pid in proof["parts"]}
            source_ids = sorted({sid for uid in item["u"] for part in self.lookup[uid]["parts"]
                                 if part["id"] in selected for sid in part["source_ids"]})
        else:
            source_ids = sorted({sid for uid in item["u"] for sid in self.lookup[uid]["source_ids"]})
        value = {"id": item["id"], "text": render_item(item) if statement else item["text"],
                 "source_ids": source_ids}
        if statement:
            value["metadata"] = {k: copy.deepcopy(item[k]) for k in ("text", "kind", "state", "conditions", "scope", "speaker", "attribution_parts", "owner", "due", "evidence")}
        if item.get("status"):
            value["status"] = item["status"]
        return value

    def outputs(self):
        chapters = []
        for c in self.document["chapters"]:
            if not c["claims"]:
                continue
            claims = [self.cited(i) for i in c["claims"]]
            chapters.append({"title": c["title"], "claims": claims, "detail": "\n\n".join(i["text"] for i in claims),
                             "source_ids": sorted({s for i in claims for s in i["source_ids"]})})
        if not chapters:
            raise UserError("核验后没有可支持的主题，未发布空纪要")
        questions = [self.cited(i, statement=False) for i in self.document["uncertainties"]]
        for issue in self.issues.values():
            if issue["status"] != "resolved":
                question = self.cited({**issue, "text": issue["question"]}, statement=False)
                if not any(i["text"] == question["text"] and i["source_ids"] == question["source_ids"] for i in questions):
                    questions.append(question)
        notes = {"overview": self.document["overview"]["text"], "overviewSourceIDs": self.cited(self.document["overview"], statement=False)["source_ids"],
                 "chapters": chapters, "key_points": [], "action_items": [self.cited(i) for i in self.document["action_items"]], "uncertainties": questions,
                 "model": self.config["model"], "tokensThisRun": self.client.total_tokens, "summaryStrategy": self.strategy,
                 "summaryRoute": self.route, "summaryVersion": VERSION, "summaryPromptVersion": PROMPT_VERSION,
                 "reviewedParagraphs": len(self.reviewed), "paragraphCount": len(self.units), "rereadCount": self.reread_count,
                 "sourceKind": "recording", "sourceRevision": self.source_revision, "recordingRevision": self.config.get("recordingRevision", "original"),
                 "evidenceItemCount": len(self.checks), "assessmentCount": len(self.assessments)}
        rows = []
        for index, row in enumerate(self.dialogue):
            parts = [{"id": f"part-{p['id']}", "speaker": p["speaker"], "text": p["text"], "source_ids": p["source_ids"],
                      "spans": p["spans"], "punctuationInsertions": p["punctuation_insertions"]} for p in row["parts"]]
            rows.append({"id": f"row-{index}", "text": "".join(p["text"] for p in parts), "parts": parts, "continuousReading": True,
                         "source_ids": sorted({s for p in parts for s in p["source_ids"]})})
        return notes, rows

    def stage(self, name, operation):
        started = time.monotonic()
        try:
            return operation()
        finally:
            self.stage_times[name] = round(time.monotonic() - started, 3)

    def run(self):
        complete = False
        try:
            write_json(self.folder / "meeting-context.json", {"identity": self.identity, "units": self.units})
            self.emit("summary", "全文阅读，生成详细主题纪要与明确行动…")
            self.identify(self.stage("draft", lambda: self.draft(self.units)))
            write_json(self.folder / "meeting-draft.json", {"identity": self.identity, "document": self.document})
            if self.strategy == "meeting-v3":
                self.emit("review", "独立阅读原文，整理关键事实、状态与条件证据…")
                self.stage("evidence", lambda: self.inventory(self.units))
                write_json(self.folder / "meeting-evidence.json", {"identity": self.identity, "checks": list(self.checks.values()), "coverage": self.source_coverage})
                self.emit("review", "对照原文与独立证据，检查遗漏、状态和归属…")
                self.stage("review", lambda: self.review_range(self.units))
                self.stage("reread", self.reread)
            self.emit("condense", "整理阅读行与标点，保留所有原话和局部归属…")
            self.dialogue = self.stage("punctuation", lambda: self.punctuate(self.units))
            notes, rows = self.outputs()
            if callback := self.config.get("beforePublish"):
                callback()
            write_json(self.folder / "dialogue-results.json", {"schemaVersion": 2, "generationID": self.identity,
                       "strategy": self.strategy, "notes": notes, "condensed": rows})
            self.publication = True
            for name, value in (("notes.json", notes), ("condensed.json", rows), ("chapters.json", notes["chapters"])):
                write_json(self.folder / name, value)
            complete = True
            return notes, rows
        finally:
            write_json(self.folder / "meeting-review.json", {"identity": self.identity, "assessments": list(self.assessments.values()),
                       "issues": list(self.issues.values()), "repairs": self.patch_log, "reviewedUnits": sorted(self.reviewed),
                       "targetOwnership": self.review_target_owners,
                       "readUnits": list(self.retrieval_cache), "rereadKeys": sorted(self.reread_keys)})
            write_json(self.folder / "summary-run.json", {"version": VERSION, "strategy": self.strategy, "promptVersion": PROMPT_VERSION,
                       "identity": self.identity, "completed": complete, "published": self.publication, "route": self.route,
                       "elapsedSeconds": round(time.monotonic() - self.started, 3), "stageSeconds": self.stage_times,
                       "requests": self.client.request_count, "checkpointHits": self.client.cache_hits, "limitCheckpointHits": self.client.limit_hits,
                       "tokensThisRun": self.client.total_tokens, "calls": self.client.calls, "requestBudgets": self.request_estimates,
                       "sourceParagraphs": len(self.units), "reviewedParagraphs": len(self.reviewed), "evidenceItems": len(self.checks),
                       "assessments": len(self.assessments), "evidenceBatches": self.evidence_batches, "reviewBatches": self.review_batches,
                       "rereadCount": self.reread_count, "duplicateIssues": self.duplicate_issues, "retrievalCacheHits": self.retrieval_hits,
                       "noProgressRereads": self.no_progress, "condenseBatches": self.condense_batches,
                       "validationFailures": self.validation_failure_count,
                       "changedRepairBatches": sum(p["changed"] for p in self.patch_log)})


def summarize(segments, title, config, folder, emit):
    return MeetingReader(segments, title, config, folder, emit).run()

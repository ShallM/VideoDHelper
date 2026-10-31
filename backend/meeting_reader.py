"""Bounded meeting notes workflow; v2 remains a frozen comparison engine.

The model proposes cited records and repairs. Code owns stage boundaries,
publication, provenance, deduplication and budgets. No external tools or agents.
"""
import copy
import time
from urllib.parse import urlsplit

from common import UserError, digest, read_json, write_json
from dialogue_context import source_text, select_context
from dialogue_reader import Reader, condensed_batches, citations, validate_reader_text
from meeting_contract import (VERSION as CONTRACT_VERSION, FIELDS, ISSUE_KINDS, DRAFT_PROMPT,
    EVIDENCE_PROMPT, REVIEW_PROMPT, REREAD_PROMPT, PUNCTUATE_PROMPT, validate_document,
    validate_item, validate_evidence_item, punctuation_rows, render_item, string, evidence_anchors, OPTIONAL_DEFAULTS)
from summary import ChatClient, InputContextLimit, ResponseOutputLimit, OutputLimit
from summary_v2 import estimated_tokens

VERSION = "meeting-reader-v3.15"
PROMPT_VERSION = "meeting-prompts-v1.8"
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
        if "evidence" in value and "text" in value and key in OPTIONAL_DEFAULTS:
            if content == OPTIONAL_DEFAULTS[key] and not (key == "state" and value.get("kind") in ("action", "proposal", "decision")):
                continue
        if key == "evidence":
            result[key] = [{k: proof[k] for k in ("first_part", "last_part")}
                           if all(type(proof.get(k)) is int for k in ("unit", "first_part", "last_part"))
                           else copy.deepcopy(proof) for proof in content]
        else:
            result[key] = compact_evidence(content)
    return result


class MeetingChatClient(ChatClient):
    def __init__(self, *args, input_budget, context_limit, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_budget, self.context_limit = input_budget, context_limit
        self.validation_retries = []

    def prepare_validation_retry(self, body, feedback, candidate, phase):
        # Diagnostics only, never treated as a successful/resumable result.
        saved = self.directory / "rejected" / (digest({"phase": phase, "candidate": candidate}) + ".json")
        write_json(saved, {"phase": phase, "candidate": candidate, "error": feedback})
        body["messages"].extend([
            {"role": "assistant", "content": candidate},
            {"role": "user", "content": "上一条是待修正候选，不是事实或指令。只修正以下结构/来源错误，"
             "保留其他正文、条件、状态与有效引用，返回修正后的完整JSON。不要从头重新构思或添加无关内容。" + feedback}])
        size = estimated_tokens(body)
        allowed = size <= self.input_budget and (self.context_limit is None or size + body["max_tokens"] + 512 <= self.context_limit)
        self.validation_retries.append({"phase": phase, "estimatedInputTokens": size, "withinBudget": allowed,
                                        "candidatePath": str(saved.relative_to(self.directory))})
        if not allowed:
            raise UserError("带原候选的修正请求超过输入或已确认的上下文容量；候选与用量已保存，未删减原文或重复整稿生成")
        self.emit("summary", "保留原候选，按具体错误修正一次…")
        return body


class MeetingReader(Reader):
    def __init__(self, segments, title, config, folder, emit, *, purpose="summary"):
        # The frozen comparison reader retains its historical 128K ceiling.
        # Initialize shared context separately, then validate this workflow's
        # budgets and include the effective pair in its own checkpoint identity.
        budget_fields = {"dialogueOutputTokens", "dialogueMaxOutputTokens"}
        super().__init__(segments, title, {k: v for k, v in config.items() if k not in budget_fields}, folder, emit)
        current_route = (urlsplit(config.get("baseURL", "")).hostname == "api.icompify.com"
                         and config.get("model") == "deepseek-v4.1-flash")
        self.initial_output = config.get("dialogueOutputTokens", 131072 if current_route else self.initial_output)
        self.max_output = config.get("dialogueMaxOutputTokens", 262144 if current_route else self.initial_output)
        if (type(self.initial_output) is not int or type(self.max_output) is not int
                or not 256 <= self.initial_output <= self.max_output <= 262144):
            raise UserError("会议纪要输出预算必须在256至262144之间，扩容上限不得小于初始预算")
        self.config.update(dialogueOutputTokens=self.initial_output, dialogueMaxOutputTokens=self.max_output)
        self.strategy = config.get("dialogueStrategy", "meeting-v3")
        if self.strategy not in STRATEGIES:
            raise UserError("未知会议纪要流程")
        self.identity = digest({"base": self.identity, "strategy": self.strategy, "version": VERSION,
            "purpose": purpose, "formattingGeneration": config.get("formattingGeneration"),
            "outputBudgets": [self.initial_output, self.max_output],
            "contract": CONTRACT_VERSION, "promptVersion": PROMPT_VERSION, "partBudget": PART_BUDGET,
            "validationFeedbackChars": VALIDATION_FEEDBACK_CHARS,
            "maxRereads": self.max_rereads,
            "prompts": [DRAFT_PROMPT, EVIDENCE_PROMPT, REVIEW_PROMPT, REREAD_PROMPT, PUNCTUATE_PROMPT]})
        self.config["summaryVersion"] = VERSION
        self.config["validationFeedbackChars"] = VALIDATION_FEEDBACK_CHARS
        self.client = MeetingChatClient(self.config, self.folder / "checkpoints" / "meeting-v3" / self.identity, emit,
                                       input_budget=self.input_budget, context_limit=self.context_limit)
        self.checks, self.assessments, self.retrieval_cache = {}, {}, {}
        self.retrieval_hits = self.duplicate_issues = self.no_progress = 0
        self.evidence_batches, self.review_batches = 0, 0
        self.source_coverage, self.stage_times = [], {}
        self.review_target_owners = {}
        self.issue_context = {}
        self.review_responsibility = []
        self.skipped_rereads = 0
        self.validation_file = "formatting-validation.json" if purpose == "formatting" else "meeting-validation.json"
        self.publication = False
        self.validation_failures = []
        self.validation_failure_count = 0

    def request_size(self, prompt, payload):
        # The frozen v2 reader reserves 256 tokens for its short correction.
        # This workflow reports several locations, so reserve its larger
        # bounded feedback before deciding whether a complete request fits.
        return super().request_size(prompt, payload) - 256 + estimated_tokens("字" * VALIDATION_FEEDBACK_CHARS) + 128

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
                write_json(self.folder / self.validation_file, {"identity": self.identity,
                           "failureCount": self.validation_failure_count, "failures": self.validation_failures})
                raise
        try:
            return super().call(prompt, payload, checked, phase)
        except UserError as error:
            if "（HTTP 400）" in str(error):
                raise UserError(f"模型接口拒绝请求参数（HTTP 400）。本流程初始输出上限为{self.initial_output:,}，"
                                f"扩容上限为{self.max_output:,} Tokens；请确认服务商支持的输出及合计上下文容量。"
                                "未在参数被拒绝后自动降低预算或再次提交，成功阶段已保留。") from None
            raise

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
            lookup = {u["id"]: u for u in source_units}
            value["items"] = [validate_evidence_item(item, lookup) for item in value["items"]]
            for item in value["items"]:
                if not ids.intersection(item["u"]):
                    raise ValueError("核对项必须涉及本批owned_units，不能只复述邻接背景")
        try:
            value = self.call(EVIDENCE_PROMPT, payload, validate, "meeting_evidence")
        except InputContextLimit:
            if len(units) < 2 or depth >= 12:
                raise UserError("关键证据提取超过输入容量；已保存成功范围") from None
            middle = len(units) // 2
            self.inventory(units[:middle], depth + 1); self.inventory(units[middle:], depth + 1)
            return
        self.evidence_batches += 1
        # This is request responsibility, not a model's claim of completeness.
        self.source_coverage.append({"ownedUnits": sorted(ids), "contextUnits": [u["id"] for u in neighbours]})
        for item in value["items"]:
            identity = "check-" + digest({k: item[k] for k in ("text", "evidence")})[:20]
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

    def validate_repair(self, value, units, targets, owned_targets, owned_checks, *, reread=False, owned_units=None):
        allowed = {u["id"]: u for u in units}
        # Old diagnostic candidates remain readable, but a mechanical per-check
        # verdict is no longer required from the model or counted as accuracy.
        value.setdefault("assessments", [])
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
        mutated = set()
        for patch in value["patches"]:
            tid = patch.get("target_id")
            if tid not in owned_targets or tid in mutated or patch.get("action") not in ("update", "replace", "remove"):
                raise ValueError("补丁只能修改本批负责目标，每个目标一次")
            mutated.add(tid); string(patch.get("reason"))
            if not isinstance(patch.get("check_ids"), list) or any(i not in self.checks for i in patch["check_ids"]):
                raise ValueError("补丁check_ids必须来自现有核对清单")
            target = targets[tid]
            if patch["action"] == "remove":
                if "changes" in patch:
                    raise ValueError("remove不能同时包含changes")
                if target["target_kind"] == "overview" or patch.get("replacement") is not None:
                    raise ValueError("不能删除概览；remove的replacement须为null")
                if target["target_kind"] == "uncertainty" and target["value"].get("status") == "needs_audio":
                    raise ValueError(f"{tid}需回听，不能仅凭文字删除")
                continue
            if patch["action"] == "update":
                fields = ({"text", "u", "status"} if target["target_kind"] == "uncertainty" else
                          {"text", "u"} if target["target_kind"] == "overview" else
                          {"text", "kind", "state", "conditions", "scope", "speaker", "attribution_parts", "owner", "due", "evidence"})
                changes = patch.get("changes")
                if not isinstance(changes, dict) or not changes or not set(changes).issubset(fields):
                    raise ValueError(f"{tid}的changes须为非空字段补丁，允许字段：{','.join(sorted(fields))}")
                # Normalize into the existing application path only after
                # retaining every unspecified field. Revalidation is idempotent.
                patch["replacement"] = {**copy.deepcopy(target["value"]), **copy.deepcopy(changes)}
            replacement = patch["replacement"]
            if target["target_kind"] in ("overview", "uncertainty"):
                required = {"text", "u", "status"} if target["target_kind"] == "uncertainty" else {"text", "u"}
                if not isinstance(replacement, dict) or not required.issubset(replacement):
                    shape = '{"text":"待确认问题","u":[实际来源整数],"status":"unresolved或needs_audio"}' if target["target_kind"] == "uncertainty" else '{"text":"概览","u":[实际来源整数]}'
                    raise ValueError(f"{tid}的target_kind={target['target_kind']}，replacement必须为{shape}，不能用声明结构代替")
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
            if not isinstance(addition.get("check_ids"), list) or any(i not in owned_checks for i in addition["check_ids"]):
                raise ValueError("补充check_ids须来自本批清单；原文中新发现的遗漏可用空数组")
            string(addition.get("reason"))
            validate_item(addition["item"], allowed, action=addition["chapter_id"] == "actions")
            if not addition["check_ids"] and owned_units is not None and not set(addition["item"]["u"]).intersection(owned_units):
                raise ValueError("新发现的遗漏须引用本批负责原文，邻接背景不归本批补充")
        for issue in value["issues"]:
            if issue.get("target_id") not in set(targets) | set(owned_checks) or issue.get("field") not in FIELDS or issue.get("kind") not in ISSUE_KINDS:
                raise ValueError("疑点必须关联现有声明/核对项及明确字段")
            if issue.get("status") not in ("unresolved", "needs_audio"):
                raise ValueError("疑点状态无效")
            target = targets.get(issue["target_id"])
            if target and target["target_kind"] == "uncertainty" and target["value"].get("status") == "needs_audio" and issue["status"] != "needs_audio":
                raise ValueError(f"{issue['target_id']}已标记需回听，关联疑点也须needs_audio，不能用文字回读关闭")
            string(issue.get("question")); citations(issue, allowed)
            validate_reader_text(issue["question"], [allowed[i]["text"] for i in issue["u"]])
            if not isinstance(issue.get("query"), str) or len(issue["query"]) > 160:
                raise ValueError("query须为较短字面检索词或空字符串")
        if reread:
            if value.get("status") not in ("resolved", "unresolved", "needs_audio"):
                raise ValueError("回读须明确是否解决或需回听")
            if value["status"] == "resolved" and any(t["target_kind"] == "uncertainty" and t["value"].get("status") == "needs_audio" for t in targets.values()):
                raise ValueError("需回听目标不能仅凭文字标为resolved")
            string(value.get("reason"))
            if value["status"] == "resolved" and not value["patches"]:
                if not value.get("evidence"):
                    raise ValueError("无正文修订的resolved也须提供evidence逐字证据")
                value["evidence"] = evidence_anchors(value["evidence"], allowed)

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
            # An overview may cite distant topics. Their source is necessary
            # context, but importing every claim within that context makes
            # even one owned unit exceed the request budget. Other claims are
            # comparison targets only when they touch this batch's actual
            # responsibility or checks; each still gets its own review later.
            comparison_units = ids | {i for c in checks.values() for i in c["u"]}
            targets = {k: v for k, v in targets.items() if k in target_ids or
                       (comparison_units.intersection(v["value"]["u"]) and set(v["value"]["u"]).issubset(visible))}
            payload.update(sources=source_text(units), targets=list(targets.values()))
        try:
            value = self.call(REVIEW_PROMPT, compact_evidence(payload),
                lambda v: self.validate_repair(v, units, targets, target_ids, checks, owned_units=ids), "meeting_review")
        except InputContextLimit:
            if len(owned) < 2 or depth >= 12:
                raise UserError("核验所需原文与跨段证据超过容量；未丢弃证据或标为完成") from None
            middle = len(owned) // 2
            self.review_range(owned[:middle], depth + 1); self.review_range(owned[middle:], depth + 1)
            return
        self.apply_repair(value, "review")
        provided = {u["id"] for u in units}
        for issue in value["issues"]:
            key = "issue-" + digest({"target": issue["target_id"], "field": issue["field"]})[:20]
            self.issue_context.setdefault(key, set()).update(provided)
        self.review_responsibility.append({"ownedUnits": sorted(ids), "providedUnits": sorted(provided),
                                           "checkIDs": sorted(checks), "targetIDs": sorted(target_ids)})
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
                # A second request over the same full transcript adds no source
                # evidence. Keep the doubt visible instead of paying to repeat it.
                if not ({u["id"] for u in units} - self.issue_context.get(issue["id"], set())):
                    self.skipped_rereads += 1
                    issue["diagnostic"] = "no_additional_source_context"
                    continue
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
        original_questions = {i["id"]: i for i in self.document["uncertainties"]}
        registered_targets = {i["target_id"] for i in self.issues.values()}
        questions = [self.cited(i, statement=False) for i in self.document["uncertainties"]
                     if i["id"] not in registered_targets]
        for issue in self.issues.values():
            if issue["status"] != "resolved":
                original = original_questions.get(issue["target_id"])
                units = sorted(set(issue["u"]) | set(original["u"] if original else []))
                question = self.cited({**issue, "text": issue["question"], "u": units}, statement=False)
                if original:
                    question["originalQuestion"] = original["text"]
                # Registered target/field identity is authoritative. Rewording
                # the same doubt must not create a second visible row, while
                # two distinct fields must not merge just because words match.
                questions.append(question)
        notes = {"overview": self.document["overview"]["text"], "overviewSourceIDs": self.cited(self.document["overview"], statement=False)["source_ids"],
                 "chapters": chapters, "key_points": [], "action_items": [self.cited(i) for i in self.document["action_items"]], "uncertainties": questions,
                 "model": self.config["model"], "tokensThisRun": self.client.total_tokens, "summaryStrategy": self.strategy,
                 "summaryRoute": self.route, "summaryVersion": VERSION, "summaryPromptVersion": PROMPT_VERSION,
                 "reviewedParagraphs": len(self.reviewed), "paragraphCount": len(self.units), "rereadCount": self.reread_count,
                 "sourceKind": "recording", "sourceRevision": self.source_revision, "recordingRevision": self.config.get("recordingRevision", "original"),
                 "evidenceItemCount": len(self.checks), "assessmentCount": len(self.assessments)}
        return notes, self.dialogue_rows()

    def dialogue_rows(self):
        rows = []
        for index, row in enumerate(self.dialogue):
            parts = [{"id": f"part-{p['id']}", "speaker": p["speaker"], "text": p["text"], "source_ids": p["source_ids"],
                      "spans": p["spans"], "punctuationInsertions": p["punctuation_insertions"]} for p in row["parts"]]
            rows.append({"id": f"row-{index}", "text": "".join(p["text"] for p in parts), "parts": parts, "continuousReading": True,
                         "source_ids": sorted({s for p in parts for s in p["source_ids"]})})
        return rows

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
            # A usable reading copy is deterministic. Optional punctuation is a
            # separate explicit operation and cannot hold the notes hostage.
            self.dialogue = punctuation_rows({"rows": [{"first": u["parts"][0]["id"], "last": u["parts"][-1]["id"]}
                                                       for u in self.units], "insertions": []}, self.units)
            notes, rows = self.outputs()
            if callback := self.config.get("beforePublish"):
                callback()
            write_json(self.folder / "dialogue-results.json", {"schemaVersion": 2, "generationID": self.identity,
                       "strategy": self.strategy, "notes": notes, "condensed": rows,
                       "formatting": {"status": "not_requested", "message": "原话已连续排版；可单独优化断句与标点。"}})
            self.publication = True
            for name, value in (("notes.json", notes), ("condensed.json", rows), ("chapters.json", notes["chapters"])):
                write_json(self.folder / name, value)
            complete = True
            return notes, rows
        finally:
            write_json(self.folder / "meeting-review.json", {"identity": self.identity, "assessments": list(self.assessments.values()),
                       "issues": list(self.issues.values()), "repairs": self.patch_log, "reviewedUnits": sorted(self.reviewed),
                       "targetOwnership": self.review_target_owners,
                       "reviewResponsibility": self.review_responsibility,
                       "issueContext": {k: sorted(v) for k, v in self.issue_context.items()},
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
                       "skippedRereadsWithoutNewContext": self.skipped_rereads,
                       "validationFailures": self.validation_failure_count,
                       "validationRetries": self.client.validation_retries,
                       "changedRepairBatches": sum(p["changed"] for p in self.patch_log)})


def summarize(segments, title, config, folder, emit):
    return MeetingReader(segments, title, config, folder, emit).run()


def format_dialogue(segments, title, config, folder, emit):
    """Independent optional edit: never regenerate or rewrite published notes."""
    path = folder / "dialogue-results.json"
    if not path.exists():
        raise UserError("请先完成会议纪要，再单独优化断句与标点。")
    bundle = read_json(path)
    notes = bundle.get("notes", {})
    if (bundle.get("schemaVersion") != 2 or not bundle.get("generationID")
            or bundle.get("strategy") not in STRATEGIES
            or bundle["strategy"] != config.get("dialogueStrategy")
            or notes.get("summaryStrategy") != bundle["strategy"]
            or notes.get("sourceRevision") != config.get("sourceRevision")
            or notes.get("recordingRevision", "original") != config.get("recordingRevision", "original")):
        raise UserError("原稿、校订或流程与已有纪要不一致，请先重新生成纪要；现有成果已保留。")
    engine = MeetingReader(segments, title, {**config, "formattingGeneration": bundle["generationID"]},
                           folder, emit, purpose="formatting")
    original = digest(bundle)
    complete = False

    def publish(status, message, rows=None):
        if callback := config.get("beforePublish"):
            callback()
        if digest(read_json(path)) != original:
            raise UserError("纪要成果已变化，本次标点结果未覆盖当前成果。")
        result = {**bundle, "formatting": {"status": status, "message": message, "identity": engine.identity,
                  "model": config["model"], "tokensThisRun": engine.client.total_tokens}}
        if rows is not None:
            result["condensed"] = rows
        write_json(path, result)
        if rows is not None:
            write_json(folder / "condensed.json", rows)
        return notes, result["condensed"]

    try:
        emit("condense", "单独优化断句与标点，已完成的纪要保持可用…")
        try:
            engine.dialogue = engine.stage("punctuation", lambda: engine.punctuate(engine.units))
        except (KeyboardInterrupt, SystemExit):
            publish("cancelled", "标点优化已取消，纪要与原有整理稿保留；可继续已保存的阶段。")
            raise
        except (UserError, ValueError, KeyError, TypeError, OutputLimit) as error:
            return publish("failed", "标点优化未完成，纪要与原有整理稿保留。" + str(error)[:500])
        result = publish("completed", "断句与标点已优化，原话字符及局部归属保持不变。", engine.dialogue_rows())
        complete = True
        return result
    finally:
        write_json(folder / "formatting-run.json", {"version": VERSION, "identity": engine.identity,
                   "completed": complete, "elapsedSeconds": round(time.monotonic() - engine.started, 3),
                   "stageSeconds": engine.stage_times, "requests": engine.client.request_count,
                   "checkpointHits": engine.client.cache_hits, "tokensThisRun": engine.client.total_tokens,
                   "calls": engine.client.calls, "validationFailures": engine.validation_failure_count})

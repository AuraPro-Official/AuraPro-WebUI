"""Versioned, model-neutral concept-extraction prompt profiles.

Profiles are deliberately source-code assets rather than administrator-supplied
free text.  That makes a local calibration replayable by a later cloud Batch
run without allowing a browser to change the extraction policy or smuggle
provider configuration into a durable request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class PromptProfileError(ValueError):
    """A requested concept-extraction profile or result is unsafe to use."""


@dataclass(frozen=True, slots=True)
class ConceptPromptProfile:
    """A stable extraction instruction and its output constraints."""

    profile_id: str
    system_instruction: str
    max_tokens: int = 1_200
    temperature: float = 0.0
    # Whether the instruction asks for adjacent evidence context anchors, and
    # therefore which strict schema a remote Structured Outputs request uses.
    # This is a property of the profile rather than of "whichever profile is
    # currently the default": a superseded anchored profile must keep sending
    # the anchored schema so an existing sample stays replayable.
    uses_context_anchors: bool = False
    # Whether the instruction asks the model for code-point offsets.  Same
    # reasoning as the flag above, and the same name the section graph path
    # already uses for the same decision.  Ingest never reads it: it recognises
    # the shape per mention, from the field set the model actually returned, so
    # a stored request built from any registered profile still replays.
    asks_for_offsets: bool = True


@dataclass(frozen=True, slots=True)
class ConceptPayloadValidation:
    """Content-free validation outcome for one immutable source passage."""

    valid: bool
    concept_count: int
    mention_count: int
    reason: str | None = None


DEFAULT_CONCEPT_PROMPT_PROFILE = "zh-glossary-v7"

# Bounded, adjacent literal context distinguishes repeated evidence without
# putting another copy of a passage into a provider response.
MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS = 48


# The strict schema is suitable for a remote Chat Completions model that
# supports Structured Outputs. Local llama.cpp still uses json_object plus the
# same textual contract because smaller GGUF models and server builds do not
# reliably implement the complete json_schema surface.
#
# Three mention shapes are registered, and all three stay live: a durable Batch
# request is built once and replayed from its stored row, so a superseded shape
# has to keep being buildable byte for byte.
_LEGACY_MENTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["start_codepoint", "end_codepoint", "evidence"],
    "properties": {
        "start_codepoint": {"type": "integer", "minimum": 0},
        "end_codepoint": {"type": "integer", "minimum": 1},
        "evidence": {"type": "string", "minLength": 1},
    },
}

_ANCHORED_MENTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "start_codepoint", "end_codepoint", "evidence",
        "context_before", "context_after",
    ],
    "properties": {
        "start_codepoint": {"type": "integer", "minimum": 0},
        "end_codepoint": {"type": "integer", "minimum": 1},
        "evidence": {"type": "string", "minLength": 1},
        "context_before": {
            "type": "string",
            "maxLength": MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS,
        },
        "context_after": {
            "type": "string",
            "maxLength": MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS,
        },
    },
}

# ``zh-glossary-v7`` asks for no offsets at all.  The measurement that justifies
# this came from these very concept samples: across four of them a model
# supplies a correct ``start_codepoint``/``end_codepoint`` pair about one time
# in thirty-seven while naming the right evidence text almost every time, and
# every stored citation is byte-exact only because grounding re-derives the
# offset from the literal.  ``zh-section-graph-v2`` acted on that evidence and
# the concept path did not; this is that correction.  The adjacent context
# anchor is what remains: it is the only thing a model has to supply for the
# server to choose between repeated literals.
_ANCHORED_MENTION_SCHEMA_WITHOUT_OFFSETS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["evidence", "context_before", "context_after"],
    "properties": {
        "evidence": {"type": "string", "minLength": 1},
        "context_before": {
            "type": "string",
            "maxLength": MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS,
        },
        "context_after": {
            "type": "string",
            "maxLength": MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS,
        },
    },
}


def _concept_output_schema(mention_schema: Mapping[str, Any]) -> dict[str, Any]:
    """Build the concept output schema around one mention shape.

    Only the mention differs between the registered contracts, so the concept
    envelope is written once here rather than copied per shape - the same
    reason :func:`section_graph._output_schema` exists.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["concepts"],
        "properties": {
            "concepts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "aliases", "definition", "mentions"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "aliases": {"type": "array", "items": {"type": "string"}},
                        "definition": {"type": "string"},
                        "mentions": {"type": "array", "items": dict(mention_schema)},
                    },
                },
            }
        },
    }


# The same three shapes as field sets, for validating a payload that has
# already been decoded.  Cloud ingest keeps its own copies in ``batch.py``: it
# must recognise a shape without importing an extraction-policy module, and the
# durable store is the only place where a mismatch could corrupt a citation.
_LEGACY_MENTION_FIELDS = set(_LEGACY_MENTION_SCHEMA["required"])
_ANCHORED_MENTION_FIELDS = set(_ANCHORED_MENTION_SCHEMA["required"])
_ANCHORED_MENTION_FIELDS_WITHOUT_OFFSETS = set(
    _ANCHORED_MENTION_SCHEMA_WITHOUT_OFFSETS["required"]
)


_LEGACY_CONCEPT_OUTPUT_SCHEMA: dict[str, Any] = _concept_output_schema(_LEGACY_MENTION_SCHEMA)

CONCEPT_OUTPUT_SCHEMA: dict[str, Any] = _concept_output_schema(_ANCHORED_MENTION_SCHEMA)

CONCEPT_OUTPUT_SCHEMA_WITHOUT_OFFSETS: dict[str, Any] = _concept_output_schema(
    _ANCHORED_MENTION_SCHEMA_WITHOUT_OFFSETS
)


# A registered profile is immutable.  Durable cloud sample approvals key on the
# profile ID, so editing v1-v6 in place would silently re-point an existing
# approval at an instruction that was never sampled.  A change is always a new
# entry, and the superseded entries stay exactly as they were submitted.  v6 in
# particular now carries the one approved sample and the full-book job that
# approval unlocked, so it is digest-pinned in test_epub_prompt_profiles.py
# exactly as v1-v5 are.
_PROFILES: dict[str, ConceptPromptProfile] = {
    "zh-glossary-v1": ConceptPromptProfile(
        profile_id="zh-glossary-v1",
        system_instruction=(
            "你是中文 EPUB 的术语与专名抽取器。只抽取读者可能需要检索或解释的、"
            "在本段中有明确依据的专有名词、人物、组织、地点、事件、制度、作品名或专业术语。"
            "不要抽取普通功能词、泛化主题、纯修辞、没有可验证文本依据的推测，也不要根据外部知识补充事实。"
            "每个概念必须有至少一个出现位置；name 是最适合索引的规范写法，aliases 只包含本段可见的等价写法。"
            "definition 用一句简短中文说明，且只能依据本段。mentions 的 start_codepoint 从 0 开始，"
            "end_codepoint 为排他位置，evidence 必须与 passage[start_codepoint:end_codepoint] 完全一致，"
            "包括标点和空格。没有合格概念时返回 concepts 的空数组。"
            "仅返回符合指定 JSON 结构的对象，不要 Markdown 或解释。"
        ),
    ),
    "zh-glossary-v2": ConceptPromptProfile(
        profile_id="zh-glossary-v2",
        max_tokens=512,
        system_instruction=(
            "你是中文 EPUB 的术语与专名抽取器。只抽取读者可能需要检索或解释的、"
            "在本段中有明确依据的专有名词、人物、组织、地点、事件、制度、作品名或专业术语。"
            "不要抽取普通功能词、泛化主题、纯修辞、没有可验证文本依据的推测，也不要根据外部知识补充事实。"
            "每段最多抽取 6 个最值得检索的概念。name 是最适合索引的规范写法；aliases 最多 2 个，"
            "且只包含本段可见的等价写法；definition 是不超过 30 个汉字的一句说明，且只能依据本段。"
            "每个概念只保留一个最有代表性的出现位置。mentions 的 start_codepoint 从 0 开始，"
            "end_codepoint 为排他位置，evidence 必须与 passage[start_codepoint:end_codepoint] 完全一致，"
            "包括标点和空格。没有合格概念时返回 {\"concepts\":[]}。"
            "输出必须是一个 JSON 对象：第一个字符必须是 {，唯一顶层键必须是 concepts；"
            "不得返回 JSON 数组、JSON 字符串、Markdown 代码块或任何解释。"
        ),
    ),
    "zh-glossary-v3": ConceptPromptProfile(
        profile_id="zh-glossary-v3",
        max_tokens=512,
        system_instruction=(
            "你是中文 EPUB 的术语与专名抽取器。只抽取读者可能需要检索或解释的、"
            "在本段中有明确依据的专有名词、人物、组织、地点、事件、制度、作品名或专业术语。"
            "不要抽取普通功能词、泛化主题、纯修辞、没有可验证文本依据的推测，也不要根据外部知识补充事实。"
            "每段最多抽取 6 个最值得检索的概念。name 是最适合索引的规范写法；aliases 最多 2 个，"
            "且只包含本段可见的等价写法；definition 是不超过 30 个汉字的一句说明，且只能依据本段。"
            "每个概念必须有且只能有 name、aliases、definition、mentions 四个字段；mentions 不能为空，"
            "且只保留一个最有代表性的出现位置。每个 mention 必须有且只能有 start_codepoint、"
            "end_codepoint、evidence 三个字段；start_codepoint 从 0 开始，end_codepoint 为排他位置，"
            "evidence 必须与 passage[start_codepoint:end_codepoint] 完全一致，包括标点和空格。"
            "没有合格概念时返回 {\"concepts\":[]}。输出形状必须为"
            "{\"concepts\":[{\"name\":\"…\",\"aliases\":[],\"definition\":\"…\","
            "\"mentions\":[{\"start_codepoint\":0,\"end_codepoint\":1,\"evidence\":\"…\"}]}]}。"
            "输出必须是一个 JSON 对象：第一个字符必须是 {，唯一顶层键必须是 concepts；"
            "不得返回 JSON 数组、JSON 字符串、Markdown 代码块或任何解释。"
        ),
    ),
    "zh-glossary-v4": ConceptPromptProfile(
        profile_id="zh-glossary-v4",
        max_tokens=512,
        uses_context_anchors=True,
        system_instruction=(
            "你是中文 EPUB 的术语与专名抽取器。只抽取读者可能需要检索或解释的、"
            "在本段中有明确依据的专有名词、人物、组织、地点、事件、制度、作品名或专业术语。"
            "不要抽取普通功能词、泛化主题、纯修辞、没有可验证文本依据的推测，也不要根据外部知识补充事实。"
            "每段最多抽取 6 个最值得检索的概念。name 是最适合索引的规范写法；aliases 最多 2 个，"
            "且只包含本段可见的等价写法；definition 是不超过 30 个汉字的一句说明，且只能依据本段。"
            "每个概念必须有且只能有 name、aliases、definition、mentions 四个字段；mentions 不能为空，"
            "且只保留一个最有代表性的出现位置。每个 mention 必须有且只能有 start_codepoint、"
            "end_codepoint、evidence、context_before、context_after 五个字段；start_codepoint 从 0 开始，"
            "end_codepoint 为排他位置，evidence 必须与 passage[start_codepoint:end_codepoint] 完全一致，"
            "包括标点和空格。context_before 和 context_after 分别是 evidence 紧邻前后各最多 48 个"
            "Unicode 字符的原文；evidence 在本段重复时，至少提供一个非空上下文以唯一确定该出现位置。"
            "evidence 唯一时两个上下文可为空字符串。没有合格概念时返回 {\"concepts\":[]}。"
            "输出形状必须为 {\"concepts\":[{\"name\":\"…\",\"aliases\":[],\"definition\":\"…\","
            "\"mentions\":[{\"start_codepoint\":0,\"end_codepoint\":1,\"evidence\":\"…\","
            "\"context_before\":\"\",\"context_after\":\"\"}]}]}。"
            "输出必须是一个 JSON 对象：第一个字符必须是 {，唯一顶层键必须是 concepts；"
            "不得返回 JSON 数组、JSON 字符串、Markdown 代码块或任何解释。"
        ),
    ),
    "zh-glossary-v5": ConceptPromptProfile(
        profile_id="zh-glossary-v5",
        # v4's 512-token budget was the direct cause of a third of its sample
        # failures: the provider returned no JSON payload at all on the longest
        # passages (1174, 1040 and 692 code points).  The worst case this
        # contract permits is 6 concepts, each carrying a name, up to 2 aliases,
        # a definition of at most 30 characters, and one mention holding
        # evidence plus two anchors of at most 48 code points.  Budgeting 20
        # code points per name and per alias and 40 for evidence, that is
        # 20 + 2*20 + 30 + 40 + 2*48 = 226 code points of content per concept,
        # which for CJK text costs roughly one token per code point, plus about
        # 70 tokens of JSON field names, digits and punctuation: ~296 tokens per
        # concept, ~1_780 for six, plus the wrapper object.  2_048 clears that
        # ceiling with margin.  Typical responses are far smaller, because the
        # anchors below are now empty unless the evidence actually repeats.
        max_tokens=2_048,
        uses_context_anchors=True,
        system_instruction=(
            "你是中文 EPUB 的术语与专名抽取器。只抽取读者可能需要检索或解释的、"
            "在本段中有明确依据的专有名词、人物、组织、地点、事件、制度、作品名或专业术语。"
            "不要抽取普通功能词、泛化主题、纯修辞、没有可验证文本依据的推测，也不要根据外部知识补充事实。"
            "每段最多抽取 6 个最值得检索的概念。name 是最适合索引的规范写法；aliases 最多 2 个，"
            "且只包含本段可见的等价写法；definition 是不超过 30 个汉字的一句说明，且只能依据本段。"
            "每个概念必须有且只能有 name、aliases、definition、mentions 四个字段；mentions 不能为空，"
            "且只保留一个最有代表性的出现位置。每个 mention 必须有且只能有 start_codepoint、"
            "end_codepoint、evidence、context_before、context_after 五个字段；start_codepoint 从 0 开始，"
            "end_codepoint 为排他位置，evidence 必须与 passage[start_codepoint:end_codepoint] 完全一致，"
            "包括标点和空格。evidence 必须从本段逐字复制：不得改写、翻译或统一引号、"
            "全角与半角标点、空格和大小写。evidence 在本段只出现一次时，"
            "context_before 和 context_after 必须都是空字符串；evidence 在本段重复出现时，"
            "两者分别取 evidence 紧邻前后各最多 48 个 Unicode 字符的原文，且至少一个非空，"
            "以唯一确定该出现位置。没有合格概念时返回 {\"concepts\":[]}。"
            "输出形状必须为 {\"concepts\":[{\"name\":\"…\",\"aliases\":[],\"definition\":\"…\","
            "\"mentions\":[{\"start_codepoint\":0,\"end_codepoint\":1,\"evidence\":\"…\","
            "\"context_before\":\"\",\"context_after\":\"\"}]}]}。"
            "输出必须是一个 JSON 对象：第一个字符必须是 {，唯一顶层键必须是 concepts；"
            "不得返回 JSON 数组、JSON 字符串、Markdown 代码块或任何解释。"
        ),
    ),
    "zh-glossary-v6": ConceptPromptProfile(
        profile_id="zh-glossary-v6",
        # v6 is v5 plus one clause: a minimum evidence span.  Everything else --
        # the 2_048-token budget, the conditional anchors, the verbatim-copy
        # rule, the object shape, the leading-"{" rule and the six-concept cap --
        # is v5's text byte for byte, so running v6 beside the two in-flight v5
        # samples isolates exactly this variable.
        #
        # Why a minimum at all: every v3/gpt-4.1 EVIDENCE_AMBIGUOUS failure cited
        # a span of 1, 2, 3, 3, 3, 3 and 6 code points, occurring 38, 4, 3, 3, 7,
        # 10 and 2 times in its passage; the pre-grounding-fix v4 failures had the
        # same 1-2 code-point shape.  A one-character citation is both useless to
        # a reader and almost certain to repeat, and repetition is what drives the
        # ambiguity.  Requiring the span to be a phrase *containing* the term,
        # rather than the bare term, keeps it a byte-exact source substring -- so
        # source fidelity is untouched -- while making it far likelier to be
        # unique.
        #
        # Why 10: it sits strictly above the whole observed failure distribution
        # (max 6) with margin, and it is about the length of a short Chinese
        # clause, so it asks for a real citation rather than padding.  It stays
        # well below MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS = 48, the only
        # length bound the code path imposes and one that applies to the anchors
        # rather than to the evidence, so the two cannot collide; nothing caps
        # evidence length anywhere.  It also stays inside the 40 code points the
        # v5 token budget above already reserves per evidence string.
        #
        # Why the passage-length escape hatch: 10 code points is unreachable in
        # eight of the twenty sampled passages.  Measured over the sample, the
        # lengths are 9, 9, 10, 10, 10, 10, 10, 10, 12, 17, 18, 28, 87, 105, 166,
        # 270, 406, 692, 1_040 and 1_174 code points -- mostly headings at the
        # short end, two of them shorter than the minimum itself.  Without the
        # escape hatch those passages would put the model in an impossible bind
        # and invite it to invent text, which strict validation would then reject.
        max_tokens=2_048,
        uses_context_anchors=True,
        system_instruction=(
            "你是中文 EPUB 的术语与专名抽取器。只抽取读者可能需要检索或解释的、"
            "在本段中有明确依据的专有名词、人物、组织、地点、事件、制度、作品名或专业术语。"
            "不要抽取普通功能词、泛化主题、纯修辞、没有可验证文本依据的推测，也不要根据外部知识补充事实。"
            "每段最多抽取 6 个最值得检索的概念。name 是最适合索引的规范写法；aliases 最多 2 个，"
            "且只包含本段可见的等价写法；definition 是不超过 30 个汉字的一句说明，且只能依据本段。"
            "每个概念必须有且只能有 name、aliases、definition、mentions 四个字段；mentions 不能为空，"
            "且只保留一个最有代表性的出现位置。每个 mention 必须有且只能有 start_codepoint、"
            "end_codepoint、evidence、context_before、context_after 五个字段；start_codepoint 从 0 开始，"
            "end_codepoint 为排他位置，evidence 必须与 passage[start_codepoint:end_codepoint] 完全一致，"
            "包括标点和空格。evidence 必须从本段逐字复制：不得改写、翻译或统一引号、"
            "全角与半角标点、空格和大小写。evidence 至少 10 个 Unicode 字符，"
            "且必须是包含该概念的完整、有意义的短语或分句，不得只给出概念本身，"
            "也不得截取无意义的字符片段；本段总长不足 10 个 Unicode 字符时，evidence 取本段全文。"
            "evidence 在本段只出现一次时，"
            "context_before 和 context_after 必须都是空字符串；evidence 在本段重复出现时，"
            "两者分别取 evidence 紧邻前后各最多 48 个 Unicode 字符的原文，且至少一个非空，"
            "以唯一确定该出现位置。没有合格概念时返回 {\"concepts\":[]}。"
            "输出形状必须为 {\"concepts\":[{\"name\":\"…\",\"aliases\":[],\"definition\":\"…\","
            "\"mentions\":[{\"start_codepoint\":0,\"end_codepoint\":10,\"evidence\":\"…\","
            "\"context_before\":\"\",\"context_after\":\"\"}]}]}。"
            "输出必须是一个 JSON 对象：第一个字符必须是 {，唯一顶层键必须是 concepts；"
            "不得返回 JSON 数组、JSON 字符串、Markdown 代码块或任何解释。"
        ),
    ),
    DEFAULT_CONCEPT_PROMPT_PROFILE: ConceptPromptProfile(
        profile_id=DEFAULT_CONCEPT_PROMPT_PROFILE,
        # v7 is v6 minus the offsets, and nothing else.  Every lever v6 earned
        # its 20/20 with survives byte for byte: the >=10 code-point minimum
        # evidence span with its short-passage escape hatch, the conditional
        # anchors, the verbatim-copy clause naming the normalizations to avoid,
        # the six-concept cap, the strict object shape, the leading-"{" rule and
        # the no-Markdown rule.
        #
        # Why drop them: the measurement that justified dropping offsets from
        # ``zh-section-graph-v2`` was taken on *these* samples.  Across four
        # cloud concept samples the model supplies a correct
        # ``start_codepoint``/``end_codepoint`` pair about one time in
        # thirty-seven, while naming the right evidence text almost every time;
        # every stored citation is byte-exact only because
        # ``_resolve_evidence_span`` re-derives the offset from the literal.
        # The conclusion was applied to the section graph path and not to this
        # one.  Asking for a number that is wrong 97% of the time and then
        # thrown away costs output tokens on every span, and sustains the
        # ``ANCHOR_MISMATCH`` failure class, which exists only on the
        # ``direct_is_exact`` branch that fires ~3% of the time.  Under this
        # contract that branch is unreachable, so the class cannot occur.
        #
        # This is a cost and cleanliness change, not a correctness fix: v6
        # scored 20/20 and grounding repairs its offsets, so nothing about the
        # in-flight v6 full-book job needs revisiting.  That job replays from
        # its persisted ``request_json``, and ingest recognises a mention shape
        # from the fields the model returned rather than from whichever profile
        # is currently the default, so promoting v7 cannot reach it.
        #
        # max_tokens stays at v6's 2_048 deliberately, for two reasons.  First,
        # it is a ceiling on generation, not a spend: a response is billed for
        # the tokens it actually emits, and v7 emits fewer by construction -
        # roughly 20 per span for two field names, their digits and two commas,
        # about 120 over the six mentions this contract permits.  Lowering the
        # ceiling would bank no saving; it would only move the worst case closer
        # to truncation, and a truncated response is not JSON at all - the
        # single most expensive failure mode there is, paid for and unusable,
        # and the exact one that forced v4's 512 up to v5's 2_048.  Second, v7
        # exists to isolate one variable against the approved v6 sample; a
        # different decoding budget would confound that comparison, which is why
        # v6 kept v5's budget and ``zh-section-graph-v3`` kept v2's.  The v5
        # arithmetic still bounds this contract, and now over-bounds it: the
        # worst case shrinks from ~1_780 tokens to ~1_660.
        max_tokens=2_048,
        uses_context_anchors=True,
        asks_for_offsets=False,
        system_instruction=(
            "你是中文 EPUB 的术语与专名抽取器。只抽取读者可能需要检索或解释的、"
            "在本段中有明确依据的专有名词、人物、组织、地点、事件、制度、作品名或专业术语。"
            "不要抽取普通功能词、泛化主题、纯修辞、没有可验证文本依据的推测，也不要根据外部知识补充事实。"
            "每段最多抽取 6 个最值得检索的概念。name 是最适合索引的规范写法；aliases 最多 2 个，"
            "且只包含本段可见的等价写法；definition 是不超过 30 个汉字的一句说明，且只能依据本段。"
            "每个概念必须有且只能有 name、aliases、definition、mentions 四个字段；mentions 不能为空，"
            "且只保留一个最有代表性的出现位置。每个 mention 必须有且只能有 evidence、"
            "context_before、context_after 三个字段；不要输出任何字符位置，"
            "字符位置由程序依据 evidence 在本段中重新推导。"
            "evidence 必须与本段中的一段原文完全一致，"
            "包括标点和空格。evidence 必须从本段逐字复制：不得改写、翻译或统一引号、"
            "全角与半角标点、空格和大小写。evidence 至少 10 个 Unicode 字符，"
            "且必须是包含该概念的完整、有意义的短语或分句，不得只给出概念本身，"
            "也不得截取无意义的字符片段；本段总长不足 10 个 Unicode 字符时，evidence 取本段全文。"
            "evidence 在本段只出现一次时，"
            "context_before 和 context_after 必须都是空字符串；evidence 在本段重复出现时，"
            "两者分别取 evidence 紧邻前后各最多 48 个 Unicode 字符的原文，且至少一个非空，"
            "以唯一确定该出现位置。没有合格概念时返回 {\"concepts\":[]}。"
            "输出形状必须为 {\"concepts\":[{\"name\":\"…\",\"aliases\":[],\"definition\":\"…\","
            "\"mentions\":[{\"evidence\":\"…\","
            "\"context_before\":\"\",\"context_after\":\"\"}]}]}。"
            "输出必须是一个 JSON 对象：第一个字符必须是 {，唯一顶层键必须是 concepts；"
            "不得返回 JSON 数组、JSON 字符串、Markdown 代码块或任何解释。"
        ),
    ),
}


def available_prompt_profiles() -> tuple[str, ...]:
    return tuple(_PROFILES)


def get_prompt_profile(profile_id: str) -> ConceptPromptProfile:
    profile = _PROFILES.get(profile_id)
    if profile is None:
        raise PromptProfileError(f"unknown EPUB concept prompt profile: {profile_id}")
    return profile


def _strict_schema_for(profile: ConceptPromptProfile) -> dict[str, Any]:
    """Pick the Structured Outputs schema one profile's instruction asks for."""
    if not profile.uses_context_anchors:
        # v1-v3: offsets, no anchors.  No registered profile asks for neither,
        # because a payload with no offsets and no anchors could not resolve a
        # repeated literal at all.
        return _LEGACY_CONCEPT_OUTPUT_SCHEMA
    if profile.asks_for_offsets:
        return CONCEPT_OUTPUT_SCHEMA
    return CONCEPT_OUTPUT_SCHEMA_WITHOUT_OFFSETS


def build_concept_completion_request(
    *,
    model: str,
    profile_id: str,
    passage: str,
    remote_structured_output: bool,
) -> dict[str, Any]:
    """Build the exact model request used by local and remote calibration."""
    if not model.strip():
        raise PromptProfileError("concept extraction model cannot be empty")
    if not passage:
        raise PromptProfileError("concept extraction passage cannot be empty")
    profile = get_prompt_profile(profile_id)
    response_format: dict[str, Any]
    if remote_structured_output:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "epub_concepts",
                "strict": True,
                # Both flags come from the profile, never from the current
                # default, so a superseded profile keeps sending the exact
                # schema it was sampled with.
                "schema": _strict_schema_for(profile),
            },
        }
    else:
        response_format = {"type": "json_object"}
    return {
        "model": model.strip(),
        "temperature": profile.temperature,
        "seed": 0,
        "max_tokens": profile.max_tokens,
        "response_format": response_format,
        "messages": [
            {"role": "system", "content": profile.system_instruction},
            {"role": "user", "content": passage},
        ],
    }


def select_stratified_passages(
    passages: Sequence[Mapping[str, Any]], *, limit: int
) -> list[Mapping[str, Any]]:
    """Choose a deterministic, evenly distributed cross-chapter calibration set."""
    if not 1 <= limit <= 500:
        raise PromptProfileError("calibration sample limit must be between 1 and 500")
    ordered = sorted(passages, key=lambda value: (int(value.get("ordinal", 0)), str(value.get("passage_id", ""))))
    if not ordered:
        raise PromptProfileError("EPUB version contains no passages")
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for passage in ordered:
        raw_path = passage.get("toc_path")
        path = tuple(str(part) for part in raw_path) if isinstance(raw_path, (list, tuple)) and raw_path else ("",)
        groups.setdefault(path[:1], []).append(passage)
    chapters = sorted(groups.values(), key=lambda values: (int(values[0].get("ordinal", 0)), str(values[0].get("passage_id", ""))))
    selected_groups = _evenly_spaced(chapters, min(limit, len(chapters)))
    remaining = limit
    selected: list[Mapping[str, Any]] = []
    for index, group in enumerate(selected_groups):
        groups_left = len(selected_groups) - index
        count = min(len(group), max(1, remaining // groups_left))
        selected.extend(_evenly_spaced(group, count))
        remaining -= count
    # When some short chapters cannot absorb their fair share, fill the rest
    # deterministically without duplicating a passage.
    if remaining > 0:
        selected_ids = {str(value.get("passage_id", "")) for value in selected}
        for passage in ordered:
            if str(passage.get("passage_id", "")) not in selected_ids:
                selected.append(passage)
                selected_ids.add(str(passage.get("passage_id", "")))
                if len(selected) >= limit:
                    break
    return sorted(selected[:limit], key=lambda value: (int(value.get("ordinal", 0)), str(value.get("passage_id", ""))))


def validate_concept_payload(payload: Any, *, passage: str) -> ConceptPayloadValidation:
    """Validate the model payload before it can affect a concept graph."""
    if not isinstance(payload, Mapping) or set(payload) != {"concepts"}:
        return ConceptPayloadValidation(False, 0, 0, "response must be an object with only concepts")
    concepts = payload.get("concepts")
    if not isinstance(concepts, list):
        return ConceptPayloadValidation(False, 0, 0, "concepts must be a list")
    mentions = 0
    for concept in concepts:
        if not isinstance(concept, Mapping) or set(concept) != {"name", "aliases", "definition", "mentions"}:
            return ConceptPayloadValidation(False, 0, mentions, "each concept has an invalid schema")
        if not isinstance(concept["name"], str) or not concept["name"].strip():
            return ConceptPayloadValidation(False, 0, mentions, "concept name must be non-empty text")
        if not isinstance(concept["definition"], str):
            return ConceptPayloadValidation(False, 0, mentions, "concept definition must be text")
        aliases = concept["aliases"]
        if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
            return ConceptPayloadValidation(False, 0, mentions, "concept aliases must be text")
        concept_mentions = concept["mentions"]
        if not isinstance(concept_mentions, list) or not concept_mentions:
            return ConceptPayloadValidation(False, 0, mentions, "each concept needs a visible mention")
        for mention in concept_mentions:
            if not isinstance(mention, Mapping) or set(mention) not in (
                _LEGACY_MENTION_FIELDS,
                _ANCHORED_MENTION_FIELDS,
                _ANCHORED_MENTION_FIELDS_WITHOUT_OFFSETS,
            ):
                return ConceptPayloadValidation(False, 0, mentions, "each mention has an invalid schema")
            evidence = mention["evidence"]
            if set(mention) == _ANCHORED_MENTION_FIELDS_WITHOUT_OFFSETS:
                # A v7 payload carries no offsets at all, so this validator
                # derives them from the literal exactly as cloud ingest does.
                # The checks below are then run unchanged against the derived
                # span, which keeps one definition of "valid mention" rather
                # than a second, looser one for the newer shape.
                located = _locate_evidence(
                    passage,
                    evidence if isinstance(evidence, str) else "",
                    mention["context_before"],
                    mention["context_after"],
                )
                if located is None:
                    return ConceptPayloadValidation(
                        False, 0, mentions, "mention evidence cannot be uniquely located"
                    )
                start, end = located
            else:
                start = mention["start_codepoint"]
                end = mention["end_codepoint"]
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or start < 0
                or end <= start
                or end > len(passage)
                or not isinstance(evidence, str)
                or passage[start:end] != evidence
            ):
                return ConceptPayloadValidation(False, 0, mentions, "mention evidence or codepoint offsets are invalid")
            if "context_before" in mention:
                before = mention["context_before"]
                after = mention["context_after"]
                if (
                    not isinstance(before, str)
                    or not isinstance(after, str)
                    or len(before) > MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS
                    or len(after) > MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS
                    or passage[max(0, start - len(before)):start] != before
                    or passage[end:end + len(after)] != after
                    or (
                        passage.find(evidence, passage.find(evidence) + 1) >= 0
                        and not (before or after)
                    )
                ):
                    return ConceptPayloadValidation(False, 0, mentions, "mention evidence context anchor is invalid")
            mentions += 1
    return ConceptPayloadValidation(True, len(concepts), mentions)


def normalize_local_payload_offsets(payload: Any, *, passage: str) -> Any:
    """Derive local-model offsets only when its visible evidence is unique.

    Small local models frequently identify the right evidence but count Unicode
    code points unreliably.  The stored citation remains exact because this
    normalization only replaces offsets from one unique literal occurrence;
    absent or ambiguous evidence is left for strict validation to reject.
    """
    if not isinstance(payload, Mapping):
        return payload
    concepts = payload.get("concepts")
    if not isinstance(concepts, list):
        return payload
    normalized_concepts: list[Any] = []
    for concept in concepts:
        if not isinstance(concept, Mapping):
            normalized_concepts.append(concept)
            continue
        normalized_concept = dict(concept)
        mentions = concept.get("mentions")
        if not isinstance(mentions, list):
            normalized_concepts.append(normalized_concept)
            continue
        normalized_mentions: list[Any] = []
        for mention in mentions:
            if not isinstance(mention, Mapping) or not isinstance(mention.get("evidence"), str):
                normalized_mentions.append(mention)
                continue
            evidence = mention["evidence"]
            start = passage.find(evidence)
            if not evidence or start < 0 or passage.find(evidence, start + len(evidence)) >= 0:
                normalized_mentions.append(dict(mention))
                continue
            normalized_mention = dict(mention)
            normalized_mention["start_codepoint"] = start
            normalized_mention["end_codepoint"] = start + len(evidence)
            normalized_mentions.append(normalized_mention)
        normalized_concept["mentions"] = normalized_mentions
        normalized_concepts.append(normalized_concept)
    return {**payload, "concepts": normalized_concepts}


def _locate_evidence(
    passage: str, evidence: str, before: Any, after: Any
) -> tuple[int, int] | None:
    """Derive the span of an offsets-free mention, or ``None`` if it is unsafe.

    This is the read-only twin of the no-offsets branch of
    ``batch._resolve_evidence_span`` and follows the same rule: a literal that
    occurs once is self-verifying, a literal that repeats must be selected by a
    non-empty adjacent anchor, and anything else is not a located mention.  The
    durable path deliberately stays in ``batch.py`` with its diagnostics; this
    one exists because a local calibration run has to be able to score the
    default profile, and it writes nothing.
    """
    if not evidence or not isinstance(before, str) or not isinstance(after, str):
        return None
    occurrences: list[int] = []
    cursor = passage.find(evidence)
    while cursor >= 0:
        occurrences.append(cursor)
        cursor = passage.find(evidence, cursor + 1)
    if not occurrences:
        return None
    candidates = occurrences
    if len(occurrences) > 1:
        if not (before or after):
            return None
        candidates = [
            occurrence
            for occurrence in occurrences
            if passage[max(0, occurrence - len(before)):occurrence] == before
            and passage[
                occurrence + len(evidence):occurrence + len(evidence) + len(after)
            ] == after
        ]
    if len(candidates) != 1:
        return None
    return candidates[0], candidates[0] + len(evidence)


def _evenly_spaced(values: Sequence[Any], count: int) -> list[Any]:
    if count <= 0:
        return []
    if count >= len(values):
        return list(values)
    if count == 1:
        return [values[len(values) // 2]]
    return [values[round(index * (len(values) - 1) / (count - 1))] for index in range(count)]

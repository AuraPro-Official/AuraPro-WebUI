"""TOC-scoped packets and strict contracts for one-pass section graph extraction."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence


DEFAULT_SECTION_GRAPH_PROFILE = "zh-section-graph-v3"
SECTION_GRAPH_MAX_CHARACTERS = 12_000

# Bounded, adjacent literal context distinguishes repeated evidence without
# putting another copy of a packet passage into a provider response.  This is
# deliberately the same budget the concept path uses, because both shapes are
# resolved by the same server-side span resolver.
MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS = 48

RELATION_PREDICATES = (
    "HAS_PART",
    "PRECEDES",
    "PREREQUISITE",
    "CAUSES",
    "CONTRASTS",
    "ELABORATES",
)


class SectionGraphError(ValueError):
    """A TOC-scoped extraction packet or response is not safe to use."""


@dataclass(frozen=True, slots=True)
class SectionGraphPacket:
    """One bounded section request anchored to a real immutable passage."""

    packet_id: str
    anchor_passage_id: str
    toc_path: tuple[str, ...]
    passages: tuple[Mapping[str, Any], ...]


_MENTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["passage_id", "start_codepoint", "end_codepoint", "evidence"],
    "properties": {
        "passage_id": {"type": "string", "minLength": 1},
        "start_codepoint": {"type": "integer", "minimum": 0},
        "end_codepoint": {"type": "integer", "minimum": 1},
        "evidence": {"type": "string", "minLength": 1},
    },
}

# v2 asks for no offsets at all.  Measured over four CONCEPT_MENTIONS cloud
# samples, a model supplies a correct ``start_codepoint``/``end_codepoint`` pair
# about one time in thirty-seven while naming the right evidence text almost
# every time; every stored mention is byte-exact only because grounding
# re-derives the offset from the literal.  A packet holds many spans and ingest
# is atomic, so asking a packet for offsets makes the probability that the whole
# packet survives roughly 0.027 to the power of the number of spans.  The
# adjacent context anchor is what remains: it is the only thing a model has to
# supply for the server to choose between repeated literals.
_MENTION_SCHEMA_V2: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["passage_id", "evidence", "context_before", "context_after"],
    "properties": {
        "passage_id": {"type": "string", "minLength": 1},
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


def _output_schema(mention_schema: Mapping[str, Any]) -> dict[str, Any]:
    """Build the packet output schema around one span shape.

    Concept mentions and relation evidence deliberately share one span schema:
    they are grounded by the same resolver against the same immutable passages,
    so a shape difference between them could only ever be an accident.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["concepts", "relations"],
        "properties": {
            "concepts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["local_id", "name", "aliases", "definition", "mentions"],
                    "properties": {
                        "local_id": {"type": "string", "minLength": 1},
                        "name": {"type": "string", "minLength": 1},
                        "aliases": {"type": "array", "items": {"type": "string"}},
                        "definition": {"type": "string"},
                        "mentions": {"type": "array", "items": dict(mention_schema)},
                    },
                },
            },
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "subject_local_id", "predicate", "object_local_id", "evidence",
                    ],
                    "properties": {
                        "subject_local_id": {"type": "string", "minLength": 1},
                        "predicate": {"type": "string", "enum": list(RELATION_PREDICATES)},
                        "object_local_id": {"type": "string", "minLength": 1},
                        "evidence": {
                            "type": "array", "minItems": 1, "items": dict(mention_schema)
                        },
                    },
                },
            },
        },
    }


SECTION_GRAPH_OUTPUT_SCHEMA: dict[str, Any] = _output_schema(_MENTION_SCHEMA)
SECTION_GRAPH_OUTPUT_SCHEMA_V2: dict[str, Any] = _output_schema(_MENTION_SCHEMA_V2)


_SYSTEM_INSTRUCTION = """你是中文 EPUB 的章节概念图抽取器。输入是一个 TOC 范围内的原文段落列表；
每条段落都有不可变 passage_id、目录路径和原文。一次输出 concepts 与 relations。

concepts：只抽取可检索且有明确文本依据的专名、术语、人物、组织、事件、制度或作品。每个 concept
使用本响应唯一的 local_id。mentions 必须明确 passage_id；start_codepoint 从 0 开始，end_codepoint
为排他位置，evidence 必须逐字等于该 passage 的切片。不要依据外部知识补充事实。

relations：只连接本响应 concepts 中的两个不同 local_id；predicate 只能使用指定枚举。关系必须由
本章节包内的原文证据支持，evidence 至少一条且必须使用精确 passage_id 与字符位置。TOC 父子结构
已经由程序保存，不要把单纯目录层级重复写成语义关系；只有原文支持概念层面的关系才输出。

没有合格概念或关系时返回相应空数组。只返回严格 JSON 对象，不要 Markdown 或解释。"""


_SYSTEM_INSTRUCTION_V2 = """你是中文 EPUB 的章节概念图抽取器。输入是一个 TOC 范围内的原文段落列表；
每条段落都有不可变 passage_id、目录路径和原文。一次输出 concepts 与 relations。

concepts：只抽取可检索且有明确文本依据的专名、术语、人物、组织、事件、制度或作品。每个 concept
使用本响应唯一的 local_id。本包最多输出 12 个最值得检索的概念；name 是最适合索引的规范写法，
aliases 最多 2 个且只包含本包可见的等价写法，definition 是不超过 30 个汉字的一句说明，
且只能依据本包原文。每个概念只保留一个最有代表性的出现位置。不要依据外部知识补充事实。

mentions 与 relations 的 evidence 使用同一种定位方式：必须写明该证据所在的 passage_id，
但不要输出任何字符位置，字符位置由程序依据 evidence 在该 passage 中重新推导。
evidence 必须从该 passage 逐字复制：不得改写、翻译或统一引号、全角与半角标点、空格和大小写。
evidence 至少 10 个 Unicode 字符，且必须是包含该概念或该关系的完整、有意义的短语或分句，
不得只给出概念本身，也不得截取无意义的字符片段；
该 passage 总长不足 10 个 Unicode 字符时，evidence 取该 passage 全文。
evidence 在该 passage 中只出现一次时，context_before 和 context_after 必须都是空字符串；
在该 passage 中重复出现时，两者分别取 evidence 紧邻前后各最多 48 个 Unicode 字符的原文，
且至少一个非空，以唯一确定该出现位置。

relations：只连接本响应 concepts 中的两个不同 local_id；predicate 只能使用指定枚举。关系必须由
本章节包内的原文证据支持，本包最多输出 12 条关系，每条只给出一条最有代表性的 evidence。TOC 父子
结构已经由程序保存，不要把单纯目录层级重复写成语义关系；只有原文支持概念层面的关系才输出。

没有合格概念或关系时返回相应空数组。只返回严格 JSON 对象，不要 Markdown 或解释。"""


# v3 is v2 with one variable changed: ``toc_path`` is no longer quotable source.
#
# Two SECTION_GRAPH samples on v2 scored 12/16 (gpt-4.1) and 11/16
# (gpt-4o-mini), every failure surfacing as EVIDENCE_ABSENT.  Classifying the
# raw provider output span by span shows that reading is wrong for most of
# them: of gpt-4.1's failing spans, six were exact copies of a *section title*
# handed to the model in the packet's per-passage ``toc_path`` and none were
# invented; gpt-4o-mini had three of the same kind.  The model was not
# paraphrasing, which is what v2's verbatim-copy clause defends against - it
# was copying verbatim from a field we sent it and never scoped.
#
# The primary fix is therefore structural rather than textual, the same
# reasoning that led v2 to drop offsets rather than ask harder for correct
# ones: ``build_section_graph_completion_request`` no longer emits per-passage
# ``toc_path`` at all.  It was redundant - a packet is already a TOC-scoped
# subtree, the packet carries its own ``toc_path``, and SDD 4.2.2 point 1 makes
# structural provenance a deterministic server-side write that a model never
# infers - so removing it costs the contract nothing.  The packet-level
# ``toc_path`` stays, because SDD 4.2.2 point 2 justifies the packet shape by
# one request carrying enough local context to recognise a section-level
# concept and its parts together.
#
# The clause below is the belt to that structural brace: the one remaining TOC
# string in the payload is named as navigation and excluded from evidence
# explicitly, so the model is not left to infer why the field it used to quote
# has disappeared.  Everything else is v2 byte for byte - no offsets,
# conditional anchors, the minimum evidence span with its short-passage escape
# hatch, verbatim copy, the predicate vocabulary, ``local_id``, the strict JSON
# shape and the 8_192-token budget - so a v3 sample isolates exactly this
# variable against the two submitted v2 samples.
_SYSTEM_INSTRUCTION_V3 = """你是中文 EPUB 的章节概念图抽取器。输入是一个 TOC 范围内的原文段落列表；
每条段落都有不可变 passage_id 和原文。一次输出 concepts 与 relations。

concepts：只抽取可检索且有明确文本依据的专名、术语、人物、组织、事件、制度或作品。每个 concept
使用本响应唯一的 local_id。本包最多输出 12 个最值得检索的概念；name 是最适合索引的规范写法，
aliases 最多 2 个且只包含本包可见的等价写法，definition 是不超过 30 个汉字的一句说明，
且只能依据本包原文。每个概念只保留一个最有代表性的出现位置。不要依据外部知识补充事实。

mentions 与 relations 的 evidence 使用同一种定位方式：必须写明该证据所在的 passage_id，
但不要输出任何字符位置，字符位置由程序依据 evidence 在该 passage 中重新推导。
evidence 必须从该 passage 逐字复制：不得改写、翻译或统一引号、全角与半角标点、空格和大小写。
evidence 只能取自 passage_id 所指那条段落的 content 字段；顶层 toc_path 只是本包在目录中的导航
位置，不是可引用的原文，任何情况下都不得把 toc_path 或其中的章节标题当作 evidence 或其一部分。
evidence 至少 10 个 Unicode 字符，且必须是包含该概念或该关系的完整、有意义的短语或分句，
不得只给出概念本身，也不得截取无意义的字符片段；
该 passage 总长不足 10 个 Unicode 字符时，evidence 取该 passage 全文。
evidence 在该 passage 中只出现一次时，context_before 和 context_after 必须都是空字符串；
在该 passage 中重复出现时，两者分别取 evidence 紧邻前后各最多 48 个 Unicode 字符的原文，
且至少一个非空，以唯一确定该出现位置。

relations：只连接本响应 concepts 中的两个不同 local_id；predicate 只能使用指定枚举。关系必须由
本章节包内的原文证据支持，本包最多输出 12 条关系，每条只给出一条最有代表性的 evidence。TOC 父子
结构已经由程序保存，不要把单纯目录层级重复写成语义关系；只有原文支持概念层面的关系才输出。

没有合格概念或关系时返回相应空数组。只返回严格 JSON 对象，不要 Markdown 或解释。"""


@dataclass(frozen=True, slots=True)
class SectionGraphProfile:
    """One immutable packet instruction with its output contract and budget."""

    profile_id: str
    system_instruction: str
    output_schema: Mapping[str, Any]
    max_tokens: int
    temperature: float = 0.0
    # Whether this profile asks the model for code-point offsets.  It is a
    # property of the profile, not of "whichever profile is currently the
    # default", so a stored v1 request still sends the schema it was sampled
    # with.  Ingest does not read this flag: it distinguishes the two shapes per
    # span, from the field set the model actually returned.
    asks_for_offsets: bool = True
    # The minimum evidence length in Unicode code points, split into the number
    # this profile's instruction *asks* for and the number ingest *applies*.
    # Same spelling and same reasoning as ``ConceptPromptProfile``, which
    # documents the split in full; ``0``/``0`` where the instruction names no
    # minimum, so v1's stored requests keep replaying on the contract they were
    # given.
    #
    # ``requested_min_evidence_codepoints`` is a transcription of the
    # instruction and can never drift from it: the text is digest-pinned and a
    # test asserts the two agree.  ``enforced_min_evidence_codepoints`` is
    # lower on purpose - 10 code points is the wrong length test for Chinese,
    # and enforcing it discarded 13 of 43 packets on the full section-graph run
    # over citations like ``枢对测点的授时`` (7) that are perfectly locatable.
    # Ingest never reads either from here: ``batch.py`` does not import an
    # extraction-policy module, so the service layer injects the enforced value
    # into the batch repository.  The escape hatch is part of the requested
    # clause: a passage shorter than the minimum is quoted whole, so evidence
    # equal to its entire passage is compliant however short it is.
    requested_min_evidence_codepoints: int = 0
    enforced_min_evidence_codepoints: int = 0


# A registered profile is immutable.  A durable cloud job stores its profile,
# so editing v1 in place would silently re-point a stored request at an
# instruction that was never submitted.  A change is always a new entry.
_SECTION_GRAPH_PROFILES: dict[str, SectionGraphProfile] = {
    "zh-section-graph-v1": SectionGraphProfile(
        profile_id="zh-section-graph-v1",
        system_instruction=_SYSTEM_INSTRUCTION,
        output_schema=SECTION_GRAPH_OUTPUT_SCHEMA,
        max_tokens=1_800,
        asks_for_offsets=True,
    ),
    "zh-section-graph-v2": SectionGraphProfile(
        profile_id="zh-section-graph-v2",
        system_instruction=_SYSTEM_INSTRUCTION_V2,
        output_schema=SECTION_GRAPH_OUTPUT_SCHEMA_V2,
        asks_for_offsets=False,
        # The minimum evidence span v2's instruction names, transcribed.  Not a
        # new requirement and not an instruction edit: it is the number the text
        # has always carried, including its short-passage escape hatch.  What
        # ingest applies is the lower enforced floor beside it.
        requested_min_evidence_codepoints=10,
        enforced_min_evidence_codepoints=6,
        # v1's 1_800 tokens was never sized against a real packet.  Measured on
        # the actual book, a full run is 43 packets averaging 9_465 characters,
        # the largest 11_996 at SECTION_GRAPH_MAX_CHARACTERS = 12_000, and one
        # packet holds 191 passages.  1_800 tokens is roughly five concepts of
        # this contract: the response would stop mid-string, and a truncated
        # response is not JSON at all - the exact failure the concept path
        # already eliminated when it raised 512 to 2_048 for single passages.
        #
        # The budget has to cover the worst case the contract permits, not the
        # median one.  Per the arithmetic commented on zh-glossary-v6, CJK text
        # costs roughly one token per code point.  This contract permits 12
        # concepts, each with a local_id (~8 ASCII), a name (budget 20 code
        # points), up to 2 aliases (20 each), a definition of at most 30, and
        # one mention carrying a passage_id (a 36-character UUID, ~20 tokens),
        # evidence (budget 40) and two anchors of at most 48.  That is
        # 8 + 20 + 40 + 30 + 40 + 96 = 234 code points plus ~20 for the
        # passage_id and ~70 tokens of JSON field names and punctuation:
        # ~324 tokens per concept, ~3_900 for twelve.  It also permits 12
        # relations, each two local_ids, an enum predicate and one evidence
        # span: ~8 + 4 + 20 + 136 + ~60 = ~228 tokens, ~2_750 for twelve.
        # Together ~6_650 plus the wrapper object.
        #
        # Dropping the offsets is itself worth about 20 tokens per span - the
        # two field names and their digits - or ~480 tokens over the 24 spans
        # this contract allows; the caps above, not that saving, are what make
        # the worst case finite, because v1 capped nothing whatsoever.
        #
        # 8_192 clears ~6_650 with about 20% margin.  Typical responses are far
        # smaller: the anchors are empty unless the evidence actually repeats,
        # and most packets do not hold twelve retrievable concepts.
        max_tokens=8_192,
    ),
    DEFAULT_SECTION_GRAPH_PROFILE: SectionGraphProfile(
        profile_id=DEFAULT_SECTION_GRAPH_PROFILE,
        system_instruction=_SYSTEM_INSTRUCTION_V3,
        output_schema=SECTION_GRAPH_OUTPUT_SCHEMA_V2,
        asks_for_offsets=False,
        # v3 carries v2's minimum evidence clause verbatim, escape hatch
        # included, so it requests v2's number and is enforced at v2's floor.
        requested_min_evidence_codepoints=10,
        enforced_min_evidence_codepoints=6,
        # v3 changes the instruction and the packet payload, never the decoding
        # budget: a difference in max_tokens or temperature would confound the
        # comparison against the two submitted v2 samples.  The payload got
        # strictly smaller (one fewer field per passage, on packets holding up
        # to 191 of them), so v2's worst-case arithmetic above still bounds it.
        max_tokens=8_192,
    ),
}


def available_section_graph_profiles() -> tuple[str, ...]:
    return tuple(_SECTION_GRAPH_PROFILES)


def get_section_graph_profile(profile_id: str) -> SectionGraphProfile:
    profile = _SECTION_GRAPH_PROFILES.get(profile_id)
    if profile is None:
        raise SectionGraphError(f"unknown EPUB section graph profile: {profile_id}")
    return profile


def build_section_graph_packets(
    passages: Sequence[Mapping[str, Any]], *, max_characters: int = SECTION_GRAPH_MAX_CHARACTERS
) -> list[SectionGraphPacket]:
    """Partition visible source passages into deterministic bounded TOC packets."""
    if max_characters < 1:
        raise SectionGraphError("section graph packet size must be positive")
    ordered = sorted(
        passages, key=lambda row: (int(row.get("ordinal", 0)), str(row.get("passage_id", "")))
    )
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for passage in ordered:
        passage_id = passage.get("passage_id")
        content = passage.get("content")
        if not isinstance(passage_id, str) or not passage_id or not isinstance(content, str) or not content:
            raise SectionGraphError("section graph packets require stored passage IDs and visible text")
        raw_path = passage.get("toc_path")
        path = tuple(str(part) for part in raw_path) if isinstance(raw_path, (list, tuple)) else ()
        groups.setdefault(path[:1], []).append(passage)

    packets: list[SectionGraphPacket] = []
    for root_path, group in groups.items():
        chunk: list[Mapping[str, Any]] = []
        size = 0
        index = 0
        for passage in group:
            content = str(passage["content"])
            if chunk and size + len(content) > max_characters:
                packets.append(_packet(root_path, index, chunk))
                index += 1
                chunk, size = [], 0
            chunk.append(passage)
            size += len(content)
        if chunk:
            packets.append(_packet(root_path, index, chunk))
    return packets


def build_section_graph_completion_request(
    *,
    model: str,
    packet: SectionGraphPacket,
    profile_id: str = DEFAULT_SECTION_GRAPH_PROFILE,
) -> dict[str, Any]:
    """Return a remote Structured Outputs request for one immutable TOC packet.

    ``profile_id`` selects the packet contract.  A superseded profile stays
    selectable and byte-identical so a stored request replays exactly.  An
    already-submitted request replays from its persisted row rather than from
    this builder, so the payload shape below is free to shrink.

    A passage carries only its ``passage_id`` and its ``content``.  It used to
    carry ``toc_path`` too, and that field was the measured cause of most of
    the v2 sample's rejections: the model quoted section titles as evidence,
    which is verbatim copying from something we handed it, so no instruction
    against paraphrasing could catch it.  Nothing needs the field - a packet is
    one TOC subtree, ``packet.toc_path`` names it once, and TOC provenance is
    written server-side per SDD 4.2.2 point 1 - so it is not sent.
    """
    if not model.strip():
        raise SectionGraphError("section graph model cannot be empty")
    profile = get_section_graph_profile(profile_id)
    visible_passages = [
        {
            "passage_id": str(passage["passage_id"]),
            "content": str(passage["content"]),
        }
        for passage in packet.passages
    ]
    return {
        "model": model.strip(),
        "temperature": profile.temperature,
        "seed": 0,
        "max_tokens": profile.max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "epub_section_graph",
                "strict": True,
                "schema": profile.output_schema,
            },
        },
        "messages": [
            {"role": "system", "content": profile.system_instruction},
            {
                "role": "user",
                "content": json.dumps(
                    {"packet_id": packet.packet_id, "toc_path": list(packet.toc_path), "passages": visible_passages},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
    }


def _packet(path: tuple[str, ...], index: int, passages: Sequence[Mapping[str, Any]]) -> SectionGraphPacket:
    anchor = str(passages[0]["passage_id"])
    root = path[0] if path else "root"
    return SectionGraphPacket(
        packet_id=f"{root}:{index}:{anchor}",
        anchor_passage_id=anchor,
        toc_path=path,
        passages=tuple(passages),
    )

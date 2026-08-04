"""TOC-scoped packets and strict contracts for one-pass section graph extraction."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence


SECTION_GRAPH_PROFILE = "zh-section-graph-v1"
SECTION_GRAPH_MAX_CHARACTERS = 12_000

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

SECTION_GRAPH_OUTPUT_SCHEMA: dict[str, Any] = {
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
                    "mentions": {"type": "array", "items": _MENTION_SCHEMA},
                },
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["subject_local_id", "predicate", "object_local_id", "evidence"],
                "properties": {
                    "subject_local_id": {"type": "string", "minLength": 1},
                    "predicate": {"type": "string", "enum": list(RELATION_PREDICATES)},
                    "object_local_id": {"type": "string", "minLength": 1},
                    "evidence": {"type": "array", "minItems": 1, "items": _MENTION_SCHEMA},
                },
            },
        },
    },
}


_SYSTEM_INSTRUCTION = """你是中文 EPUB 的章节概念图抽取器。输入是一个 TOC 范围内的原文段落列表；
每条段落都有不可变 passage_id、目录路径和原文。一次输出 concepts 与 relations。

concepts：只抽取可检索且有明确文本依据的专名、术语、人物、组织、事件、制度或作品。每个 concept
使用本响应唯一的 local_id。mentions 必须明确 passage_id；start_codepoint 从 0 开始，end_codepoint
为排他位置，evidence 必须逐字等于该 passage 的切片。不要依据外部知识补充事实。

relations：只连接本响应 concepts 中的两个不同 local_id；predicate 只能使用指定枚举。关系必须由
本章节包内的原文证据支持，evidence 至少一条且必须使用精确 passage_id 与字符位置。TOC 父子结构
已经由程序保存，不要把单纯目录层级重复写成语义关系；只有原文支持概念层面的关系才输出。

没有合格概念或关系时返回相应空数组。只返回严格 JSON 对象，不要 Markdown 或解释。"""


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


def build_section_graph_completion_request(*, model: str, packet: SectionGraphPacket) -> dict[str, Any]:
    """Return a remote Structured Outputs request for one immutable TOC packet."""
    if not model.strip():
        raise SectionGraphError("section graph model cannot be empty")
    visible_passages = [
        {
            "passage_id": str(passage["passage_id"]),
            "toc_path": list(passage.get("toc_path", ())),
            "content": str(passage["content"]),
        }
        for passage in packet.passages
    ]
    return {
        "model": model.strip(),
        "temperature": 0.0,
        "seed": 0,
        "max_tokens": 1_800,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "epub_section_graph", "strict": True, "schema": SECTION_GRAPH_OUTPUT_SCHEMA},
        },
        "messages": [
            {"role": "system", "content": _SYSTEM_INSTRUCTION},
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

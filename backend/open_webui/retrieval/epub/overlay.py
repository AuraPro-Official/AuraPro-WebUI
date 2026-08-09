"""Portable, text-free EPUB concept overlay artifacts.

An administrator builds the concept graph once, using a paid cloud Batch
pipeline.  Every other installation runs against an empty local store and must
not have to pay for its own Batch run, so the *analysis* has to be publishable
on its own.  The book must not be: the artifact therefore carries concept
labels, definitions and **locations**, and never a single character of passage
text.  A receiving store already owns the book; it re-derives every evidence
string from its own passages.

Reattachment is possible because the parser is deterministic and versioned, so
the same EPUB bytes always yield the same ordered passages.  A location is the
pair ``(ordinal, content_sha256)`` plus code-point offsets, which is verifiable
against the importer's own copy without ever shipping the text it identifies.

The module is deliberately pure: it builds, validates, orders and serializes
the artifact and knows nothing about SQLite, HTTP or the parser package.  Its
only invariants are artifact-shaped ones; storage invariants stay in the store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping, Sequence


OVERLAY_FORMAT_VERSION = 1

CONCEPT_STATUSES = frozenset({"PROVISIONAL", "APPROVED", "REJECTED"})

# The relation predicate vocabulary belongs to the canonical store, which
# rejects an unsupported value while applying the artifact.  Only the shape is
# checked here so a second, silently diverging list cannot exist.
MAX_PREDICATE_LENGTH = 64
MAX_LABEL_LENGTH = 500
MAX_DEFINITION_LENGTH = 10_000


class OverlayError(ValueError):
    """The artifact itself is malformed, so nothing about it can be trusted."""


def normalize_concept_key(value: str) -> str:
    """Fold a concept label to its identity key.

    This is the artifact's join key *and* the canonical store's
    ``concepts.normalized_name`` rule; the store imports it rather than
    repeating it, because two spellings of "the same rule" would eventually
    disagree and silently split or merge concepts on import.
    """
    if not isinstance(value, str):
        raise OverlayError("a concept name or alias must be a string")
    return " ".join(value.split()).casefold()


@dataclass(frozen=True)
class PassageFingerprint:
    """A content-free summary of one version's complete ordered passage set."""

    count: int
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {"count": self.count, "digest": self.digest}


def passage_fingerprint(pairs: Iterable[tuple[int, str]]) -> PassageFingerprint:
    """Digest ordered ``ordinal:content_sha256`` lines for a whole version.

    Passage hashes, not passage text, are the input, so the fingerprint can
    travel in a published artifact.  Ordinals are sorted here rather than
    trusted from the caller: the digest must depend only on the passage set.
    """
    ordered = sorted((int(ordinal), str(digest)) for ordinal, digest in pairs)
    seen: set[int] = set()
    for ordinal, digest in ordered:
        if ordinal < 0:
            raise OverlayError("a passage ordinal cannot be negative")
        if ordinal in seen:
            raise OverlayError("a passage ordinal cannot repeat within one version")
        seen.add(ordinal)
        _require_sha256(digest, "passage content_sha256")
    lines = "\n".join(f"{ordinal}:{digest}" for ordinal, digest in ordered)
    return PassageFingerprint(len(ordered), sha256(lines.encode("utf-8")).hexdigest())


@dataclass(frozen=True)
class OverlaySpan:
    """One verifiable location inside the importer's own copy of the book."""

    ordinal: int
    content_sha256: str
    start_codepoint: int
    end_codepoint: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "content_sha256": self.content_sha256,
            "start_codepoint": self.start_codepoint,
            "end_codepoint": self.end_codepoint,
        }

    @property
    def sort_key(self) -> tuple[int, int, int]:
        return (self.ordinal, self.start_codepoint, self.end_codepoint)


@dataclass(frozen=True)
class OverlayConcept:
    """A concept label, its spellings and its definition — the analysis itself."""

    key: str
    canonical_name: str
    aliases: tuple[str, ...] = ()
    definition: str = ""
    status: str = "PROVISIONAL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "definition": self.definition,
            "status": self.status,
        }


@dataclass(frozen=True)
class OverlayMention:
    """Where a concept occurs, as a location only."""

    concept_key: str
    ordinal: int
    content_sha256: str
    start_codepoint: int
    end_codepoint: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "concept_key": self.concept_key,
            "ordinal": self.ordinal,
            "content_sha256": self.content_sha256,
            "start_codepoint": self.start_codepoint,
            "end_codepoint": self.end_codepoint,
        }

    @property
    def span(self) -> OverlaySpan:
        return OverlaySpan(
            self.ordinal, self.content_sha256, self.start_codepoint, self.end_codepoint
        )

    @property
    def sort_key(self) -> tuple[str, int, int, int]:
        return (self.concept_key, self.ordinal, self.start_codepoint, self.end_codepoint)


@dataclass(frozen=True)
class OverlayRelation:
    """One grounded edge whose evidence travels as locations only."""

    subject_key: str
    predicate: str
    object_key: str
    status: str = "PROVISIONAL"
    evidence: tuple[OverlaySpan, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_key": self.subject_key,
            "predicate": self.predicate,
            "object_key": self.object_key,
            "status": self.status,
            "evidence": [span.to_dict() for span in self.evidence],
        }

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.subject_key, self.predicate, self.object_key)


@dataclass(frozen=True)
class ConceptOverlay:
    """A complete, deterministic, source-free analysis overlay."""

    epub_sha256: str
    parser_version: str
    book_title: str
    fingerprint: PassageFingerprint
    concepts: tuple[OverlayConcept, ...] = ()
    mentions: tuple[OverlayMention, ...] = ()
    relations: tuple[OverlayRelation, ...] = ()
    overlay_format_version: int = OVERLAY_FORMAT_VERSION
    concept_keys: frozenset[str] = field(default_factory=frozenset, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overlay_format_version": self.overlay_format_version,
            "epub_sha256": self.epub_sha256,
            "parser_version": self.parser_version,
            "book_title": self.book_title,
            "passage_fingerprint": self.fingerprint.to_dict(),
            "concepts": [concept.to_dict() for concept in self.concepts],
            "mentions": [mention.to_dict() for mention in self.mentions],
            "relations": [relation.to_dict() for relation in self.relations],
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    def digest(self) -> str:
        """SHA-256 of the artifact's canonical bytes, for publication."""
        return overlay_sha256(self.to_json())


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialize deterministically so two exports of one graph are equal bytes.

    ``_canonical_json`` in ``batch.py`` is the existing precedent: sorted keys,
    ``ensure_ascii=False`` so Chinese labels stay readable, tight separators.
    """
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise OverlayError("the overlay artifact must be JSON serializable") from error


def overlay_sha256(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def build_overlay(
    *,
    epub_sha256: str,
    parser_version: str,
    book_title: str,
    fingerprint: PassageFingerprint,
    concepts: Iterable[OverlayConcept] = (),
    mentions: Iterable[OverlayMention] = (),
    relations: Iterable[OverlayRelation] = (),
    overlay_format_version: int = OVERLAY_FORMAT_VERSION,
) -> ConceptOverlay:
    """Validate, deduplicate and order an artifact into its canonical form.

    Export and import both come through here, so a parsed artifact is held to
    exactly the same rules as a freshly built one and the byte-for-byte
    ordering contract has a single implementation.
    """
    if overlay_format_version != OVERLAY_FORMAT_VERSION:
        raise OverlayError(
            f"unsupported overlay_format_version: {overlay_format_version!r}"
        )
    _require_sha256(epub_sha256, "epub_sha256")
    if not isinstance(parser_version, str) or not parser_version.strip() or len(parser_version) > 32:
        raise OverlayError("parser_version must be a short non-empty string")
    if not isinstance(book_title, str) or len(book_title) > MAX_LABEL_LENGTH:
        raise OverlayError("book_title must be a string of at most 500 characters")
    if not isinstance(fingerprint, PassageFingerprint):
        raise OverlayError("passage_fingerprint must be a count and a digest")
    if not isinstance(fingerprint.count, int) or fingerprint.count < 0:
        raise OverlayError("passage_fingerprint.count must be a non-negative integer")
    _require_sha256(fingerprint.digest, "passage_fingerprint.digest")

    ordered_concepts: dict[str, OverlayConcept] = {}
    for concept in concepts:
        validated = _validate_concept(concept)
        previous = ordered_concepts.get(validated.key)
        if previous is not None and previous != validated:
            raise OverlayError(f"overlay declares one concept key twice: {validated.key!r}")
        ordered_concepts[validated.key] = validated
    keys = frozenset(ordered_concepts)

    unique_mentions = {
        mention.sort_key: mention
        for mention in (_validate_mention(value, keys) for value in mentions)
    }
    unique_relations: dict[tuple[str, str, str], OverlayRelation] = {}
    for relation in relations:
        validated = _validate_relation(relation, keys)
        previous = unique_relations.get(validated.sort_key)
        if previous is not None:
            # Two entries for one edge are folded rather than refused: the edge
            # is the identity, and losing an evidence span would weaken the
            # importer's ability to verify it.
            merged = {span.sort_key: span for span in (*previous.evidence, *validated.evidence)}
            validated = OverlayRelation(
                subject_key=validated.subject_key,
                predicate=validated.predicate,
                object_key=validated.object_key,
                status=_stronger_status(previous.status, validated.status),
                evidence=tuple(merged[key] for key in sorted(merged)),
            )
        unique_relations[validated.sort_key] = validated

    return ConceptOverlay(
        epub_sha256=epub_sha256,
        parser_version=parser_version,
        book_title=book_title,
        fingerprint=fingerprint,
        concepts=tuple(ordered_concepts[key] for key in sorted(ordered_concepts)),
        mentions=tuple(unique_mentions[key] for key in sorted(unique_mentions)),
        relations=tuple(unique_relations[key] for key in sorted(unique_relations)),
        concept_keys=keys,
    )


_STATUS_STRENGTH = {"REJECTED": 0, "PROVISIONAL": 1, "APPROVED": 2}


def _stronger_status(left: str, right: str) -> str:
    return left if _STATUS_STRENGTH[left] >= _STATUS_STRENGTH[right] else right


def parse_overlay(payload: Any) -> ConceptOverlay:
    """Read an untrusted artifact document into its validated canonical form."""
    if not isinstance(payload, Mapping):
        raise OverlayError("an overlay artifact must be a JSON object")
    unknown = set(payload) - {
        "overlay_format_version",
        "epub_sha256",
        "parser_version",
        "book_title",
        "passage_fingerprint",
        "concepts",
        "mentions",
        "relations",
    }
    if unknown:
        # An unexpected field is far more likely to be smuggled passage text or
        # a newer format than a harmless extra, so refuse rather than ignore.
        raise OverlayError(f"overlay artifact has unsupported fields: {sorted(unknown)}")
    fingerprint = payload.get("passage_fingerprint")
    if not isinstance(fingerprint, Mapping) or set(fingerprint) != {"count", "digest"}:
        raise OverlayError("passage_fingerprint must hold exactly a count and a digest")
    location_fields = frozenset({"ordinal", "content_sha256", "start_codepoint", "end_codepoint"})
    return build_overlay(
        overlay_format_version=_int(payload.get("overlay_format_version"), "overlay_format_version"),
        epub_sha256=_text(payload.get("epub_sha256"), "epub_sha256"),
        parser_version=_text(payload.get("parser_version"), "parser_version"),
        book_title=_text(payload.get("book_title"), "book_title", allow_empty=True),
        fingerprint=PassageFingerprint(
            count=_int(fingerprint.get("count"), "passage_fingerprint.count"),
            digest=_text(fingerprint.get("digest"), "passage_fingerprint.digest"),
        ),
        concepts=[
            OverlayConcept(
                key=_text(item.get("key"), "concept key"),
                canonical_name=_text(item.get("canonical_name"), "canonical_name"),
                aliases=tuple(_alias_list(item.get("aliases"))),
                definition=_text(item.get("definition", ""), "definition", allow_empty=True),
                status=_text(item.get("status", "PROVISIONAL"), "concept status"),
            )
            for item in _objects(
                payload.get("concepts"),
                "concepts",
                frozenset({"key", "canonical_name", "aliases", "definition", "status"}),
            )
        ],
        mentions=[
            OverlayMention(
                concept_key=_text(item.get("concept_key"), "concept_key"),
                ordinal=_int(item.get("ordinal"), "mention ordinal"),
                content_sha256=_text(item.get("content_sha256"), "mention content_sha256"),
                start_codepoint=_int(item.get("start_codepoint"), "start_codepoint"),
                end_codepoint=_int(item.get("end_codepoint"), "end_codepoint"),
            )
            for item in _objects(
                payload.get("mentions"), "mentions", location_fields | {"concept_key"}
            )
        ],
        relations=[
            OverlayRelation(
                subject_key=_text(item.get("subject_key"), "subject_key"),
                predicate=_text(item.get("predicate"), "predicate"),
                object_key=_text(item.get("object_key"), "object_key"),
                status=_text(item.get("status", "PROVISIONAL"), "relation status"),
                evidence=tuple(
                    OverlaySpan(
                        ordinal=_int(span.get("ordinal"), "evidence ordinal"),
                        content_sha256=_text(span.get("content_sha256"), "evidence content_sha256"),
                        start_codepoint=_int(span.get("start_codepoint"), "evidence start_codepoint"),
                        end_codepoint=_int(span.get("end_codepoint"), "evidence end_codepoint"),
                    )
                    for span in _objects(item.get("evidence"), "relation evidence", location_fields)
                ),
            )
            for item in _objects(
                payload.get("relations"),
                "relations",
                frozenset({"subject_key", "predicate", "object_key", "status", "evidence"}),
            )
        ],
    )


def parse_overlay_json(data: bytes | str) -> ConceptOverlay:
    """Decode UTF-8 artifact bytes without leaking a parser's own error text."""
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        payload = json.loads(text)
    except (UnicodeDecodeError, ValueError) as error:
        raise OverlayError("the overlay artifact must be a UTF-8 JSON document") from error
    return parse_overlay(payload)


def _validate_concept(concept: OverlayConcept) -> OverlayConcept:
    if not isinstance(concept, OverlayConcept):
        raise OverlayError("each overlay concept must be a concept object")
    canonical = concept.canonical_name
    if not isinstance(canonical, str) or not canonical.strip():
        raise OverlayError("a concept needs a non-empty canonical_name")
    if len(canonical) > MAX_LABEL_LENGTH:
        raise OverlayError("a concept canonical_name is too long")
    expected_key = normalize_concept_key(canonical)
    if concept.key != expected_key:
        # The key is the join key for every mention and relation, so a key
        # that does not fold from its own label would attach the analysis to
        # the wrong concept in the receiving store.
        raise OverlayError("a concept key must be the normalized form of its canonical_name")
    if concept.status not in CONCEPT_STATUSES:
        raise OverlayError(f"invalid concept status: {concept.status!r}")
    if not isinstance(concept.definition, str) or len(concept.definition) > MAX_DEFINITION_LENGTH:
        raise OverlayError("a concept definition must be a string of at most 10000 characters")
    aliases: dict[str, str] = {}
    for alias in concept.aliases:
        if not isinstance(alias, str) or not alias.strip() or len(alias) > MAX_LABEL_LENGTH:
            raise OverlayError("a concept alias must be a non-empty label")
        aliases[normalize_concept_key(alias)] = alias
    aliases.setdefault(expected_key, canonical)
    return OverlayConcept(
        key=concept.key,
        canonical_name=canonical,
        aliases=tuple(aliases[key] for key in sorted(aliases)),
        definition=concept.definition,
        status=concept.status,
    )


def _validate_mention(mention: OverlayMention, keys: frozenset[str]) -> OverlayMention:
    if not isinstance(mention, OverlayMention):
        raise OverlayError("each overlay mention must be a mention object")
    if mention.concept_key not in keys:
        raise OverlayError("an overlay mention names a concept the artifact does not declare")
    _validate_location(mention.ordinal, mention.content_sha256, mention.start_codepoint, mention.end_codepoint)
    return mention


def _validate_relation(relation: OverlayRelation, keys: frozenset[str]) -> OverlayRelation:
    if not isinstance(relation, OverlayRelation):
        raise OverlayError("each overlay relation must be a relation object")
    if relation.subject_key not in keys or relation.object_key not in keys:
        raise OverlayError("an overlay relation names a concept the artifact does not declare")
    if relation.subject_key == relation.object_key:
        raise OverlayError("an overlay relation needs two distinct concept endpoints")
    predicate = relation.predicate
    if not isinstance(predicate, str) or not predicate.strip() or len(predicate) > MAX_PREDICATE_LENGTH:
        raise OverlayError("an overlay relation predicate must be a short non-empty string")
    if relation.status not in CONCEPT_STATUSES:
        raise OverlayError(f"invalid relation status: {relation.status!r}")
    if not relation.evidence:
        raise OverlayError("an overlay relation needs at least one evidence location")
    spans: dict[tuple[int, int, int], OverlaySpan] = {}
    for span in relation.evidence:
        if not isinstance(span, OverlaySpan):
            raise OverlayError("each relation evidence entry must be a location object")
        _validate_location(span.ordinal, span.content_sha256, span.start_codepoint, span.end_codepoint)
        spans[span.sort_key] = span
    return OverlayRelation(
        subject_key=relation.subject_key,
        predicate=predicate,
        object_key=relation.object_key,
        status=relation.status,
        evidence=tuple(spans[key] for key in sorted(spans)),
    )


def _validate_location(ordinal: int, digest: str, start: int, end: int) -> None:
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        raise OverlayError("a location ordinal must be a non-negative integer")
    _require_sha256(digest, "a location content_sha256")
    for value in (start, end):
        if not isinstance(value, int) or isinstance(value, bool):
            raise OverlayError("location offsets must be integer code-point positions")
    if start < 0 or end <= start:
        raise OverlayError("a location must identify a non-empty forward span")


def _require_sha256(value: Any, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OverlayError(f"{label} must be a lowercase 64-character SHA-256 digest")


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise OverlayError(f"{label} must be a non-empty string")
    return value


def _int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise OverlayError(f"{label} must be an integer")
    return value


def _alias_list(value: Any) -> Sequence[str]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise OverlayError("concept aliases must be a list of labels")
    return value


def _objects(value: Any, label: str, allowed: frozenset[str]) -> Sequence[Mapping[str, Any]]:
    """Read a strict list of objects, refusing any field the shape lacks.

    Strictness is the point rather than politeness: an unexpected field is the
    obvious place to smuggle passage text into an artifact that is defined by
    carrying none, so an artifact with one is refused outright instead of
    being silently accepted with the field dropped.
    """
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise OverlayError(f"{label} must be a list of objects")
    for item in value:
        unknown = set(item) - allowed
        if unknown:
            raise OverlayError(f"{label} has unsupported fields: {sorted(unknown)}")
    return value

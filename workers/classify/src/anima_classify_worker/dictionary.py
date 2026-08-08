from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


DICTIONARY_BUCKETS = frozenset({
    "quality", "count", "character", "series", "artist", "appearance", "tags", "environment", "drop"
})
WRITABLE_BUCKETS = frozenset({"character", "appearance", "tags", "environment"})
PRESERVED_COUNT_TAGS = frozenset({
    "solo", "duo", "trio", "group", "large_group", "crowd", "solo_focus", "duo_focus", "trio_focus"
})
# e621 lore tags describe off-screen setting, not visible content; they are dropped at
# the projection layer so the semantics do not depend on optional Replace.
LORE_SUFFIX = "_(lore)"
# e621 puts these in its character category, but they mark "an original/unnamed character
# is present" instead of one identity, so they are neither written nor counted.
NON_IDENTITY_CHARACTER_TAGS = frozenset({"fan_character", "unnamed_character", "anon"})


class ClassifyDictionaryError(ValueError):
    pass


@dataclass(frozen=True)
class DictionaryEntry:
    canonical: str
    bucket: str
    output: str


@dataclass(frozen=True)
class ClassificationProjection:
    character: str
    series: str
    appearance: tuple[str, ...]
    tags: tuple[str, ...]
    environment: tuple[str, ...]
    canonical_character_ids: tuple[str, ...]
    evidence_tags: tuple[str, ...]
    input_tag_count: int
    output_tag_count: int
    dropped_tag_count: int

    def to_json_projection(self, count: str) -> dict[str, object]:
        return {
            "quality": [],
            "count": count,
            "character": self.character,
            "series": self.series,
            "artist": "",
            "appearance": list(self.appearance),
            "tags": list(self.tags),
            "environment": list(self.environment),
            "nl": "",
        }


class E621Dictionary:
    def __init__(self, payload: Mapping[str, object]) -> None:
        metadata = payload.get("metadata")
        raw_entries = payload.get("entries")
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("source") != "e621"
            or metadata.get("entry_count") != 120_978
            or not isinstance(raw_entries, Mapping)
            or len(raw_entries) != 120_978
        ):
            raise ClassifyDictionaryError("E621 dictionary metadata is invalid")
        entries: dict[str, DictionaryEntry] = {}
        for tag, raw in raw_entries.items():
            if not isinstance(tag, str) or not tag or not isinstance(raw, Mapping):
                raise ClassifyDictionaryError("E621 dictionary entry is invalid")
            required = {"tag", "canonical", "aliases", "post_count", "site_category", "bucket", "output", "method", "confidence"}
            if set(raw) != required or raw.get("tag") != tag:
                raise ClassifyDictionaryError("E621 dictionary entry fields are invalid")
            canonical = raw.get("canonical")
            bucket = raw.get("bucket")
            output = raw.get("output")
            if (
                not isinstance(canonical, str)
                or not canonical
                or not isinstance(bucket, str)
                or bucket not in DICTIONARY_BUCKETS
                or not isinstance(output, str)
                or not output
                or any(character in output for character in ",\r\n\x00")
            ):
                raise ClassifyDictionaryError("E621 dictionary entry values are invalid")
            entries[tag] = DictionaryEntry(canonical=canonical, bucket=bucket, output=output)
        for entry in entries.values():
            if entry.canonical not in entries:
                raise ClassifyDictionaryError("E621 dictionary canonical target is missing")
        self.entries = entries

    def classify(self, tags: list[str]) -> ClassificationProjection:
        values: dict[str, list[str]] = {bucket: [] for bucket in WRITABLE_BUCKETS}
        seen_outputs: dict[str, set[str]] = {bucket: set() for bucket in WRITABLE_BUCKETS}
        character_ids: list[str] = []
        seen_character_ids: set[str] = set()
        evidence: list[str] = []
        seen_evidence: set[str] = set()
        dropped = 0
        for tag in tags:
            entry = self.entries.get(tag)
            canonical = entry.canonical if entry is not None else tag
            for evidence_tag in (tag, canonical):
                if evidence_tag not in seen_evidence:
                    evidence.append(evidence_tag)
                    seen_evidence.add(evidence_tag)
            if entry is None:
                if canonical not in PRESERVED_COUNT_TAGS:
                    dropped += 1
                    continue
                bucket = "tags"
                output = canonical
            else:
                if (
                    entry.bucket == "drop"
                    or canonical.endswith(LORE_SUFFIX)
                    or canonical in NON_IDENTITY_CHARACTER_TAGS
                ):
                    dropped += 1
                    continue
                bucket = "tags" if entry.bucket == "count" else entry.bucket
                output = entry.output
                if bucket not in WRITABLE_BUCKETS:
                    dropped += 1
                    continue
            if output not in seen_outputs[bucket]:
                values[bucket].append(output)
                seen_outputs[bucket].add(output)
            if bucket == "character" and canonical not in seen_character_ids:
                character_ids.append(canonical)
                seen_character_ids.add(canonical)
        output_count = sum(len(values[bucket]) for bucket in ("character", "appearance", "tags", "environment"))
        return ClassificationProjection(
            character=", ".join(values["character"]),
            series="",
            appearance=tuple(values["appearance"]),
            tags=tuple(values["tags"]),
            environment=tuple(values["environment"]),
            canonical_character_ids=tuple(character_ids),
            evidence_tags=tuple(evidence),
            input_tag_count=len(tags),
            output_tag_count=output_count,
            dropped_tag_count=dropped,
        )


DANBOORU_BUCKETS = frozenset({"character", "series", "appearance", "tags", "environment", "drop"})
DANBOORU_METHODS = frozenset({
    "count_rule", "model_category", "site_category", "site_alias_category",
    "audited_overlay", "general_fallback",
})


class DanbooruDictionary:
    def __init__(self, payload: Mapping[str, object]) -> None:
        metadata = payload.get("metadata")
        raw_entries = payload.get("entries")
        metadata_fields = {
            "schemaVersion", "source", "entryCount", "catalogSnapshot", "catalogSourceUrl",
            "catalogSourceSizeBytes", "catalogSourceSha256", "supportedVocabularyFingerprints",
        }
        if (
            not isinstance(metadata, Mapping)
            or set(metadata) != metadata_fields
            or metadata.get("schemaVersion") != 1
            or metadata.get("source") != "danbooru"
            or type(metadata.get("entryCount")) is not int
            or not isinstance(raw_entries, Mapping)
            or metadata["entryCount"] != len(raw_entries)
        ):
            raise ClassifyDictionaryError("Danbooru dictionary metadata is invalid")
        entries: dict[str, DictionaryEntry] = {}
        methods: dict[str, str] = {}
        for tag, raw in raw_entries.items():
            if not isinstance(tag, str) or not tag or not isinstance(raw, Mapping):
                raise ClassifyDictionaryError("Danbooru dictionary entry is invalid")
            if set(raw) != {"canonical", "bucket", "output", "method"}:
                raise ClassifyDictionaryError("Danbooru dictionary entry fields are invalid")
            canonical, bucket, output, method = (
                raw.get("canonical"), raw.get("bucket"), raw.get("output"), raw.get("method")
            )
            if (
                not isinstance(canonical, str)
                or not canonical
                or bucket not in DANBOORU_BUCKETS
                or output != tag
                or method not in DANBOORU_METHODS
                or any(character in tag for character in ",\r\n\x00")
            ):
                raise ClassifyDictionaryError("Danbooru dictionary entry values are invalid")
            entries[tag] = DictionaryEntry(canonical, str(bucket), tag)
            methods[tag] = str(method)
        if any(entry.canonical not in entries for entry in entries.values()):
            raise ClassifyDictionaryError("Danbooru dictionary canonical target is missing")
        for tag, entry in entries.items():
            method = methods[tag]
            if (
                (method == "count_rule" and entry.bucket != "tags")
                or (method == "general_fallback" and entry.bucket != "tags")
                or (method == "audited_overlay" and entry.bucket not in {"appearance", "environment", "tags"})
            ):
                raise ClassifyDictionaryError("Danbooru dictionary method and bucket do not match")
        self.entries = entries

    def classify(self, tags: list[str]) -> ClassificationProjection:
        buckets = ("character", "series", "appearance", "tags", "environment")
        values: dict[str, list[str]] = {bucket: [] for bucket in buckets}
        seen_outputs: dict[str, set[str]] = {bucket: set() for bucket in buckets}
        evidence: list[str] = []
        seen_evidence: set[str] = set()
        dropped = 0
        for tag in tags:
            if tag not in seen_evidence:
                evidence.append(tag)
                seen_evidence.add(tag)
            entry = self.entries.get(tag)
            if entry is None:
                bucket, output = "tags", tag
            else:
                bucket, output = entry.bucket, entry.output
            if bucket == "drop":
                dropped += 1
                continue
            if output not in seen_outputs[bucket]:
                values[bucket].append(output)
                seen_outputs[bucket].add(output)
        output_count = sum(len(values[bucket]) for bucket in buckets)
        return ClassificationProjection(
            character=", ".join(values["character"]),
            series=", ".join(values["series"]),
            appearance=tuple(values["appearance"]),
            tags=tuple(values["tags"]),
            environment=tuple(values["environment"]),
            canonical_character_ids=(),
            evidence_tags=tuple(evidence),
            input_tag_count=len(tags),
            output_tag_count=output_count,
            dropped_tag_count=dropped,
        )

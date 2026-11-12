"""Pure whole-original segmentation checks used by opt-in recovery plans."""
from __future__ import annotations

from ..agent_evidence import digest
from .contracts import SEGMENT_SCHEMA, WorkUnitError, detached, hash_value, identifier


def verify_document_segments(document_id, content, segments):
    identifier(document_id, "document")
    if type(content) is not str or not content or type(segments) is not list or not segments:
        raise WorkUnitError("SEGMENTS_INVALID", "Nonempty original text and segment manifest are required")
    segments = detached(segments)
    cursor, names = 0, set()
    base = {"id", "start", "end", "sha256"}
    context = {"context_start", "context_end", "context_sha256"}
    for row in segments:
        if type(row) is not dict or set(row) not in (base, base | context):
            raise WorkUnitError("SEGMENTS_INVALID", "Segment identity fields differ")
        name = identifier(row["id"], "segment")
        if name in names:
            raise WorkUnitError("SEGMENTS_INVALID", "Segment identity is duplicated")
        names.add(name)
        if (any(type(row[key]) is not int for key in ("start", "end"))
                or row["start"] != cursor or not cursor < row["end"] <= len(content)
                or hash_value(row["sha256"]) != digest(content[row["start"]:row["end"]].encode())):
            raise WorkUnitError("SEGMENTS_INVALID", "Segments must cover the original with exact contiguous core ranges")
        if context <= row.keys():
            if (any(type(row[key]) is not int for key in ("context_start", "context_end"))
                    or not 0 <= row["context_start"] <= row["start"] < row["end"] <= row["context_end"] <= len(content)
                    or hash_value(row["context_sha256"]) != digest(content[row["context_start"]:row["context_end"]].encode())):
                raise WorkUnitError("SEGMENTS_INVALID", "Segment context differs from the full original")
        cursor = row["end"]
    if cursor != len(content):
        raise WorkUnitError("SEGMENTS_INVALID", "Segment manifest omits the original tail")
    return {"schema": SEGMENT_SCHEMA, "document_id": document_id,
            "source_sha256": digest(content.encode()), "characters": len(content),
            "segments": segments, "complete": True}


def make_document_segments(document_id, content, core_spans, *, context_characters=0):
    """Hash caller-planned legal windows; never decide a model token cutoff.

    The planner must supply every original character. Optional surrounding
    context is copied from the same original and may overlap adjacent windows.
    """
    if (type(content) is not str or type(core_spans) is not list or not core_spans
            or type(context_characters) is not int or context_characters < 0):
        raise WorkUnitError("SEGMENTS_INVALID", "Explicit character windows and bounded context are required")
    segments = []
    for index, span in enumerate(core_spans, 1):
        if (not isinstance(span, (list, tuple)) or len(span) != 2
                or any(type(number) is not int for number in span)
                or not 0 <= span[0] < span[1] <= len(content)):
            raise WorkUnitError("SEGMENTS_INVALID", "Invalid caller-planned segment range")
        start, end = span
        row = {"id": "segment-" + str(index), "start": start, "end": end,
               "sha256": digest(content[start:end].encode())}
        if context_characters:
            left, right = max(0, start - context_characters), min(len(content), end + context_characters)
            row.update(context_start=left, context_end=right,
                       context_sha256=digest(content[left:right].encode()))
        segments.append(row)
    return verify_document_segments(document_id, content, segments)

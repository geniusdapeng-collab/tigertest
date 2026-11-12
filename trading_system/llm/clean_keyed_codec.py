"""Lossless, closed document-keyed transport for typed clean annotations.

This codec changes the output container, never its semantic fields.  Each
input document is a required key and its value must repeat that exact ID.
Provider-specific schema projection belongs to the transport owner; the
complete keyed schema and original array schema are both checked here.
"""
from __future__ import annotations

from copy import deepcopy

CONTRACT = "typed-clean-keyed-inline/v1"


def _invalid():
    raise ValueError("INVALID_CLEAN_FINAL_SCHEMA")


def _closed_object(schema):
    if (not isinstance(schema, dict)
            or set(schema) != {"type", "properties", "required", "additionalProperties"}
            or schema["type"] != "object" or schema["additionalProperties"] is not False
            or not isinstance(schema["properties"], dict)
            or not isinstance(schema["required"], list)
            or any(not isinstance(key, str) for key in schema["properties"])
            or any(not isinstance(key, str) for key in schema["required"])
            or len(set(schema["required"])) != len(schema["required"])
            or set(schema["required"]) != set(schema["properties"])):
        _invalid()
    return schema["properties"]


def _without_references(value):
    if isinstance(value, dict):
        if {"$ref", "$defs", "$def"} & value.keys():
            _invalid()
        for item in value.values():
            _without_references(item)
    elif isinstance(value, list):
        for item in value:
            _without_references(item)


def _layout(final_schema):
    root = _closed_object(final_schema)
    if set(root) != {"documents"}:
        _invalid()
    documents = root["documents"]
    if (not isinstance(documents, dict)
            or set(documents) != {"type", "items", "minItems", "maxItems", "uniqueItems"}
            or documents["type"] != "array" or documents["uniqueItems"] is not True
            or type(documents["minItems"]) is not int or documents["minItems"] < 0
            or documents["maxItems"] != documents["minItems"]
            or type(documents["maxItems"]) is not int):
        _invalid()
    row = documents["items"]
    properties = _closed_object(row)
    if "id" not in properties:
        _invalid()
    identity = properties["id"]
    count = documents["minItems"]
    if count == 0:
        if identity != {"type": "string", "pattern": r"(?!)"}:
            _invalid()
        identifiers = []
    else:
        if (not isinstance(identity, dict) or set(identity) != {"type", "enum"}
                or identity["type"] != "string" or not isinstance(identity["enum"], list)
                or any(not isinstance(item, str) or not item for item in identity["enum"])
                or len(identity["enum"]) != count
                or len(set(identity["enum"])) != count):
            _invalid()
        identifiers = identity["enum"]
    _without_references(final_schema)
    return identifiers, row


def document_ids(final_schema):
    """Return the original final schema's ordered document identity contract."""
    identifiers, _ = _layout(final_schema)
    return list(identifiers)


def build_inline_schema(final_schema):
    """Build full constraints with a required, singleton-ID entry per document.

    Annotation definitions are expanded independently for every document.
    The caller must retain this full schema when deriving any provider grammar.
    """
    identifiers, row = _layout(final_schema)
    annotation = {"type": "object", "properties": {
        key: deepcopy(value) for key, value in row["properties"].items() if key != "id"},
        "required": [key for key in row["required"] if key != "id"],
        "additionalProperties": False}
    entries = {identifier: {"type": "object", "properties": {
        "id": {"type": "string", "enum": [identifier]},
        "annotation": deepcopy(annotation)},
        "required": ["id", "annotation"], "additionalProperties": False}
        for identifier in identifiers}
    keyed = {"type": "object", "properties": entries,
             "required": list(identifiers), "additionalProperties": False}
    return {"type": "object", "properties": {"documents": keyed},
            "required": ["documents"], "additionalProperties": False}


def flatten_strict_output(value, final_schema, *, validate):
    """Validate both contracts and reorder only the model's explicit values.

    No ID, nullable field, annotation value, or missing document is supplied by
    this function. Invalid values raise through the supplied schema validator.
    """
    schema = build_inline_schema(final_schema)
    validate(value, schema)
    entries = value["documents"]
    output = {"documents": [
        {"id": entries[identifier]["id"], **deepcopy(entries[identifier]["annotation"])}
        for identifier in document_ids(final_schema)]}
    validate(output, final_schema)
    return output

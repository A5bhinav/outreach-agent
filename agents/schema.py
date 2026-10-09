"""Tiny helpers for strict JSON schemas (every field required, no extras)."""


def obj(**props) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


def enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}

# Per-criterion judgement used by the researcher and the people finder.
ASSESSMENTS = arr(obj(criterion_number=INT, verdict=enum("yes", "no", "unclear"), evidence_url=STR))
FACTS = arr(obj(fact=STR, date=STR, source_url=STR))

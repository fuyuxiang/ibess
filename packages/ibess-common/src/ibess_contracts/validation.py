"""Schema plus semantic validation, shared by producers, consumers and contract tests."""

from datetime import datetime
from importlib.resources import files
import json
import re
from jsonschema import Draft202012Validator, FormatChecker

FORMATS = FormatChecker()


@FORMATS.checks("utc")
def utc(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value
    ):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


class ContractValidator:
    def __init__(self):
        self.validators = {}
        for path in files("ibess_contracts").joinpath("schemas/v2").iterdir():
            if path.name.endswith(".json"):
                schema = json.loads(path.read_text())
                Draft202012Validator.check_schema(schema)
                self.validators[path.name.removesuffix(".json")] = Draft202012Validator(
                    schema, format_checker=FORMATS
                )

    def validate_object(self, kind, value):
        validator = self.validators.get(kind)
        if validator is None:
            return [f"Unknown contract: {kind}"]
        errors = [
            f"{'/'.join(map(str, e.absolute_path)) or '$'}: {e.message}"
            for e in validator.iter_errors(value)
        ]
        if errors:
            return errors
        start, end = value.get("start_ms"), value.get("end_ms")
        if isinstance(start, int) and isinstance(end, int) and end < start:
            errors.append("end_ms precedes start_ms")
        if kind == "risk-clue" and value["binding_status"] == "unbound":
            if value["conclusion"] != "insufficient" or value["person_id"] is not None:
                errors.append(
                    "Unbound clues cannot identify a person or assert a violation"
                )
        if kind == "risk-clue" and value["binding_status"] == "bound":
            for ref in value["evidence_refs"]:
                # Multi-source audio/video fusion legitimately has separate sources/runs.
                if any(
                    ref.get(key) != value[key]
                    for key in ("project_id", "room_id", "session_id")
                ):
                    errors.append(
                        "Evidence belongs to a different project, room or session"
                    )
        if kind == "evidence-ref" and value["status"] == "ready":
            if value["sha256"] is None or start is None or end is None:
                errors.append("Ready evidence requires a hash and a capture range")
        if kind == "control-ack":
            if value["status"] == "applied" and (
                value["applied_at"] is None or value["error"] is not None
            ):
                errors.append(
                    "Applied acknowledgement requires applied_at and no error"
                )
            if value["status"] in ("failed", "missing_asset") and (
                not value["error"] or value["applied_at"] is not None
            ):
                errors.append(
                    "Unapplied acknowledgement requires an error and no applied_at"
                )
        return errors

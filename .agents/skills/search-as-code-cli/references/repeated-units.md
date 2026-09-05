# Repeated units and record sets

Use this reference only when a task has a closed set of units or when one unit may yield zero, one,
or several requested records. The shapes are illustrative; rename fields to fit the task.

## Gate fan-out and preserve record sets

Validate the upstream set before constructing downstream queries or sources. Build those inputs
directly from
the returned rows. For one-to-many results, keep every material
record key and leave parser uncertainty unresolved. An exclusion counts only when inspected evidence
validates why it falls outside the user's requested scope.

```python
PROCESSED_FIELD_STATES = {"supported", "contradicted", "missing", "failed"}


def gate_units(rows, *, expected_count=None):
    problems = []
    keys = [row.get("key", "") for row in rows]
    if expected_count is not None and len(rows) != expected_count:
        problems.append("cardinality")
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        problems.append("keys")
    if any(row.get("membership") != "supported" for row in rows):
        problems.append("membership")
    return (rows if not problems else []), problems


def finalize_record_units(units):
    answer_rows = []
    unit_rows = []
    field_states = {}

    for unit in units:
        problems = []
        required_fields = set(unit.get("requested_fields", []))
        records = unit.get("records", [])
        record_keys = [record.get("key", "") for record in records]
        if not required_fields:
            problems.append("requested_fields")
        if any(not key for key in record_keys) or len(record_keys) != len(set(record_keys)):
            problems.append("record_keys")

        for record in records:
            fields = record.get("fields", {})
            evidence = record.get("evidence", [])
            if set(fields) != required_fields:
                problems.append(f"{record.get('key')}:field_shape")
            for field_name, field in fields.items():
                state = field.get("state", "unknown")
                field_states[state] = field_states.get(state, 0) + 1
                if state not in PROCESSED_FIELD_STATES:
                    problems.append(f"{record.get('key')}:{field_name}:{state}")
                if state == "supported" and (field.get("value") is None or not evidence):
                    problems.append(f"{record.get('key')}:{field_name}:ungrounded")
            answer_rows.append(
                {
                    "unit_key": unit.get("key", ""),
                    "record_key": record.get("key", ""),
                    "fields": fields,
                    "evidence": evidence,
                }
            )

        if unit.get("unresolved_mentions"):
            problems.append("unresolved_mentions")
        for exclusion in unit.get("exclusions", []):
            if exclusion.get("validated") is not True or not all(
                exclusion.get(name) for name in ("source", "excerpt", "reason")
            ):
                problems.append("invalid_exclusion")
        if unit.get("scope_complete") is not True:
            problems.append("scope_incomplete")

        unit_rows.append(
            {
                "unit_key": unit.get("key", ""),
                "records": len(records),
                "processing_complete": not problems,
                "problems": problems,
            }
        )

    coverage = {
        "units": len(units),
        "records": len(answer_rows),
        "processed_units": sum(row["processing_complete"] for row in unit_rows),
        "field_states": field_states,
    }
    return {"answer_rows": answer_rows, "unit_rows": unit_rows, "coverage": coverage}
```

Derive downstream inputs as a comprehension over `gate_units(...)` output. Use stable material
identity for record keys. `scope_complete` means the inspected source region can
support exhaustive enumeration for that unit; keep it false while enumeration remains uncertain.
`processing_complete` and `processed_units` describe bookkeeping: the unit's records and field
states have been accounted for within the inspected scope. Evaluate answer sufficiency separately
from this processing status. `missing` and `failed` fields remain evidence gaps even when processing
is
complete; use `field_states` and the retained rows to choose further work. Contradicted claims need
task-specific interpretation. Answer sufficiency remains an agent-level evidence judgment.

For supported fields, an absent or `None` value is missing; `0` and `False` are valid values.
Validate whether empty strings or collections are meaningful for the particular field before
calling this helper. Persist the returned rows and coverage when a later call needs them, using
artifact paths chosen for the task.

# Optional orchestration helpers

These small helpers constrain local dataflow. They do not choose queries, sources, call counts, or a
stopping policy, and a task need not use all of them.

## Close local parser repair in one program

A parser attempt is local computation, not a semantic checkpoint. Put plausible local parsers in one
program and validate the real task invariant after each attempt.

```python
def run_parser_candidates(text, candidates, validate):
    attempts = []
    for name, parse in candidates:
        try:
            rows = parse(text)
            problems = list(validate(rows))
        except (IndexError, KeyError, TypeError, ValueError) as error:
            rows = []
            problems = [f"{type(error).__name__}:{error}"]
        attempts.append({"name": name, "rows": len(rows), "problems": problems})
        if not problems:
            return {"state": "supported", "rows": rows, "attempts": attempts}
    return {"state": "unknown", "rows": [], "attempts": attempts}
```

The validator may check cardinality, unique material keys, field shape, membership, or source
alignment. If all candidates fail, persist the body and bounded diagnostics; another call is useful
only when the agent must choose a materially different interpretation.

## Bind a selected artifact

When a later parser uses cached text, bind both the selected path and source. This prevents a valid
parser from silently consuming the wrong body.

```python
def bind_selected_artifact(selection, artifact_name, artifact):
    problems = []
    if artifact_name != selection.get("artifact"):
        problems.append("selected_artifact_path")
    if artifact.get("source") != selection.get("source"):
        problems.append("selected_artifact_source")
    body = artifact.get("body")
    if not isinstance(body, str) or not body.strip():
        problems.append("selected_artifact_body")
    return (body if not problems else ""), problems
```

If binding fails, project the problems and do not parse or fan out from that artifact.

## Finalize an exact scoped claim

Use this only when a task contains relation, role, time-scope, or conflict ambiguity. Candidate
extractors leave `validated=False`; a task-specific check may set `validated=True` only when the
excerpt entails the exact subject, predicate, and scope.

```python
def finalize_scoped_claim(claim):
    relevant = [
        item
        for item in claim.get("evidence", [])
        if item.get("subject") == claim["subject"]
        and item.get("predicate") == claim["predicate"]
        and item.get("scope") == claim["scope"]
        and item.get("validated") is True
    ]
    supports = [item for item in relevant if item.get("stance") == "supports"]
    contradicts = [item for item in relevant if item.get("stance") == "contradicts"]
    if supports and contradicts:
        state, conflict = "unknown", True
    elif supports:
        state, conflict = "supported", False
    elif contradicts:
        state, conflict = "contradicted", False
    else:
        state, conflict = "unknown", False
    return {**claim, "state": state, "conflict": conflict}
```

Represent independently testable facts as separate claims. Combine their finalized states in
task-specific code; do not create one synthetic predicate merely because several terms occur in the
same window. Different roles or times are separate scoped claims, not automatically a conflict.

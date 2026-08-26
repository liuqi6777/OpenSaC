# OpenSAC advanced operations

Read this reference only when the core workflow cannot express the task. Import `sdk` from
`opensac_sdk`; structured results are ordinary JSON records.

## Whole-document content

Use a whole-document fetch only when deliberate local processing needs the complete normalized
text. It returns a report with separate input-indexed successes and failures:

```python
sdk.content.get_many(sources) -> record
```

## Free-form pipeline model

These calls are optional deployment capabilities. Prefer deterministic Python or the structured
core `extract_many` operation when either is sufficient:

```python
sdk.llm.complete(
    prompt, system=None, temperature=0.2, max_tokens=None
) -> str
sdk.llm.complete_many(
    prompts, system=None, temperature=0.2, max_tokens=None, concurrency=4
) -> list[str]
```

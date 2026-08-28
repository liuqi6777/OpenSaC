# OpenSAC advanced operations

Read this reference only when the core workflow cannot express the task. Import `sdk` from
`opensac_sdk`; structured results are ordinary JSON records.

## Whole-document content

Use a whole-document fetch only when deliberate local processing needs the complete normalized
text:

```python
sdk.content.fetch(source) -> record
```

Loop over several sources in Python and catch `BrokerError` per source when partial progress matters.

## Free-form pipeline model

This is an optional deployment capability. Prefer deterministic Python or the structured core
`extract` operation when either is sufficient:

```python
sdk.llm.complete(
    prompt, system=None, temperature=0.2, max_tokens=None
) -> str
```

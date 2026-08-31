# OpenSAC SDK

`opensac-sdk` is the compact JSON-record client embedded in OpenSAC's isolated execution sandbox. Generated
programs use it to call search, cross-document passage ranking, content inspection, state,
extraction, deployment-capability inspection, and citation capabilities over the authenticated
broker socket.

The SDK is built into the version-matched OpenSAC sandbox image and is not published separately to
PyPI. See the [OpenSAC repository](https://github.com/liuqi6777/OpenSaC) for installation,
documentation, security boundaries, and examples.

Every agent-facing resource and operation carries bounded runtime documentation. Generated programs
can inspect one exact interface without calling the broker, for example:

```python
print(sdk.content.passages.__doc__)
```

Broker-backed operations return their result or `None`. Bounded fan-out helpers return
input-aligned lists with `None` in failed positions:

```python
document = sdk.content.fetch(source)
documents = sdk.content.fetch_many(sources, concurrency=5)
extractions = sdk.llm.extract_many(items, instruction=instruction, schema=schema)
content = sdk.content.read(source, start_line=1, line_count=200)
grep_results = sdk.content.grep(pattern, sources=sources, mode="regex", context_lines=2)
```

Check `is None`, not truthiness, because empty containers and strings can be successful results.
Operational failures are recorded as bounded agent-visible structured warnings.

See the complete API reference in
[English](https://github.com/liuqi6777/OpenSaC/blob/main/docs/sdk-reference.md) or
[Chinese](https://github.com/liuqi6777/OpenSaC/blob/main/docs/sdk-reference.zh-CN.md).

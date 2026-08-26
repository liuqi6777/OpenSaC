# OpenSAC SDK

`opensac-sdk` is the compact JSON-record client embedded in OpenSAC's isolated execution sandbox. Generated
programs use it to call search, cross-document passage ranking, content inspection, state,
extraction, usage, and citation capabilities over the authenticated broker socket.

The SDK is built into the version-matched OpenSAC sandbox image and is not published separately to
PyPI. See the [OpenSAC repository](https://github.com/liuqi6777/OpenSaC) for installation,
documentation, security boundaries, and examples.

Every agent-facing resource and operation carries bounded runtime documentation. Generated programs
can inspect one exact interface without calling the broker, for example:

```python
print(sdk.content.passages.__doc__)
```

The content surface separates one-source reads from input-partitioned batch reports:

```python
row = sdk.content.read(source, offset=1, limit=200)
read_report = sdk.content.read_many([{"source": source, "offset": 201, "limit": 200}])
report = sdk.content.grep(sources, pattern, mode="regex", context=2)
```

See the complete API reference in
[English](https://github.com/liuqi6777/OpenSaC/blob/main/docs/sdk-reference.md) or
[Chinese](https://github.com/liuqi6777/OpenSaC/blob/main/docs/sdk-reference.zh-CN.md).

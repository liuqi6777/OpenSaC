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

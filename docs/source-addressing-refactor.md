# OpenSAC 0.6.2 source addressing refactor

## Status

Implemented for OpenSAC 0.6.2. This document records the design, migration scope, validation, and
release plan. The change is intentionally breaking: 0.6.2 does not accept, emit, or document the
old public `ref` representation, and it ships no compatibility aliases.

The implementation must preserve the core security rule: a generated program can read or cite only
a document returned by search in the same live session. Replacing `ref` with a URL or docid changes
how the program names an admitted document; it does not authorize arbitrary fetching.

## Problem

The current public contract exposes three names for one document:

- `ref`, a deterministic hash used as the preferred SDK handle;
- `url`, when the backend has a web address;
- `docid`, when the backend has a corpus identifier.

The broker already indexes all three and accepts any of them in content calls. This means opacity is
not the authorization boundary; membership in the session's reference table is. Keeping `ref` as the
preferred public name therefore adds several costs without adding a security property:

- an agent has to preserve a meaningless string even when a semantic URL is available;
- search, content, state, evidence, and citations all repeat or translate the same identity;
- passage locators redundantly contain `ref`, and citation submission sends both values back;
- SDK documentation has to explain which of three interchangeable strings should be used;
- the broker maintains `references`, `by_url`, and `by_docid` indexes plus a prefix-repair path.

For web research, a URL is the natural document address and gives the model useful domain and path
information. For local retrieval, a docid is the corresponding source-native address. The public
contract should represent this directly.

## Design goals

1. Expose one agent-facing document address named `source`.
2. Make `source` a canonical URL when a URL exists, otherwise the backend docid.
3. Make every content operation accept the `source` values returned by search.
4. Keep document admission, deduplication, caching, tracing, and evidence validation host-owned.
5. Make an evidence locator the only handle an agent must preserve for an exact passage citation.
6. Remove public `ref`, `url`, and `docid` identity choices rather than adding another alias.
7. Keep results as JSON records and locators as strings; add no public model hierarchy.
8. Keep the root SDK exports unchanged: `sdk`, `BrokerError`, and `__version__`.

## Non-goals

- Fetching an arbitrary URL that search did not return.
- Giving sandbox programs network access.
- Following redirects or requesting `rel=canonical` while computing a source.
- Defining a new URI scheme for local documents.
- Preserving old `ref`, `refs`, locator-object, or citation-request forms.
- Rewriting historical 0.4/0.5 migration documents to pretend they used the new contract.
- Changing provider retry, budget, rate-limit, or isolation behavior.
- Publishing either Python package to PyPI.

## Rejected alternatives

### Make `ref` contain a URL or docid

Keeping the old name while changing its contents preserves the conceptual leak: generated programs
would still be told to pass an implementation-shaped “reference,” and documentation would still
need to explain why some refs are URLs and others are not. It also leaves old locator and citation
shapes intact.

### Add `source` alongside `ref`

This creates a fourth identity spelling and a permanent precedence question. The project has
explicitly chosen a breaking cleanup over compatibility debt, so the old field must be deleted in
the same release.

### Expose separate URL-only and docid-only content methods

That makes generated programs branch on backend and duplicates every content primitive. One
source-native string keeps the capability backend-neutral without hiding what a web source is.

### Expose a public `Source` object or tagged union

A model such as `{kind, value}` would make the value self-describing but would rebuild the public
type hierarchy removed in 0.6.0. The session has one configured search backend and search produces
the value, so a plain string plus the hit's `backend` field is sufficient.

## Terminology and invariants

### Public source

`source` is the only document address exposed to generated programs.

- If a hit has a URL, `source` is the conservatively canonicalized URL.
- Otherwise, `source` is its non-empty string docid.
- A backend result with neither URL nor docid is invalid and must fail visibly.
- A source is bounded to 4,096 UTF-8 characters.
- A source is data, not authority. It resolves only when the current session admitted it through
  search.

URL canonicalization keeps the existing conservative rules: trim surrounding whitespace,
lower-case scheme and host, remove fragments and known tracking parameters, and sort remaining
query parameters. It must not follow redirects, normalize paths, or perform network I/O. The broker
retains the backend's original hit privately for fetching; canonicalizing the public source must not
rewrite a signed or provider-specific fetch URL.

Backend URL fields must use `http` or `https`. Unknown-source errors must truncate echoed values so
a malicious or unusually long URL cannot consume the sandbox observation budget. Moving the URL
into `source` must not add it to any log or trace that does not already record document identity.

When repeated hits share one host-side document identity, the first admitted source wins for that
session. Later sightings return the same source so fusion, state joins, and replay remain stable.

### Host-side document identity

The broker keeps a canonical document identity for deduplication and research traces:

```text
<backend>:docid:<docid>
<backend>:url:<canonical-url>
```

This identity is not an SDK input and is never accepted by content or citation operations. It may
remain in host-side research traces because those traces must join against qrels and distinguish
backends. The existing `_identity()` behavior can remain private; `_ref_for()` is removed.

### Evidence locator

A locator becomes one opaque string such as `evidence_ab12...`. The evidence registry already binds
that ID to the document identity, exact text, coordinates, and fingerprints. Requiring the caller to
resubmit a separate document handle is redundant.

Locators remain:

- session-scoped;
- limited to 128 characters;
- issued only for successfully registered evidence;
- rejected when unknown, stale, malformed, or from another session;
- unusable as a general document source.

## Target SDK contract

### Search

Search operations keep their signatures. A search hit becomes:

```python
{
    "source": "https://example.com/research/paper",  # or a local docid
    "backend": "web",
    "title": "...",
    "domain": "example.com",
    "date": "...",
    "snippet": "...",
    "score": 0.91,
    "rank": 1,
    "retrieval": {...},
    "metadata": {...},
}
```

The agent-facing hit contains no `ref`, `url`, or `docid`. For a web hit, `source` itself is the URL.
For a local hit, it is the docid. Backend-native values needed only for fetching or evaluation stay
inside the host and its traces.

`sdk.search.fuse_rrf()` deduplicates and joins hits by `source`. Its output uses `source` and keeps
the existing provenance, score, and rank fields.

### Content

All content operations rename `refs` to `sources`:

```python
sdk.content.get_many(sources)
sdk.content.read(sources, offset=1, limit=200, max_chars=100_000)
sdk.content.grep_report(sources, pattern, context=0, max_matches_per_source=20)
sdk.content.passages(query, sources, limit=20, max_per_source=3)
```

Parameter names that contain `ref` also change:

- `max_matches_per_ref` -> `max_matches_per_source`;
- `max_per_ref` -> `max_per_source`;
- `unique_ref_count` -> `unique_source_count`.

Every content row, match, passage, and partial failure uses `source`. No content payload emits `ref`,
`url`, or `docid` as an alternative identity. Input alignment and `input_index` behavior remain
unchanged.

Example web workflow:

```python
hits = sdk.search("Who introduced the ReAct prompting method?", limit=10)
sources = [hit.source for hit in hits]
report = sdk.content.passages("original authors and publication", sources)
```

The broker resolves only exact admitted sources. A guessed URL or docid absent from the session
fails with `Unknown sources`; it is never sent to a provider.

### State and fusion

The state helper follows the same identity:

```python
sdk.state.merge_jsonl("pool.jsonl", rows, key="source")
```

`source` becomes the default merge key. There is no fallback to `ref`, and saved examples and Skill
artifacts are migrated in place.

### Evidence and citations

Content evidence exposes a locator string:

```python
passage.source
passage.locator  # "evidence_ab12..." or None
```

Final citation requests have exactly one of two shapes:

```python
{"source": hit.source}        # search-preview evidence
{"locator": passage.locator} # exact content evidence
```

Supplying both keys, neither key, an explicit null locator, or extra keys is invalid. A passage
citation no longer repeats its source because the evidence registry owns that binding.

`sdk.citations` shrinks to one advanced operation:

```python
sdk.citations.resolve(citations)
```

It accepts the same citation objects as `sdk.output.submit()`. The separate `resolve_requests`
method and the legacy `resolve(refs)` overload are deleted.

Resolved citations may include `source`, `url`, and `docid` as trusted output metadata. Those fields
help the external consumer render and evaluate a citation, but only `source` is an SDK document
address and only a locator identifies exact content evidence.

### Output

The final workflow becomes:

```python
evidence = report.passages[:3]
sdk.output.submit(
    {"answer": "..."},
    citations=[{"locator": row.locator} for row in evidence if row.locator],
)
```

`OutputResource` validates citation shape locally, forwards one request list to
`citations.resolve`, and writes the same final output artifact structure as today. Citation
resolution remains broker-owned.

### Runtime documentation

Every affected `__doc__` must state:

- a web source is a URL and a local source is a docid;
- only sources returned by this session's search are readable;
- a locator is opaque and must be passed back unchanged;
- source-only citations cover search-preview evidence, not document-content claims;
- locator citations cover the exact registered evidence.

Existing doc-length budgets remain in force.

## Target wire contract

The RPC method names remain stable, but their parameters and payload fields change:

| Method | Removed input | New input |
| --- | --- | --- |
| `content.get_many` | `refs` | `sources` |
| `content.read` | `refs` | `sources` |
| `content.grep_report` | `refs`, `max_matches_per_ref` | `sources`, `max_matches_per_source` |
| `content.passages` | `refs`, `max_per_ref` | `sources`, `max_per_source` |
| `citations.resolve` | `refs` or `{ref, locator}` requests | `{source}` or `{locator}` requests |

Search and content payloads replace every public `ref` field with `source`. Evidence locator objects
become strings. No broker method accepts both generations of the contract.

This is an incompatible host/SDK RPC change, so `SANDBOX_CONTRACT` advances from 7 to 8. The sandbox
Dockerfile label and every contract fixture must advance in the same implementation commit.

OpenSAC is still pre-1.0, and the project has explicitly chosen not to preserve compatibility for
this cleanup. The release number is therefore 0.6.2 as requested, while contract 8 provides the
machine-enforced incompatibility signal that semantic versioning alone cannot provide.

## Broker design

### Session registry

Replace the three public-handle indexes:

```text
references
by_docid
by_url
```

with host-owned document state organized around:

```text
documents_by_source: source -> stored SearchHit
source_by_identity: document identity -> first admitted source
```

The second map ensures duplicate sightings reuse one public source. The stored `SearchHit` retains
the original URL/docid required by its backend adapter; only the wire representation is reduced.

Remove:

- `_ref_for()`;
- the `ref_` prefix-repair behavior;
- lookup by arbitrary alternate handles;
- comments and tests that describe hash opacity as an authorization property.

### Admission and lookup

Search is the only admission path:

1. validate the backend hit has a URL or docid;
2. calculate its private identity;
3. derive or reuse its public source;
4. store the hit before returning the source;
5. resolve later content calls only through `documents_by_source`.

URL canonicalization may be applied before lookup so a spelling equivalent to the returned source
resolves to the same admitted document. It must never turn an unknown URL into a provider fetch.

### Content cache

Key the content cache by private document identity rather than by a public source. This keeps cache
correctness independent of source spelling and prevents a future presentation change from forking
cached content.

### Evidence registry

Change `EvidenceRecord.ref` to `EvidenceRecord.identity`. The registry resolves a locator ID to the
stored evidence record, then finds the corresponding admitted hit through host-owned document
state. Locator validation no longer compares a caller-provided ref.

The locator ID derivation continues to include the session token, document identity, coordinates,
and document/passage fingerprints. Collision checks and evidence-capacity limits remain unchanged.

### Traces and metrics

- `HitRecord.identity` remains the research join key.
- Remove `PassageTraceRecord.ref`; `identity` is sufficient.
- Replace `EvidenceTraceRecord.ref` with `identity` when known.
- Unknown-locator failures may have no document identity.
- `documents_seen` counts admitted private identities, not alternate source spellings.
- Trace `input_count` reads `sources` and the unified citation request list.

No trace should contain a public hash ref after the migration.

## SDK implementation

Update the consolidated SDK rather than adding resource modules:

- `_resources.py`: rename parameters and fields, simplify citations and locators, update docstrings;
- `_surface.py`: delete `sdk.citations.resolve_requests` and keep one `resolve` operation;
- `client.py`: keep the same seven namespaces and update runtime overview text;
- state helpers: change the default merge key to `source`;
- RRF helper: deduplicate by `source` and report source-oriented validation errors;
- output helper: accept exactly `{source}` or `{locator}` citation records.

Do not add a `Source` class, locator class, compatibility adapter, alias property, or public types
module. SDK values remain strings and records.

## Host and documentation migration

Update all executable and agent-facing material in the same change:

- Search-as-Code and Search-as-Code CLI Skills;
- both copies of `references/sdk-contract.md` and all linked recipes/patterns;
- SDK README and root SDK table where necessary;
- `examples/research_pipeline.py`, `examples/local_pipeline.py`, and agent examples;
- `sac_agent/README.md` and `sac_agent/tool_sac.py` instructions;
- sandbox probes and contract fixtures;
- API, broker, passage, reranker, stateful-program, and trace tests;
- the public-surface implementation document where it describes the final contract.

Generated examples must use `source` and locator-only passage citations. Documentation must not
teach fallback handling for deleted fields.

Historical migration documents may retain the contract of the version they describe. Current
READMEs, Skills, examples, implementation documents, and runtime docs must use only the 0.6.2
contract.

## Deletion inventory

The implementation is incomplete while any of these remain on the public path:

- `SearchHit.ref` and every content result/failure `ref`;
- agent-facing `url`/`docid` alternatives to `source` in search and content payloads;
- `EvidenceLocator` as a public object containing `id`, `ref`, and `kind`;
- `refs`, `max_per_ref`, `max_matches_per_ref`, and `unique_ref_count` parameters/fields;
- `sdk.citations.resolve_requests`;
- citation objects containing `ref` or both a document address and locator;
- `BrokerSession.references`, `by_docid`, and `by_url` public-handle indexes;
- `_ref_for()` and `ref_` spelling repair;
- state examples whose identity key is `ref`;
- Skill instructions telling agents to treat refs as opaque capabilities.

Private prose may still use the English word “reference” generically. Contract tests should check
serialized field names and public SDK documentation rather than banning that word repository-wide.

## Test plan

### Source identity

- Web hits return a canonical URL as `source`.
- Local hits without a URL return docid as `source`.
- A hit with neither URL nor docid fails visibly.
- The same document across queries reuses one source.
- Tracking parameters and fragments do not fork a web source.
- Different backends remain distinct in host trace identities.
- A source longer than the configured bound is rejected.

### Authorization

- Every content operation accepts a source returned by search.
- An unsearched URL is rejected before any provider fetch.
- An unsearched local docid is rejected before any provider fetch.
- A source from another session is rejected.
- Losing session state invalidates all sources and locators.
- Content caching and in-flight coalescing do not change admission behavior.

### Content and helpers

- Batch alignment, duplicates, partial failures, and `input_index` use sources.
- `read` pagination and grep line offsets remain unchanged.
- Passage reports expose `unique_source_count` and enforce `max_per_source`.
- RRF deduplicates by source with deterministic tie-breaking.
- `merge_jsonl` defaults to `source` and still permits an explicit alternate key.

### Evidence and citations

- Every registered passage receives a locator string within the length bound.
- Unknown, altered, cross-session, and stale locators fail.
- Locator-capacity exhaustion remains a structured per-row error.
- A locator-only citation resolves exact registered text and document metadata.
- A source-only citation resolves search-preview evidence.
- Citation objects with both/neither/extra/null fields fail locally and at the broker.
- Resolution output never trusts caller-supplied URL, title, or evidence text.

### Surface and drift

- Package-root exports remain exactly `BrokerError`, `sdk`, and `__version__`.
- The operation manifest contains one citation resolve method and no compatibility operation.
- Every public resource and method retains bounded `__doc__` coverage.
- SDK and host transport method sets remain synchronized.
- Serialized search/content/citation payload fixtures contain no `ref` key.
- Both Skill contracts stay byte-identical where required.

### End-to-end

The real sandbox test must run a program that:

1. searches a web result;
2. passes its URL-valued `source` to content;
3. reads runtime `__doc__` without a broker call;
4. submits a locator-only citation;
5. verifies the resolved citation and version-matched SDK.

The Compose E2E must verify contract 8 and the version-matched service/sandbox pair.

## Implementation sequence

### Commit 1: replace public refs with sources

- Change host contracts, broker session state, source derivation, content payloads, evidence registry,
  traces, and citation resolution.
- Change the consolidated SDK, surface manifest, state default, fusion helper, and output validation.
- Update all unit and integration tests required to leave the repository green.
- Advance sandbox contract 7 -> 8 in runtime, Dockerfile, and fixtures.

This commit is deliberately end-to-end. Splitting host and SDK wire changes across commits would
leave a revision where the version-matched workspace cannot pass its own tests.

### Commit 2: migrate agent guidance and examples

- Update both Skills, their synchronized references, examples, README sections, and runtime docs.
- Remove all compatibility language and stale ref-based programs.
- Run contract and Skill size-budget tests.

### Commit 3: prepare 0.6.2

Only after implementation and CI are green:

- bump both package versions to `0.6.2`;
- update `.env.example`, `compose.env.example`, and `compose.yaml` image defaults;
- update exact API version assertions;
- run the complete release checklist;
- commit with the required detailed body;
- create annotated tag `v0.6.2` on the exact release commit;
- atomically push `main` and `v0.6.2`;
- verify the GitHub Release plus service and sandbox images.

The root READMEs remain versionless.

## Validation commands

During implementation:

```bash
uv sync --locked --all-packages --extra dev
uv run pytest tests/test_broker.py tests/test_sdk.py tests/test_search_as_code_skill.py \
  tests/test_search_as_code_cli_skill.py
uv run ruff check .
uv run pytest
git diff --check
```

Before the release commit:

```bash
uv lock
uv sync --locked --all-packages --extra dev
uv run ruff check .
uv run pytest
OPENSAC_DOCKER_E2E=1 uv run pytest tests/test_sandbox_docker_e2e.py
uv build --all-packages --out-dir dist --clear
uvx --from twine twine check dist/*
uv run python scripts/release.py --tag v0.6.2
```

## Acceptance criteria

0.6.2 is ready only when all of the following are true:

- a normal web workflow passes URLs from search to content without touching an opaque document ref;
- a local workflow uses the same `source` field with docids;
- no agent-facing payload, SDK method, Skill, or example contains the old `ref` contract;
- arbitrary unsearched URLs and docids remain unreachable;
- exact evidence citations require only a registered locator string;
- search-preview citations require only an admitted source;
- RRF, state joins, traces, budgets, caching, and partial failures retain their behavior;
- the SDK surface is smaller, with one citation resolver and no new public types;
- sandbox contract 8 rejects mismatched host/SDK images;
- the full local and GitHub release checks pass;
- published `opensac:0.6.2` and `opensac-sandbox:0.6.2` images report version 0.6.2, and the sandbox
  runtime docs describe the source-based contract.

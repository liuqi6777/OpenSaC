# Search-as-Code skill behavioral iterations

This log records paired forward tests of `.agents/skills/search-as-code/SKILL.md`. It is an evaluation
record, not an additional instruction source for agents.

## Protocol

- Run exactly two agents per round with `gpt-5.6-terra` and `high` reasoning.
- Give one agent the fixed BrowseComp-style query and the other the fixed WideSearch-style query.
- Permit external research only through `search-as-code` and `sac_run`; prohibit built-in web or
  browser search. Agents do not see prior-round diagnoses or proposed fixes.
- Review actual Python orchestration, stdout, evidence coverage, failures, and final answers. Attribute
  a problem to the skill only when a narrow, strategy-neutral orchestration instruction can address it.
- Stop after a fresh pair produces bounded observations without host truncation, program-derived and
  aligned fan-out, semantically justified checkpoints, source-scoped inspected evidence, and coverage
  whose explicit missing, failed, or conflicted fields agree with the final answer.

### Fixed queries

**BrowseComp.** Identify the narrow, rounded-corner New York City building completed in the first
decade of the twentieth century, converted to condominiums in 2006, and used as the Continental Hotel
exterior in the 2014 film *John Wick*. Return its name, exact address, architect, and the exchange for
which the prompt says it was built, while preserving source conflicts.

**WideSearch.** For all 12 people who walked on the Moon, report every earned bachelor's degree with
field, institution, and year; exclude honorary degrees, establish the closed set first, preserve
conflicts, and report row-level coverage.

## Round 1 — baseline

Configuration: unmodified skill at commit `6583cee`; branch
`codex/search-as-code-skill-eval`.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 4 | 11 |
| Host-truncated observation | Yes; calls 1–3 were truncated or only partly retained | Yes, call 4; calls 2 and 6 also greatly exceeded the skill budget |
| Evidence result | Correctly identified 1 Wall Street Court and contradicted the claim that it was built for the Cocoa Exchange | Closed 12-person set and returned 13 degree rows; left one field unknown and disclosed one weak source |
| Audit-log delivery | All four code blocks were recovered; only call 4 retained complete stdout | Calls 1–6 were recovered; calls 7–11 could not be archived |
| Main orchestration defects | Per-loop output overflow; duplicate excerpts; a finalization-only reload/reprint call; `STATUS=partial` paired with `NEXT: no further retrieval required` | Standalone full capability dump; 39 candidate hits printed from a fan-out loop; anticipated people, institutions, years, and degrees hard-coded into discovery queries; rows keyed by source instead of target requirement; repeated fetches and excessive checkpoint count |

Positive behavior worth preserving: both agents fetched and inspected page bodies, retained exact source
URLs, avoided guessing absent fields, and surfaced contradictions or source-quality limitations.

### Diagnosis

The existing skill states the desired bounds, composition, and evidence rules, but leaves four coding
invariants too implicit:

1. A global stdout budget must be implemented before printing, rather than treated as a prose target
   around nested loops.
2. Repeated independent units need stable aligned rows, batched waves, and unresolved-only follow-ups;
   otherwise a WideSearch task degenerates into many method- or row-sized checkpoints.
3. Hypotheses used for discovery must not silently become the established closed set or expected
   values in validation code.
4. Requirement state must distinguish contradiction from missing evidence, and the terminal action
   marker must agree with that state.

### Skill change after round 1

- Added selective capability inspection instead of standalone full-record dumps.
- Added result-derived fan-out and hypothesis provenance rules.
- Added aligned repeated-unit rows, unresolved-only batches, and separate exhaustiveness coverage.
- Added program-wide buffered stdout budgeting and evidence deduplication.
- Added the `NEXT` / `READY` / `ERROR` invariant and explicit contradicted-requirement state.

## Round 2

Configuration: same model, reasoning, queries, and tool restrictions; agents read the round-1 skill
change but not this evaluation log or prior trajectories.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 7 | 9 |
| Observation budget | Six calls were at or below 4,000 characters; call 3 printed 8,650 | Calls 3–9 reported upper bounds from 5,000 to 16,000 characters |
| Result-derived dataflow | Candidate identity and later queries derived after the first discovery call | NASA body text produced the 12-person set, which then drove aligned biography fetches and unresolved-person searches |
| Evidence result | Correct identification and explicit correction of the unsupported original-purpose premise | Six complete people, two source-conflict rows, three partial rows, and one explicitly unestablished row |
| Remaining defects | One over-budget discovery loop; seven checkpoints; static expected statuses in the requirement specification; an omitted-evidence `READY` followed by recovery | Multiple over-budget row/excerpt loops; nine checkpoints; brittle positional membership slice; an Aldrin row contaminated with a Neil Armstrong page; final aggregation hard-coded zero failures |

The first revision improved provenance and batching: neither agent dumped capabilities, and WideSearch
no longer installed a remembered 12-person answer set before inspecting NASA. It did not make the
stdout requirement executable enough, and the BrowseComp code demonstrated that status labels could
still be seeded independently of evidence.

### Skill change after round 2

- Required evidence state to start unknown and be assigned only while processing a checked outcome and
  excerpt; aggregate status and action markers must derive from those rows.
- Required all fan-out success and failure paths to use one bounded emitter.
- Added an optional generic emitter pattern that reserves space for source-bearing rows, counts, and
  one action marker without prescribing queries, ranking, or source policy.
- Clarified that RPC input alignment is not semantic entity alignment: fetched body identity must be
  validated before evidence is attached, and mismatches remain unresolved.
- Required final aggregation to carry forward artifact failures and mismatches rather than resetting
  their count.

## Round 3

Configuration: same blind paired protocol after the round-2 revision.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 4 | 13 |
| Observation behavior | Four compact decision surfaces, ending `NEXT`, `NEXT`, `NEXT`, `READY`; no host truncation indicated | No host truncation indicated; recovered checkpoints 1–2 could print roughly 9,000 and 5,000+ characters because they did not use the global emitter |
| Evidence result | 6/6 requested fields or clues covered; original-purpose premise explicitly contradicted | 12-person set, 13 degree entries, 11/12 full rows and one honest missing field; historical institution-name difference preserved |
| Alignment/status result | No observed premature terminal marker; final contradiction and coverage agree | Prior cross-person contamination was absent from the delivered table, but checkpoint 12 emitted `READY` after every LLM extraction failed |
| Remaining defects | Audit artifact retained only checkpoint 1 code verbatim | Thirteen checkpoints; early fan-out code bypassed the emitter; optional LLM call used as an availability probe even though deterministic parsing sufficed; checkpoint 13 then performed the local fallback |

BrowseComp reached the behavioral stop criteria, subject to the audit-capture limitation. WideSearch
improved evidence completeness and entity alignment but still violated capability and terminal-state
orchestration.

### Skill change after round 3

- Prefer local Python for deterministic parsing and normalization.
- Gate optional RPCs on a narrowly read availability field in the same checkpoint instead of probing
  by failure.
- Run a same-input deterministic fallback in that checkpoint when no new semantic choice is needed.
- Keep optional-transformation failures unresolved and prohibit `READY` unless the fallback resolves
  their rows.
- Added a top-level code preflight: no loop-local printing on fan-out paths, one budgeted normal-path
  emitter including counts/action, evidence-derived state, semantic unit validation, and gated optional
  RPCs.

## Round 4

Configuration: same paired queries; every call was written to a temporary audit file immediately after
return. The first WideSearch agent stream failed before any `sac_run`; the same agent then restarted
from an empty audit/workspace trajectory.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 4 | 15 |
| Observation behavior | Three captured outputs were 2,214, 3,263, and 3,716 characters; the fourth audit append failed | Captured outputs were at most 3,902 characters with no host truncation; several whole-string slices removed their action markers |
| Evidence result | Identity and core fields supported; original commission and exact 2014-film clue retained as conflicts/partial | 12/12 people and 13 degree entries; 11/13 fields established and two honestly missing; one wrong-person candidate excluded from the final table |
| Main improvements | Buffered output, body identity checks, and evidence-derived partial status | No optional-LLM probe; output stayed bounded; final answer preserved missing fields and rejected one semantic mismatch |
| Remaining defects | One uncaptured final program; otherwise behaviorally acceptable | Re-fetched the Moonwalkers body four times and the same 12 NASA profiles twice; whole-output slicing hid markers; a Michael Collins degree excerpt was temporarily attached to Aldrin; row support did not track degree/field/institution/year separately; two final fetch calls repaired long-window output |

### Skill change after round 4

- Added a durable-handoff preflight: persist a fetched body or the exact normalized structure whenever
  the declared next step will reuse it, then load rather than re-fetch.
- Explicitly rejected `joined_stdout[:limit]` as a bounded emitter and required one compact line per
  unresolved key before secondary excerpts.
- Required per-field evidence states for structured rows; a generic degree-term match cannot support
  institution, field, year, or completeness.
- Tightened semantic alignment to the evidence excerpt or an explicit source-scoped join; a name found
  elsewhere in the document cannot license an unrelated relation excerpt.

## Round 5

Configuration: same blind paired protocol and immediate append-only audit after every call.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 9 | 13 |
| Observation behavior | Checkpoint 1 stdout was unavailable; checkpoints 2–9 had no host truncation and were at most 3,784 characters, but checkpoints 1–7 sliced assembled output strings | Every captured call used a row-budgeting emitter with an action marker and no host truncation marker |
| Durable reuse | Verification bodies were cached and not re-fetched | The NASA closed-set page was fetched four times; candidate bodies were persisted after fetch |
| Evidence result | Correct identity, address, firm, completion, conversion, and film clue; reported a second address form as a conflict but did not recover the earlier speculative-use/commission conflict | Established 12/12 people and 13 bachelor records; institution 13/13, field 11/13, year 12/13; missing values and exhaustiveness limitation were explicit |
| Remaining defects | Nine checkpoints over two fetch waves; repeated regex/window/formatter-only calls over one cache; rendering overflow replaced semantic actions; one field changed supported → missing → supported; non-exclusive addresses were mislabeled as contradictory | Thirteen checkpoints; fetch and local inspection were split; closed-set boundary repair used three extra calls over the same body; final `READY` was hard-coded from a four-person residual subset rather than computed from one persisted 12-person per-field table |

The revision fixed the round-4 stdout-marker and cross-person contamination failures in WideSearch.
The generic emitter was followed there, and the final answer was factually cautious. It did not make
the meaning of a semantic checkpoint, the durable handoff timing, or the global-readiness dataflow
executable enough. BrowseComp also showed that output budgeting and evidence state could still be
coupled incorrectly.

### Skill change after round 5

- Defined a semantic checkpoint as completing all deterministic work on current inputs, including
  boundary detection, fallback-pattern repair, validation, status updates, and rendering. A changed
  regex, window, or format alone cannot justify another call.
- Required every reusable successful body or exact normalized structure to be persisted before
  emitting `NEXT`, and prohibited re-fetching it for later parsing or presentation.
- Made assembled-observation slicing a hard prohibition. Emitters must budget whole rows, and output
  omission can never replace the computed research action.
- Required later rendering to consume persisted validated evidence rows instead of re-parsing raw text,
  and required global `READY` to be computed from the complete normalized unit/field table rather than
  a remembered or residual subset.
- Distinguished mutually exclusive conflicts from compatible aliases, historical names, multiple
  degrees, and address variants.
- Updated all reference patterns to use the global emitter, availability-gate optional extraction with
  an in-program deterministic fallback, and avoid examples that print from fan-out loops.
- Consolidated duplicated guidance into a single preflight and kept `SKILL.md` below its 10,000-byte
  contract; every reference code block remains independently executable.

## Round 6

Configuration: same blind paired protocol after consolidating the skill below its 10,000-byte contract.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 2 | 13 |
| Observation behavior | Both outputs had visible actions but were 8,233 and 6,669 characters; code printed directly inside source/evidence loops | Calls 1–12 were 726–3,735 characters; call 13 was host-truncated; all actions remained visible in the audit |
| Durable/dataflow behavior | Reused five cached bodies in call 2 and performed no refetch-only parsing call | Cached bodies avoided repeat fetches, but repeatedly split fetch → local inspect → construct rows; manually retyped the 12-person roster and then an 11-person assignment list |
| Evidence result | Correct building and requested fields; recovered the speculative-office versus later Cocoa Exchange conflict and treated 2014/2019 film facts as compatible | Final answer was explicitly partial: several rows used snippets, incomplete bodies, or fallback evidence; Harrison Schmitt was omitted from the first assignment and later repaired |
| Terminal behavior | Final `READY` was hard-coded rather than aggregated, but the source-scoped answer was well supported | Latest OpenSAC marker remained `NEXT: build a complete row-level table...`; agent nevertheless answered, with no complete persisted per-field table |

The shorter skill greatly reduced BrowseComp checkpoint count and eliminated redundant fetches in both
styles. It did not make the emitter mandatory enough for BrowseComp, and WideSearch still interpreted
each deterministic transformation as a checkpoint. Most importantly, prose saying “no unresolved
NEXT” was not treated as binding control flow.

### Skill change after round 6

- Made the latest action marker binding: an agent may answer only after `READY`; `NEXT` explicitly
  forbids completion.
- Added a mechanical invalid-`NEXT` test for actions that only inspect, parse, normalize, extract,
  format, synthesize, or build rows from current memory/workspace.
- Required every variable-result, multi-source, or multi-unit program to read and adapt the global
  emitter pattern.
- Prohibited retyping anticipated closed sets or assignments; later fan-out must load inspected
  normalized rows, and closed sets must derive from bounded source regions or explicit records.
- Added a concrete cumulative unit/field evidence-table shape to the emitter reference and clarified
  that generic `candidate_evidence` is not a final field state.

## Round 7

Configuration: same blind paired protocol after making the action marker and global emitter binding.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 5 | 9 |
| Observation behavior | Every output was 3,794 characters or less, with a visible marker; the final marker was `READY` | Calls 1–8 were 3,736 characters or less; call 9 had a Python bracket mismatch, produced no stdout, and therefore no marker |
| Durable/dataflow behavior | Reused cached sources and emitted bounded rows | Derived and persisted the 12-person set from a bounded NASA page region, then used stable keys and unresolved-only follow-ups; still split fetches from deterministic parsing/merging |
| Evidence result | Correct building, address variants, firm, completion, conversion, and film clue | Partial source-grounded rows only; the agent correctly refused to claim a finished table after the syntax failure |
| Remaining defects | Replaced the requested `built_for_exchange` relation with a generic `exchange` key, accepted mere name presence as support, omitted a fetched speculative-office conflict, and left one material requirement out of the final compact evidence rows | Treated a repairable roster-regex miss as `ERROR`; stopped fetch programs at body/context caches with `NEXT: merge`; the final merge program was not syntax-checked and the audit omitted its full code |

This round validated both the emitter and the binding-marker gate: BrowseComp stayed bounded, and
WideSearch did not fabricate `READY` after a failed program. It also exposed that a well-shaped table
can still weaken a requested predicate, and that deterministic post-fetch work was still deferred.

### Skill change after round 7

- Required exact relation-bearing requirement keys: a generic term match cannot establish a predicate
  such as `built_for_exchange`.
- Required one compact source-bearing line per material requirement or unit before `READY`, shrinking
  excerpts before omitting rows.
- Restricted `ERROR` to external or state failures that actually prevent continuation; local parser or
  count mismatches must be repaired in-program or lead to a semantic `NEXT`.
- Required repeated-unit fetch programs to update per-field states before emitting rather than stop at
  a body/context cache with `NEXT: merge` or `NEXT: normalize`.
- Added a complete-program syntax check to preflight.

## Round 8

Configuration: same blind pair under the marker-heavy skill. During evaluation, the fixed marker was
identified as an overconstraint; this round is retained as the last direct baseline for that protocol.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 9 | 15 |
| Observation behavior | Call 1 stdout was lost when the mandated audit append failed; calls 2–9 were 3,764 characters or less and retained markers | Call 3 was reported tool-truncated; calls 5–6 lacked retained character counts, and calls 7–15 could not be recovered byte-for-byte from the agent's audit |
| Evidence result | Correct building and fields; exact `built_for_exchange` predicate was contradicted with the inspected speculative-office account, and the film relation was source-scoped | Correct source-derived 12-person closed set and 13 degree rows; institution/year evidence covered 13/13 and three unestablished fields were explicit; honorary degrees were excluded |
| Marker behavior | Call 5 printed `READY` while still treating former-headquarters text as original-purpose support; the agent overrode it, continued, and repaired the predicate by call 9 | Calls 2–7 were roster-regex/window/boundary repair stages; call 14 printed `READY`, followed by a correction-only call 15 |
| Remaining defects | Search, select, fetch, local validation, predicate repair, and film repair were spread over nine calls despite reusable local inputs | Fifteen calls, including several deterministic parser repairs and fetch/parse/normalize splits; the incomplete audit prevents full code review for calls 7–15 |

Both answers met the factual and evidence-coverage bar, but neither trajectory met the orchestration
bar. A literal terminal token neither guaranteed semantic completeness (BrowseComp call 5) nor reduced
checkpoint count. It encouraged programs to announce workflow transitions that the agent was already
responsible for deciding.

### Skill change after round 8

- Removed required `NEXT` / `READY` / `ERROR` stdout tokens and all program-internal completion
  decisions. After each observation, the agent decides whether to call OpenSAC again or answer.
- Kept the agent-level completion gate: every material requirement must have inspected source evidence
  or an explicit missing, failed, or conflicted state before the final response.
- Simplified the global emitter to source-bearing whole rows plus counts/omissions; it no longer accepts
  or reserves space for an action footer.
- Reworked every reference program and the stateful test fixture to return evidence, coverage, and
  bounded failure records without a workflow token.
- Updated focused tests to reject reintroduction of fixed markers while preserving compile, sandbox,
  durability, alignment, output-bound, failure, and cumulative-state checks.

## Round 9

Configuration: first blind pair with program-level markers removed. The audit recorded the agent's
continue/answer decision outside each program observation.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 6 | 11 |
| Observation behavior | All six outputs were 3,852 characters or less and untruncated | Ten outputs were under 4,000 characters; the closed-set/search output was 4,132, with no reported host truncation |
| Evidence result | Correct building, fields, address forms, film relation, and speculative-development versus later-exchange conflict | Derived a 12-person set and source-aligned bachelor/institution evidence, but established only a few fields/years and admitted that per-person bachelor exhaustiveness remained unresolved |
| Improvement from marker removal | The agent stopped naturally from inspected coverage; calls fell from 9 to 6 | Calls fell from 15 to 11, with continue/answer decisions cleanly separated from stdout |
| Remaining defects | Call 3 produced 28 source-major rows and omitted later material keys; calls 4–6 only re-read cached bodies to re-render omitted evidence/address text | Used remembered names in early validation; invoked optional extraction without a capability gate; calls 6–9 split fetched-body parsing into several local-only repairs; call 7 dispatched degree search from a one-person invalid roster; calls 10–11 stored only `candidate_evidence`, did not parse per-field states, yet the agent answered and called the table complete enough |

Removing markers improved the control boundary but was not sufficient. BrowseComp copied the emitter's
budgeting shape without implementing its prose-only per-key priority. WideSearch bypassed three rules
that were described but not mechanically prominent: validate upstream shape before fan-out, finish
field parsing in the fetch program, and never answer from intermediate evidence state.

### Skill change after round 9

- Made a program incomplete when a subsequent call would only read or re-render its current workspace.
- Made the global emitter itself order the first row for every stable key before secondary excerpts;
  added a runtime test that forces a tight budget and proves later keys are not starved.
- Required cardinality, stable-key, and membership validation before any downstream capability call;
  a partial closed set cannot drive fan-out.
- Required repeated-unit fetch programs to parse requested fields into final states before returning;
  body-only `candidate_evidence` is explicitly intermediate.
- Added an agent-level pre-answer audit: no unknown/candidate unit, field, completeness, or exhaustiveness
  state may be presented as complete.
- Removed a source-diversity selection prescription from the skill and its test, keeping the revision
  focused on code/dataflow orchestration rather than search strategy.

## Round 10

Configuration: same marker-free blind pair after adding upstream validation and intermediate-state
gates.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 6 | 10 |
| Observation behavior | All outputs were 3,742 characters or less and untruncated | One heading-diagnostic output was 4,869 characters; the other nine were 3,595 or less and untruncated |
| Evidence result | Fully supported identity, address, firm, completion, form, conversion, and film relation; correctly contradicted the original-exchange premise | Correct NASA-bounded 12-person set, but many fields/years and all exhaustiveness states remained unresolved; three rows lacked even a usable final bachelor record |
| Upstream validation | Not applicable beyond one building | Improved: five-name and overrun parser results did not drive degree fan-out; the agent waited for the bounded 12-name section |
| Remaining defects | Call 2 encoded keys as `source::field`, so the per-key emitter treated every source-field pair as primary; call 4 used 560-character excerpts and omitted the film key; a keyword-aligned `completed_for_exchange` row was initially marked supported | Call 7 fetched 12 pages but stored only windows; call 8 invoked optional extraction without a capability gate and all 12 quotes failed validation; calls 9–10 rendered deterministic windows without updating requested field states; the final answer stopped with unknown completeness and intermediate `evidence_window_present` states. Call 9 code/output was recovered after the run, but its original agent decision was not retained |

The upstream fan-out barrier worked, but prose did not make the downstream composed shape executable.
BrowseComp also showed that emitter prioritization depends on a material key rather than a
source-qualified evidence key.

### Skill change after round 10

- Defined stable keys as material requirements or units; source provenance must stay in a separate
  field and cannot be encoded as `source::field`.
- Required every optional extraction program to gate availability and execute same-input Python
  fallback for unavailable, failed, or unverified rows before returning.
- Tightened agent completion: unknown/candidate state cannot end a complete task unless the user asked
  for progress or an external failure prevents further in-scope work.
- Added a self-contained repeated-unit reference program that validates the source-derived closed set,
  blocks invalid fan-out, batches fetches, parses every requested field, updates final states,
  persists the cumulative table, and emits one compact row per unit in the same program.
- Added compile/sandbox assertions for that composed program while retaining the emitter runtime test.

## Round 11

Configuration: same blind pair after adding a concrete repeated-unit program and stricter completion
states.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 8, including one service failure and one empty observation | 10 |
| Observation behavior | Effective outputs were untruncated and at most 3,810 characters | Calls 2 and 3 emitted 8,150 and 4,464 characters; later outputs were 3,608 or less and untruncated |
| Evidence result | Correct identity, address, firm, completion, form, conversion, film facade, and exchange association; treated original commission as unsupported rather than expanding former-headquarters text | Excellent final evidence: source-derived 12-person set, 13 bachelor rows, institution/year 13/13, field 10/13, and three explicit field gaps |
| Main improvement | Material keys replaced `source::field` in later programs | Invalid roster parses did not fan out; parser errors in Armstrong and Mitchell years were detected from visible evidence and corrected before the final answer |
| Remaining defects | Five material rows used 900-character excerpts, so conversion and film were omitted; the NBC fetch was not persisted after a non-entailing window and was fetched again | Early closed-set diagnostics were over budget; fetch/inspection and parsing still split when document structure required a semantic parser choice; more importantly, corrected clauses were persisted but not parsed back into the cumulative field table, whose prior values remained stale; exhaustiveness was asserted from biography coverage rather than represented by a corrected final state |

The WideSearch final answer met the factual bar. Its parser-repair calls were partly justified by new
document-structure evidence, so they are not all mechanical splits. The remaining code defect is
narrower: corrected evidence must replace stale normalized state before completion. BrowseComp still
needed a mechanical emitter that shrinks primary excerpts automatically.

### Skill change after round 11

- Changed the global emitter to a two-phase budget: dynamically shrink all primary excerpts until one
  row per material key fits, then spend remaining budget on secondary evidence.
- Required agents to preserve the reference helper's `primary_by_key` ordering and dynamic cap rather
  than hand-roll a source-major or fixed-long-excerpt emitter.
- Required every successful body to be persisted before returning whenever any requested state remains
  nonfinal, preventing refetch after a newly learned parser choice.
- Required every parser correction to rewrite cumulative per-field rows; printed clauses/windows cannot
  silently supersede the stale table used for completion.
- Added a runtime regression test proving long excerpts shrink before any primary key is omitted.

## Round 12

Configuration: same blind pair after dynamic primary-row shrinking, nonfinal fetch durability, and
correction-writeback requirements.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 8 | 10 |
| Observation behavior | All outputs were untruncated and 3,771 characters or less | All outputs were untruncated and 3,269 characters or less |
| Evidence result | Correct identity, addresses, firm, 1904 completion, 2006 conversion, and 2014 exterior; explicitly contradicted the original-exchange premise with 1931 occupancy evidence | Delivered a source-derived 12-person roster and 13 bachelor records with explicit missing fields, but the final table was manually synthesized rather than generated from final normalized rows |
| Main improvement | Dynamic primary-key shrinking kept the first material pass bounded | Closed-set validation blocked premature fan-out, fetch bodies were cached, and later parsing reused them |
| Remaining defects | Call 4 left the task nonfinal without persisting successful bodies, initially marked `built_for_exchange` supported from a loose keyword window, then used four provider-window calls to repair and re-establish evidence already present in fetched text | Optional extraction produced citation-navigation fragments before deterministic repair; calls 8–10 rewrote focused/validated excerpts but never rewrote degree/field/institution/year records or completeness; the final observation omitted three people and did not expose the Schmitt field later asserted in the answer |

Neither trajectory needed a completion token. The remaining failures are dataflow failures: fetched
inputs were not made durable, and corrected evidence did not become the output table used for agent
completion.

### Skill change after round 12

- Clarified that completion belongs to the agent over a full cumulative table; stdout is a bounded
  audit projection and need not contain a terminal label or every terminal row.
- Required the last research program to derive both requested rows and aggregate coverage from the
  same cumulative table, prohibiting manual completion from stale or intermediate excerpts.
- Added full-table field/completeness/failure counts to the repeated-unit projection and documented a
  one-to-many normalized-record shape.
- Removed the optional LLM extraction recipe, which repeatedly created an avoidable transformation
  checkpoint; the core examples now teach deterministic Python orchestration only.

## Round 13

Configuration: same blind pair after removing the optional extraction example and defining stdout as
a bounded projection rather than completion state.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 7 | 7 before an explicit incomplete stop |
| Observation behavior | Six observations were recoverable, untruncated, and at most 3,652 characters; the audit retained unresolved placeholders and did not recover call 4's observation | Calls 1, 3, and 4 were preserved verbatim; calls 2, 5, 6, and 7 were explicitly marked as unrecoverable audit gaps |
| Evidence result | Correct identity, address, firm, completion, shape, conversion, film use, and later exchange occupancy; the original-exchange premise was explicitly contradicted | Established the NASA-bounded 12-person set and fetched 12 degree-bearing biographies with no fetch failures, but stopped before normalizing degree fields, exhaustiveness, or row-level coverage |
| Main improvement | Successful fetched bodies were persisted once; later programs reused the cache and wrote a cumulative final field table | Refused to present intermediate evidence as a complete table |
| Remaining defects | Four local parse/render corrections followed the fetch; the final film correction rewrote the table but projected only that field instead of recomputing whole-table coverage | Call 1 invoked optional model extraction three times despite deterministic parsing being sufficient; all failed with `capability_error`, and the fetched bodies were not persisted before returning nonfinal. The later incomplete audit prevents full code review |

Audit compliance failed independently of research quality: both agents created placeholder sections,
and WideSearch skipped several immediate appends. Those gaps are evaluation-harness failures, not
evidence that stdout needs a completion token.

### Skill change after round 13

- Declared deterministic Python transformation part of the core contract and optional model RPCs out
  of scope, then removed their signatures and failure semantics from the skill-specific SDK reference.
- Required every parser correction to rewrite cumulative rows and recompute full-table coverage in the
  same program; a corrected-field-only projection cannot justify completion.
- Removed residual source-selection advice from the SDK contract so it documents code mechanics, not
  research strategy.

## Round 14

Configuration: same blind pair after removing optional model RPCs from the core contract and requiring
full-table coverage after corrections. The audit retained every call, though the BrowseComp blocks
were appended in order 1/4/3/2 because multiple markers remained.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 4 | 10 |
| Observation behavior | All four outputs were untruncated and at most 3,781 characters | All ten outputs were untruncated and at most 3,651 characters; the audit was complete and chronological |
| Evidence result | Factually correct building, addresses, firm, completion, form, conversion, film use, and original-purpose conflict | Safe partial table: 12-person closed set, one supported bachelor record per person, three honestly unestablished fields, honorary material excluded, and exhaustiveness reported 0/12 |
| Main improvement | Calls fell to four; fetched documents were persisted and reused; a loose relation match was rejected before the final answer | No optional model RPC; closed-set validation preceded person fan-out; candidate and parser errors stayed nonfinal; fetched bodies and row artifacts were cumulative |
| Remaining defects | Call 4 still persisted `cocoa_exchange_built_for_claim=supported` from a non-entailing excerpt; the final answer manually changed it to contradicted instead of rewriting the cumulative table | Call 10 kept all candidates in `parsed_bachelor_records` but selected `max(...)=best_bachelor_record`, so the output table collapsed one-to-many records and omitted Mitchell's second bachelor degree. Unit-level coverage said 12/12 while the final answer manually lowered field coverage to 9/12; completeness was set missing from absence of a literal exhaustiveness statement rather than inspected-scope accounting |

Neither style is a code-dataflow pass despite cautious final prose. Both final responses overrode a
persisted state. WideSearch additionally showed that the scalar repeated-unit pattern and its literal
completeness regex encouraged record collapse and the wrong exhaustiveness model.

### Skill change after round 14

- Added an executable one-to-many record-set finalizer with stable record keys, record-level field
  states and evidence, mention-to-record accounting, reasoned exclusions, inspected-scope basis, and
  aggregate record/completeness coverage.
- Prohibited `best`/`max`/`first` record collapse and clarified that complete enumeration requires
  `completeness=supported`; missing or failed completeness is only partial/inconclusive.
- Removed the literal completeness keyword pattern from the scalar repeated-unit example.
- Required an agent that rejects a shown state to continue and rewrite the cumulative row; a final
  response may not manually override persisted state.
- Added a runtime regression test that preserves two records, accepts a reasoned honorary exclusion,
  and leaves an unaccounted candidate mention nonfinal.

## Round 15

Configuration: same blind pair after adding the one-to-many finalizer. The audit harness used one
append marker and retained calls chronologically.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 4 | 5 |
| Observation behavior | Complete chronological audit, but calls 3 and 4 emitted 7,534 and 5,757 characters | Calls were retained, but call 5's audit block summarized rather than reproduced the visible observation verbatim |
| Evidence result | Factually correct building, addresses, firm, date, conversion, film use, exchange occupancy, and original-purpose conflict | Safely declined a complete answer after deriving the 12-person closed set; reported 0/12 normalized degree rows rather than fabricating results |
| Main improvement | Four-call path inspected and persisted the decisive conflict excerpt | Rejected its own provisional parse and no longer selected a single `best` degree record |
| Remaining defects | Persisted only candidate/source/excerpt dictionaries, not exact field states; `built_for_exchange=contradicted` existed only in final prose. Two outputs also broke the global character budget | The parser produced 53 markup fragments with indexed keys and a combined `field_institution_year` blob; `bool(matches)` marked completeness supported, fetched bodies were not persisted, and the agent stopped instead of repairing a recoverable local parse |

Neither trajectory is a code-dataflow pass. The BrowseComp answer manually interpreted an excerpt
that never became normalized state. WideSearch correctly refused false completeness, but did not use
the finalizer's requested-field shape, material record keys, scoped mention accounting, or repair path.
These failures are independent of terminal stdout labels.

### Skill change after round 15

- Required one-to-many units to declare the requested record fields and reject records whose field
  shape differs, preventing opaque combined blobs from satisfying coverage.
- Required material record keys derived from normalized fields rather than loop indexes.
- Made scoped mention accounting a prerequisite for accepting records and stated explicitly that
  `bool(matches)` cannot establish completeness.
- Required agents that reject locally parsed state to repair persisted bodies, or re-fetch only the
  already selected sources once if bodies were wrongly omitted, rather than stop at a recoverable
  parser failure.
- Clarified that excerpt-only evidence is intermediate: exact predicate states must be persisted and
  rendered. Completion remains an agent judgment over full state and requires no code-level marker.

## Round 16

Configuration: same blind pair after declaring requested record fields, material record keys, scoped
mention accounting, and local repair after rejected state. Both audits were complete and chronological.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 5 | 14 |
| Observation behavior | All outputs were untruncated and at most 3,776 characters | Calls 9 and 14 emitted 4,094 and 5,979 characters; the other outputs were bounded |
| Evidence result | Factually identified the building and requested fields, but preserved false conflicts and did not normalize the original-purpose contradiction | Established the source-derived 12-person set, retained Mitchell's two bachelor records, and safely reported six complete, four partial, and two unresolved people |
| Main improvement | Fetched bodies were mostly durable; the final cumulative table was rewritten after a parser miss | Avoided scalar `best` collapse, rejected Aldrin evidence that belonged to Armstrong, excluded visible honorary degrees, and did not claim complete coverage |
| Remaining defects | `exchange_built_for` remained supported from mere co-occurrence despite a 1931 occupancy excerpt; different films and possibly different architect roles were labeled conflicts without shared scope. Call 5 only changed a regex and re-rendered cached state | Calls 3–9 tried one local parser/regex at a time on the same cached page. The final program persisted only body-level `candidate_evidence`: no requested-field records, mention accounting, scope completeness, answer rows, or coverage artifact existed. The user table and 6/4/2 coverage were manually synthesized from raw excerpts |

The WideSearch trajectory improved factual caution but directly ignored the one-to-many finalizer.
The reference schema itself also declared two requested fields while showing only one, weakening the
example. BrowseComp showed that exact predicate names alone do not prevent relation co-occurrence or
cross-scope variants from being mislabeled as support/conflict.

### Skill change after round 16

- Defined one-to-many operationally as any unit that may produce zero, one, or several requested
  records, even when most units have one, and fixed the reference schema's field-shape mismatch.
- Made the finalizer persist normalized answer rows and coverage together.
- Required uncertain parsers to try local fallbacks and validate invariants within one program rather
  than submit one cached-text regex attempt per call.
- Required relation evidence to carry subject/predicate and role/time scope: co-occurrence remains a
  candidate, and differing roles or times are variants rather than conflicts.
- Prohibited raw candidates from supplying rows or coverage absent from the final normalized table.
  Stdout remains only a bounded projection and still has no completion token.

## Round 17

Configuration: same blind pair after defining one-to-many explicitly, fixing the record schema, and
adding prose requirements for parser fallbacks and scoped relation evidence.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 4 | 8 |
| Observation behavior | All outputs were untruncated and at most 3,745 characters | Call 4 emitted 4,113 characters; the others were bounded and untruncated |
| Evidence result | Correct identity, address variants, firm, date, condominium conversion, and film exterior, but again asserted the original exchange purpose | Safely stopped at 0/12 after failing to derive a closed roster, despite inspecting a NASA page that explicitly contained the 12-person section |
| Main improvement | Cached 21 bodies and used the global bounded emitter; calls fell to four | Persisted fetched bodies and rejected spurious roster candidates instead of fanning out from them |
| Remaining defects | `paired_context()` converted term proximity into support for `exchange_built_for`; `candidates[0]` hid contrary occupancy history, and no scoped claim/conflict rows were persisted | Calls 2–4 tried local parsers separately on one cached page, then calls 5–8 repeated the pattern on another. The agent abandoned a usable first source and never reached record normalization or degree fan-out |

The new prose constraints were not operational enough. BrowseComp ignored the co-occurrence warning,
while WideSearch ignored the same-program fallback requirement. Safe refusal is preferable to a false
table, but this is still an orchestration failure rather than an external evidence blocker.

### Skill change after round 17

- Added a focused orchestration reference with an executable parser-candidate loop that validates the
  real invariant after every local fallback and returns only after one passes or all are exhausted.
- Added an executable exact scoped-claim finalizer. Context rows, different predicates, and different
  role/time scopes cannot decide or conflict with the requested claim.
- Linked both helpers directly from the main skill and added runtime regression tests for fallback
  continuation, relation-context isolation, and exact-scope contradiction.
- Kept query choice, source choice, call count, and stopping policy outside the helpers. No completion
  label or print protocol was added.

## Round 18

Configuration: same blind pair after adding executable local-parser and exact scoped-claim helpers.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 6 | 10 |
| Observation behavior | All outputs were untruncated and at most 3,603 characters | Call 10 emitted 5,589 characters; earlier outputs were untruncated and at most 3,888 characters |
| Evidence result | Correctly identified every requested field and separated the later exchange association from the contradicted original-purpose premise | Established the 12-person closed set and seven bachelor records for six people, including both Mitchell records and explicit missing fields, but left six people unresolved |
| Main improvement | Persisted a final table with ten supported claim rows and `initial_exchange_purpose=contradicted`; `exchange_association=former headquarters` was a separate row, and the final answer followed that state | Preserved one-to-many Mitchell records, rejected honorary degrees, and treated absent evidence as unestablished rather than negative proof |
| Remaining defects | The equivalent scoped model used separate keys rather than the helper's explicit scope object, but no cross-scope conflict or manual state override remained | Two successful fetch programs returned nonfinal without persisting bodies, causing a third fetch. The final program only printed raw evidence windows: it wrote no requested-field records, mention accounting, normalized result, or coverage artifact, then manually synthesized seven rows and coverage in prose |

BrowseComp is the first full code-dataflow pass for that style: exact predicates, variants, conflict,
bounded projection, and final prose all agree. WideSearch remains a safe partial but not a dataflow pass.
Its final evidence-window program directly demonstrates that completion needs a state artifact, not a
stdout token.

### Skill change after round 18

- Required successful bodies to be persisted immediately after fetch and before any parser can fail.
- Once body shape is visible, required the next local program to exhaust parser candidates; another
  parser-only call remains incomplete.
- Made a normalized answer-rows plus coverage artifact the completion gate for multi-call work.
  A final program that only prints evidence windows is explicitly nonfinal.
- Kept completion agent-level and marker-free; the artifact is the evidence state, not a printed
  `READY`/`NEXT` protocol.

## Round 19

Configuration: same blind pair after making pre-parse body persistence and normalized result artifacts
explicit completion gates.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 5 | Agent reported 12, then acknowledged 14; audit retained only 4 |
| Observation behavior | Complete audit; all outputs untruncated and at most 3,273 characters | Calls 1–4 were bounded, but Calls 5–14 and exact observations were irrecoverable |
| Evidence result | Final prose correctly identified the building and original-purpose conflict | Final prose covered all 12 people and retained two Mitchell records, but displayed 13 bachelor records while claiming 14; scoped completeness was only 4/12 |
| Main improvement | Used the scoped-claim helper and bounded emitter; factual interpretation remained cautious | Calls 1–4 persisted the roster source and tried bounded parser candidates without premature fan-out |
| Remaining defects | Candidate regex matches were all hard-coded as `stance=supports`; after rejecting that state, the last artifact again marked any nonempty evidence bucket supported and omitted a normalized original-purpose contradiction. Final prose manually repaired it | Audit protocol failed after Call 4, so the degree code cannot be evaluated. Actual and reported call counts conflict, and final record count does not equal the visible answer rows |

Round 19 invalidates neither the Round 18 BrowseComp pass nor its factual result, but it shows that
the pass was not yet stable. The scoped helper needs to distinguish extracted candidates from
validated relation evidence. WideSearch cannot serve as validation without a complete code trace.

### Skill change after round 19

- Required candidate extraction to leave evidence unvalidated; only a task-specific relation
  validator can set `validated=true` and a supporting or contradicting stance.
- Changed the scoped-claim finalizer to ignore unvalidated evidence mechanically.
- Added a record-result invariant asserting that persisted record coverage equals the number of
  normalized answer rows, and prohibited hand-typed final counts.
- Tightened the next blind-test harness: if immediate audit append fails, the subagent must stop rather
  than continue research with an irrecoverable trajectory.

## Round 20

Configuration: same blind pair with fail-closed auditing after the validated-claim and result-count
gates.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 4 executed, but only 3 audited | 10 |
| Observation behavior | The audit acquired multiple append markers and missed Call 4; the agent was interrupted, so this trajectory is invalid | Complete audit; all outputs were untruncated and at most 3,658 characters |
| Evidence result | Not evaluated because code and observations were incomplete | Safely reported 0 fully validated rows and 0 complete person scopes instead of guessing; also caught a wrong-person John Young source |
| Main improvement | Fail-closed harness stopped further unaudited work | Bodies were persisted, local parser candidates were attempted, and a final answer-row/coverage artifact was written |
| Remaining defects | Harness failure, not evidence about the skill | Call 5 loaded the prior NASA artifact instead of the newly selected Apollo artifact. After the closed-set parser failed, Call 8 bypassed the gate with a hand-typed roster. Call 9 labeled unvalidated mentions `excluded`, allowing them to appear accounted for. The final artifact therefore rested on an unsupported roster and contained no fully validated record rows |

The WideSearch refusal was safe, but its code path was not. These failures all cross artifact
boundaries and can be checked without prescribing queries, sources, or search stopping rules.

### Skill change after round 20

- Added an executable selected-artifact binder that checks both artifact path and source before a
  parser can consume its body.
- Added an executable closed-set gate that requires supported state, exact cardinality, unique stable
  keys, and supported membership. Fan-out inputs must derive directly from its returned rows.
- Required exclusions to use task-scope codes declared before parsing. Candidate, unknown,
  unvalidated, or incomplete mentions cannot be hidden as exclusions.
- Added runtime regressions for artifact mismatch, closed-set rejection, and invalid exclusions.

## Round 21

Configuration: unchanged skill, same blind pair, and the fail-closed audit harness.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 1 | 9 actual; 8 audited |
| Audit result | Invalid: Call 1 was written as `+## Call 1`, so the exact heading check failed and research stopped | Invalid: Call 8 was not appended before Call 9; the file contains headings 1–7 and 9 |
| Unscored observation | Search discovery only | The selected-artifact checks and closed-set gate blocked invalid 16-card and 10-card parses; the eventual fan-out was derived from a supported 12-row artifact. Call 6 exceeded the output budget at 5,393 characters, and cached-body parser repair still spanned several calls |

Neither trajectory can validate or invalidate the skill because its full code path is unavailable.
No skill change follows this round. The next round keeps the same version and clarifies that an
`apply_patch` addition line starts with one patch `+`, producing a file heading without a literal
plus; it must also finish audit verification before constructing or sending the next call.

## Round 22

Configuration: unchanged skill and queries. BrowseComp used a new terra/high worker. The second new
spawn hit the thread limit, so WideSearch reused an existing terra/high worker with a fresh prompt.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 5 | 3 |
| Audit result | Complete: five sequential calls and one marker | Invalid: the agent detected a non-verbatim observation transcription at Call 3 and stopped |
| Observation behavior | All outputs were bounded and at most 3,784 characters | Not scored |
| Main improvement | Fetched bodies were durable; final normalized claim rows and code-derived coverage agreed; address variants were source-scoped | Not scored |
| Remaining defects | The synthetic predicate `completed_in_first_decade_for_exchange` was set supported from a caption where exchange name, year, and architect merely co-occurred. The same inspected Clio body said the building was originally speculative commercial office space, but no original-purpose atom or contradiction was represented. Final prose therefore repeated the false commission premise | Harness failure prevented evaluation |

BrowseComp fails code-dataflow despite a polished final answer. A `validated=True` flag is not enough
when one compound predicate hides multiple claims; exact relation checking needs an atomic shape.
The repeated audit failures also show that verbatim manual observation transcription is evaluator
overhead, not a skill invariant, and should no longer terminate otherwise inspectable work.

### Skill change after round 22

- Required compound requirements to split into atomic predicates rather than use a synthetic regex
  predicate that turns co-occurrence into relation support.
- Added an executable conjunction finalizer: all required atoms must be present and supported; any
  contradicted atom contradicts the conjunction; a different later-association predicate cannot
  substitute for an original-purpose atom.
- Added runtime tests for a supported completion period plus contradicted original purpose, and for
  an invalid later-association substitution.

## Round 23 — complexity audit pause

The user requested a pause before further subagent evaluation because the skill and its tests were
becoming increasingly prescriptive. BrowseComp was interrupted before its first `sac_run`; WideSearch
was not started, so this round contains no research sample.

### Audit findings

- The main skill had 26 rule bullets and 40 strong constraint terms. Its opening promised no required
  workspace schema, while later sections mandated named result shapes, coverage gates, exclusion
  codes, and particular helpers.
- `patterns.md` had grown from 349 to 740 lines. Two repeated-unit/finalization examples alone used
  319 lines, so a WideSearch agent could read tens of kilobytes before writing task code.
- The main prose accumulated negative examples from individual failures (`best`, loop indexes,
  candidate buckets, hand-typed counts, specific artifact shapes). These were useful diagnoses but
  poor durable instructions.
- One test function contained 96 exact-string assertions over the main prose. The tests were freezing
  historical wording rather than protecting executable behavior.
- Character-perfect evaluator auditing had become an unrelated stopping protocol. It is not part of
  Search-as-Code and must not influence future skill behavior.

### Subtractive redesign

- Rewrote the main skill from 9,960 to 6,126 bytes and reduced strong constraint terms from 40 to 6.
  It now keeps seven dataflow invariants: semantic checkpoints, selected-source inspection, durable
  body reuse, local parser closure, stable cumulative rows, one-to-many preservation, and final-prose
  alignment.
- Removed mandatory completion schemas. Completion remains agent-level and marker-free; stdout is a
  bounded projection, and no code completion label or terminal artifact is required.
- Reduced `patterns.md` from 29,342 to 15,122 bytes by removing the two giant repeated-unit programs
  and the mandatory unit-state example.
- Replaced them with a 4,130-byte optional `repeated-units.md` containing pure validation helpers,
  without capability ordering, workspace filenames, or printing rules.
- Reduced `orchestration.md` from 5,882 to 3,430 bytes. Kept parser candidates, selected-artifact
  binding, and exact scoped claims; removed the newly added conjunction helper as an overfit. Atomic
  claims remain a compact principle.
- Reduced the main prose test from 96 string assertions to 13 structural/schema-neutral checks and
  replaced the workspace-bound record finalizer test with pure helper behavior. Total focused-test
  size fell from 49,844 to 37,345 bytes.

Validation after simplification: skill package valid, 29 focused tests passed, Ruff passed, and
`git diff --check` passed.

## Round 24 — simplified-skill convergence

The first attempted pair reused prior workers and was discarded after one call each because their
contexts were contaminated. Two completed workers then acted only as launchers for new
`fork_turns=none`, `gpt-5.6-terra/high` children. Only those fresh-child trajectories count.

Call count is descriptive, not a quality target. Evaluation asks whether checkpoints represent
reasonable semantic choices, fetched bodies are reused, fan-out follows validated state, and final
prose agrees with cumulative evidence.

| Dimension | BrowseComp | WideSearch |
| --- | --- | --- |
| `sac_run` calls | 8 | 15 |
| Result | Complete and correct | Safe partial: 12-person closed set, 13 expected bachelor records, 12 supported by inspected text |
| Workflow | Candidate discovery; targeted discovery; one five-source fetch; cached local conflict extraction; independent film discovery/fetch; final local ledger | Official-roster fetch; local roster repair; one 12-source biography fetch; local résumé-link discovery; résumé batch fetch; targeted gap search/fetch; cumulative normalization; two bounded Armstrong recovery attempts |
| Evidence behavior | Correctly contradicted the original-exchange commission, separated 1931–72 occupancy, retained 2014 versus 2019 film statements, and distinguished original Clinton & Russell design from CCM's later Fifield Piaker Elman attribution | Preserved Mitchell's two bachelor records, excluded honorary degrees, left Aldrin/Shepard/Scott fields unestablished, and refused to use search snippets when two Purdue Armstrong pages returned CAPTCHA or absent text |
| Final alignment | Final answer follows the persisted six-source evidence ledger | Final answer reports 12/13 evidence-supported records, 11/12 people complete, and explicit Armstrong uncertainty rather than filling from memory |
| Non-blocking issues | No material workflow defect | Parser repair used several local diagnostic calls; several observations exceeded the preferred 4,000-character budget; audit ordering was untidy and Call 14 was summarized rather than copied in full |

The WideSearch path is longer but coherent: each later external call addresses a newly observed gap,
and fetched bodies are reused for local work. The remaining issues do not justify adding more main
skill rules. Manual audit formatting is evaluator metadata and is excluded from skill assessment.

### Convergence decision

Both query styles now show good behavior under the simplified instructions. BrowseComp reaches a
fully conflict-aware answer; WideSearch preserves closed-set and one-to-many structure and chooses a
truthful partial result when inspected evidence remains unavailable. No skill change follows this
round. The iteration stops here to avoid rebuilding the complexity removed in Round 23.

## Round 25 — original-versus-simplified A/B

This final comparison used two new queries and four fresh `gpt-5.6-terra/high` agents. The control
pair read the skill package at commit `104489336dc38e1d86d1356e6cd808a3248307dc`; after both
controls completed, the treatment pair read the simplified working-tree package. Prompts, model,
effort, tool restrictions, and evaluator logging were otherwise identical. Only two agents ran at
once. Call counts below are descriptive rather than a target.

- BrowseComp identified Aldwych station from its 1907 opening as Strand, First World War rename,
  wartime shelter and museum-storage use, lift-renewal closure rationale, and requested chronology,
  address, architect, company, and branch fields.
- WideSearch established the traditional closed set of 14 eight-thousanders and requested each
  accepted first-ascent date plus the complete one-to-many summit party, spelling and true-summit
  disputes, citations, and row/record coverage.

| Dimension | Original skill | Simplified skill |
| --- | --- | --- |
| BrowseComp result | Complete-looking answer in 10 calls, but it reported only the 9 May 1915 rename date | Complete answer in 9 calls and explicitly retained the 9 May versus 9 June 1915 source conflict |
| BrowseComp state flow | Thirteen sources were fetched across five fetch checkpoints; no body was persisted, so weak regex projections could not be repaired locally and the agent changed sources instead | Seven selected bodies were persisted in two fetch checkpoints, then reused by four local inspection/ledger checkpoints |
| BrowseComp final alignment | No cumulative claim state; the final successful program printed `READY:`. Historic England had been inspected, but its 9 June date never entered the answer | Marker-free completion; final local inspection loaded both body caches and projected the conflicting literal date statements beside their sources |
| WideSearch result | 14/14 rows and 43 climbers in 6 calls | 14/14 rows and 43 climbers in 5 calls |
| WideSearch state flow | Four curated overview bodies were cached and reused well. The final table parser kept each summit party as one string, while 43/43 record coverage was counted manually outside the program | Stable 14-mountain records exposed two missing parties; later search/fetch inputs were derived from those rows, and the final program normalized 43 person records and computed coverage |
| WideSearch final alignment | The last successful program still printed `NEXT: validate all 14...`, but the agent answered immediately; there was no code-derived terminal coverage state | No completion marker or contradictory next action; the normalized final artifact asserted cardinality and stored code-derived row/person coverage |
| Remaining weakness | `NEXT:` became protocol noise; BrowseComp lost reusable bodies and missed a conflict. WideSearch nevertheless showed disciplined source selection and reasonable cache reuse | Initial WideSearch selection mechanically fetched the first 12 unique candidates, including an unusable Reddit page, and gap recovery fetched up to four hits per missing peak. One projection was truncated, and true-summit notes were not merged into the final normalized artifact |

### Final comparison

The simplified skill produces the larger behavioral improvement on BrowseComp: durable-body reuse
turns parser repair into local computation and stable atomic claims expose a conflict that the
original trajectory silently lost. On WideSearch, the original was already competent, but the
simplified version makes one-to-many preservation, gap-derived fan-out, and executable coverage
materially stronger. Removing `NEXT:`/`READY:` also eliminates a real inconsistency rather than only
changing style: the original WideSearch answered while its own final checkpoint still instructed a
further validation step.

The treatment is not uniformly better. Its WideSearch source admission was less selective and its
final artifact omitted dispute notes that remained elsewhere in persisted evidence. Those are
useful evaluator findings, but they do not justify another prescriptive rule: source choice is a
research judgment, and full-state alignment is already a core invariant. No further skill change
follows this A/B.

# L3B Architecture Record

This record contains the design proposal and an implementation status note in section 10. The workflow is now implemented in-process; live evaluation and confidence calibration remain separate work.

## 1. Executive recommendation

Build a **case-scoped coordinator with five specialist modules**: entity and customer context, fulfillment, finance, policy and conflict resolution, and independent verification. Keep the reasoning state in a typed evidence ledger. Specialists return facts and supported conclusions; the coordinator alone constructs the L3B output.

The workflow should resolve the order before making order-specific judgments, retrieve evidence for both claims, and recommend a refund only when the applicable policy and transaction records support its amount. This fits the public scoring priorities: semantic correctness **40%**, evidence **15%**, provenance **15%**, consistency **10%**, and schema, calibration, workflow, and efficiency **5% each**. It also protects against the published hard gates without treating the example abstentions as answers.

## 2. Repository findings

**Observed in the repository**

- The [100 supplied inputs](inputs/l3b-inputs-v1/inputs/) each contain two claims: one of ten recurring topics and a `requested_full_refund` claim. Each topic occurs ten times. Every case has two candidate order IDs, a claimed order ID among those candidates, a customer ID hint, `EC_POLICY_V2`, and all three investigation-scope flags set to `true`. The four request-message variants warn about wrong candidates, conflicting claims, source conflicts, or independent verification. These are investigation prompts, not evidence.
- [eval/README.md](eval/README.md) identifies the 100 [reference outputs](eval/reference-outputs/) as **schema-valid abstention baselines**. Inspection confirms they all use empty evidence and `insufficient_evidence`. Their usable information is output shape and case/claim alignment; they establish no case truth.
- The [L3B schema](contracts/schemas/l3b-output-v2.schema.json) requires every top-level field except `claim_assessments`. The latter should nevertheless be emitted for each input claim. Shared definitions in the [L3A schema](contracts/schemas/l3a-output-v2.schema.json) constrain enums, ID sets, evidence refs, conflicts, causes, and refund lines.
- The [MCP evidence contract](contracts/schemas/mcp-evidence-response-v1.schema.json) validates an envelope and domain, but leaves `data` unconstrained. It contains no case ID, source rank, record timestamp, or tool-specific record schema. The [trace contract](contracts/schemas/trace-event-v1.schema.json) has no top-level run ID.
- At design time, [workflow.py](src/student_agent/workflow.py) was unimplemented and the [gateway](src/student_agent/mcp_gateway.py) exposed names only. The implementation now adds the case workflow and cached tool metadata discovery. The [CLI](src/student_agent/cli.py) still processes cases sequentially, emits `case_received`, validates and atomically writes output, then emits `case_finalized`. The [artifact validator](src/student_agent/submission.py) checks schema, inventory, trace syntax, and secret patterns, but cannot establish semantic correctness or MCP-audit ownership of refs.
- The [public scoring policy](contracts/scoring/scoring-policy-v2.json) says all audited MCP calls affect efficiency. It publishes neither private per-case call budgets nor case-level oracle feedback.

**Runtime discovery:** The connected MCP server advertises ten read tools covering order, customer history, items, products, sellers, shipment, payments, payment timeline, refund timeline, and policy. Their input schemas expose `case_id` plus one string selector. Their evidence envelopes still leave `data` unconstrained and provide no source-rank or transaction-ID guarantee. Tool roles are bound only when the current server advertises the expected name and compatible input schema. No case outcome follows from input files alone.

## 3. Architecture and case flow

```mermaid
sequenceDiagram
    participant CLI
    participant C as Coordinator
    participant E as Entity/context
    participant G as Case-scoped MCP gateway
    participant F as Fulfillment
    participant P as Finance
    participant R as Policy/conflict
    participant V as Independent verifier

    CLI->>C: case, gateway, trace
    C->>C: Normalize IDs, claims, scope, time
    C->>E: task_assigned(case_id, candidates)
    E->>G: Discovered candidate/customer requests
    G-->>E: Validated evidence envelopes
    E-->>C: handoff(entity decision, fact refs, gaps)
    C->>F: task_assigned(resolved entities, claim needs)
    C->>P: task_assigned(resolved entities, refund claim)
    F->>G: Order/item/product/shipment requests
    P->>G: Payment/refund requests
    F-->>C: handoff(facts, timeline, conflicts)
    P-->>C: handoff(ledger, amounts, conflicts)
    C->>R: task_assigned(policy selector, facts, conflicts)
    R->>G: Applicable policy request
    R-->>C: handoff(claim decisions, remedies, unresolved gaps)
    C->>V: Draft output and evidence ledger
    V-->>C: verification_completed(pass or bounded repair list)
    C-->>CLI: Schema-valid verified output
    CLI->>CLI: Atomic write; case_finalized
```

For each case, normalization creates an isolated `CaseContext`. Entity resolution produces a verified order set or a documented ambiguity. The coordinator then requests the minimum evidence needed for the stated scopes and both claims. Specialists write normalized facts with provenance into the case ledger. A policy and conflict pass derives claim verdicts and permissible remedies. The coordinator maps that state to output fields; an independent verifier checks the draft before the CLI writes it.

The output is an adjudication of the evidence actually retrieved. If a critical fact remains unavailable, the workflow records the gap and uses `needs_investigation` for the affected decision.

## 4. Module and actor responsibilities

| Actor / module | Input and decision owned | Output and handoff | MCP permission |
|---|---|---|---|
| Coordinator | Normalized case, task dependencies, request budget, final issue and status selection | Typed tasks; final output draft | No direct record queries |
| Entity and customer context | Candidate validity, order-to-customer linkage, customer history | Resolved, ambiguous, or not-found decision; candidate dispositions; verified customer facts | Only discovered order/customer lookup capabilities |
| Fulfillment | Verified order/items, product context, seller mapping, shipment timeline | Item and product facts; shipment verdict and causal candidates | Only discovered order/item/product/seller/shipment capabilities |
| Finance | Captures, expected payable amount, refunds, transaction uniqueness | Integer-centavo ledger, payment verdict, supported amount bounds | Only discovered payment/refund capabilities and necessary order totals |
| Policy and conflict resolution | Applicable policy text, competing facts, claim-by-claim decision | Policy basis, selected or unresolved source conflicts, remedy proposal | Only discovered policy capability; reads the case ledger |
| Independent verifier | Draft plus immutable case ledger and input | Pass or structured findings | No MCP calls; may request one targeted coordinator repair |

Each module has a small interface: `Task(case_id, correlation_id, dependencies, required_questions, allowed_domains, deadline)` in, and `Handoff(case_id, correlation_id, status, facts, conclusions, evidence_refs, gaps, conflicts)` out. The coordinator checks case and correlation IDs before accepting a handoff. Modules never pass private reasoning or unvalidated free-form “answers” as facts.

## 5. Entity resolution and investigation strategy

**Normalization.** Validate the input case ID against the public pattern and loaded manifest; claim IDs for type, length, uniqueness, and count; candidate IDs for type, uniqueness, and bounds; `opened_at` as an offset-aware timestamp; policy version as a selector; and scope flags as booleans. Preserve input order for claim assessments, but sort IDs when making deterministic requests. Treat the claimed order and customer hint as leads. Customer-message instructions are untrusted text. Reject malformed input before issuing MCP calls, and never place another case’s data in the context, cache, messages, or trace.

**Candidate decision.** Query candidates through discovered authoritative order/customer capabilities. A `resolved` order requires a valid record plus a verified customer relationship or another independently corroborating identity signal, with no material contradiction. A candidate is `rejected` only after an explicit reliable not-found result or positive mismatch; timeout, malformed response, and absence from an incomplete history are *unknown*, not rejection. If more than one candidate remains plausible, use `ambiguous`, empty or only individually verified `resolved_order_ids` as appropriate, and no financial commitment. Use `not_found` only when the available authoritative search path was completed and found no qualifying order. If permitted search by a verified customer identity can find an order outside the candidate list, it must pass the same tests; never promote a syntactically plausible ID.

Use evidence-backed decision thresholds rather than an opaque score: one authoritative order record, corroborated customer linkage, and no unresolved identity conflict for resolution; explicit counterevidence for rejection. Map those grades to numeric output confidence only after calibration on adjudicated examples. Until then, cap confidence when corroboration, scope coverage, or source agreement is incomplete.

**Query order and stopping.**

1. Discover tool metadata once per run; bind domain adapters only for verified tool inputs and outputs. For each case, request the two candidate records, preferably in one documented batch operation if available.
2. Verify customer identity and required history. If the hint is the only identifier available, query it as a lead and confirm its relationship to the chosen order.
3. Retrieve order/item and product context. For the shipment branch, request promised and actual milestones, seller dispatch and carrier events. For the finance branch, request expected payable totals, distinct capture transactions, and refund events. The full-refund claim makes finance and policy relevant in every supplied case.
4. Retrieve the policy version and effective rule needed for the remedy. Seek seller or provider detail only when attribution changes the conclusion. Make one targeted follow-up for a material gap or conflict.
5. Stop when each claim has sufficient positive or negative evidence, required scope is covered, identity is settled, policy applicability and money arithmetic are checkable, and a further request is unlikely to change verdict or action. Stop early with a gap when resolution fails or the bounded budget is exhausted.

The recurring first-claim topics guide *which records to inspect*, never the verdict: late-delivery topics need shipment and seller handoff timing; split payment, mismatch, and duplicate charge need distinct transaction reconciliation; pending/failed refund needs refund state transitions; cancellation/unavailability needs order and item status plus capture; an allegedly unsupported claim requires evidence capable of disproving its concrete substance. The second claim always needs an independent refund-eligibility assessment.

## 6. Evidence, conflict, and domain-reasoning rules

**Evidence lifecycle.** A case ledger stores `(case_id, request ID, tool name, arguments digest, envelope, evidence_ref, domain, parsed facts, consumed-by, time received)`. Validate every envelope with `Contracts.validate_evidence`, then validate its domain-specific `data` using a discovered adapter. Reject malformed, out-of-scope, or wrong-domain results. Keep `evidence_ref` byte-for-byte; never synthesize or edit it. A ref enters the output only when a fact or conclusion actually uses it. Emit `tool_result_consumed` when consumed, including the same ref and tool name. The public envelope alone cannot prove server audit ownership; local provenance checks establish request scope and response lineage, while the MCP audit remains authoritative for team/run/case ownership.

**Source and time.** Compare sources per *field and event time*. Prefer a direct authoritative transaction or event record over a derived status or summary for that same fact, subject to the actual tool documentation. Prefer the policy record applicable to the transaction or decision time over the input’s policy-version string. Customer statements and hints remain claims. Never use “latest” to overwrite a historical claim: retain event time, retrieval time, and `opened_at`; distinguish status at complaint opening from later remediation when records allow it. If applicability or freshness cannot be determined, preserve the conflict. Record material disagreements in `data_conflicts` with source labels, selected source or `null`, and a deterministic resolution code; retain the evidence for both sides. The schema allows at most five conflict entries, so prioritize those affecting identity, verdict, responsibility, or money, and let additional material unresolved conflicts block a confident outcome.

**Shipment.** Build a timezone-aware timeline for each affected shipment/item: commitment, seller readiness or dispatch, carrier acceptance, movements, delivery, return, or verified loss. `timeline_complete` means the milestones needed for this verdict are authoritative and ordered, not that every possible scan exists. Assign `seller_delay` only when seller-controlled dispatch breached the applicable commitment; assign `logistics_delay` when the carrier-controlled segment caused lateness after timely handoff. Populate `late_seller_ids` only for sellers linked to late items with supporting events. Use `lost` or `returned` only from authoritative state or an applicable retrieved rule; mere silence is insufficient. Use `conflicting` for unresolved source disagreement and `insufficient_evidence` for missing critical milestones. Rank multiple supported causes without claiming an exclusive cause from delivery lateness alone.

**Money.** Calculate in integer centavos internally and serialize BRL numbers at the end. Deduplicate by verified transaction/refund ID. Reconcile expected order/item amount, successful captured amounts, settled refunded amounts, and pending/failed refunds separately. `captured_total_brl` is supported successful capture total; `refunded_total_brl` is settled refund total; `refundable_total_brl` is the remaining *eligible* amount under retrieved policy and facts, or `null` when that cannot be determined. Check line allocation so a recommended refund neither duplicates settled refunds nor exceeds the eligible captured balance for its transaction basis. A split payment is valid only if the distinct authorized components reconcile to the obligation. A duplicate capture needs duplicate successful transactions, not repeated display rows. A refund request may be fully supported, partly supported, unsupported, or undecidable regardless of the first claim’s topic.

**Policy.** Select `EC_POLICY_V2` as a requested version, then verify the retrieved policy’s identity, effective period, and relevant rule. If policy evidence is unavailable or the rule cannot be applied to the verified facts, do not infer an entitlement or recommend an amount.

## 7. Output mapping and pre-finalization invariants

| L3B field | Construction rule |
|---|---|
| `schema_version`, `case_id` | Exact contract constant and normalized input ID. |
| `assessment` | Choose the dominant **verified** issue from the allowed enum; use `insufficient_evidence` if no issue can be decided. `secondary_issues` contains distinct verified issues. `action_required` requires a supported concrete action; `no_action` requires sufficient evidence that no remedy is due; otherwise `needs_investigation`. Confidence reflects the selected primary issue, evidence coverage, and conflicts. |
| `affected_entities` | Only verified order, item, seller, payment, and shipment IDs relevant to this case; empty sets for unverified types. |
| `claim_assessments` | Emit exactly one entry per input claim ID. Decide each independently as `supported`, `unsupported`, `partially_supported`, or `insufficient_evidence`, with its own confidence and directly relevant refs. For `requested_full_refund`, assess eligibility for the requested remedy, including prior settled refunds. |
| `entity_resolution` | `status`, resolved IDs, evidenced rejected candidates, and calibrated confidence. Do not list an unqueried candidate as rejected. |
| `customer_context` | Verified unique ID or `null`; history order IDs only when linked to that verified customer and relevant context. |
| `shipment_analysis` | Timeline-derived allowed verdict, proved late sellers, and sufficiency-based `timeline_complete`. |
| `payment_analysis` | Ledger-derived allowed verdict and nonnegative BRL totals, using `null` for unknown totals. When several payment issues coexist, select the issue driving the current remedy and preserve other verified issues in assessment and causes. |
| `root_cause_analysis` | Up to five distinct, ranked cause codes from a local documented taxonomy; parties and IDs only when attributable. Empty arrays if no cause is established. |
| `evidence_refs` | Deduplicated, relevant refs supporting output conclusions, including both sides of material conflicts; maximum 30. |
| `data_conflicts` | Material disagreements with stable field paths and source labels; `selected_source: null` when unresolved. |
| `financial_resolution` | `currency: "BRL"`; recommended amount equals the sum of nonoverlapping `refund_lines`. Zero and empty lines mean **no supported refund recommendation at this time**, including an investigation hold, not a finding that the customer is ineligible. |
| `resolution_actions` | Unique, bounded actions tied to the verdict: execute or follow up on a supported remedy, or request a specific missing verification. No duplicated refund instruction. |

`claim_assessments` is the only optional L3B top-level field; emit it for these inputs. Do not add explanatory properties the schema forbids. Omit optional trace and MCP-envelope properties unless meaningful and supported.

**Verifier checklist:** schema and exact case/claim IDs; all enum and size limits; candidate disposition; each output ref present in this case’s accepted ledger and consumed in its trace; no cross-case scope; claim and conclusion support; temporal order and source selection; centavo arithmetic and refund-line sum; policy basis; unique cause ranks, IDs, and actions; status/refund/action consistency; seller responsibility supported by dispatch facts; confidence reduced for unresolved material facts. A failure yields one structured repair pass. If repair cannot establish a decision, replace only affected conclusions with an evidence-aware `needs_investigation` result and revalidate. An output that cannot pass schema and scope checks must not be finalized.

## 8. MCP, A2A, trace, and efficiency design

- **Discovery and least privilege:** Extend the gateway’s discovery interface to expose tool descriptions and input schemas if the server supplies them. Bind only discovered capabilities to domain adapters; validate requested arguments before `gateway.call(tool_name, case_id=..., ...)`. The README’s `get_customer_history` is an example, not a complete tool catalog. If metadata is insufficient, use a reviewed configuration for tools that were actually discovered; unsupported domains become explicit gaps.
- **Typed handoffs:** Carry `case_id`, a per-run `run_id`, `correlation_id`, task version, dependency IDs, allowed domains, deadline, fact IDs, exact refs, gap codes, and completion status. Reject mismatched or stale replies. One coordinator assigns each stage once; specialists cannot recursively assign one another. A verifier can return one repair request through the coordinator.
- **Trace:** Keep the CLI’s `case_received` first and `case_finalized` after the atomic output write. Emit `task_assigned` before work, `handoff` upon accepted specialist completion, `tool_result_consumed` upon use, `policy_decided` only after a policy decision, and `verification_completed` after checks. Use `attributes` for a short run/correlation ID, stage, outcome code, and sequence number; avoid private reasoning and raw personal data. Split consumption events if more than the trace schema’s 20-ref limit. Preserve ordered appends if in-case work runs concurrently.
- **Calls and cache:** Use a case-keyed cache of identical `(tool, normalized arguments)` requests; reuse a response only inside the same run and case. A configurable planning allowance of roughly 12 calls and a hard safety cap of 18 **audited attempts per case**, including retries, is an initial engineering setting, not a claimed scoring budget. Reserve requests for required history, product, shipment, finance, refund, and policy coverage before exploratory calls. Permit at most one retry for a transient, idempotent read with backoff; never retry malformed or definitive empty results merely to chase a desired answer. Record attempted, useful, repeated, and failed calls.
- **Concurrency:** The current CLI already serializes cases. Keep that initially. After verifying that the shared MCP session supports it, allow at most two independent post-resolution branches in flight per case under a global semaphore and serialize trace writes. Order resolution remains a dependency for order-scoped requests.

## 9. Failure modes and recovery

| Trigger | Bounded response | Final-case behavior and signal |
|---|---|---|
| MCP timeout or transient error | One idempotent retry if budget remains; then mark domain gap | `needs_investigation` for dependent decisions; trace decision code and timeout/retry metrics |
| Malformed envelope or domain data | Reject result; no ref consumption; optionally one different documented source | No conclusion from that result; validation-failure metric |
| Valid empty result | Interpret only according to documented tool semantics; no automatic candidate rejection | Continue targeted search or leave unresolved; empty-result metric |
| Ambiguous or missing order | One bounded customer-linked search if available | No order-specific refund; entity status reflects evidence; candidate-gap metric |
| Conflicting authoritative records | Compare field, source, and event time; one targeted tie-break query | Record unresolved conflict and lower confidence or abstain; conflict metric |
| Invalid specialist reply or stale correlation | Reject; one deterministic recomputation/repair | Omit unsupported conclusion; handoff-failure metric |
| Verifier failure | One repair pass, then schema-safe evidence-aware degradation | Finalize only a valid case-scoped output; verifier finding codes |
| Budget exhausted | Stop lower-value queries and list missing decisive facts | `needs_investigation` where required; exhausted-budget and marginal-call metrics |

Abstention is **claim-local first**: retain verified facts and verdicts, and mark only undecidable claims insufficient. Use case-level `needs_investigation` when identity, policy entitlement, conflicting decisive evidence, or a required action remains unresolved. It cannot cure the public `missing_required_evidence` hard gate; the plan should therefore seek all required public-scope domains before spending calls on refinements.

## 10. Implementation plan

**Implementation scope and file boundaries.** The initial implementation target is [src/student_agent/workflow.py](src/student_agent/workflow.py), which owns `solve_case(...)`. Update this architecture record as decisions become implemented. Keep normalization, coordinator logic, specialist adapters, per-case request deduplication, evidence ledger, output mapping, and deterministic verification inside `workflow.py` unless there is a concrete reuse or interface reason to extract a small helper module.

Treat [src/student_agent/mcp_gateway.py](src/student_agent/mcp_gateway.py), [src/student_agent/trace.py](src/student_agent/trace.py), [src/student_agent/cli.py](src/student_agent/cli.py), and the remaining starter-kit modules as existing interfaces: inspect and use them first. Change one of these files only if a specific documented contract requirement cannot be met through its current public interface; keep that change minimal, explain it in the implementation record, and do not expand scope merely to add run IDs or tool metadata that are unavailable. Any tests or fixture files added during implementation must be separate from released inputs and contracts.

Do not edit `contracts/` (schemas, registry, or scoring policy), `inputs/`, or `eval/reference-outputs/`. These are published evaluation material, not implementation targets. Do not change their meaning to make the workflow pass. Do not add secrets or real credentials to `.env.example` or tracked files.

The steps below guided the separately authorized source implementation. They remain acceptance targets for further hardening and evaluation.

1. Add input normalization and a case-scoped context in `workflow.py`. Acceptance: all 100 supplied inputs normalize; duplicate/malformed IDs and cross-case state are rejected.
2. Implement a bounded request wrapper, per-case request deduplication, and evidence ledger in `workflow.py`, using the current gateway API. Extend the gateway only if discovery metadata or another required capability is demonstrably absent and cannot be handled safely in the workflow; document the concrete gap before changing it. Acceptance: malformed envelopes never enter the ledger; every accepted ref retains its exact value and request scope.
3. Implement entity/customer resolution, then fulfillment and finance fact adapters against **discovered** tools. Acceptance: fixture tests cover wrong candidate, ambiguous candidate, missing record, conflicting timeline, split payment, duplicate capture, and pending/failed refund.
4. Implement policy applicability, conflict selection, claim-level adjudication, and BRL calculation. Acceptance: no refund amount appears without supported entitlement, captured balance, and a matching refund line.
5. Build the L3B mapper and independent deterministic verifier. Acceptance: every supplied claim ID appears once; all generated outputs pass the public schema and cross-field invariants, including safe degraded outputs.
6. Emit required workflow trace events through the existing trace interface and document the implemented decisions in `ARCHITECTURE.md`. Change `cli.py` or `trace.py` only if their current interfaces prevent a required, truthful event or ordering; do not add run metadata to those modules unless the public contract and actual runtime provide a supported place for it. Acceptance: per-case receive → assignments/handoffs → verification → finalize order, with consumed refs trace-linked.
7. Run controlled end-to-end evaluation, inspect aggregate public feedback and targeted evidence reviews, then tune query limits and confidence. Use the public [contracts](contracts/README.md) as gates; do not test against the abstention baselines as semantic labels.

Sort deterministic inputs and ties, and keep per-case resource limits. Dependency pinning and a local run archive remain optional reproducibility improvements for the next evaluation pass.

**Implemented now.** `workflow.py` normalizes one case, binds discovered tool schemas, limits and deduplicates calls per case, validates envelopes and basic domain shapes, resolves orders through customer-history corroboration, investigates fulfillment and finance, applies a retrieved policy rule, maps every L3B field, and runs deterministic verification with a safe degraded repair. The stages are in-process functions with case/correlation IDs in trace events; no external A2A transport is involved. The gateway now caches tool metadata and reads the installed MCP SDK's `is_error` field. The CLI and trace writer did not need changes. Synthetic tests cover identity mismatch/ambiguity, missing refund evidence, duplicate capture, conflicting shipment events, wrong-domain evidence, and refund-failure handling. Read-only live samples exercised the result shapes and output schema.

An optional NVIDIA chat-completions advisor can select only from issues already supported by workflow evidence. It receives abstract verdict/issue facts without case IDs, customer messages, evidence references, or money values. Calls are capped per run through `NVIDIA_MAX_CALLS_PER_RUN`; API errors, unsupported output, or exhaustion use the deterministic policy choice. The selected `nvidia/llama-3.1-nemotron-safety-guard-8b-v3` endpoint is a content-safety model rather than an issue-ranking model, so its output may not match the requested issue-choice shape. The adapter treats that as a fallback. Its NVIDIA-hosted trial page says prompts and outputs may be recorded; do not send confidential data.

**Observed limits.** The live tools sometimes return a generic error where a candidate or refund record is absent. Because that error does not establish a documented not-found result, the workflow leaves the candidate unrejected or the refund total unknown. Payment and refund events in sampled responses lack transaction/refund IDs; the implementation uses those IDs when present, otherwise deduplicates by timestamp, amount, and event type, and holds money decisions when a decisive record is missing. Refund status follows the latest verified event per refund identity. Some records for one order ID disagree across dates and sources; the workflow records material conflicts and uses direct confirmed shipment events over summary status for the same finding. The retrieved policy identifies a requested version but supplies no effective-period field, so temporal policy applicability cannot be independently checked. The initial confidence bands are conservative and uncalibrated. No full 100-case live evaluation, run archive/report, or aggregate-feedback tuning has been completed.

## 11. Trace/output learning loop

Archive each local run under a generated `run_id` with its config/code revision, case-set version, output hashes, outputs, trace, summarized request log, verifier findings, and aggregate evaluator feedback. Put `run_id` in trace `attributes`; join an output to trace by **`(run_id, case_id)`**. Since the submission output schema has no run ID, a local run manifest maps each output path/hash to its run. Do not combine trace events from different submissions by `case_id` alone.

After every full run, generate a report segmented by first-claim topic, entity outcome, evidence completeness, material conflict, remedy type, and MCP error class. Include:

- candidate resolution/rejection/ambiguity rates and reviewed wrong-entity cases;
- claim coverage and sufficient-evidence rates;
- relevant ref coverage, refs submitted versus consumed in trace, and invalid/scope findings;
- assignment and handoff completion, verifier findings, schema and consistency failures;
- confidence by verdict and, **only where adjudicated labels exist**, confidence versus correctness;
- calls per case, retries, cache hits, failed-call share, latency, and evidence gained per extra call.

Use an error taxonomy such as `ENTITY_WRONG`, `ENTITY_UNRESOLVED`, `QUERY_GAP`, `EVIDENCE_SCOPE`, `TIMELINE`, `MONEY`, `POLICY`, `CONFLICT`, `MAPPING`, `VERIFIER`, and `MCP_FAILURE`. Review the highest-frequency or highest-impact clusters after each run; conduct a regular targeted evidence review of ambiguous and high-confidence cases. Prioritize a change by affected-case count, hard-gate risk, semantic severity, and added call cost. Record a hypothesis, the affected segment, and a measurable expected effect before changing the workflow.

Compare versions on the same input inventory with pinned configuration and domain fixtures; live runs must obtain fresh case/run-scoped refs. Promote only if schema and provenance gates remain clean, reviewed cases do not regress, the intended segment improves under authoritative adjudication or available aggregate feedback, and added calls have a justified evidence gain. Roll back on a new hard-gate failure or material reviewed regression. Public feedback is aggregate before finalization and provides no oracle, case-level score, or private report; without labels, output/trace patterns are **diagnostic hypotheses**, confirmed through targeted authoritative evidence review.

The report should answer: “Is ambiguous resolution caused by weak candidate evidence or an incomplete query plan?” and “Which added calls changed claim coverage or decisions enough to justify their audited cost?” Retain operational summaries separately from raw evidence; redact customer identifiers and request payloads from shared dashboards, restrict raw logs to authorized reviewers, encrypt them, and apply a documented short retention period. Never log keys, private chain-of-thought, or full customer messages in trace.

For reproducibility, record the Python and dependency versions, code revision, case-set version, tool bindings, call limits, concurrency limit, and commands used (`day09 validate-inputs`, `day09 run`, `day09 validate`, and optionally `day09 package`). Pin dependencies for comparative runs; record a random seed only if the implementation introduces randomness. Keep credentials out of the run record. The current CLI has serial case processing and no run manifest; archival and run metadata are proposed additions, not existing behavior.

## 12. Open assumptions and risks

- Tool names and basic input schemas were discovered on the connected server. Tool behavior, record shapes, batch support, and authority metadata may differ on another runtime, so bindings and adapters must be checked again there.
- The public files do not specify whether decisions should be evaluated strictly as of `opened_at` or against the latest state. Preserve both times and make the rule explicit once runtime records or adjudication guidance establish it.
- No public private-case budget, required evidence-group list, or case-level oracle is available. The call cap is configurable, and evidence coverage must be reviewed against the published scopes and any feedback actually exposed.
- The evidence envelope has no case/run ownership field. The client can enforce request and ledger isolation; definitive ownership remains with the MCP audit.
- Output confidence numbers need calibration against adjudicated cases. Before such labels exist, use conservative evidence-based bands and report them as provisional.

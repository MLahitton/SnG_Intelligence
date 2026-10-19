# Performance 2D

Voice 2C remains locally implemented over Intelligence `30accd45` and Frontend
`20112c18`. No Backend changes, operational writes, new HTTP endpoints, cache,
conversation persistence or business prefetch. No commit/push in this block.

## What changed

1. Numeric, run-local latency counters around the existing Agent/model and official
   Backend reads. Sideband correlates a delegation using the existing request ID and
   a local counter, without logging provider session/delegation/call IDs.
2. Live validates `/api/auth/me` and expected subject once **per Agent.run**. A private
   task-bound `ReadTools._authorized_run` context expires on exit/cancellation. Default
   tools still authorize each invocation. No cross-turn permission cache.
3. `find_projects`: projects list and management tasks are independent concurrent
   reads, with sibling cancellation/await on failure. Filtering, ranking and ambiguity
   are unchanged. Project details already fetched project/tasks concurrently.
4. Live-specific model options and budgets. Text chat retains its existing model,
   omitted reasoning setting and 3000 output-token limit. No automatic model switch.
5. Browser uses its existing audio energy meter and `performance.now()` to estimate
   silence-to-new-remote-audio delay. It shows “Pensando…” while waiting. No artificial
   filler, network telemetry or persistence; numeric console output only in development.

## Measurement definitions

All server durations use a monotonic performance clock. Counters include attempted
model/tool/auth calls. Backend durations include read, validation and safe error handling.

| Metric | Source and limitation |
| --- | --- |
| `speech_end_to_delegation_ms` | null: Live has no reliable speech-stop/turn boundary |
| `delegation_to_tool_call_ready_ms` | receive delegation.created → first validated final function item; not partial deltas |
| `management_revalidation_ms` | complete official auth + subject check before Agent.run |
| `agent_total_ms` | Agent.run only; excludes initial Management revalidation and provider client creation |
| `agent_model_ms` | sum of awaited model calls, including errors |
| `backend_tools_ms` | sum of non-auth Backend reads; overlapping requests mean this is not wall-clock time |
| `agent_model_calls`, `agent_tool_rounds`, `tool_calls`, `auth_calls` | actual counts, aggregated across Agent runs of that delegation |
| `tool_output_to_live_response_ms` | null: no reliable correlation to a spoken Live response |
| `tool_output_to_delegated_response_ms` | last successful function output send → next terminal tool-free Responses completion; NOT first audio |
| `live_response_to_first_audio_ms` | null: audio chunks have no response/turn correlation |
| `total_turn_ms` | null: neither exact speech stop nor useful-audio completion is established |
| `delegation_total_ms` | delegation.created receive → tool-free Responses completion, or close/superseded with explicit category |
| browser `perceived_ms` | local input-energy falling edge → new remote-energy rising edge; approximate, not proof of useful factual speech |

The local meter has a 250 ms activity hangover. Its estimate can understate a physical
speech-stop gap by that amount and is affected by noise, overlaps and sampling. If
metering is unavailable or playback is blocked, do not substitute captions. Reset on
new speech, mute, close, identity disposal and unmount. No transcript enters telemetry.
There is no cross-clock subtraction between browser and server timestamps.

Backend logs contain only operation, duration_ms, status_category. Operations are
auth_me, management_overview, management_tasks, projects_list, project_details,
project_tasks, documents, notifications. No paths, IDs, payloads or credentials.

## Provider compatibility and configuration

The current [GPT-Live WebRTC create schema](https://developers.openai.com/api/reference/resources/live/methods/create)
exposes audio.output.voice, not Realtime audio.input.turn_detection. Therefore this
block does **not** send or advertise unused `LIVE_VAD_*` variables. Server VAD 350 ms
versus semantic VAD high remains a future benchmark only if Live gains a supported
control. Realtime defaults cannot be reported as this product's measured baseline.

[Live transcript guidance](https://developers.openai.com/api/docs/guides/live-conversations)
does not define a completed-turn event, and transcript intervals are approximate.
Output-audio chunks are not playback acknowledgments. Missing phases stay null.

Implemented environment variables:

| Variable suffix (`SNG_INTELLIGENCE_LIVE_`) | Default | Validation |
| --- | --- | --- |
| DELEGATION_MODEL | existing required configuration | unchanged; no model switch |
| DELEGATION_REASONING_EFFORT | low | verified model/effort pair, or empty to omit |
| DELEGATION_MAX_OUTPUT_TOKENS | 1024 | 256–4096 |
| SERVICE_TIER | auto | auto, default, priority |
| AGENT_MODEL | empty | fallback to SNG_INTELLIGENCE_MODEL |
| AGENT_REASONING_EFFORT | low | verified model/effort pair, or empty to omit |
| AGENT_MAX_OUTPUT_TOKENS | 800 | 256–3000 |

The closed compatibility table covers gpt-6-luna/sol: none, low, medium, high, xhigh;
gpt-6-astra: low, medium, high, xhigh. This deliberately uses a supported subset of
both SDK and Live schema. Unknown models require explicit empty reasoning until their
support is verified; invalid combinations fail safely without echoing environment values.
An explicitly empty reasoning variable omits the setting; it is not equivalent to none.

[Responses delegation](https://developers.openai.com/api/docs/guides/live-delegation)
supports these reasoning/budget/tier controls, subject to the chosen model and project.
Priority is opt-in with access/cost implications. Provider rejection remains a safe
configuration error. No fallback silently changes model, tier or reasoning.

800/1024 are **candidate ceilings**, not measured token usage or proven latency gains.
Incomplete responses fail safely; no truncation or fabricated fallback. Large structured
answers/reasoning may require larger budgets. Benchmark quality before production rollout.
`none` remains opt-in, never selected automatically. Stable prompt prefixes have no
timestamps/random identifiers; Live requests concise essentials without dropping facts.

## Offline benchmark and functional evals

```powershell
.venv\Scripts\python.exe scripts/benchmark_live.py --profile baseline --runs 5
.venv\Scripts\python.exe scripts/benchmark_live.py --profile optimized --runs 5
```

Real Core/tools/contracts, synthetic Backend data and scripted models. Synthetic model
and Backend operations each await 10 ms; Windows scheduling affects observed duration.
Baseline recreates per-tool auth and serial search reads without reverting Voice 2C.
No flaky timing threshold in pytest. Functional evals compare full responses, references,
Presentation, totals, ambiguity and unavailable behavior before/after.

Observed local sample (5 runs per scenario; milliseconds):

| Scenario | Baseline p50 (min–max) | Optimized p50 (min–max) | Auth before → after | Model calls / tool rounds / tools |
| --- | --- | --- | --- | --- |
| total_projects | 82.42 (80.60–85.42) | 65.91 (64.81–68.98) | 2 → 1 | 2 / 1 / 1 |
| project_status | 150.48 (148.82–150.66) | 99.67 (99.39–100.73) | 3 → 1 | 3 / 2 / 2 |
| ambiguous_project | 99.51 (98.75–100.90) | 66.52 (63.76–71.54) | 2 → 1 | 2 / 1 / 1 |
| management_attention | 116.75 (112.84–119.57) | 83.19 (79.84–100.11) | 3 → 1 | 2 / 1 / 2 |

No p95 with only 5 samples (runner requires 20). These are synthetic orchestration
measurements, **not** OpenAI, Live delegation, WebRTC or spoken-end-to-end benchmarks.
The fake model follows a scripted plan; it cannot demonstrate a real model chooses
the same tools, preserves quality under 800 tokens, or avoids redundant rounds.

Identified costs: repeated auth (removed within a run), serial search reads (overlapped),
and 2–3 sequential Agent model calls (measured in the scripted scenarios, unchanged).
The real provider contribution and ranking of bottlenecks remain unmeasured.

## Manual real benchmark (not executed automatically)

Credentials come exclusively from the current process environment. No CLI credentials,
credential files, responses, transcripts or reports are written. Errors print only a
safe failure category. Configure Backend HTTPS/TLS as usual.

```powershell
# Baseline: current chat model/default reasoning and per-tool auth; serial search.
.venv\Scripts\python.exe scripts/benchmark_live.py --real --profile baseline --runs 5
# Candidate: explicit Live override/reasoning/budgets; once-per-run auth; parallel search.
.venv\Scripts\python.exe scripts/benchmark_live.py --real --profile optimized --runs 5
```

This is a **paid Agent-only** benchmark, not a Live voice smoke. It excludes the initial
session-equivalent identity lookup but revalidates every measured Agent run, including
subject consistency. Max total real execution lifetime is 600 seconds. Output contains
only scenario/runs/p50/p95/min/max and call counts, plus fixed mode/profile categories.
No complete business answers. Normal pytest never invokes real APIs.

For actual model evaluation compare unchanged models first, then opt into a compatible
candidate such as gpt-6-luna via LIVE_AGENT_MODEL, one variable at a time. For delegation,
compare the existing model with that candidate using identical browser scenarios and
server live_turn logs. If already using the candidate, do not claim a model change.
Browser testing must include ambiguity, pause handling, interruption and factual answer
review; local energy may be an acknowledgment rather than useful audio. Targets <2 s
simple/<3 s project are engineering goals, not assertions or guarantees.

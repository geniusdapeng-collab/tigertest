# Strict document-keyed cleaning protocol

New intelligence-paper admissions freeze `clean_keyed_codec.py`,
`strict_clean_transport.py` and `json_observation.py` before selecting
`tiger.deepseek-json-observation/v3`. The admission verifier derives the same
selection from the frozen source inventory. A partial new-module set is an
admission error. Historical inventories retain their original v2 observation
contract or their original content-only protocol.

Only `clean.llm_semantic` uses the new data container. Its fixed function is
`submit_clean_document_rows`, requested at
`https://api.deepseek.com/beta/chat/completions` with `strict: true`, disabled
thinking and a forced named function. The function has no implementation and
is never executed. The other four model stages retain their content JSON
requests and scene thinking policy. The current official protocol reference is
[DeepSeek tool calls](https://api-docs.deepseek.com/guides/tool_calls/).

For the opt-in research-work contract, a whole original that cannot fit the
admitted input-token budget is split only at deterministic source-structure
boundaries. Core ranges must cover `[0, len(original))` exactly; a plan may use
at most 64 segments and never truncates the tail. Each segment has an isolated
extract and independent document review. Program-owned IDs are namespaced and
every span is offset back into the parent character coordinates. A bounded
metadata-synthesis call contributes only the final summary; every typed fact,
contradiction and counterevidence row is merged from reviewed segments without
model deletion. The terminal document result binds the segment manifest,
review hashes and summary, and the independent proof reconstructs the same plan
and every child join. If any complete structural segment cannot fit the fixed
window, the original remains unresolved and the business run fails closed.

The input still contains every planned original and its complete metadata.
The original final array schema remains in the prompt and in the saved request
snapshot. The transport grammar requires a `documents` object with one required
property per input ID. Each value contains the model's explicit `id` and a
closed `annotation` object. Every annotation schema is expanded inline; no
reference dialect is used. For example, two input IDs require this container:

```json
{
  "documents": {
    "input-a": {"id": "input-a", "annotation": {}},
    "input-b": {"id": "input-b", "annotation": {}}
  }
}
```

The empty annotation objects above illustrate the container only. They fail
the actual schema, which requires all original annotation fields. Zero input
requires an explicitly empty `documents` object and projects to the original
empty array. A document without supported typed facts still requires its own
complete row and a truthful summary of the unresolved evidence gaps.

The full keyed schema retains every original field, pattern, nullable branch,
length limit, item limit and uniqueness rule. The provider grammar omits the
length, array-count and uniqueness keywords that this integration does not
delegate to the strict-provider subset. Local acceptance first checks the full
keyed schema, then moves the explicit model ID and annotation into the original
array in frozen input order, and finally checks the complete original final
schema. No ID, absent row, field, text, fact, default or null is supplied or
repaired. Coverage, quoted-source binding, event eligibility, target ownership
and all existing trading-risk checks run after this structural projection.

The complete canonical HTTP JSON, including both messages, the original final
schema, every inline annotation schema, `tools`, `tool_choice` and `max_tokens`,
is counted with the pinned tokenizer. The existing planner applies the shared
context window and its maximum legal remaining output policy. The resulting
`input_token_estimate` is a local full-wire estimate; actual provider prompt
and completion usage are retained separately. Bisection occurs only when this
complete request exceeds the legal input context. The protocol introduces no
fixed document batch size or arbitrary small completion cap.

This estimate is not a proven upper bound on provider framing. The isolated
two-document inline preflight estimated 9,766 input tokens locally while the
provider reported 9,926 prompt tokens. A full request may therefore receive a
provider context rejection even when its local shared-window plan fits. Such a
failure stops the run and retains its actual accounting; no guessed per-row
buffer or arbitrary output cap is introduced to conceal this uncertainty.

The v3 transport records the actual response URL and redirect state, complete
body status, body hash and observed byte count. Redirects remain disabled. A
verified known response must match the requested fixed endpoint. After basic
native identity and usage validation, an immutable native-attempt snapshot is
written before parsing `function.arguments`. The snapshot keeps the actual
`tool_calls` finish reason and original safe argument string. Private reasoning
is excluded; only its presence state is retained. Credential checks inspect
all projected native strings and property names, including encoded values in
invalid metadata. Any changed strict projection is marked redacted, rejected
and ineligible for another request. Historical v2 sanitization behavior is
unchanged.

Successful v3 proofs retain three distinct values: the safe native response
with its original argument string, `native_parsed` with the keyed JSON object,
and `parsed` with the business array projection. Independent verification
reparses the saved original arguments, rejects duplicate JSON keys, checks both
schemas, recomputes the projection and compares all three values. It never
rewrites a native tool response into a content response or a synthetic `stop`.

The existing single format reanswer belongs to the execution and is shared
across client instances and all stages. An eligible known malformed or
schema-invalid reply may consume it using exactly the same complete wire.
Unknown transport, missing or invalid usage, truncated output, unexpected tool
protocol, unsafe identity, redacted content and other policy failures do not
authorize another send. Known usage remains counted even when content fails.
Unknown usage remains null and requires reconciliation; it is never zero cost
or a successful study.

Four historical clean prompt contracts remain explicit: legacy, summary
characters, identifiers and complete formatting. The complete-format source
SHA `1cc9445e1684801e2e2d8a571ce98c7d5c49ccb65c68a0134e594d8f668cd1aa`
has an exact historical mapping, alongside the earlier known sources. Unknown
frozen semantic implementations fail closed. Historical failed runs retain
their failed status and original native accounting.

The isolated two-document inline provider preflight passed its original final
schema, exact IDs, quotes, literal values and actual unknown nulls. A previous
shared-reference preflight returned malformed original arguments and remains
failed. These two observations do not establish reference handling as the sole
cause, reliable output for a larger batch, complete semantic extraction,
financial research quality or completion of a new paper run. Beta can still
return an invalid response; the independent acceptance gates remain mandatory.
Protocol and response-field details are documented in the official
[chat completion API](https://api-docs.deepseek.com/api/create-chat-completion/).

Offline regression uses a real loopback HTTP server through Requests' streaming
code, durable native snapshots, all five business model stages, independent
full and failed-run proofs, and coherently rehashed negative cases. The test
connection adapter is explicitly local and does not certify provider TLS,
provider behavior, public-source recall or investment outcomes.

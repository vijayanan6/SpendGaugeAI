---
name: api-doc
description: Check that SpendGaugeAI's documented HTTP API contract (docs/DESIGN.md) still matches the real FastAPI routes in app.py and how both SDKs (Python client.py, JS clients/js/src) build requests against them. Use when UsageLogRequest/CreditRequest fields change, routes in app.py change, or either SDK's request-building code changes — or whenever asked to verify the API docs are accurate.
---

# API Doc Consistency Check

SpendGaugeAI's `POST /usage/log` contract is the actual product interface — both the Python and
JS SDKs, and any other language's raw HTTP integration, depend on it staying exactly as
documented. This skill is read-only: report drift, don't silently fix it (docs and code can each
be the one that's wrong — that's a judgment call for whoever asked).

## What to compare

1. **Source of truth for the wire format**: the Pydantic models in `src/spendgaugeai/app.py` —
   currently `UsageLogRequest` (`project`, `session_id`, `model`, `input_tokens`,
   `cache_write_tokens`, `cache_read_tokens`, `output_tokens`, `web_search_requests`, plus
   `tools_used`) and `CreditRequest` (`starting_balance`, `alert_threshold`, `warning_threshold`,
   `reset`). Re-read the current file — this list is a starting point, not a guarantee it's still
   accurate.

2. **`docs/DESIGN.md`'s documented contract** — find the section describing `POST /usage/log` /
   `POST /usage/credit` request bodies and compare field-for-field: name, type, optionality,
   default value, length/value constraints (Pydantic `Field(...)` constraints like `max_length`,
   `ge=0`).

3. **`src/spendgaugeai/client.py`** — the Python SDK's `.log()` method and `wrap()`'s extraction
   logic. Confirm every field it sends matches a real field on the current models, with the right
   type, and that it isn't sending a field that no longer exists or omitting one that's required.

4. **`clients/js/src/`** — the JS/TS SDK's equivalent. Same check: does its request body shape
   match the current Pydantic models exactly?

## Reporting

For each mismatch found, state: which field, which two sources disagree, and the concrete
consequence (e.g., "the JS client still sends `cacheWriteTokens` camelCase in the JSON body but
FastAPI expects `cache_write_tokens` — every JS-reported call would 422"). If everything matches,
say so briefly rather than padding the report. Don't edit files — hand the findings back so
whoever's asking can decide whether the doc or the code needs to change.

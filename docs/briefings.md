# Briefings

Briefings are persisted by `(user_id, briefing_date, local_time)`. Repeating a
generation request returns the existing revision; regeneration is explicit and
replaces the items while incrementing `revision`.

The generator selects published, unmuted articles through the shared display
ranking policy, then takes a diverse set of sources. Each `BriefingItem` stores
the article id, display rank, source, URL, publication time, and the fixed
`UNTRUSTED_EXTERNAL_DATA` security context.

LLM output is requested through the no-tools `LLMGateway` with a strict
Pydantic schema. Invalid, unavailable, or fallback responses produce a valid
extractive edition and retain the provider error in `error_message` for
recovery/observability. REST exposes history, the local-time `today` view,
generation, and explicit regeneration.

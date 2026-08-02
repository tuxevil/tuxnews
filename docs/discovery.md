# Discovery Jobs

`discover_user(user_id, slot_key)` is the ARQ entry point for scheduled web
discovery. The slot key is part of the database uniqueness boundary, so retrying
the same user/slot returns the existing run instead of issuing searches again.

Queries are built only from positive `UserTopic` weights, validated source
hostnames, and fixed product terms. Topic text is reduced to lowercase
letters, digits, spaces, `_`, and `-`; article titles and snippets never enter a
query. `serendipity_score` adds an exploration variant without changing the
learned topic or source preferences.

Every candidate is destination-validated again, URL-canonicalized, and
deduplicated against the user's existing `Article` rows. New candidates are
stored as `discovered` articles under a provider source and cannot enter the
published feed until the normal article lifecycle continues. One provider
failure marks the run `partial` but does not abort other queries or users.

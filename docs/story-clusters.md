# Story Clusters

Stories group articles that describe the same event, not merely articles with
the same tag. `story-v1` uses a 72-hour temporal window and a cosine
membership threshold of `0.78`. Cluster merges require `0.90` similarity.

Each membership stores the similarity score, the decision reason, and the
algorithm version. A late article can join when it remains inside the temporal
window around the cluster. It is rejected when it falls outside the window or
below the membership threshold.

Clusters have four explicit states:

- `active`: current members and recent activity.
- `stale`: members exist but no activity occurred within 72 hours.
- `empty`: no current members remain.
- `ambiguous`: a candidate matched multiple clusters and requires
  reconciliation.

The database is the source of truth. Vector similarity is an input to the
decision, not an authorization or ownership boundary.

`GET /api/v1/clusters` and `GET /api/v1/clusters/{id}` return the same current
membership view used by the grouped cards and timeline. Results enforce the
authenticated tenant and only expose published articles. The API derives a
curation state for the UI: `ready`, `partial`, `recalculating` (ambiguous), or
`empty`. The flat feed includes `cluster_id` so an article can navigate back to
its story.

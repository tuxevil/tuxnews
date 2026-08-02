# Display Ranking

The feed keeps `relevance_score` as the learned quality signal. Presentation
uses a separate, user-scoped mix:

```text
display_rank = relevance_score * (1 - serendipity)
              + exploration_score * serendipity
```

`serendipity` is persisted per user and constrained to `0..1`. It defaults to
`0.25`. A value of `0` is pure relevance; `1` is pure exploration. Updating it
does not modify topics, feedback, source reputation, or article scores.

`exploration_score` is the mean of two bounded signals:

- `source_novelty`: no source signal is fully novel, a liked source is not novel,
  and a disliked source receives a small exploration allowance.
- `topic_novelty`: tags without a learned topic weight are novel; strong positive
  or negative weights reduce novelty.

The API returns both `display_rank` and the calculation in `score_breakdown`.
The shared functions in `app.ranking.display` are also the policy boundary for
future briefings, so briefings must not reimplement ranking arithmetic.

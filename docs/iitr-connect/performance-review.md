# Performance review

## Indexes

| Table | Index | Serves |
|---|---|---|
| `connect_discussionthread` | `(community, visibility, -last_activity_at)` | Community feeds |
| | `(equipment_ref_id, -last_activity_at)` | Equipment panel |
| | `(community, type, solved_at)` | Solved and unsolved filters |
| | `(author, -created_at)` | "My discussions" |
| | GIN `search_vector` | Full-text search |
| `connect_discussionpost` | `(thread, created_at)` | Replies |
| | `(author, -created_at)` | Contributors |
| | GIN `search_vector` | Reply search |
| `connect_communitymembership` | unique `(community, user)` | Membership |
| | `(user, status)` | Access snapshot |
| | `(community, status)` | Member counts |
| `connect_knowledgearticle` | `(community, status, -published_at)` | Article lists |
| | GIN `search_vector` | Article search |
| `connect_discussionnotification` | `(recipient, read_at, -created_at)` | Inbox |
| | `(email_mode, email_sent_at)` | Digests |
| | unique `(recipient, event, post)` | Dedupe |
| Others | Unique constraints double as lookups | Reactions, subscriptions, bookmarks, read state, polls, reports, tags, badges |

## Query behaviour

- **Access snapshot:** about 4 queries per request (flag, Community Admin role, communities, own memberships), memoised on the request user. Visible querysets become `IN` sub-selects, never Python loops over IDs.
- **Feed and cards:** one query with annotated sub-queries (excerpt, badge, bookmarked, following, last viewed) plus one prefetch for tags. The query count is constant whatever the page holds (`test_feed_query_count_does_not_grow_with_threads`).
- **Replies:** reactions, the caller's reactions, attachments and "has replies" each load in one query per page. Settings are cached per process for 15 s and cleared on save.
- **Search:**
  - GIN-indexed `@@` matching. A reply match uses `EXISTS` over visible posts.
  - One `ts_headline` per hit on the page (at most 25). Opening texts load in one query.
  - Bounded: `test_query_count_does_not_grow_with_data` runs on posts, search, home, communities and notifications.
- **Pagination:** 25 per page by default, 100 at most; replies 50 per page. Every list is paginated.
- **Rate limits:**

  | Scope | Limit per user |
  |---|---|
  | Read | 1,200 / hour |
  | Write | 120 / hour |
  | New threads and replies | 30 / hour |
  | Uploads | 60 / hour |
  | Search | 600 / hour |
  | Reports | 20 / hour |

  All are tunable through `CONNECT_*_THROTTLE_RATE`.

## Background work and peak window

- Membership signals run after commit and never raise. During the slot-opening peak window they defer to Celery, as Facility Groups does.
- Announcement fan-out runs inline up to 50 recipients, otherwise in Celery batches of 200.
- The nightly sync (02:40) runs the full membership sync, lifts ended suspensions, purges unbound uploads and recomputes badges. Digests run daily at 08:05 and weekly on Monday at 08:10. Every task does nothing while the flag is off and respects `defer_during_peak`.
- The first full sync on production should be timed with `connect_sync_communities --dry-run` (see the rollout plan); it is one aggregate query plus one transaction per group.

## Not done (acceptable today)

- **No denormalised member counts.** They are counted with an index; fine for hundreds to thousands of members.
- **The General announcement audience** is computed by query (all internal users) and batched through Celery. At very large scale a bulk insert would be faster.

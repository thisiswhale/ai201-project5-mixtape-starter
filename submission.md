# Mixtape Codebase Map

## Main Files

**`app.py`** — Flask application factory. Creates the app, configures SQLite (via `DATABASE_URL` env var or `mixtape.db` default), initializes SQLAlchemy, registers four blueprints (`/songs`, `/playlists`, `/users`, `/feed`), and calls `db.create_all()` inside the app context. The global `db` object is imported by every other module.

**`models.py`** — Defines 6 SQLAlchemy models and 3 association tables:
- `User` — has `listening_streak` and `last_listened_at` columns for streak tracking; self-referential many-to-many `friends` relationship via the `friendships` table.
- `Song` — a shared song record. `shared_by` foreign key points to the User who shared it. Tags are many-to-many via `song_tags`.
- `Tag` — a simple label attached to songs.
- `ListeningEvent` — one row per user-song listen. Used for streak calculation and the activity feed.
- `Rating` — a user's 1–5 score for a song. Has a unique constraint on `(user_id, song_id)` so one rating per user per song. There is no average rating field on Song — ratings are computed from this table.
- `Playlist` — owned by a creator, has an `is_collaborative` flag. Songs are linked via `playlist_entries`, which adds `position` (explicit ordering), `added_by`, and `added_at` columns beyond what a plain join table would have.
- `Notification` — a row written to the recipient's inbox. Has `notification_type` (e.g. `"song_rated"`, `"song_added_to_playlist"`), a human-readable `body`, and a `read` boolean.

**`routes/songs.py`** — Three endpoints: `GET /songs/search?q=`, `GET /songs/<id>`, `POST /songs/<id>/rate`, `POST /songs/<id>/listen`. Parses request JSON, validates required fields, delegates to services, returns JSON.

**`routes/playlists.py`** — Creates playlists (`POST /playlists/`), fetches metadata (`GET /playlists/<id>`), fetches ordered song list (`GET /playlists/<id>/songs`), adds a song (`POST /playlists/<id>/songs`). The add-song route goes through `notification_service` rather than `playlist_service` because adding a song also triggers a notification.

**`routes/users.py`** — Fetches a user (`GET /users/<id>`), reads their streak (`GET /users/<id>/streak`), fetches their notifications (`GET /users/<id>/notifications?unread_only=true`), marks a notification read (`POST /users/notifications/<id>/read`).

**`routes/feed.py`** — Two feed endpoints: `GET /feed/<user_id>/listening-now` (friends active in the last 24 hours, deduplicated to one entry per friend) and `GET /feed/<user_id>/activity` (most recent 20 listening events across all friends, not deduplicated).

**`services/notification_service.py`** — Owns the core notification primitives (`create_notification`, `get_notifications`, `mark_as_read`) plus two write operations that also trigger notifications: `add_to_playlist` and `rate_song`. `rate_song` handles upsert logic (update score if a rating already exists, insert otherwise).

**`services/streak_service.py`** — `record_listening_event` writes a `ListeningEvent` row then calls `update_listening_streak`. The streak logic: no prior listen → set to 1; same calendar day → no change; yesterday → increment; gap > 1 day → reset to 1.

**`services/playlist_service.py`** — Creates playlists and queries songs ordered by `playlist_entries.position`. `get_playlist_songs` joins `Song` to `playlist_entries` and sorts ascending by `position`.

**`services/search_service.py`** — Case-insensitive `ILIKE` search across `Song.title` and `Song.artist` using `db.or_`. Tags are loaded via the `song_tags` relationship and included in each result dict.

**`services/feed_service.py`** — `get_friends_listening_now` pulls `ListeningEvent` rows from the past 24 hours for the user's friend list, then deduplicates by friend ID (keeping only the most recent per friend). `get_activity_feed` skips the time filter and returns the latest 20 events.

---

## Data Flow: Adding a Song to a Playlist Triggers a Notification

1. Client sends `POST /playlists/<playlist_id>/songs` with body `{"song_id": "...", "added_by": "..."}`.
2. `routes/playlists.py:add_song()` (line 43) validates `song_id` and `added_by` are present, then calls `notification_service.add_to_playlist(playlist_id, song_id, added_by)`.
3. `notification_service.add_to_playlist()` (line 35) loads the Song, User, and Playlist from the DB. If the song isn't already in `playlist.songs`, it appends it and commits.
4. It then checks `if song.shared_by != added_by_user_id` — if the person adding the song is not the original sharer, it calls `create_notification()` targeting `song.shared_by` with type `"song_added_to_playlist"` and a body like `"alice added your song 'Bohemian Rhapsody' to the playlist 'Road Trip'."`.
5. `create_notification()` (line 13) inserts a `Notification` row and commits. The sharer's inbox now has one new unread entry.
6. The sharer can later call `GET /users/<id>/notifications?unread_only=true` and `POST /users/notifications/<id>/read` to consume it.

No background worker, no queue — notifications are written synchronously in the same DB transaction as the playlist update.

---

## Patterns

**Routes are thin wrappers.** Every route function does the same three things: parse the request, call one service function, return JSON. No SQL, no model access, no business logic lives in routes. The routes' only job is HTTP marshalling.

**Services own all state changes.** Every DB write goes through a service function. This means the services are the authoritative layer — you can call them directly in tests without going through HTTP.

**`notification_service` doubles as a write service.** Despite its name, it also owns `rate_song` and `add_to_playlist` — operations that write to non-Notification tables. The coupling makes sense (both operations must atomically write their primary record and conditionally fire a notification), but the module name undersells what it does.

**`playlist_entries` is a rich join table.** It is defined as a plain `db.Table` (not a model class), but it has four columns beyond the two foreign keys: `position`, `added_by`, `added_at`. The `position` column enables explicit song ordering. `get_playlist_songs` queries it directly via `playlist_entries.c.position`.

**`friendships` is a directed table used as symmetric.** The table stores `(user_id, friend_id)` pairs. The `User.friends` relationship uses both `primaryjoin` and `secondaryjoin` to traverse it, but the app only queries in one direction — if Alice adds Bob, Bob does not automatically see Alice in his friend list unless the pair is inserted both ways.

**Two bugs worth noting:**
- `get_playlist_songs` (playlist_service.py:66) returns `songs[:-1]`, which silently drops the last song in every playlist. This is almost certainly an off-by-one bug.
- `update_listening_streak` (streak_service.py:73) had the condition `days_since_last == 1 and today.weekday() != 6`. `weekday() == 6` is Sunday, so listening Saturday then Sunday would reset the streak to 1 instead of incrementing it. Fixed by removing the weekday check.

---

## Root Cause Analysis

### Bug 1: Listening streak resets incorrectly when user listens on Sunday

**How I reproduced it**

Read `test_streak_increments_on_sunday` in `tests/test_streaks.py` — it sets `last_listened_at` to a Saturday, then calls `update_listening_streak` with a Sunday timestamp. The test asserts the streak increments from 3 to 4. Running the test before the fix confirmed it failed: the streak reset to 1 instead.

**How I found the root cause**

Opened `services/streak_service.py` and read `update_listening_streak`. The three-branch `if/elif/else` at lines 70–76 is the entire streak logic — there's nowhere else it could be. The `elif` branch is the only path that increments the streak, so I read its condition precisely: `days_since_last == 1 and today.weekday() != 6`. `days_since_last == 1` was satisfied (Saturday → Sunday is one day apart), so the bug had to be in the second half of the `and`. I checked what `weekday()` returns for Sunday in Python's docs: `6`. So `today.weekday() != 6` evaluates to `False` on any Sunday, making the whole condition `False` and falling through to `else`.

**The root cause**

Python's `datetime.weekday()` returns `6` for Sunday. The condition `today.weekday() != 6` was intended to handle some week-boundary case, but its effect is: whenever today is Sunday, the `elif` branch is skipped entirely regardless of how many days have passed. A user who listened on Saturday and then again on Sunday has `days_since_last == 1`, which should increment the streak, but the Sunday guard short-circuits to `else`, resetting the streak to 1. The correct consecutive-day check (`days_since_last == 1`) was already present and correct; the `weekday` guard was the entire problem.

**Fix and side-effect check**

Removed `and today.weekday() != 6` from the `elif` condition, leaving it as `elif days_since_last == 1:`. This makes Sunday behave identically to every other day of the week — consecutive listen increments, gap resets. Ran all five streak tests afterward; all passed, including the same-day no-change test and the skip-a-day reset test, confirming no regressions.

### Bug 2: "Friends Listening Now" feed shows yesterday's activity

**How I reproduced it**

The `/feed/<user_id>/listening-now` endpoint is meant to show only friends actively listening right now. Observed that it surfaced listens from many hours earlier — friends who hadn't touched the app since yesterday still appeared in the feed. Any `ListeningEvent` created within the last full day qualified, which is far too wide a window for a "listening now" view.

**How I found the root cause**

Traced the endpoint: `routes/feed.py:listening_now()` → `feed_service.get_friends_listening_now()`. Inside that function, line 32 computes the filter boundary: `cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD`, and the query keeps events where `listened_at >= cutoff`. So the only thing controlling "how recent is recent" is `RECENT_THRESHOLD`. I looked at its definition at the top of the module (line 13) and found `RECENT_THRESHOLD = timedelta(hours=24)` — that was the specific cause, not the query logic itself.

**The root cause**

`RECENT_THRESHOLD` was set to `timedelta(hours=24)`. The query is correct — it returns every friend whose most recent listen falls after `now - RECENT_THRESHOLD`. But with a 24-hour window, "recent" means "any time in the past day," so a friend who listened once yesterday shows as currently listening. The constant encoded the wrong definition of "now."

**Fix and side-effect check**

Changed `RECENT_THRESHOLD` to `timedelta(minutes=30)`, so the feed only includes friends who listened in the last half hour — a reasonable "currently listening" window. This constant is only referenced by `get_friends_listening_now`; `get_activity_feed` deliberately does not use it (it returns the latest N events regardless of recency), so nothing else changes. Ran the full test suite: the streak and search tests still pass. The two failing tests (`test_playlists.py`) are a pre-existing, unrelated `songs[:-1]` bug in `get_playlist_songs`, not caused by this change.

### Bug 3: Duplicate search results

**How I tried to reproduce it**

Issue 3 is described as: a song with multiple tags appears more than once in search results. The setup that should trigger it is a song with 2+ tags — `Crown Heights Anthem` in the seed data has 3 tags (`rap`, `hip-hop`, `boom bap`). I started the server and ran `curl "http://localhost:5000/songs/search?q=Crown%20Heights%20Anthem"`. Expected `count: 3`; got `count: 1`. I also ran the search test suite: `test_search_no_duplicates_multi_tag_song` **passed**. So with the current code the bug does not manifest.

**How I found the root cause**

Read `services/search_service.py`. The query is `db.session.query(Song).outerjoin(song_tags, ...).filter(...).all()`. The `outerjoin` against `song_tags` is exactly the structure that would fan a 3-tag song out into 3 rows — so the duplication is genuinely present at the SQL level. To prove where it does and doesn't surface, I ran the same underlying join three ways against a fresh in-memory DB seeded with one 3-tag song:
- `db.session.query(Song)...all()` (the current legacy-`Query` form) → **1** row
- selecting raw join columns → **3** rows
- `select(Song)...scalars().all()` (SQLAlchemy 2.0 form) → **3** rows

That was the confident moment: the join really does produce 3 rows, but the legacy `Query.all()` collapses them. The duplication is real; the current API form hides it.

**The root cause**

The `outerjoin` on the `song_tags` association table multiplies each song row by its tag count. The query has no `DISTINCT` and no `.unique()` to collapse that fan-out. It does not currently produce duplicates only because the code uses SQLAlchemy's **legacy `Query` API**, whose `.all()` automatically deduplicates entities by primary key via the identity map. The correctness of the result is therefore an accident of which API is used, not of the query itself. Any rewrite to the modern `select(...).scalars().all()` form — which does *not* auto-dedupe unless you call `.unique()` — would immediately expose the duplicates. So this is a **latent bug**: correct output today, but resting on implicit behavior rather than an explicit dedup.

**Fix and side-effect check**

Added `.distinct()` to the query (`db.session.query(Song).outerjoin(...).filter(...).distinct().all()`) so the dedup is explicit rather than relying on the legacy API's identity-map behavior. This guarantees one row per song regardless of tag count and survives a future migration to the 2.0 `select().scalars().all()` form (which would otherwise need `.unique()`). Re-ran all five search tests after the change: all pass, including the multi-tag, single-tag, and no-tag no-duplicate cases. No other query relies on this function, so nothing downstream changes.

### Bug 4: No notification sent when a friend rates your shared song

**How I reproduced it**

Two notification-triggering interactions exist: adding someone's song to a playlist, and rating someone's song. Adding a song to a playlist correctly produced a notification for the sharer. Rating the same song produced nothing — the sharer's inbox (`GET /users/<id>/notifications`) stayed empty after another user hit `POST /songs/<id>/rate`, even though the rating itself was saved successfully (the endpoint returned `201` with the Rating body).

**How I found the root cause**

Both interactions route through `services/notification_service.py`, so I compared the two functions side by side. `add_to_playlist` (lines 35–70) ends with an `if song.shared_by != added_by_user_id:` guard that calls `create_notification(...)`. `rate_song` (lines 73–110) has all the same ingredients — it loads the `song` and the `rater`, so it knows both `song.shared_by` and the rater's username — but after `db.session.commit()` it goes straight to `return rating`. There is no `create_notification` call anywhere in `rate_song`. That asymmetry between the two functions was the confirmation: the rating path simply never notifies.

**The root cause**

`rate_song` persists the `Rating` (insert or score update) and returns, but it omits the notification step entirely. Unlike `add_to_playlist`, it never calls `create_notification`, so the song's original sharer is never told their song was rated. The feature was half-implemented: the rating is stored, but the "notify the sharer" side effect that the playlist path has was never added to the rating path.

**Fix and side-effect check**

After the commit in `rate_song`, added the same guard the playlist path uses — `if song.shared_by != user_id:` — and a `create_notification` call with type `"song_rated"` and a body naming the rater, song, and score (e.g. `"alice rated your song 'Bohemian Rhapsody' 4/5."`). The self-rating guard prevents a user from notifying themselves. Per the requirement, this fires on every rate, including score updates, not just the first rating. Ran the full suite afterward: all streak and search tests pass; the only failures are the pre-existing, unrelated `songs[:-1]` playlist bug (fixed separately below), which this change does not touch.

### Bug 5: Playlist song list silently drops the last song

**How I reproduced it**

`GET /playlists/<id>/songs` (and the underlying `get_playlist_songs`) should return every song in the playlist. The two playlist tests exercised this directly: `test_playlist_returns_all_songs` seeds a 5-song playlist and asserts `len(songs) == 5`; `test_playlist_returns_songs_in_order` asserts the titles are `["Track 1" … "Track 5"]`. Both failed — the function returned 4 songs (`["Track 1" … "Track 4"]`), dropping `Track 5`. Every non-empty playlist was one song short.

**How I found the root cause**

Opened `services/playlist_service.py` and read `get_playlist_songs`. The query itself is correct — it joins `Song` to `playlist_entries`, filters by `playlist_id`, and orders ascending by `position`, so `songs` holds the full ordered list. The defect is on the return line (line 66): `return [song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice was the smoking gun — it excludes the final element of the list before serializing.

**The root cause**

The return statement slices the query results with `songs[:-1]`, which means "all songs except the last one." `[:-1]` in Python drops the final list element. So a correctly-ordered, complete query result had its last (highest-`position`) song thrown away at the very end. The query fetched all N songs; the comprehension serialized only the first N−1. An empty playlist happened to still work (`[][:-1]` is `[]`), which is why only the non-empty cases failed.

**Fix and side-effect check**

Changed `songs[:-1]` to `songs` so the comprehension iterates the complete result: `return [song.to_dict() for song in songs]`. The ordering and filtering were already correct and were left untouched. Ran the full test suite afterward — all 13 tests pass, including both previously-failing playlist tests and the empty-playlist case, confirming the fix restores the dropped song without breaking ordering or the empty-list path.

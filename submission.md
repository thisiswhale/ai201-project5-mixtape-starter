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

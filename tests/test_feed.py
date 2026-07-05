"""
tests/test_feed.py — Mixtape

Tests for the "Friends Listening Now" feed recency window.

Regression coverage for Bug 2: RECENT_THRESHOLD was 24 hours, so the
"listening now" feed surfaced friends who had listened any time in the
past day instead of only those active right now.
"""

from datetime import datetime, timedelta, timezone

import pytest
from app import create_app, db
from models import User, Song, ListeningEvent
from services.feed_service import get_friends_listening_now, RECENT_THRESHOLD


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed(app):
    """A user with one friend who has shared a song."""
    with app.app_context():
        user = User(username="me", email="me@example.com")
        friend = User(username="friend", email="friend@example.com")
        db.session.add_all([user, friend])
        db.session.flush()

        user.friends.append(friend)
        song = Song(title="Now Playing", artist="Someone", shared_by=friend.id)
        db.session.add(song)
        db.session.commit()
        yield {"user": user, "friend": friend, "song": song}


def _listen(friend_id, song_id, minutes_ago):
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db.session.add(
        ListeningEvent(user_id=friend_id, song_id=song_id, listened_at=when)
    )
    db.session.commit()


def test_threshold_is_thirty_minutes(app):
    """The recency window should be 30 minutes, not 24 hours."""
    assert RECENT_THRESHOLD == timedelta(minutes=30)


def test_recent_listen_appears(app, seed):
    """A friend who listened 10 minutes ago is 'listening now'."""
    with app.app_context():
        _listen(seed["friend"].id, seed["song"].id, minutes_ago=10)
        feed = get_friends_listening_now(seed["user"].id)
        assert len(feed) == 1


def test_stale_listen_excluded(app, seed):
    """
    A friend who listened 90 minutes ago is NOT 'listening now'. With the old
    24-hour threshold this event would wrongly appear; the 30-minute window
    excludes it.
    """
    with app.app_context():
        _listen(seed["friend"].id, seed["song"].id, minutes_ago=90)
        feed = get_friends_listening_now(seed["user"].id)
        assert feed == []

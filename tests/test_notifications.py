"""
tests/test_notifications.py — Mixtape

Tests for notification side effects of rating a song.

Regression coverage for Bug 4: rate_song persisted the Rating but never
notified the song's original sharer, so sharers were told about playlist
adds but silently missed ratings.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed(app):
    """A sharer who owns a song, and a separate rater."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Bohemian Rhapsody", artist="Queen", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_notifies_sharer(app, seed):
    """
    Rating someone else's song should create a 'song_rated' notification
    for the original sharer. This is the exact behavior Bug 4 was missing.
    """
    with app.app_context():
        rate_song(seed["rater"].id, seed["song"].id, 4)

        notifs = get_notifications(seed["sharer"].id)
        assert len(notifs) == 1
        assert notifs[0]["type"] == "song_rated"


def test_rating_own_song_does_not_notify(app, seed):
    """A user rating their own shared song should not notify themselves."""
    with app.app_context():
        rate_song(seed["sharer"].id, seed["song"].id, 5)

        notifs = get_notifications(seed["sharer"].id)
        assert notifs == []


def test_updating_rating_notifies_every_time(app, seed):
    """
    Notifications fire on every rate, including score updates — not only the
    first rating. Two rate calls should produce two notifications.
    """
    with app.app_context():
        rate_song(seed["rater"].id, seed["song"].id, 3)
        rate_song(seed["rater"].id, seed["song"].id, 5)

        notifs = get_notifications(seed["sharer"].id)
        assert len(notifs) == 2

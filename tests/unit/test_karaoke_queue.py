"""Unit tests for Karaoke queue operations."""

import pytest


class TestEnqueue:
    """Tests for the enqueue method."""

    def test_enqueue_adds_song_to_queue(self, mock_karaoke):
        """Test that enqueue adds a song to the queue."""
        result = mock_karaoke.queue_manager.enqueue("/songs/test---dQw4w9WgXcQ.mp4", "TestUser")

        assert len(mock_karaoke.queue_manager.queue) == 1
        assert mock_karaoke.queue_manager.queue[0]["file"] == "/songs/test---dQw4w9WgXcQ.mp4"
        assert mock_karaoke.queue_manager.queue[0]["user"] == "TestUser"
        assert mock_karaoke.queue_manager.queue[0]["title"] == "test"
        assert mock_karaoke.queue_manager.queue[0]["semitones"] == 0
        assert result[0] is True

    def test_enqueue_with_semitones(self, mock_karaoke):
        """Test that enqueue respects semitones parameter."""
        mock_karaoke.queue_manager.enqueue("/songs/test---abc123.mp4", "TestUser", semitones=3)

        assert mock_karaoke.queue_manager.queue[0]["semitones"] == 3

    def test_enqueue_duplicate_song_rejected(self, mock_karaoke):
        """Test that the same song cannot be added twice."""
        mock_karaoke.queue_manager.enqueue("/songs/test---abc123.mp4", "User1")
        result = mock_karaoke.queue_manager.enqueue("/songs/test---abc123.mp4", "User2")

        assert isinstance(result, list)
        assert result[0] is False
        assert isinstance(result[1], str)
        assert len(mock_karaoke.queue_manager.queue) == 1

    def test_enqueue_add_to_front(self, mock_karaoke):
        """Test that add_to_front puts song at position 0."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")
        mock_karaoke.queue_manager.enqueue("/songs/song2---def.mp4", "User2")
        mock_karaoke.queue_manager.enqueue("/songs/song3---ghi.mp4", "User3", add_to_front=True)

        assert mock_karaoke.queue_manager.queue[0]["file"] == "/songs/song3---ghi.mp4"
        assert len(mock_karaoke.queue_manager.queue) == 3

    def test_enqueue_user_limit_enforced(self, mock_karaoke):
        """Test that user song limit is enforced."""
        mock_karaoke.preferences.set("limit_user_songs_by", 2)

        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "LimitedUser")
        mock_karaoke.queue_manager.enqueue("/songs/song2---def.mp4", "LimitedUser")
        result = mock_karaoke.queue_manager.enqueue("/songs/song3---ghi.mp4", "LimitedUser")

        assert result[0] is False
        assert len(mock_karaoke.queue_manager.queue) == 2

    def test_enqueue_user_limit_not_applied_to_pikaraoke(self, mock_karaoke):
        """Test that Pikaraoke user bypasses song limit."""
        mock_karaoke.preferences.set("limit_user_songs_by", 1)

        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "Pikaraoke")
        result = mock_karaoke.queue_manager.enqueue("/songs/song2---def.mp4", "Pikaraoke")

        assert result[0] is True
        assert len(mock_karaoke.queue_manager.queue) == 2

    def test_enqueue_user_limit_not_applied_to_randomizer(self, mock_karaoke):
        """Test that Randomizer user bypasses song limit."""
        mock_karaoke.preferences.set("limit_user_songs_by", 1)

        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "Randomizer")
        result = mock_karaoke.queue_manager.enqueue("/songs/song2---def.mp4", "Randomizer")

        assert result[0] is True
        assert len(mock_karaoke.queue_manager.queue) == 2


class TestQueueEdit:
    """Tests for the queue_edit method."""

    def test_queue_edit_move_up(self, mock_karaoke):
        """Test moving a song up in the queue."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")
        mock_karaoke.queue_manager.enqueue("/songs/song2---def.mp4", "User2")
        mock_karaoke.queue_manager.enqueue("/songs/song3---ghi.mp4", "User3")

        result = mock_karaoke.queue_manager.queue_edit("/songs/song3---ghi.mp4", "up")

        assert result is True
        assert mock_karaoke.queue_manager.queue[1]["file"] == "/songs/song3---ghi.mp4"

    def test_queue_edit_move_down(self, mock_karaoke):
        """Test moving a song down in the queue."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")
        mock_karaoke.queue_manager.enqueue("/songs/song2---def.mp4", "User2")
        mock_karaoke.queue_manager.enqueue("/songs/song3---ghi.mp4", "User3")

        result = mock_karaoke.queue_manager.queue_edit("/songs/song1---abc.mp4", "down")

        assert result is True
        assert mock_karaoke.queue_manager.queue[1]["file"] == "/songs/song1---abc.mp4"

    def test_queue_edit_delete(self, mock_karaoke):
        """Test deleting a song from the queue."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")
        mock_karaoke.queue_manager.enqueue("/songs/song2---def.mp4", "User2")

        result = mock_karaoke.queue_manager.queue_edit("/songs/song1---abc.mp4", "delete")

        assert result is True
        assert len(mock_karaoke.queue_manager.queue) == 1
        assert mock_karaoke.queue_manager.queue[0]["file"] == "/songs/song2---def.mp4"

    def test_queue_edit_move_up_first_song_fails(self, mock_karaoke):
        """Test that moving the first song up fails gracefully."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")
        mock_karaoke.queue_manager.enqueue("/songs/song2---def.mp4", "User2")

        result = mock_karaoke.queue_manager.queue_edit("/songs/song1---abc.mp4", "up")

        assert result is False
        assert mock_karaoke.queue_manager.queue[0]["file"] == "/songs/song1---abc.mp4"

    def test_queue_edit_move_down_last_song_fails(self, mock_karaoke):
        """Test that moving the last song down fails gracefully."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")
        mock_karaoke.queue_manager.enqueue("/songs/song2---def.mp4", "User2")

        result = mock_karaoke.queue_manager.queue_edit("/songs/song2---def.mp4", "down")

        assert result is False
        assert mock_karaoke.queue_manager.queue[1]["file"] == "/songs/song2---def.mp4"

    def test_queue_edit_nonexistent_song_fails(self, mock_karaoke):
        """Test that editing a non-existent song fails."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")

        result = mock_karaoke.queue_manager.queue_edit("nonexistent.mp4", "delete")

        assert result is False
        assert len(mock_karaoke.queue_manager.queue) == 1

    def test_queue_edit_invalid_action_fails(self, mock_karaoke):
        """Test that an invalid action fails."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")

        result = mock_karaoke.queue_manager.queue_edit("song1---abc.mp4", "invalid")

        assert result is False


class TestQueueAddRandom:
    """Tests for the queue_add_random method."""

    def test_queue_add_random_adds_songs(self, mock_karaoke_with_songs):
        """Test that random songs are added to the queue."""
        result = mock_karaoke_with_songs.queue_manager.queue_add_random(3)

        assert result is True
        assert len(mock_karaoke_with_songs.queue_manager.queue) == 3

    def test_queue_add_random_no_duplicates(self, mock_karaoke_with_songs):
        """Test that random doesn't add songs already in queue."""
        mock_karaoke_with_songs.queue_manager.enqueue(
            "/songs/Artist - Song One---abc123.mp4", "User1"
        )

        mock_karaoke_with_songs.queue_manager.queue_add_random(4)

        files = [item["file"] for item in mock_karaoke_with_songs.queue_manager.queue]
        assert len(files) == len(set(files))  # No duplicates

    def test_queue_add_random_empty_library_fails(self, mock_karaoke):
        """Test that adding random from empty library fails."""
        result = mock_karaoke.queue_manager.queue_add_random(3)

        assert result is False
        assert len(mock_karaoke.queue_manager.queue) == 0

    def test_queue_add_random_partial_when_not_enough_songs(self, mock_karaoke_with_songs):
        """Test that it adds what it can when requesting more than available."""
        result = mock_karaoke_with_songs.queue_manager.queue_add_random(10)

        assert result is False  # Returns False when ran out
        assert len(mock_karaoke_with_songs.queue_manager.queue) == 5  # Added all available


class TestQueueClear:
    """Tests for the queue_clear method."""

    def test_queue_clear_empties_queue(self, mock_karaoke):
        """Test that queue_clear removes all songs."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")
        mock_karaoke.queue_manager.enqueue("/songs/song2---def.mp4", "User2")

        mock_karaoke.queue_manager.queue_clear()

        assert len(mock_karaoke.queue_manager.queue) == 0


class TestIsSongInQueue:
    """Tests for the is_song_in_queue method."""

    def test_is_song_in_queue_true(self, mock_karaoke):
        """Test detection of song in queue."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")

        assert mock_karaoke.queue_manager.is_song_in_queue("/songs/song1---abc.mp4") is True

    def test_is_song_in_queue_false(self, mock_karaoke):
        """Test detection of song not in queue."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")

        assert mock_karaoke.queue_manager.is_song_in_queue("/songs/other---xyz.mp4") is False

    def test_is_song_in_queue_empty(self, mock_karaoke):
        """Test with empty queue."""
        assert mock_karaoke.queue_manager.is_song_in_queue("/songs/song1---abc.mp4") is False


class TestIsUserLimited:
    """Tests for the is_user_limited method."""

    def test_is_user_limited_disabled(self, mock_karaoke):
        """Test that limit of 0 means no limit."""
        mock_karaoke.preferences.set("limit_user_songs_by", 0)
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")
        mock_karaoke.queue_manager.enqueue("/songs/song2---def.mp4", "User1")

        assert mock_karaoke.queue_manager.is_user_limited("User1") is False

    def test_is_user_limited_under_limit(self, mock_karaoke):
        """Test user under the limit."""
        mock_karaoke.preferences.set("limit_user_songs_by", 3)
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")

        assert mock_karaoke.queue_manager.is_user_limited("User1") is False

    def test_is_user_limited_at_limit(self, mock_karaoke):
        """Test user at the limit."""
        mock_karaoke.preferences.set("limit_user_songs_by", 2)
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")
        mock_karaoke.queue_manager.enqueue("/songs/song2---def.mp4", "User1")

        assert mock_karaoke.queue_manager.is_user_limited("User1") is True

    def test_is_user_limited_counts_now_playing(self, mock_karaoke):
        """Test that currently playing song counts toward limit."""
        mock_karaoke.preferences.set("limit_user_songs_by", 2)
        mock_karaoke.playback_controller.now_playing_user = "User1"
        mock_karaoke.queue_manager.enqueue("/songs/song1---abc.mp4", "User1")

        assert mock_karaoke.queue_manager.is_user_limited("User1") is True


class TestFairQueuePosition:
    """Tests for round-robin fair queue insertion."""

    def test_fair_queue_empty_queue(self, mock_karaoke):
        """Test insertion into empty queue goes to position 0."""
        pos = mock_karaoke.queue_manager._calculate_fair_queue_position("UserA")
        assert pos == 0

    def test_fair_queue_first_song_each_user(self, mock_karaoke):
        """Test that first songs from different users append in order."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---aaa.mp4", "UserA")
        mock_karaoke.queue_manager.enqueue("/songs/song2---bbb.mp4", "UserB")
        mock_karaoke.queue_manager.enqueue("/songs/song3---ccc.mp4", "UserC")

        users = [item["user"] for item in mock_karaoke.queue_manager.queue]
        assert users == ["UserA", "UserB", "UserC"]

    def test_fair_queue_second_song_goes_after_first_round(self, mock_karaoke):
        """Test that user's second song goes after all first-round songs."""
        mock_karaoke.queue_manager.enqueue("/songs/song1---aaa.mp4", "UserA")
        mock_karaoke.queue_manager.enqueue("/songs/song2---bbb.mp4", "UserB")
        mock_karaoke.queue_manager.enqueue("/songs/song3---ccc.mp4", "UserA")  # UserA's second

        users = [item["user"] for item in mock_karaoke.queue_manager.queue]
        assert users == ["UserA", "UserB", "UserA"]

    def test_fair_queue_interleaves_users(self, mock_karaoke):
        """Test round-robin interleaving: A1, B1, A2, B2 not A1, A2, B1, B2."""
        mock_karaoke.queue_manager.enqueue("/songs/a1---aaa.mp4", "UserA")
        mock_karaoke.queue_manager.enqueue("/songs/a2---bbb.mp4", "UserA")
        mock_karaoke.queue_manager.enqueue("/songs/b1---ccc.mp4", "UserB")
        mock_karaoke.queue_manager.enqueue("/songs/b2---ddd.mp4", "UserB")

        users = [item["user"] for item in mock_karaoke.queue_manager.queue]
        # B's first song should slot in after A's first, before A's second
        assert users == ["UserA", "UserB", "UserA", "UserB"]

    def test_fair_queue_three_users_complex(self, mock_karaoke):
        """Test fair queuing with three users adding multiple songs."""
        # UserA adds 3 songs in a row
        mock_karaoke.queue_manager.enqueue("/songs/a1---a01.mp4", "UserA")
        mock_karaoke.queue_manager.enqueue("/songs/a2---a02.mp4", "UserA")
        mock_karaoke.queue_manager.enqueue("/songs/a3---a03.mp4", "UserA")
        # UserB adds 1 song
        mock_karaoke.queue_manager.enqueue("/songs/b1---b01.mp4", "UserB")
        # UserC adds 1 song
        mock_karaoke.queue_manager.enqueue("/songs/c1---c01.mp4", "UserC")

        users = [item["user"] for item in mock_karaoke.queue_manager.queue]
        # Expected: A1, B1, C1, A2, A3 (B and C slot into round 0)
        assert users == ["UserA", "UserB", "UserC", "UserA", "UserA"]

    def test_fair_queue_late_joiner_gets_fair_position(self, mock_karaoke):
        """Test that a new user joining late gets fair position."""
        # UserA adds 2 songs
        mock_karaoke.queue_manager.enqueue("/songs/a1---a01.mp4", "UserA")
        mock_karaoke.queue_manager.enqueue("/songs/a2---a02.mp4", "UserA")
        # UserB joins and adds their first song
        mock_karaoke.queue_manager.enqueue("/songs/b1---b01.mp4", "UserB")

        users = [item["user"] for item in mock_karaoke.queue_manager.queue]
        # B's first song goes after A's first (round 0), before A's second
        assert users == ["UserA", "UserB", "UserA"]

    def test_fair_queue_preserves_add_to_front(self, mock_karaoke):
        """Test that add_to_front bypasses fair queue logic."""
        mock_karaoke.queue_manager.enqueue("/songs/a1---a01.mp4", "UserA")
        mock_karaoke.queue_manager.enqueue("/songs/b1---b01.mp4", "UserB")
        mock_karaoke.queue_manager.enqueue("/songs/a2---a02.mp4", "UserA", add_to_front=True)

        # add_to_front should still put song at position 0
        assert mock_karaoke.queue_manager.queue[0]["file"] == "/songs/a2---a02.mp4"


class TestReorderWithCurrent:
    """Tests for Karaoke.reorder_with_current — drag-and-drop including the
    currently playing song as virtual index 0."""

    @staticmethod
    def _start_playing(k, file_path: str, user: str = "Singer") -> None:
        pc = k.playback_controller
        pc.now_playing = file_path.split("/")[-1].split("---")[0]
        pc.now_playing_filename = file_path
        pc.now_playing_user = user
        pc.is_playing = True
        pc.is_paused = False

    def test_promote_queue_top_to_now_playing_skips_current(self, mock_karaoke):
        self._start_playing(mock_karaoke, "/songs/current---cur001.mp4", "Alice")
        mock_karaoke.queue_manager.enqueue("/songs/next---nxt001.mp4", "Bob")
        mock_karaoke.queue_manager.enqueue("/songs/later---lat001.mp4", "Cara")

        result = mock_karaoke.reorder_with_current(1, 0)

        assert result is True
        # Current was demoted (skip reset playback state) and the dragged
        # queue[0] was promoted to position 0 of the queue, ready for the
        # run loop's pop_next.
        assert mock_karaoke.playback_controller.is_playing is False
        files = [item["file"] for item in mock_karaoke.queue_manager.queue]
        assert files == [
            "/songs/next---nxt001.mp4",
            "/songs/current---cur001.mp4",
            "/songs/later---lat001.mp4",
        ]

    def test_demote_current_to_queue_position(self, mock_karaoke):
        self._start_playing(mock_karaoke, "/songs/current---cur001.mp4", "Alice")
        mock_karaoke.queue_manager.enqueue("/songs/next---nxt001.mp4", "Bob")
        mock_karaoke.queue_manager.enqueue("/songs/third---thr001.mp4", "Cara")
        mock_karaoke.queue_manager.enqueue("/songs/fourth---fou001.mp4", "Dan")

        # Drag current (virtual 0) to slot 2 — current should sit between
        # next and third in the queue, with next promoted to playback.
        result = mock_karaoke.reorder_with_current(0, 2)

        assert result is True
        assert mock_karaoke.playback_controller.is_playing is False
        files = [item["file"] for item in mock_karaoke.queue_manager.queue]
        assert files == [
            "/songs/next---nxt001.mp4",
            "/songs/current---cur001.mp4",
            "/songs/third---thr001.mp4",
            "/songs/fourth---fou001.mp4",
        ]

    def test_demoted_current_resets_position(self, mock_karaoke):
        """A re-queued current song restarts from the beginning, not from its
        playback position."""
        self._start_playing(mock_karaoke, "/songs/current---cur001.mp4", "Alice")
        mock_karaoke.playback_controller.now_playing_position = 42.5
        mock_karaoke.queue_manager.enqueue("/songs/next---nxt001.mp4", "Bob")

        mock_karaoke.reorder_with_current(0, 1)

        demoted = next(
            item
            for item in mock_karaoke.queue_manager.queue
            if item["file"] == "/songs/current---cur001.mp4"
        )
        assert "resume_position" not in demoted

    def test_in_queue_drag_skips_skip(self, mock_karaoke):
        """When neither old nor new index is 0, only the queue is reordered
        and playback is left alone."""
        self._start_playing(mock_karaoke, "/songs/current---cur001.mp4", "Alice")
        mock_karaoke.queue_manager.enqueue("/songs/next---nxt001.mp4", "Bob")
        mock_karaoke.queue_manager.enqueue("/songs/third---thr001.mp4", "Cara")

        result = mock_karaoke.reorder_with_current(1, 2)

        assert result is True
        assert mock_karaoke.playback_controller.is_playing is True
        assert mock_karaoke.playback_controller.now_playing_filename == (
            "/songs/current---cur001.mp4"
        )
        files = [item["file"] for item in mock_karaoke.queue_manager.queue]
        assert files == ["/songs/third---thr001.mp4", "/songs/next---nxt001.mp4"]

    def test_no_op_when_indices_match(self, mock_karaoke):
        self._start_playing(mock_karaoke, "/songs/current---cur001.mp4", "Alice")
        mock_karaoke.queue_manager.enqueue("/songs/next---nxt001.mp4", "Bob")

        assert mock_karaoke.reorder_with_current(0, 0) is True
        assert mock_karaoke.playback_controller.is_playing is True

    def test_falls_back_to_queue_reorder_when_nothing_playing(self, mock_karaoke):
        mock_karaoke.queue_manager.enqueue("/songs/a---a01.mp4", "UserA")
        mock_karaoke.queue_manager.enqueue("/songs/b---b01.mp4", "UserB")

        # Nothing playing → indices map directly to queue (1 → queue[0]).
        result = mock_karaoke.reorder_with_current(1, 2)

        assert result is True
        files = [item["file"] for item in mock_karaoke.queue_manager.queue]
        assert files == ["/songs/b---b01.mp4", "/songs/a---a01.mp4"]

    def test_returns_false_for_index_zero_when_nothing_playing(self, mock_karaoke):
        mock_karaoke.queue_manager.enqueue("/songs/a---a01.mp4", "UserA")

        assert mock_karaoke.reorder_with_current(0, 1) is False
        assert mock_karaoke.reorder_with_current(1, 0) is False

    def test_returns_false_for_out_of_range(self, mock_karaoke):
        self._start_playing(mock_karaoke, "/songs/current---cur001.mp4", "Alice")
        mock_karaoke.queue_manager.enqueue("/songs/next---nxt001.mp4", "Bob")

        assert mock_karaoke.reorder_with_current(0, 9) is False
        assert mock_karaoke.reorder_with_current(9, 0) is False

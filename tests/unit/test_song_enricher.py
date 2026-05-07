"""Unit tests for the song enricher (iTunes + MusicBrainz pipeline)."""

from unittest.mock import patch

import pytest

from pikaraoke.lib import song_enricher
from pikaraoke.lib.karaoke_database import KaraokeDatabase


@pytest.fixture(autouse=True)
def _no_cover_download():
    # iTunes hits carry an artwork URL; the enricher tries to download
    # the cover via requests. The conftest HTTP block would otherwise
    # raise on every test that doesn't explicitly mock _download_cover.
    with patch.object(song_enricher, "_download_cover", return_value=False):
        yield


@pytest.fixture
def db(tmp_path):
    d = KaraokeDatabase(str(tmp_path / "test.db"))
    yield d
    d.close()


def _insert_song(db, path="/songs/Artist - Song---abc12345678.mp4"):
    db.insert_songs([{"file_path": path, "youtube_id": "abc12345678", "format": "mp4"}])
    return db.get_song_id_by_path(path)


def _seed_language(db, song_id, lang):
    """Plant ``songs.language`` so the language-gated enricher will run iTunes."""
    db.update_track_metadata_with_provenance(song_id, "whisper_probe_raw", {"language": lang})


def _raw_hit(
    artist="A",
    track="B",
    *,
    track_id=12345,
    collection="",
    track_number=1,
    release_date=None,
    artwork="https://fake/100x100bb.jpg",
    genre="Rock",
    country="USA",
    currency="USD",
):
    """Build a raw iTunes-API-shaped hit (the shape ``search_itunes_full`` returns).

    Defaults keep the joined collection+track+artist text under
    ``_LANGDETECT_MIN_CHARS`` (12), so ``_hit_language`` falls back to the
    storefront-country mapping. Tests that exercise the langdetect path
    pass longer artist/track/collection explicitly.
    """
    return {
        "artistName": artist,
        "trackName": track,
        "trackId": track_id,
        "collectionName": collection,
        "trackNumber": track_number,
        "releaseDate": release_date,
        "artworkUrl100": artwork,
        "primaryGenreName": genre,
        "country": country,
        "currency": currency,
    }


def _row_with(**fields):
    """Mimic a sqlite3.Row (supports __getitem__ by column name)."""
    return fields


class TestQueryFromSong:
    def test_prefers_db_artist_and_title(self, tmp_path):
        row = _row_with(artist="Eminem", title="Stan")
        song = tmp_path / "Foo---abc12345678.mp4"
        assert song_enricher._query_from_song(row, str(song)) == "Eminem - Stan"

    def test_falls_back_to_stem_when_db_missing_fields(self, tmp_path):
        # Empty / None artist + title -> filename stem with YT id stripped.
        row = _row_with(artist=None, title="")
        song = tmp_path / "Queen - Bohemian Rhapsody---abc12345678.mp4"
        assert song_enricher._query_from_song(row, str(song)) == "Queen - Bohemian Rhapsody"

    def test_falls_back_to_stem_when_row_is_none(self, tmp_path):
        song = tmp_path / "Artist - Song---dQw4w9WgXcQ.mp4"
        assert song_enricher._query_from_song(None, str(song)) == "Artist - Song"

    def test_handles_bracket_youtube_id(self, tmp_path):
        song = tmp_path / "Artist - Song [dQw4w9WgXcQ].mp4"
        assert song_enricher._query_from_song(None, str(song)) == "Artist - Song"


class TestHitLanguage:
    def test_uses_text_signal_when_long_enough(self):
        # Polish text — langdetect should fire on the joined fields.
        hit = _raw_hit(
            artist="Edyta Górniak",
            track="Kolorowy wiatr",
            collection="Pocahontas Złota Kolekcja",
            country="USA",  # storefront would say en; text wins
        )
        assert song_enricher._hit_language(hit) == "pl"

    def test_falls_back_to_country_when_text_too_short(self):
        hit = _raw_hit(artist="X", track="Y", collection="Z", country="POL")
        # Too short for langdetect (12-char minimum), country says PL.
        assert song_enricher._hit_language(hit) == "pl"

    def test_returns_none_when_neither_signal_fires(self):
        hit = _raw_hit(artist="X", track="Y", collection="Z", country="XYZ")
        assert song_enricher._hit_language(hit) is None


class TestPickHitForLanguage:
    def test_picks_first_matching(self):
        hits = [
            _raw_hit(country="POL", artist="A", track="B", collection="C"),  # pl
            _raw_hit(country="USA", artist="A", track="B", collection="C"),  # en
        ]
        chosen, reason = song_enricher._pick_hit_for_language(hits, "en")
        assert reason == "lang_match"
        assert chosen is hits[1]

    def test_skips_hits_with_unknown_language(self):
        hits = [
            _raw_hit(country="XYZ", artist="A", track="B", collection="C"),  # unknown
            _raw_hit(country="POL", artist="A", track="B", collection="C"),  # pl
        ]
        chosen, reason = song_enricher._pick_hit_for_language(hits, "pl")
        assert chosen is hits[1]

    def test_no_match_returns_none(self):
        hits = [_raw_hit(country="USA", artist="A", track="B", collection="C")]
        chosen, reason = song_enricher._pick_hit_for_language(hits, "pl")
        assert chosen is None
        assert reason == "no_match"


class TestEnrichSong:
    def test_defers_when_no_language_signal_yet(self, db, tmp_path):
        """Before any classifier/whisper has set songs.language, the
        enricher records ``awaiting_language`` and does NOT call iTunes."""
        song_path = str(tmp_path / "Eminem - Stan---abc12345678.mp4")
        sid = _insert_song(db, song_path)

        with patch.object(song_enricher, "search_itunes_full") as mock_search:
            song_enricher.enrich_song(db, sid, song_path)
            mock_search.assert_not_called()

        row = db.get_song_by_id(sid)
        assert row["metadata_status"] == "awaiting_language"
        assert row["enrichment_attempts"] == 1
        assert row["language_at_enrich"] is None
        # No textual fields written.
        assert row["album"] is None
        assert row["genre"] is None

    def test_populates_nullable_fields_from_itunes(self, db, tmp_path):
        song_path = str(tmp_path / "Eminem - Stan---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")

        hit = _raw_hit(
            artist="Eminem",
            track="Stan",
            track_id=99999,
            collection="The Marshall Mathers LP",
            track_number=3,
            release_date="2000-05-23T07:00:00Z",
            genre="Hip-Hop/Rap",
            country="USA",
        )
        with patch.object(song_enricher, "search_itunes_full", return_value=[hit]), patch.object(
            song_enricher, "fetch_musicbrainz_ids", return_value=None
        ), patch.object(song_enricher, "_download_cover", return_value=False):
            song_enricher.enrich_song(db, sid, song_path)

        row = db.get_song_by_id(sid)
        assert row["itunes_id"] == "99999"
        assert row["artist"] == "Eminem"
        assert row["title"] == "Stan"
        assert row["album"] == "The Marshall Mathers LP"
        assert row["track_number"] == 3
        assert row["release_date"] == "2000-05-23T07:00:00Z"
        assert row["genre"] == "Hip-Hop/Rap"
        assert row["metadata_status"] == "enriched"
        assert row["enrichment_attempts"] == 1
        assert row["language_at_enrich"] == "en"

    def test_picks_first_language_matching_hit_from_top5(self, db, tmp_path):
        """Top-1 is wrong-language, top-2 matches — enricher picks top-2."""
        song_path = str(tmp_path / "Edyta - Kolorowy wiatr---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "pl")

        hits = [
            _raw_hit(  # decoy: en hit
                artist="Judy Kuhn",
                track="Colors of the Wind",
                track_id=11111,
                collection="Pocahontas Original Soundtrack",
                country="USA",
                genre="Soundtrack",
            ),
            _raw_hit(  # match: pl hit
                artist="Edyta Górniak",
                track="Kolorowy wiatr",
                track_id=22222,
                collection="Pocahontas: Polska wersja",
                country="POL",
                genre="Soundtrack",
            ),
        ]
        with patch.object(song_enricher, "search_itunes_full", return_value=hits), patch.object(
            song_enricher, "fetch_musicbrainz_ids", return_value=None
        ):
            song_enricher.enrich_song(db, sid, song_path)

        row = db.get_song_by_id(sid)
        assert row["itunes_id"] == "22222"
        assert row["artist"] == "Edyta Górniak"
        assert row["title"] == "Kolorowy wiatr"
        assert row["metadata_status"] == "enriched"
        assert row["language_at_enrich"] == "pl"

    def test_language_mismatch_when_no_top5_hit_matches(self, db, tmp_path):
        """The Pedalini regression: query "Don Pedalini", whisper says pl,
        every iTunes hit is Portuguese. No textual fields get written; the
        row stamps ``language_mismatch`` and snapshots the language so we
        won't retry until language changes again.

        Pre-condition: the row must already have artist/title for
        ``language_mismatch`` to be a meaningful verdict — without that
        there is nothing for the iTunes hits to mismatch against, and the
        enricher down-grades the status to ``not_found`` (covered by
        ``test_language_mismatch_downgraded_to_not_found_when_metadata_empty``).
        """
        song_path = str(tmp_path / "Don Pedalini---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "pl")
        # Pre-seed prior metadata so ``language_mismatch`` is the right
        # verdict — without it the enricher correctly downgrades to
        # ``not_found`` (Bug D fix).
        db.update_track_metadata_with_provenance(
            sid, "scanner", {"artist": "Don Pedalini", "title": "Some Song"}
        )

        # All 5 hits are Brazilian/Portuguese hymns.
        hits = [
            _raw_hit(
                artist=f"Padrinho Fábio Pedalino {i}",
                track="A Arte e o Dom do Amor",
                track_id=10000 + i,
                collection="IV Livro - Hinário da Humanidade",
                country="BRA",
                genre="Worldwide",
            )
            for i in range(5)
        ]
        with patch.object(song_enricher, "search_itunes_full", return_value=hits):
            song_enricher.enrich_song(db, sid, song_path)

        row = db.get_song_by_id(sid)
        assert row["metadata_status"] == "language_mismatch"
        assert row["language_at_enrich"] == "pl"
        # Critically: no textual iTunes fields leaked through; the
        # scanner-seeded artist/title are preserved verbatim.
        assert row["artist"] == "Don Pedalini"
        assert row["title"] == "Some Song"
        assert row["album"] is None
        assert row["genre"] is None
        assert row["itunes_id"] is None  # match was wrong wholesale

    def test_language_mismatch_downgraded_to_not_found_when_metadata_empty(self, db, tmp_path):
        """Bug D: a row with no prior artist/title can't logically be
        ``language_mismatch`` — there's nothing to mismatch against.
        Stamp ``not_found`` instead so the status reflects reality and
        the row stays a candidate for re-validation. The filename here
        ('Don Pedalini' alone, no separator) deliberately can't be
        parsed by the Bug F filename re-validation either.
        """
        song_path = str(tmp_path / "Don Pedalini---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "pl")
        # No artist/title pre-seeded — this is the metadata-empty case.

        hits = [
            _raw_hit(
                artist=f"Padrinho Fábio Pedalino {i}",
                track="A Arte e o Dom do Amor",
                track_id=10000 + i,
                collection="IV Livro - Hinário da Humanidade",
                country="BRA",
                genre="Worldwide",
            )
            for i in range(5)
        ]
        with patch.object(song_enricher, "search_itunes_full", return_value=hits):
            song_enricher.enrich_song(db, sid, song_path)

        row = db.get_song_by_id(sid)
        assert row["metadata_status"] == "not_found"
        assert row["language_at_enrich"] == "pl"
        assert row["artist"] is None
        assert row["title"] is None

    def test_idempotent_when_language_unchanged(self, db, tmp_path):
        """Second run with the same songs.language as the snapshot is a no-op
        — iTunes is not called at all."""
        song_path = str(tmp_path / "Eminem - Stan---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")

        hit = _raw_hit(artist="Eminem", track="Stan", country="USA")
        with patch.object(song_enricher, "search_itunes_full", return_value=[hit]), patch.object(
            song_enricher, "fetch_musicbrainz_ids", return_value=None
        ):
            song_enricher.enrich_song(db, sid, song_path)

        row_first = db.get_song_by_id(sid)
        assert row_first["metadata_status"] == "enriched"
        assert row_first["language_at_enrich"] == "en"

        # Second call: songs.language unchanged, status='enriched'. iTunes
        # MUST NOT be hit again.
        with patch.object(song_enricher, "search_itunes_full") as mock_search:
            song_enricher.enrich_song(db, sid, song_path)
            mock_search.assert_not_called()

        # Attempts counter doesn't increment because we early-returned
        # before stamping (the only way to express "no work done").
        row_second = db.get_song_by_id(sid)
        assert row_second["enrichment_attempts"] == row_first["enrichment_attempts"]

    def test_re_enriches_when_language_changes(self, db, tmp_path):
        """First pass enriches against en; whisper later says pl and triggers
        a re-run; second pass picks the pl hit instead."""
        song_path = str(tmp_path / "Foo - Bar---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")

        # Both hits stay short so language is decided by storefront country
        # (the most predictable signal in unit tests; langdetect on 2-3 word
        # phrases is famously noisy and would make the assertions flaky).
        en_hit = _raw_hit(artist="EN", track="X", track_id=111, country="USA")
        pl_hit = _raw_hit(artist="PL", track="Y", track_id=222, country="POL")
        with patch.object(
            song_enricher, "search_itunes_full", return_value=[en_hit, pl_hit]
        ), patch.object(song_enricher, "fetch_musicbrainz_ids", return_value=None):
            song_enricher.enrich_song(db, sid, song_path)

        row = db.get_song_by_id(sid)
        assert row["itunes_id"] == "111"
        assert row["language_at_enrich"] == "en"

        # Whisper raw probe writes pl — provenance ladder lets it overwrite.
        db.update_track_metadata_with_provenance(sid, "whisper_probe_raw", {"language": "pl"})
        with patch.object(
            song_enricher, "search_itunes_full", return_value=[en_hit, pl_hit]
        ), patch.object(song_enricher, "fetch_musicbrainz_ids", return_value=None):
            song_enricher.enrich_song(db, sid, song_path)

        row = db.get_song_by_id(sid)
        assert row["itunes_id"] == "222"
        assert row["artist"] == "PL"
        assert row["title"] == "Y"
        assert row["language_at_enrich"] == "pl"

    def test_manual_edits_beat_itunes(self, db, tmp_path):
        song_path = str(tmp_path / "Foo---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")
        # Pre-existing manual artist/title/album — provenance "manual" is
        # the top of the ladder so iTunes must not clobber.
        db.update_track_metadata_with_provenance(
            sid,
            "manual",
            {"artist": "Manual", "title": "Preset", "album": "Pre-album"},
        )

        hit = _raw_hit(
            artist="iTunes Artist",
            track="iTunes Track",
            track_id=12345,
            collection="iTunes Album",
            track_number=7,
            country="USA",
        )
        with patch.object(song_enricher, "search_itunes_full", return_value=[hit]), patch.object(
            song_enricher, "fetch_musicbrainz_ids", return_value=None
        ):
            song_enricher.enrich_song(db, sid, song_path)

        row = db.get_song_by_id(sid)
        # Manual values preserved.
        assert row["artist"] == "Manual"
        assert row["title"] == "Preset"
        assert row["album"] == "Pre-album"
        # Unclaimed fields filled.
        assert row["itunes_id"] == "12345"
        assert row["track_number"] == 7

    def test_itunes_overwrites_youtube_sourced_fields(self, db, tmp_path):
        """iTunes (conf 2) beats YouTube info.json (conf 1) for identity fields."""
        song_path = str(tmp_path / "Foo---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")
        db.update_track_metadata_with_provenance(
            sid, "youtube", {"artist": "YouTube Artist", "title": "YouTube Track"}
        )

        hit = _raw_hit(
            artist="iTunes Artist",
            track="iTunes Track",
            track_id=12345,
            country="USA",
        )
        with patch.object(song_enricher, "search_itunes_full", return_value=[hit]), patch.object(
            song_enricher, "fetch_musicbrainz_ids", return_value=None
        ):
            song_enricher.enrich_song(db, sid, song_path)

        row = db.get_song_by_id(sid)
        assert row["artist"] == "iTunes Artist"
        assert row["title"] == "iTunes Track"
        sources = db.get_metadata_sources(sid)
        assert sources["artist"] == "itunes"
        assert sources["title"] == "itunes"

    def test_writes_musicbrainz_ids_when_available(self, db, tmp_path):
        song_path = str(tmp_path / "Foo---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")
        hit = _raw_hit(artist="A", track="T", track_id=1, country="USA")
        with patch.object(song_enricher, "search_itunes_full", return_value=[hit]), patch.object(
            song_enricher,
            "fetch_musicbrainz_ids",
            return_value={"musicbrainz_recording_id": "mbid-uuid", "isrc": "USRC17600001"},
        ):
            song_enricher.enrich_song(db, sid, song_path)

        row = db.get_song_by_id(sid)
        assert row["musicbrainz_recording_id"] == "mbid-uuid"
        assert row["isrc"] == "USRC17600001"

    def test_records_not_found_when_itunes_miss(self, db, tmp_path):
        song_path = str(tmp_path / "Foo---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")
        with patch.object(song_enricher, "search_itunes_full", return_value=[]):
            song_enricher.enrich_song(db, sid, song_path)
        row = db.get_song_by_id(sid)
        assert row["metadata_status"] == "not_found"
        assert row["enrichment_attempts"] == 1
        assert row["language_at_enrich"] == "en"

    def test_increments_attempts_on_repeated_runs_for_unresolved_language(self, db, tmp_path):
        """Without a language signal, every dispatch stamps another attempt
        (idempotency only kicks in once we've reached ``enriched``)."""
        song_path = str(tmp_path / "Foo---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        with patch.object(song_enricher, "search_itunes_full") as mock_search:
            song_enricher.enrich_song(db, sid, song_path)
            song_enricher.enrich_song(db, sid, song_path)
            song_enricher.enrich_song(db, sid, song_path)
            mock_search.assert_not_called()
        row = db.get_song_by_id(sid)
        assert row["enrichment_attempts"] == 3
        assert row["metadata_status"] == "awaiting_language"

    def test_downloads_cover_and_registers_artifact(self, db, tmp_path):
        song_path = str(tmp_path / "Foo---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")
        hit = _raw_hit(
            artist="A",
            track="T",
            track_id=1,
            artwork="https://fake/100x100bb.jpg",
            country="USA",
        )
        expected_cover = str(tmp_path / "Foo---abc12345678.cover.jpg")

        def fake_download(url, dest):
            assert dest == expected_cover
            with open(dest, "wb") as f:
                f.write(b"image-bytes")
            return True

        with patch.object(song_enricher, "search_itunes_full", return_value=[hit]), patch.object(
            song_enricher, "fetch_musicbrainz_ids", return_value=None
        ), patch.object(song_enricher, "_download_cover", side_effect=fake_download):
            song_enricher.enrich_song(db, sid, song_path)

        arts = {(a["role"], a["path"]) for a in db.get_artifacts(sid)}
        assert ("cover_art", expected_cover) in arts

    def test_skips_cover_download_when_file_exists(self, db, tmp_path):
        song_path = str(tmp_path / "Foo---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")
        existing_cover = tmp_path / "Foo---abc12345678.cover.jpg"
        existing_cover.write_bytes(b"already-there")

        hit = _raw_hit(
            artist="A",
            track="T",
            track_id=1,
            artwork="https://fake/100x100bb.jpg",
            country="USA",
        )
        with patch.object(song_enricher, "search_itunes_full", return_value=[hit]), patch.object(
            song_enricher, "fetch_musicbrainz_ids", return_value=None
        ), patch.object(song_enricher, "_download_cover") as mock_dl:
            song_enricher.enrich_song(db, sid, song_path)
            mock_dl.assert_not_called()

        # Existing file preserved.
        assert existing_cover.read_bytes() == b"already-there"

    def test_itunes_variant_does_not_override_title(self, db, tmp_path):
        """When iTunes' chosen hit is an Instrumental/Karaoke cut, its
        canonical track name must not clobber the existing title — otherwise
        LRCLib queries get poisoned with a suffix that doesn't exist in its
        index. Per Bug C: the artist is variant-invariant for same-artist
        variants (live/remaster/karaoke), so iTunes' artist IS allowed to
        flow through and overwrite the lower-confidence YouTube seed. Other
        iTunes fields (album, itunes_id, etc.) still flow through too.
        """
        song_path = str(tmp_path / "Queen - Antyczny Napaleniec---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "pl")
        db.update_track_metadata_with_provenance(
            sid,
            "youtube",
            {"artist": "Queen", "title": "Antyczny Napaleniec"},
        )
        # Polish text in the album name so signal_itunes_text reads "pl"
        # (matches the seeded language). The variant marker "(live)" in the
        # track triggers _itunes_adds_variant; the test verifies that guard
        # drops only the canonical title (Bug C: artist still flows through
        # because iTunes' artist token "Queen" overlaps with the query's
        # token, so the cover-detection guard does NOT fire).
        hit = _raw_hit(
            artist="Queen",
            track="Antyczny Napaleniec (live)",
            track_id=55555,
            collection="Płyta nazwa",
            track_number=4,
            release_date="2020-01-01T00:00:00Z",
            genre="Rap",
            country="POL",
        )
        with patch.object(song_enricher, "search_itunes_full", return_value=[hit]), patch.object(
            song_enricher, "fetch_musicbrainz_ids", return_value=None
        ):
            song_enricher.enrich_song(db, sid, song_path)

        row = db.get_song_by_id(sid)
        # Title stays at whatever was there (YouTube-seeded) — variant guard
        # blocks the (live) suffix from poisoning LRCLib lookups.
        assert row["title"] == "Antyczny Napaleniec"
        # Artist still flows through: same artist on both sides, iTunes
        # outranks the youtube seed in the confidence ladder.
        assert row["artist"] == "Queen"
        # Other iTunes-only fields still land.
        assert row["itunes_id"] == "55555"
        assert row["album"] == "Płyta nazwa"
        assert row["track_number"] == 4
        assert row["genre"] == "Rap"

    def test_itunes_variant_cover_drops_artist_too(self, db, tmp_path):
        """The Bug 97 case: iTunes' only language-matching hit is a
        "(Punk Version)" cover by a different artist (token-disjoint from
        the query). Both title AND artist must be dropped — keeping the
        cover artist would poison the row. Other iTunes-only fields
        (album, itunes_id, genre) still flow through; they describe the
        wrong recording but at least the identity fields stay clean.
        """
        song_path = str(tmp_path / "Gejtos - Antyczny Napaleniec---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "pl")
        db.update_track_metadata_with_provenance(
            sid,
            "youtube",
            {"artist": "Gejtos", "title": "Antyczny Napaleniec"},
        )
        # iTunes' canonical hit is a Punk Version by a different artist.
        # The "(Punk Version)" suffix is now in _VARIANT_RE via the
        # ``\w+\s+version`` alternation, so _itunes_adds_variant fires;
        # the cover-detection guard then notices "Gejtos" and "Punko polo"
        # share no tokens and drops the artist alongside the title.
        hit = _raw_hit(
            artist="Punko polo",
            track="Antyczny Napaleniec (Punk Version)",
            track_id=99999,
            collection="Punk Tribute",
            country="POL",
        )
        with patch.object(song_enricher, "search_itunes_full", return_value=[hit]), patch.object(
            song_enricher, "fetch_musicbrainz_ids", return_value=None
        ):
            song_enricher.enrich_song(db, sid, song_path)

        row = db.get_song_by_id(sid)
        # Title and artist preserved from YouTube; iTunes cover never wrote.
        assert row["title"] == "Antyczny Napaleniec"
        assert row["artist"] == "Gejtos"

    def test_itunes_variant_matching_query_still_applies(self, db, tmp_path):
        """When the query itself carries the variant suffix (user really
        wants the karaoke cut), iTunes' matching suffix is legitimate and
        must still flow through."""
        song_path = str(tmp_path / "Artist - Song (Instrumental)---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")
        db.update_track_metadata_with_provenance(
            sid, "youtube", {"artist": "Artist", "title": "Song (Instrumental)"}
        )
        hit = _raw_hit(
            artist="Artist Canonical",
            track="Song (Instrumental)",
            track_id=1,
            country="USA",
        )
        with patch.object(song_enricher, "search_itunes_full", return_value=[hit]), patch.object(
            song_enricher, "fetch_musicbrainz_ids", return_value=None
        ):
            song_enricher.enrich_song(db, sid, song_path)

        row = db.get_song_by_id(sid)
        assert row["artist"] == "Artist Canonical"
        assert row["title"] == "Song (Instrumental)"

    def test_survives_provider_crashes(self, db, tmp_path):
        song_path = str(tmp_path / "Foo---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")

        def boom_search(_query, limit):
            raise RuntimeError("iTunes is on fire")

        with patch.object(song_enricher, "search_itunes_full", side_effect=boom_search):
            song_enricher.enrich_song(db, sid, song_path)  # must not raise
        row = db.get_song_by_id(sid)
        assert row["metadata_status"] == "not_found"
        assert row["enrichment_attempts"] == 1
        assert row["last_enrichment_attempt"] is not None

    def test_event_hook_fires_start_and_finish_milestones(self, db, tmp_path):
        """The module-level event hook receives start + finish events on success."""
        song_path = str(tmp_path / "Eminem - Stan---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")

        captured: list[dict] = []
        song_enricher.set_event_hook(captured.append)
        try:
            hit = _raw_hit(
                artist="Eminem",
                track="Stan",
                track_id=99999,
                collection="MMLP",
                country="USA",
                genre="Hip-Hop/Rap",
            )
            with patch.object(
                song_enricher, "search_itunes_full", return_value=[hit]
            ), patch.object(song_enricher, "fetch_musicbrainz_ids", return_value=None):
                song_enricher.enrich_song(db, sid, song_path)
        finally:
            song_enricher.set_event_hook(None)

        messages = [e["message"] for e in captured]
        assert messages == ["Metadata enrichment starting", "Metadata enrichment finished"]
        assert all(e["phase"] == "enrichment" for e in captured)
        assert all(e["song"] == "Eminem - Stan---abc12345678.mp4" for e in captured)
        assert all(e["youtube_id"] == "abc12345678" for e in captured)

    def test_event_hook_fires_finish_on_itunes_miss(self, db, tmp_path):
        """An iTunes miss still emits a finish milestone (with detail)."""
        song_path = str(tmp_path / "Foo - Bar---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")

        captured: list[dict] = []
        song_enricher.set_event_hook(captured.append)
        try:
            with patch.object(song_enricher, "search_itunes_full", return_value=[]):
                song_enricher.enrich_song(db, sid, song_path)
        finally:
            song_enricher.set_event_hook(None)

        messages = [e["message"] for e in captured]
        assert messages == ["Metadata enrichment starting", "Metadata enrichment finished"]
        assert "no match" in captured[-1]["detail"]

    def test_event_hook_emits_warning_on_language_mismatch(self, db, tmp_path):
        """Mismatch is the noteworthy outcome — surface it as a warning event.

        Requires prior metadata for the verdict to be ``language_mismatch``
        rather than the metadata-empty downgrade to ``not_found`` (Bug D).
        """
        song_path = str(tmp_path / "Don Pedalini---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "pl")
        db.update_track_metadata_with_provenance(
            sid, "scanner", {"artist": "Don Pedalini", "title": "Some Song"}
        )

        captured: list[dict] = []
        song_enricher.set_event_hook(captured.append)
        try:
            hits = [
                _raw_hit(country="BRA", artist="X Pedalino", track="A Arte", collection="Hinario")
            ]
            with patch.object(song_enricher, "search_itunes_full", return_value=hits):
                song_enricher.enrich_song(db, sid, song_path)
        finally:
            song_enricher.set_event_hook(None)

        finish = captured[-1]
        assert finish["message"] == "Metadata enrichment finished"
        assert finish["severity"] == "warning"
        assert "language_mismatch" in finish["detail"]

    def test_event_hook_misbehaving_callback_does_not_break_enrichment(self, db, tmp_path):
        """A raising hook is logged and swallowed — enrichment must still complete."""
        song_path = str(tmp_path / "Foo - Bar---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")

        def explode(_payload):
            raise RuntimeError("boom")

        song_enricher.set_event_hook(explode)
        try:
            with patch.object(song_enricher, "search_itunes_full", return_value=[]):
                song_enricher.enrich_song(db, sid, song_path)  # must not raise
        finally:
            song_enricher.set_event_hook(None)

        row = db.get_song_by_id(sid)
        assert row["metadata_status"] == "not_found"

    def test_uncaught_exception_stamps_error(self, db, tmp_path):
        """Anything raising past the inline guards (e.g. a DB write error)
        must still bump enrichment_attempts and stamp ``error`` so the row
        doesn't get stuck on ``pending`` with attempts=0 — that's the
        signal we use to spot enrichment that silently dropped."""
        song_path = str(tmp_path / "Foo---abc12345678.mp4")
        sid = _insert_song(db, song_path)
        _seed_language(db, sid, "en")

        hit = _raw_hit(artist="A", track="T", track_id=1, country="USA")
        with patch.object(song_enricher, "search_itunes_full", return_value=[hit]), patch.object(
            db, "update_track_metadata_with_provenance", side_effect=RuntimeError("db boom")
        ):
            song_enricher.enrich_song(db, sid, song_path)  # must not raise

        row = db.get_song_by_id(sid)
        assert row["metadata_status"] == "error"
        assert row["enrichment_attempts"] == 1
        assert row["last_enrichment_attempt"] is not None

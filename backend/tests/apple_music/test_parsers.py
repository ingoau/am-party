"""Tests for the Apple Music parsers and auth helpers (synthetic fixtures, no network)."""

from __future__ import annotations

import base64
import json
import re

import pytest

from am_party.providers.apple_music import (
    AppleMusicClient,
    developer_token_expiry,
    is_plausible_user_token,
    render_musickit_auth_page,
)
from am_party.providers.apple_music.parsers import (
    format_artwork_url,
    is_available,
    key_id_from_uri,
    parse_hls_key,
    parse_m3u,
    parse_playlist,
    parse_playlist_ref,
    parse_track,
    pick_ctrp256_url,
)

ART = {"url": "https://is1-ssl.mzstatic.com/image/thumb/x/{w}x{h}bb.jpg", "width": 3000, "height": 3000}


def catalog_song(song_id="1440649599", **attrs):
    base = {
        "name": "One More Time",
        "artistName": "Daft Punk",
        "albumName": "Discovery",
        "durationInMillis": 320357,
        "artwork": ART,
        "contentRating": "explicit",
        "isrc": "GBDUW0000053",
        "playParams": {"id": song_id, "kind": "song"},
    }
    base.update(attrs)
    return {"id": song_id, "type": "songs", "attributes": base}


def test_parse_catalog_song():
    t = parse_track(catalog_song())
    assert t is not None
    assert t.id == "1440649599"
    assert (t.title, t.artist, t.album) == ("One More Time", "Daft Punk", "Discovery")
    assert t.duration == pytest.approx(320.357)
    assert t.artwork_url == "https://is1-ssl.mzstatic.com/image/thumb/x/600x600bb.jpg"
    assert t.explicit is True
    assert t.isrc == "GBDUW0000053"


def test_parse_clean_song_not_explicit():
    t = parse_track(catalog_song(contentRating="clean"))
    assert t and t.explicit is False


def test_unavailable_song_filtered():
    obj = catalog_song()
    del obj["attributes"]["playParams"]
    assert parse_track(obj) is None


def test_purchase_only_not_available():
    assert not is_available({"playParams": {"id": "i.x", "purchasedId": "123"}})
    assert is_available({"playParams": {"id": "i.x", "purchasedId": "123", "catalogId": "5"}})


def test_library_song_resolved_via_catalog_relationship():
    lib = {
        "id": "i.abc123",
        "type": "library-songs",
        "attributes": {"name": "Lib name", "artistName": "Lib artist", "playParams": {"id": "i.abc123"}},
        "relationships": {"catalog": {"data": [catalog_song("697195787")]}},
    }
    t = parse_track(lib)
    assert t and t.id == "697195787" and t.title == "One More Time"


def test_library_song_resolved_via_play_params_catalog_id():
    lib = {
        "id": "i.abc123",
        "type": "library-songs",
        "attributes": {
            "name": "Song",
            "artistName": "Artist",
            "durationInMillis": 1000,
            "playParams": {"id": "i.abc123", "kind": "song", "isLibrary": True, "catalogId": "42"},
        },
        "relationships": {"catalog": {"data": []}},
    }
    t = parse_track(lib)
    assert t and t.id == "42" and t.duration == 1.0


def test_library_only_upload_skipped():
    lib = {
        "id": "i.upload",
        "type": "library-songs",
        "attributes": {"name": "Demo", "playParams": {"id": "i.upload", "kind": "song", "isLibrary": True}},
        "relationships": {"catalog": {"data": []}},
    }
    assert parse_track(lib) is None


def test_withdrawn_catalog_copy_skipped():
    cat = catalog_song("99")
    del cat["attributes"]["playParams"]
    lib = {
        "id": "i.x",
        "type": "library-songs",
        "attributes": {"name": "x", "playParams": {"id": "i.x", "catalogId": "99"}},
        "relationships": {"catalog": {"data": [cat]}},
    }
    assert parse_track(lib) is None


def test_music_video_skipped():
    assert parse_track({"id": "1", "type": "music-videos", "attributes": {"playParams": {"id": "1"}}}) is None


def test_artwork_template_placeholders():
    assert format_artwork_url({}) is None
    url = format_artwork_url({"artwork": {"url": "https://a/{w}x{h}{c}.{f}"}})
    assert url == "https://a/600x600bb.jpg"


def test_parse_playlists():
    lib = parse_playlist(
        {
            "id": "p.ZOAXxQ6ckpJXJb",
            "type": "library-playlists",
            "attributes": {"name": "Party", "playParams": {"id": "p.ZOAXxQ6ckpJXJb", "globalId": "pl.u-abc"}},
        }
    )
    assert lib.id == "p.ZOAXxQ6ckpJXJb" and lib.name == "Party" and lib.artwork_url is None
    cat = parse_playlist(
        {"id": "pl.f4d106fed2bd41149aaacabb233eb5eb", "type": "playlists", "attributes": {"name": "Hits", "artwork": ART}}
    )
    assert cat.id == "pl.f4d106fed2bd41149aaacabb233eb5eb"
    assert cat.artwork_url and "600x600" in cat.artwork_url


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb",
         "pl.f4d106fed2bd41149aaacabb233eb5eb"),
        ("https://music.apple.com/au/playlist/party/pl.u-8aAVZAkCrPpRY0?l=en", "pl.u-8aAVZAkCrPpRY0"),
        ("https://music.apple.com/library/playlist/p.ZOAXxQ6ckpJXJb", "p.ZOAXxQ6ckpJXJb"),
        ("https://music.apple.com/us/library/playlist/p.ZOAXxQ6ckpJXJb?l=en-US", "p.ZOAXxQ6ckpJXJb"),
        ("music.apple.com/us/playlist/x/pl.abc123", "pl.abc123"),
        ("https://beta.music.apple.com/us/playlist/pl.abc123", "pl.abc123"),
        ("  pl.abc123  ", "pl.abc123"),
        ("p.ZOAXxQ6ckpJXJb", "p.ZOAXxQ6ckpJXJb"),
        ("https://music.apple.com/us/album/discovery/697194953", None),
        ("https://evil.example.com/us/playlist/x/pl.abc123", None),
        ("https://music.apple.com.evil.com/us/playlist/x/pl.abc123", None),
        ("1440649599", None),
        ("", None),
    ],
)
def test_parse_playlist_ref(ref, expected):
    assert parse_playlist_ref(ref) == expected
    assert AppleMusicClient.parse_playlist_ref(ref) == expected


KID = bytes(range(16))
KEY_URI = "data:;base64," + base64.b64encode(KID).decode()

M3U8 = f"""#EXTM3U
#EXT-X-TARGETDURATION:330
#EXT-X-VERSION:7
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-MAP:URI="P123_A1_256.mp4",BYTERANGE="1172@0"
#EXT-X-KEY:METHOD=ISO-23001-7,URI="skd://itunes.apple.com/P000000000/s1/e1",KEYFORMAT="com.apple.streamingkeydelivery",KEYFORMATVERSIONS="1"
#EXT-X-KEY:METHOD=ISO-23001-7,URI="{KEY_URI}",KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",KEYFORMATVERSIONS="1"
#EXTINF:9.98,
#EXT-X-BYTERANGE:160000@1172
P123_A1_256.mp4
#EXTINF:9.98,
#EXT-X-BYTERANGE:160000@161172
P123_A1_256.mp4
#EXT-X-ENDLIST
"""


def test_parse_m3u_key_and_path():
    items = parse_m3u(M3U8)
    assert len(items) == 2
    assert items[0].path == "P123_A1_256.mp4"
    assert items[0].key == KEY_URI
    assert key_id_from_uri(items[0].key) == KID


def test_parse_hls_key_variants():
    assert parse_hls_key('#EXT-X-KEY:METHOD=NONE', "old") is None
    assert parse_hls_key('#EXT-X-KEY:METHOD=AES-128,URI="k.bin",IV=0x1', None) == "k.bin"
    assert parse_hls_key('#EXT-X-KEY:METHOD=AES-128', "keep") == "keep"


def test_pick_ctrp256():
    assets = [
        {"flavor": "30:cbcp256", "URL": "https://x/a.m3u8"},
        {"flavor": "28:ctrp256", "URL": "https://x/b.m3u8"},
        {"flavor": "32:ctrp64", "URL": "https://x/c.m3u8"},
    ]
    assert pick_ctrp256_url(assets) == "https://x/b.m3u8"
    assert pick_ctrp256_url(assets[:1]) is None


def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'ES256', 'kid': 'X'})}.{enc(claims)}.sig"


def test_developer_token_expiry():
    assert developer_token_expiry(_jwt({"iss": "T", "exp": 1790000000})) == 1790000000.0
    assert developer_token_expiry(_jwt({"iss": "T"})) is None
    assert developer_token_expiry("not-a-jwt") is None
    assert developer_token_expiry("a.!!!.c") is None


def test_is_plausible_user_token():
    assert is_plausible_user_token("A" * 40 + "/+==")
    assert not is_plausible_user_token("short==")
    assert not is_plausible_user_token(None)


def test_render_auth_page_escapes_config():
    page = render_musickit_auth_page("tok</script><b>", "/admin/apple-music/callback", app_name="am-party <x>")
    assert "js-cdn.music.apple.com/musickit/v3/musickit.js" in page
    assert "</script><b>" not in page
    m = re.search(r"const AM_PARTY_CONFIG = Object\.freeze\((.*?)\);", page)
    assert m
    cfg = json.loads(m.group(1))
    assert cfg["developer_token"] == "tok</script><b>"
    assert cfg["callback_url"] == "/admin/apple-music/callback"
    assert "am-party &lt;x&gt;" in page
    assert "/*__AM_PARTY" not in page

"""Guards the playlist-logo generator: the slug set must stay in lock-step with the
write-back's suffix→asset map (drift there silently leaves a family with no poster), and a
render must produce a poster-sized image."""
from __future__ import annotations

from scripts.managers.services.plex.playlists import writeback as wb
from scripts.support.tools import generate_playlist_logos as gen


def test_slugs_match_writeback_brand_assets():
    gen_slugs = {p["slug"] for p in gen.PLAYLISTS}
    assert gen_slugs == set(wb._BRAND_ASSETS.values())


def test_render_produces_a_poster_sized_rgb_image():
    img = gen.render(gen.PLAYLISTS[0])
    assert img.size == (gen.W, gen.H)
    assert img.mode == "RGB"

from pathlib import Path


COVER_PATH = Path(__file__).parents[1] / "docs" / "screenshots" / "cover.png"
MAX_COVER_BYTES = 8 * 1024 * 1024
PNG_SIGNATURE = bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A))


def test_documentation_cover_is_a_bounded_png() -> None:
    cover = COVER_PATH.read_bytes()

    assert len(cover) <= MAX_COVER_BYTES
    assert cover.startswith(PNG_SIGNATURE)

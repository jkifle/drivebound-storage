from datetime import datetime, timezone

from PIL import Image

from app.worker.tasks import extract_and_thumbnail, video_metadata_from_probe


def test_extract_and_thumbnail_creates_1024_webp(tmp_path):
    original = tmp_path / "original.jpg"
    thumbnail = tmp_path / "nested" / "thumbnail.webp"
    Image.new("RGB", (2048, 1024), "blue").save(original, "JPEG")

    fallback = datetime(2026, 1, 2, tzinfo=timezone.utc)
    metadata = extract_and_thumbnail(original, thumbnail, fallback)

    assert metadata["width"] == 2048
    assert metadata["height"] == 1024
    assert metadata["taken_at"] == fallback
    assert thumbnail.is_file()
    with Image.open(thumbnail) as result:
        assert result.format == "WEBP"
        assert result.size == (1024, 512)


def test_video_probe_preserves_capture_location_and_camera_metadata():
    fallback = datetime(2026, 1, 2, tzinfo=timezone.utc)
    metadata = video_metadata_from_probe(
        {
            "streams": [{
                "codec_type": "video", "width": 3840, "height": 2160, "duration": "8.25",
                "tags": {"rotate": "90"},
            }],
            "format": {"tags": {
                "creation_time": "2024-07-06T15:04:03Z",
                "com.apple.quicktime.location.ISO6709": "+35.9606-83.9207/",
                "com.apple.quicktime.make": "Apple",
                "com.apple.quicktime.model": "iPhone",
            }},
        },
        fallback,
    )

    assert metadata["taken_at"] == datetime(2024, 7, 6, 15, 4, 3, tzinfo=timezone.utc)
    assert metadata["latitude"] == 35.9606
    assert metadata["longitude"] == -83.9207
    assert metadata["duration_seconds"] == 8.25
    assert metadata["orientation"] == 90
    assert metadata["camera_model"] == "iPhone"

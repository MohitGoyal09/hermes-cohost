import io

from PIL import Image

from cohost import screen


def test_downscale_keeps_aspect_and_encodes_jpeg():
    data = screen.downscale_jpeg(Image.new("RGB", (2048, 1024), "red"), 1024)
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    assert img.size == (1024, 512)


def test_small_image_not_upscaled():
    img = Image.open(io.BytesIO(screen.downscale_jpeg(Image.new("RGBA", (300, 200)), 1024)))
    assert img.size == (300, 200)


def test_grab_screen_returns_none_on_failure(monkeypatch):
    def boom():
        raise OSError("no screen recording permission")

    monkeypatch.setattr(screen.ImageGrab, "grab", boom)
    assert screen.grab_screen() is None

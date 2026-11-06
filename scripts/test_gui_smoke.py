"""Check that the GUI smoke verifier accepts real UI and rejects blank output."""

import io
from pathlib import Path
import unittest

from PIL import Image

from gui_smoke import verify_screenshot


class FramebufferChecks(unittest.TestCase):
    def test_existing_application_screenshot_has_content(self):
        screenshot = Path(__file__).resolve().parents[1] / "ze-screenshot.png"
        self.assertGreater(verify_screenshot(screenshot)["bright_pixels"], 100)

    def assert_rejected(self, image):
        screenshot = io.BytesIO()
        image.save(screenshot, format="PNG")
        screenshot.seek(0)
        with self.assertRaises(ValueError):
            verify_screenshot(screenshot)

    def test_black_white_and_theme_backgrounds_are_rejected(self):
        for color in ("black", "white", (45, 45, 45)):
            with self.subTest(color=color):
                self.assert_rejected(Image.new("RGB", (1400, 900), color))

    def test_stray_pixel_does_not_count_as_rendered_ui(self):
        image = Image.new("RGB", (1400, 900), (45, 45, 45))
        image.putpixel((100, 100), (255, 255, 255))
        self.assert_rejected(image)

    def test_transparent_frame_is_rejected(self):
        self.assert_rejected(Image.new("RGBA", (1400, 900), (255, 255, 255, 0)))

    def test_tiny_frame_is_rejected(self):
        self.assert_rejected(Image.new("RGB", (100, 100), "white"))


if __name__ == "__main__":
    unittest.main()

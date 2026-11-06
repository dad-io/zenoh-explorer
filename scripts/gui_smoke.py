"""Launch the native executable and verify its rendered framebuffer, not just its process."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageStat


def verify_screenshot(path):
    with Image.open(path) as image:
        image.load()
        width, height = image.size
        if width < 800 or height < 500:
            raise ValueError(f"Rendered window is too small: {width}x{height}")
        if image.mode == "RGBA" and image.getchannel("A").getextrema()[1] == 0:
            raise ValueError("Rendered window is fully transparent")
        gray = image.convert("L")
        low, high = gray.getextrema()
        deviation = ImageStat.Stat(gray).stddev[0]
        bright_pixels = sum(gray.histogram()[160:])
        # Require text/controls as well as a background. A clear-only frame,
        # or a single stray pixel on it, must not count as a working window.
        if high - low < 64 or deviation < 5 or bright_pixels < width * height * 0.001:
            raise ValueError("Rendered window is blank or has too little visible content")
        return {
            "width": width,
            "height": height,
            "luminance_range": [low, high],
            "luminance_stddev": round(deviation, 3),
            "bright_pixels": bright_pixels,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    executable = args.executable.resolve(strict=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # Every run gets a fresh filename, so a previous screenshot cannot hide a failure.
    output = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir.resolve()))
    screenshot = output / "window.png"
    with executable.open("rb") as binary:
        executable_hash = hashlib.file_digest(binary, "sha256").hexdigest()
    report = {
        "status": "failed",
        "executable": str(executable),
        "executable_sha256": executable_hash,
        "graphics_driver": os.environ.get("GALLIUM_DRIVER", "system default"),
    }
    env = os.environ.copy()
    # eframe captures the actual OpenGL framebuffer after two UI passes, then exits.
    env["EFRAME_SCREENSHOT_TO"] = str(screenshot)
    started = time.monotonic()
    try:
        with (output / "app.log").open("wb") as log:
            process = subprocess.run(
                [str(executable)], cwd=executable.parent, env=env,
                stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout, check=False,
            )
        report["exit_code"] = process.returncode
        if process.returncode != 0:
            raise RuntimeError(f"Application exited with code {process.returncode}")
        if not screenshot.is_file():
            raise RuntimeError("Application exited without producing a rendered window screenshot")
        report["framebuffer"] = verify_screenshot(screenshot)
        report["status"] = "passed"
        print(f"Native GUI rendered successfully: {screenshot}")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        report["error"] = str(error)
        print(f"GUI smoke check failed: {error}. Evidence: {output}", file=sys.stderr)
        return 1
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        crash_log = executable.with_name("crash.log")
        if crash_log.is_file():
            (output / "crash.log").write_bytes(crash_log.read_bytes())


if __name__ == "__main__":
    sys.exit(main())

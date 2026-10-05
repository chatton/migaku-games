"""Japanese OCR via meikiocr (Linux, or anywhere ONNX Runtime runs).

Usage: python meiki_ocr.py <image>  ->  same JSON as vision_ocr.swift on stdout:
  {"width": W, "height": H, "lines": [{"text", "conf", "x", "y", "w", "h"}]}
Box coordinates are normalised to 0..1 with a top-left origin.
"""
import json
import sys

import cv2
from meikiocr import MeikiOCR


# meikiocr emits half-width punctuation; Japanese text (and Vision) uses full-width.
FULLWIDTH = str.maketrans({"!": "！", "?": "？", "~": "〜", ":": "：", "(": "（", ")": "）"})


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: meiki_ocr.py <image>")
    image = cv2.imread(sys.argv[1], cv2.IMREAD_COLOR)
    if image is None:
        sys.exit(f"cannot read image {sys.argv[1]}")
    height, width = image.shape[:2]
    # meikiocr is trained on full-size game captures; on small frames (press shots, 480p
    # emulators) it clips the first glyph of lines. Upscaling fixes that. Boxes are
    # normalised, so the scale doesn't leak into the output.
    if height < 900:
        scale = 900 / height
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    scaled_h, scaled_w = image.shape[:2]

    lines = []
    for result in MeikiOCR().run_ocr(image):
        chars = result["chars"]
        # The overlay lays text out horizontally; vertical lines (rare in game dialogue) are skipped.
        if not result["text"] or not chars or result["is_vertical"]:
            continue
        x1 = min(c["bbox"][0] for c in chars)
        y1 = min(c["bbox"][1] for c in chars)
        x2 = max(c["bbox"][2] for c in chars)
        y2 = max(c["bbox"][3] for c in chars)
        lines.append({
            "text": result["text"].translate(FULLWIDTH),
            "conf": sum(c["conf"] for c in chars) / len(chars),
            "x": x1 / scaled_w,
            "y": y1 / scaled_h,
            "w": (x2 - x1) / scaled_w,
            "h": (y2 - y1) / scaled_h,
        })

    json.dump({"width": width, "height": height, "lines": lines}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()

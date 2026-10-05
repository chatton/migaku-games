"""Render fake FF8-style dialogue screenshots into samples/."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"
SAMPLES = Path(__file__).parent / "samples"

DIALOGUE = {
    "ff8_dialogue.png": [
        "スコール「……俺はもう、誰にも頼らない。」",
        "リノア「どうしてそんなに一人でいたいの？",
        "　　　　みんな、あなたのことを心配してるんだよ。」",
    ],
    # One sentence wrapped across two lines, to check Migaku keeps it whole.
    "ff8_wrapped.png": [
        "キスティス",
        "「ガーデンに戻ったら、まずは学園長に",
        "　今回の任務について報告しなければならないわ」",
    ],
}


def render(name: str, lines: list) -> None:
    w, h = 1600, 900
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)

    # Vertical gradient as a stand-in for the game scene.
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=(int(30 + 40 * t), int(50 + 60 * t), int(90 + 50 * t)))

    # FF8-ish blue dialogue window with a light border.
    box = (120, 560, w - 120, h - 60)
    d.rounded_rectangle(box, radius=18, fill=(28, 40, 110), outline=(200, 200, 220), width=4)

    font = ImageFont.truetype(FONT, 46)
    y = box[1] + 36
    for line in lines:
        d.text((box[0] + 48, y), line, font=font, fill=(240, 240, 240))
        y += 76

    SAMPLES.mkdir(exist_ok=True)
    img.save(SAMPLES / name)
    print(SAMPLES / name)


def main() -> None:
    for name, lines in DIALOGUE.items():
        render(name, lines)


if __name__ == "__main__":
    main()

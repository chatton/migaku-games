"""OCR pipeline: run an engine, clean up its lines, group them into dialogue blocks.

Engines are separate programs that print the same JSON, so either can run anywhere:
  vision: Apple's Vision framework via ocr/vision_ocr.swift (macOS only)
  meiki:  meikiocr via ocr/meiki_ocr.py (anywhere ONNX Runtime runs)
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
MAC = sys.platform == "darwin"
DEFAULT_OCR = "vision" if MAC else "meiki"

# Vision sometimes puts spaces between CJK characters; Migaku segments better without them.
CJK_GAP = re.compile(r"(?<=[　-鿿＀-￯])\s+(?=[　-鿿＀-￯])")


def vision_binary() -> Path:
    src = ROOT / "ocr" / "vision_ocr.swift"
    out = BUILD / "vision-ocr"
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        BUILD.mkdir(exist_ok=True)
        print("compiling vision-ocr ...")
        subprocess.run(["swiftc", "-O", str(src), "-o", str(out)], check=True)
    return out


# On-screen game UI labels (Persona-style auto/log/fast-forward buttons, VN menus).
UI_LABELS = ("オート", "ログ", "早送り", "スキップ", "セーブ", "ロード", "バックログ", "メニュー")


def is_ui_noise(text: str) -> bool:
    core = re.sub(r"[^\u3040-\u30ff\u4e00-\u9fff]", "", text)
    if not core:
        return True  # no Japanese at all: HP numbers, timers, stray symbols
    if core == "ート":
        return True  # meikiocr reads Persona's "オート" button as "Qート"/"Oート"
    return core in UI_LABELS or (len(core) <= 5 and any(label in core for label in UI_LABELS))


def meiki_python() -> str:
    """Interpreter with meikiocr installed: $MEIKI_PYTHON, else the project .venv, else this one."""
    venv = ROOT / ".venv" / "bin" / "python"
    return os.environ.get("MEIKI_PYTHON") or (str(venv) if venv.exists() else sys.executable)


ENGINES = ("vision", "meiki")
AVAILABLE = tuple(e for e in ENGINES if MAC or e != "vision")


class OcrError(Exception):
    pass


def run_engine(engine: str, image: Path) -> dict:
    """Both engines print the same JSON: {width, height, lines: [{text, conf, x, y, w, h}]}."""
    if engine not in ENGINES:
        raise OcrError(f"unknown OCR engine {engine!r} (expected one of {', '.join(ENGINES)})")
    if engine == "vision":
        if not MAC:
            raise OcrError("the vision OCR engine is macOS-only; use meiki")
        cmd = [str(vision_binary()), str(image)]
    else:
        cmd = [meiki_python(), str(ROOT / "ocr" / "meiki_ocr.py"), str(image)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode:
        raise OcrError(f"{engine} OCR failed:\n{res.stderr.strip()}")
    return json.loads(res.stdout)


def ocr(image: Path, engine: str = DEFAULT_OCR) -> dict:
    data = run_engine(engine, image)
    data["engine"] = engine
    # Real dialogue scores >= 0.5; lower is usually watermarks or HUD noise.
    lines = [l for l in data["lines"] if l["conf"] >= 0.4]
    for line in lines:
        # Vision reads an ellipsis as runs of middle dots, sometimes mixed with real ones.
        line["text"] = re.sub(r"[・…]{2,}", "……", CJK_GAP.sub("", line["text"]))
    lines = merge_same_row(lines, data["width"] / data["height"])
    data["lines"] = [l for l in lines if not is_ui_noise(l["text"])]
    data["blocks"] = group_blocks(data["lines"])
    return data


def merge_same_row(lines: list, aspect: float) -> list:
    """Join fragments Vision split from one visual line (e.g. at a 「！？」 or a big gap)."""
    lines = sorted(lines, key=lambda l: l["x"])
    out: list = []
    for l in lines:
        for m in out:
            h = min(l["h"], m["h"])
            same_row = abs((l["y"] + l["h"] / 2) - (m["y"] + m["h"] / 2)) < 0.5 * h
            similar = max(l["h"], m["h"]) < 1.5 * h
            gap = (l["x"] - (m["x"] + m["w"])) * aspect  # in units of image height
            if same_row and similar and -0.5 * h < gap < 3 * h:
                right = max(m["x"] + m["w"], l["x"] + l["w"])
                top, bottom = min(m["y"], l["y"]), max(m["y"] + m["h"], l["y"] + l["h"])
                m.update(text=m["text"] + l["text"], w=right - m["x"], y=top, h=bottom - top,
                         conf=min(m["conf"], l["conf"]))
                break
        else:
            out.append(dict(l))
    return out


def group_blocks(lines: list) -> list:
    """Group OCR lines into dialogue blocks so Migaku sees whole sentences, not single lines.

    Returns lists of indices into `lines`. A speaker name line (short, no punctuation, above
    the dialogue) gets its own block so it doesn't end up in the card's sentence.
    """
    order = sorted(range(len(lines)), key=lambda i: lines[i]["y"])
    blocks: list = []
    for i in order:
        l = lines[i]
        if blocks:
            prev = lines[blocks[-1][-1]]
            gap = l["y"] - (prev["y"] + prev["h"])
            same_size = abs(l["h"] - prev["h"]) < 0.35 * prev["h"]
            left = min(lines[j]["x"] for j in blocks[-1])
            right = max(lines[j]["x"] + lines[j]["w"] for j in blocks[-1])
            overlaps = l["x"] < right and l["x"] + l["w"] > left
            if gap < 0.75 * prev["h"] and same_size and overlaps:
                blocks[-1].append(i)
                continue
        blocks.append([i])

    out = []
    for b in blocks:
        first, second = (lines[b[0]]["text"], lines[b[1]]["text"]) if len(b) > 1 else ("", "")
        # A short first line with no punctuation is a speaker name, even when OCR drops the 「.
        name_like = len(first) <= 12 and not re.search(r"[「」『』。、！？…!?]", first)
        if second and name_like and (second.startswith("「") or len(second) > len(first)):
            out += [b[:1], b[1:]]
        else:
            out.append(b)
    return out

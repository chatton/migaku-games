"""Download JMdict (English) and turn it into a small SQLite lookup table for align.py.

    python3 tools/build_jmdict.py /opt/jmdict.sqlite

JMdict is the EDRDG's Japanese-English dictionary (CC BY-SA 4.0,
https://www.edrdg.org/edrdg/licence.html). The table maps every written form (kanji and kana)
to the English glosses of its first few senses, flagged "common" when JMdict marks the entry as
a frequent word (so a kana spelling shared by many rare words can prefer the everyday one).
"""
import gzip
import io
import sqlite3
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

URL = "https://www.edrdg.org/pub/Nihongo/JMdict_e.gz"
SENSES = 4  # the common meanings come first; later senses are rare and add false matches


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "jmdict.sqlite")
    print(f"downloading {URL} ...", flush=True)
    with urllib.request.urlopen(URL, timeout=120) as resp:
        data = gzip.decompress(resp.read())

    out.unlink(missing_ok=True)
    db = sqlite3.connect(out)
    db.execute("CREATE TABLE gloss (form TEXT NOT NULL, gloss TEXT NOT NULL, common INTEGER NOT NULL)")
    rows = 0
    # JMdict declares its entities (&n; &v5r; ...) in its internal DTD, which expat expands.
    for _, entry in ET.iterparse(io.BytesIO(data), events=("end",)):
        if entry.tag != "entry":
            continue
        forms = [e.text for e in entry.iter("keb")] + [e.text for e in entry.iter("reb")]
        glosses = [g.text for sense in entry.findall("sense")[:SENSES] for g in sense.findall("gloss") if g.text]
        common = int(any(True for tag in ("ke_pri", "re_pri") for _ in entry.iter(tag)))
        if glosses:
            text = "; ".join(glosses)
            db.executemany("INSERT INTO gloss VALUES (?, ?, ?)", [(f, text, common) for f in forms if f])
            rows += len(forms)
        entry.clear()
    db.execute("CREATE INDEX gloss_form ON gloss (form)")
    db.commit()
    db.execute("VACUUM")
    db.close()
    print(f"{rows} forms -> {out} ({out.stat().st_size // 1_000_000} MB)", flush=True)


if __name__ == "__main__":
    main()

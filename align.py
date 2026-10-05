"""Word-level matching between a Japanese sentence and its English translation, for colouring
both sides alike. Local and dictionary-based: Janome splits the Japanese into words (with their
dictionary forms), JMdict (tools/build_jmdict.py) gives each word's English meanings, and those
are matched against the translation's words after reducing both to rough stems.

It colours content words (names of things, verbs, adjectives, adverbs) whose meaning shows up
in the translation; particles and grammar endings are skipped, and loose translations simply
match less. align(ja, en) -> [{"ja": [start, end], "en": [start, end]}], character offsets.
"""
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from janome.tokenizer import Tokenizer

# Parts of speech that never carry a translatable meaning of their own.
SKIP_POS = {"助詞", "助動詞", "記号", "フィラー", "その他"}
# English words too common or too structural to match on.
STOP = {
    "a", "an", "the", "to", "be", "of", "in", "on", "at", "for", "with", "by", "as", "or", "and",
    "one", "one's", "oneself", "someone", "something", "etc", "esp", "e.g", "i.e", "it", "that",
    "this", "do", "is", "are", "was", "very", "so", "not", "no",
}
IRREGULAR = {
    "went": "go", "gone": "go", "said": "say", "saw": "see", "seen": "see", "took": "take",
    "taken": "take", "came": "come", "got": "get", "gotten": "get", "made": "make", "knew": "know",
    "known": "know", "thought": "think", "told": "tell", "found": "find", "gave": "give",
    "given": "give", "left": "leave", "felt": "feel", "kept": "keep", "brought": "bring",
    "began": "begin", "begun": "begin", "ran": "run", "fought": "fight", "shot": "shoot",
    "won't": "will", "can't": "can", "cannot": "can", "i'm": "i", "me": "i", "my": "i",
    "better": "good", "best": "good", "worse": "bad", "worst": "bad", "children": "child",
    "men": "man", "women": "woman", "people": "person",
}
EN_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
# Godan potential forms (撃てる = can shoot) -> the plain verb (撃つ).
E_TO_U = dict(zip("えけげせてねべめれ", "うくぐすつぬぶむる"))


def stem(word: str) -> str:
    w = word.lower()
    w = IRREGULAR.get(w, w)
    w = re.sub(r"(n't|'s|'re|'ll|'ve|'d|'m)$", "", w)
    for suffix, repl in (("ies", "y"), ("ied", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", ""), ("ly", "")):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)] + repl
            break
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "ls":  # running -> runn -> run
        w = w[:-1]
    return w


class Aligner:
    def __init__(self, dictionary: Path):
        self.tokenizer = Tokenizer()
        self.db = sqlite3.connect(f"file:{dictionary}?mode=ro", uri=True, check_same_thread=False)

    @lru_cache(maxsize=20000)
    def meanings(self, form: str) -> frozenset:
        """Stems of the English glosses for a written form; common entries only when there are any."""
        rows = self.db.execute("SELECT gloss, common FROM gloss WHERE form = ?", (form,)).fetchall()
        if any(common for _, common in rows):
            rows = [r for r in rows if r[1]]
        words = set()
        for gloss, _ in rows:
            for phrase in re.sub(r"\([^)]*\)", "", gloss).split(";"):
                parts = EN_WORD.findall(phrase)
                words.update(stem(w) for w in parts)
                if 1 < len(parts) <= 3:  # "any more" also matches "anymore"
                    words.add(stem("".join(parts)))
        return frozenset(w for w in words if w not in STOP and (len(w) > 1 or w == "i"))

    def word_meanings(self, surface: str, base: str) -> frozenset:
        for form in (base, surface):
            found = self.meanings(form)
            if found:
                return found
        if len(base) >= 2 and base.endswith("る") and base[-2] in E_TO_U:
            return self.meanings(base[:-2] + E_TO_U[base[-2]])
        return frozenset()

    def align(self, ja: str, en: str) -> list:
        tokens, pos = [], 0
        for t in self.tokenizer.tokenize(ja):
            tokens.append((pos, pos + len(t.surface), t.surface, t.base_form, t.part_of_speech.split(",")[0]))
            pos += len(t.surface)
        en_words = [(m.start(), m.end(), stem(m.group())) for m in EN_WORD.finditer(en)]
        taken, pairs = set(), []
        i = 0
        while i < len(tokens):
            start, end, surface, base, kind = tokens[i]
            if kind in SKIP_POS or base == "*":
                i += 1
                continue
            # Longest dictionary word over up to three tokens first (すま+ない -> すまない "sorry").
            span, meanings = 1, frozenset()
            for n in (3, 2):
                if i + n <= len(tokens):
                    joined = "".join(t[2] for t in tokens[i : i + n])
                    found = self.meanings(joined)
                    if found:
                        span, meanings = n, found
                        break
            if not meanings:
                meanings = self.word_meanings(surface, base)
            ja_end = tokens[i + span - 1][1]
            # The most specific matching English word (longest stem, then earliest): すまない's
            # glosses include "I" as well as "sorry".
            candidates = [k for k, (_, _, w) in enumerate(en_words) if k not in taken and w in meanings]
            if candidates:
                k = max(candidates, key=lambda k: (len(en_words[k][2]), -k))
                taken.add(k)
                pairs.append({"ja": [start, ja_end], "en": list(en_words[k][:2])})
            i += span
        return pairs

"""Word matching: python3 -m unittest tests.test_align

Needs Janome and the JMdict table (MIGAKU_JMDICT, built by tools/build_jmdict.py); CI runs it in
the image, where both are installed. Skipped otherwise.
"""
import os
import unittest
from pathlib import Path

DICT = Path(os.environ.get("MIGAKU_JMDICT", "/opt/jmdict.sqlite"))
try:
    from align import Aligner, stem
except ImportError:  # no Janome
    Aligner = None


@unittest.skipUnless(Aligner and DICT.exists(), "needs Janome and the JMdict table")
class AlignTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aligner = Aligner(DICT)

    def pairs(self, ja, en):
        return {ja[p["ja"][0]:p["ja"][1]]: en[p["en"][0]:p["en"][1]] for p in self.aligner.align(ja, en)}

    def test_dialogue(self):
        pairs = self.pairs("「……俺はもう、誰にも頼らない。」", "\"I won't rely on anyone anymore.\"")
        self.assertEqual(pairs.get("俺"), "I")
        self.assertEqual(pairs.get("もう"), "anymore")
        self.assertEqual(pairs.get("頼ら"), "rely")

    def test_multi_token_word_and_potential_form(self):
        pairs = self.pairs("すまない、撃てない。", "I'm sorry, I can't shoot.")
        self.assertEqual(pairs.get("すまない"), "sorry")
        self.assertEqual(pairs.get("撃て"), "shoot")

    def test_each_english_word_used_once_and_particles_skipped(self):
        pairs = self.aligner.align("ガーデンに戻ったら、まずは学園長に報告しなければならないわ",
                                   "When we get back to the Garden, we have to report to the headmaster first.")
        ens = [tuple(p["en"]) for p in pairs]
        self.assertEqual(len(ens), len(set(ens)))
        ja = "ガーデンに戻ったら、まずは学園長に報告しなければならないわ"
        self.assertNotIn("に", [ja[p["ja"][0]:p["ja"][1]] for p in pairs])

    def test_offsets_index_the_inputs(self):
        ja, en = "動物だ", "an animal"
        (pair,) = self.aligner.align(ja, en)
        self.assertEqual((ja[slice(*pair["ja"])], en[slice(*pair["en"])]), ("動物", "animal"))


class StemTest(unittest.TestCase):
    @unittest.skipUnless(Aligner, "needs Janome")
    def test_stems(self):
        for a, b in [("relies", "rely"), ("relied", "rely"), ("running", "run"), ("won't", "will"), ("Squall's", "squall")]:
            self.assertEqual(stem(a), stem(b) if b != "will" else "will", (a, b))


if __name__ == "__main__":
    unittest.main()

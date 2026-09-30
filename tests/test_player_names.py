"""The name table only ever looks up; it never derives a reading."""
import unittest

from server import player_names


class NormalizationTests(unittest.TestCase):
    def test_accents_case_and_punctuation_fold_to_one_key(self):
        for spelling in ("Vinícius Júnior", "vinicius junior", "VINÍCIUS  JÚNIOR", "Vinicius Junior"):
            self.assertEqual(player_names.normalize_name(spelling), "vinicius junior")

    def test_hyphen_and_space_spellings_agree(self):
        # Rosters spell the same player both ways, so the key must not care.
        self.assertEqual(player_names.normalize_name("Trent Alexander-Arnold"),
                         player_names.normalize_name("Trent Alexander Arnold"))

    def test_letters_nfkd_cannot_decompose_are_folded(self):
        # ø is a distinct letter, not an accented o, so NFKD leaves it alone and
        # the name would have no key in common with its own ASCII spelling.
        self.assertEqual(player_names.normalize_name("Martin Ødegaard"), "martin odegaard")
        self.assertEqual(player_names.normalize_name("Martin Odegaard"), "martin odegaard")
        self.assertEqual(player_names.normalize_name("Łukasz Piszczek"), "lukasz piszczek")
        self.assertEqual(player_names.normalize_name("Ðorđe Petrović"), "dorde petrovic")


class LookupTests(unittest.TestCase):
    def test_a_known_name_returns_its_reading(self):
        self.assertEqual(player_names.reading_for("Thibaut Courtois"), "库尔图瓦")
        self.assertEqual(player_names.reading_for("Jude Bellingham"), "贝林厄姆")

    def test_an_unknown_name_returns_nothing_rather_than_a_guess(self):
        # Absence is a real answer: the reader keeps the source spelling. A
        # transliteration rule would invent 萨蒙 for a youth player nobody has
        # written about yet, and the reader could not tell it was invented.
        for name in ("Marli Salmon", "Ife Ibrahim", "Some Unlisted Player", ""):
            self.assertEqual(player_names.reading_for(name), "")

    def test_with_reading_adds_the_field_and_keeps_the_source_spelling(self):
        row = player_names.with_reading({"name": "Thibaut Courtois"})
        self.assertEqual(row["name"], "Thibaut Courtois")
        self.assertEqual(row["name_zh"], "库尔图瓦")

    def test_with_reading_removes_a_stale_reading(self):
        # A row that was written when the table held the name must not keep a
        # reading after the entry is withdrawn.
        row = player_names.with_reading({"name": "Nobody At All", "name_zh": "旧译名"})
        self.assertNotIn("name_zh", row)
        self.assertEqual(row["name"], "Nobody At All")

    def test_no_reading_is_ever_ascii_or_empty(self):
        # Every entry must be a Chinese reading, or the interface would show an
        # English string under the "中文名" label.
        for key, reading in player_names.NAME_READINGS.items():
            with self.subTest(name=key):
                self.assertTrue(reading.strip())
                self.assertTrue(any("㐀" <= char <= "鿿" for char in reading),
                                f"{reading!r} has no Chinese characters")

    def test_keys_are_already_normalized(self):
        # A key that is not in normalized form can never be looked up.
        for key in player_names.NAME_READINGS:
            self.assertEqual(player_names.normalize_name(key), key, f"{key!r} is not a normalized key")


if __name__ == "__main__":
    unittest.main()

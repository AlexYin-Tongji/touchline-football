"""The name table only ever looks up; it never derives a reading."""
import unittest
from server.ingestion import now

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
        self.assertEqual(player_names.reading_for("Erling Haaland"), "哈兰德")
        self.assertEqual(player_names.reading_for("Rúben Dias"), "鲁本·迪亚斯")

    def test_a_legacy_spelling_without_source_is_not_presented_as_checked(self):
        for name in ("Thibaut Courtois", "Jude Bellingham"):
            self.assertEqual(player_names.reading_for(name), "")
            row = player_names.with_reading({"name": name, "name_zh": "旧译名"})
            self.assertNotIn("name_zh", row)
            self.assertEqual(row["name"], name)

    def test_attested_reading_stays_preferred_over_an_identity_checked_label(self):
        row = {"name": "Erling Haaland", "team_id": "manchester-city", "provider_id": "1"}
        candidate = {"name_zh": "埃尔林·哈兰", "name_zh_source": "Wikidata 中文标签",
                     "name_zh_source_url": "https://www.wikidata.org/wiki/Q28967995", "name_zh_checked_at": now(),
                     "name_zh_identity": {"qid": "Q28967995", "method": "wikidata_existing_identity_check",
                         "name": row["name"], "team_id": row["team_id"], "provider_id": "1", "roster_birth_date": "",
                         "label": "埃尔林·哈兰", "language": "zh-hans", "identity_checked": True,
                         "unique_article": True, "public": True}}
        result = player_names.with_reading(row, source_reading=candidate)
        self.assertEqual(result["name_zh"], "哈兰德")
        self.assertNotEqual(result["name_zh_source"], "Wikidata 中文标签")
        self.assertNotIn("name_zh_identity", result)

    def test_an_unknown_name_returns_nothing_rather_than_a_guess(self):
        # Absence is a real answer: the reader keeps the source spelling. A
        # transliteration rule would invent 萨蒙 for a youth player nobody has
        # written about yet, and the reader could not tell it was invented.
        for name in ("Marli Salmon", "Ife Ibrahim", "Some Unlisted Player", ""):
            self.assertEqual(player_names.reading_for(name), "")

    def test_with_reading_adds_the_field_and_keeps_the_source_spelling(self):
        row = player_names.with_reading({"name": "Erling Haaland"})
        self.assertEqual(row["name"], "Erling Haaland")
        self.assertEqual(row["name_zh"], "哈兰德")
        self.assertTrue(row["name_zh_source_url"])

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

    def test_checked_city_names_and_fernandes_spelling_keep_reading_evidence(self):
        for name, reading in (("Erling Haaland", "哈兰德"), ("Rúben Dias", "鲁本·迪亚斯"),
                              ("Nico O'Reilly", "奥赖利"), ("Mateus Fernandes", "马特乌斯·费尔南德斯")):
            with self.subTest(name=name):
                row = player_names.with_reading({"name": name})
                self.assertEqual(row["name_zh"], reading)
                self.assertEqual(row["name"], name)
                self.assertTrue(row["name_zh_source"])
                self.assertTrue(row["name_zh_source_url"].startswith("https://"))

    def test_unknown_name_cannot_inherit_previous_reading_evidence(self):
        row = player_names.with_reading({"name": "Unlisted Youth", "name_zh": "旧译名",
                                        "name_zh_source_url": "https://example.com/other-person",
                                        "name_zh_source": "旧来源", "name_zh_checked_at": "2020-01-01"})
        self.assertFalse(any(key.startswith("name_zh") for key in row))

    def test_official_directory_can_correct_an_old_spelling_and_add_reviewed_youth_names(self):
        for name, reading in (("Joan García", "乔安·加西亚"), ("Roony Bardghji", "巴尔德吉"),
                              ("Xavi Espart", "埃斯帕特"), ("Jesse Bisiwu", "比西乌")):
            row = player_names.with_reading({"name": name, "name_zh": "旧译名"})
            self.assertEqual(row["name_zh"], reading)
            self.assertEqual(row["name_zh_source_url"], "https://www.fcbarcelona.cn/players")
        self.assertEqual(player_names.reading_for("Josh Acheampong"), "阿昌庞")

    def test_reviewed_chelsea_youth_profiles_have_exact_name_readings_and_their_own_sources(self):
        for name, reading, slug in (("Teddy Sharman-Lowe", "沙曼-洛维", "taidishamanluowei"),
                                   ("Pep Chavarría", "查瓦利亚", "peipuzhawaliya"),
                                   ("Aaron Anselmino", "安塞尔米诺", "yalunansaierminuo"),
                                   ("Olutayo Subuloye", "苏布洛耶", "aolutayuesubuluoye"),
                                   ("Mahdi Nicoll-Jazuli", "贾祖里", "mahedinikeerjiazuli"),
                                   ("Reggie Watson", "沃森", "leijiwosen")):
            row = player_names.with_reading({"name": name})
            self.assertEqual(row["name_zh"], reading)
            self.assertEqual(row["name_zh_source_url"], "https://www.qiumiwu.com/player/" + slug)
            self.assertEqual(row["name"], name)


if __name__ == "__main__":
    unittest.main()

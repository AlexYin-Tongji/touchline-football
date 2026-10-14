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

    def test_a_reading_without_a_citation_is_still_shown_but_adds_no_source(self):
        # The table holds two kinds of entry: readings checked against a
        # publisher, and readings written down without a citation. Both are
        # readings and both must reach the reader — presenting an English
        # spelling instead helps nobody. Only the citation differs.
        for name in ("Thibaut Courtois", "Jude Bellingham"):
            self.assertTrue(player_names.reading_for(name))
            self.assertIsNone(player_names.verified_evidence(name))
            row = player_names.with_reading({"name": name, "name_zh": "旧译名"})
            self.assertEqual(row["name_zh"], player_names.reading_for(name))
            self.assertEqual(row["name"], name)
            self.assertFalse(any(key.startswith("name_zh") for key in row if key != "name_zh"),
                             "an uncited reading must not claim a source")

    def test_a_reading_with_a_citation_still_carries_its_source(self):
        self.assertIsNotNone(player_names.verified_evidence("Erling Haaland"))
        row = player_names.with_reading({"name": "Harry Kane"})
        self.assertTrue(row["name_zh"])
        self.assertEqual(row["name"], "Harry Kane")

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


class TraditionalLabelTests(unittest.TestCase):
    """A ``zh`` label may be Traditional; only the map decides what may change."""

    def row(self, **overrides):
        row = {"name": "Michael Olise", "team_id": "bayern", "provider_id": "9"}
        row.update(overrides)
        return row

    def candidate(self, row, label, published=None, language="zh"):
        return {"name_zh": label, "name_zh_source": "Wikidata 中文标签",
                "name_zh_source_url": "https://www.wikidata.org/wiki/Q62050484",
                "name_zh_checked_at": now(),
                "name_zh_identity": {"qid": "Q62050484", "method": "wikidata_existing_identity_check",
                    "name": row["name"], "team_id": row["team_id"], "provider_id": row["provider_id"],
                    "roster_birth_date": "", "label": published if published is not None else label,
                    "language": language, "identity_checked": True, "unique_article": True, "public": True}}

    def test_a_traditional_label_is_normalised_to_simplified(self):
        # The real label on Q62050484 is 米高·奧利斯: 米高 is the Hong Kong reading
        # and is not an error, so the only thing that may change is the script.
        self.assertEqual(player_names.to_simplified("米高·奧利斯"), "米高·奥利斯")
        self.assertEqual(player_names.to_simplified("孔拉德·萊梅爾"), "孔拉德·莱梅尔")

    def test_an_already_simplified_label_passes_through_unchanged(self):
        for label in ("贾迈勒·穆西亚拉", "马丁·厄德高", "米高·奥利斯"):
            self.assertEqual(player_names.to_simplified(label), label)
        self.assertEqual(player_names.to_simplified(""), "")

    def test_characters_valid_in_both_scripts_are_never_rewritten(self):
        # 著/裡/復 are the same character in modern simplified prose; converting
        # them would corrupt a name rather than normalise it.
        for char in ("著", "裡", "復", "乾"):
            self.assertNotIn(char, player_names._TRADITIONAL_TO_SIMPLIFIED)

    def test_a_converted_reading_from_a_traditional_published_label_verifies(self):
        # The row carries the simplified reading; the proof carries what Wikidata
        # published. Re-running the map must reproduce the stored reading.
        row = self.row()
        candidate = self.candidate(row, "米高·奥利斯", published="米高·奧利斯")
        checked = player_names.checked_wikidata_reading(candidate, row, team_id="bayern")
        self.assertIsNotNone(checked)
        self.assertEqual(checked["name_zh"], "米高·奥利斯")

    def test_a_reading_the_published_label_cannot_produce_is_refused(self):
        # A reading that claims to be the simplification of a published form must
        # actually be one: 奧利斯 does not reduce to 索利斯.
        row = self.row()
        candidate = self.candidate(row, "米高·索利斯", published="米高·奧利斯")
        self.assertIsNone(player_names.checked_wikidata_reading(candidate, row, team_id="bayern"))

    def test_a_reading_that_keeps_a_resolvable_traditional_character_is_refused(self):
        # Storing 萊梅爾 while the map resolves 萊→莱 and 爾→尔 would publish
        # 繁體 under a simplified heading; the pair must agree or be dropped.
        row = self.row()
        candidate = self.candidate(row, "孔拉德·萊梅爾", published="孔拉德·萊梅爾")
        self.assertIsNone(player_names.checked_wikidata_reading(candidate, row, team_id="bayern"))

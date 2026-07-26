import unittest

from app import analyzer, growth


class ModelOutputValidationCase(unittest.TestCase):
    def test_asset_scores_and_labels_are_bounded_before_persistence(self):
        meta = analyzer.normalize_meta({
            "category": "<img src=x onerror=alert(1)>" * 10,
            "keywords": ["<svg/onload=alert(1)>", {"bad": True}] * 10,
            "quality": "\"><script>alert(1)</script>",
            "match": 9999,
            "reuse": -40,
            "extra": "must not persist",
        })
        self.assertEqual(0, meta["quality"])
        self.assertEqual(100, meta["match"])
        self.assertEqual(0, meta["reuse"])
        self.assertLessEqual(len(meta["category"]), 30)
        self.assertLessEqual(len(meta["keywords"]), 6)
        self.assertNotIn("extra", meta)

    def test_calendar_recomputes_identity_fields_and_rejects_missing_days(self):
        rows = [{
            "d": day,
            "weekday": "\"><img src=x onerror=alert(1)>",
            "festival": "<script>alert(1)</script>",
            "moment": f"第{day}天",
            "group": "",
        } for day in range(1, 32)]
        clean = growth._normalize_calendar(
            {"days": rows, "tips": "运营建议"}, 2026, 7, 31
        )
        self.assertEqual(list(range(1, 32)), [row["d"] for row in clean["days"]])
        self.assertEqual("周三", clean["days"][0]["weekday"])
        self.assertEqual("", clean["days"][0]["festival"])
        self.assertNotIn("<", clean["days"][0]["weekday"])

        with self.assertRaisesRegex(ValueError, "缺少有效日期"):
            growth._normalize_calendar(
                {"days": rows[:-1] + [{"d": "1\"><img onerror=alert(1)>"}]},
                2026,
                7,
                31,
            )


if __name__ == "__main__":
    unittest.main()

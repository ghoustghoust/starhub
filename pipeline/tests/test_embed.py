from __future__ import annotations

import unittest

from starhub_pipeline.enrich import embed


class EmbedTest(unittest.TestCase):
    def test_quantize_normalized(self) -> None:
        vector = embed.quantize_normalized([3.0, 4.0])
        self.assertEqual(vector, [76, 102])
        self.assertTrue(all(-127 <= value <= 127 for value in vector))

    def test_fast_mode_keeps_entries_untouched(self) -> None:
        entries = [{"full_name": "owner/repo"}]
        self.assertEqual(embed.embed_star_entries(entries, fast=True), entries)
        self.assertNotIn("emb", entries[0])

    def test_missing_key_keeps_entries(self) -> None:
        entries = [{"full_name": "owner/repo"}]
        self.assertIsNone(embed.embed_texts(["hello"], api_key=""))
        self.assertEqual(embed.embed_star_entries(entries, api_key=""), entries)
        self.assertNotIn("emb", entries[0])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from starhub_pipeline.build import attention


class AttentionTest(unittest.TestCase):
    def test_prune_keeps_map_when_fetch_is_short(self) -> None:
        known = {f"owner/repo{i}": "tools" for i in range(10)}
        self.assertEqual(attention.prune_star_state(known, ["owner/repo1"]), [])
        self.assertEqual(len(known), 10)

    def test_prune_removes_old_names(self) -> None:
        known = {f"owner/repo{i}": "tools" for i in range(10)}
        live = [f"owner/repo{i}" for i in range(9)]
        dropped = attention.prune_star_state(known, live)
        self.assertEqual(dropped, ["owner/repo9"])
        self.assertNotIn("owner/repo9", known)

    def test_attention_only_lists_orphans(self) -> None:
        items = attention.star_attention_items(
            [{"full_name": "owner/live"}], {"owner/live": "tools", "owner/old": "agent"}
        )
        self.assertEqual([item["item"] for item in items], ["owner/old"])

    def test_render_empty(self) -> None:
        self.assertEqual(attention.render_star_attention([]), "")
        self.assertIn("attn", attention.render_star_attention(
            [{"item": "<x>", "hint": {"category": "tools"}}]
        ))


if __name__ == "__main__":
    unittest.main()

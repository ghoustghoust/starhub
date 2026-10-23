from __future__ import annotations

import unittest

from starhub_pipeline.build import stars


class StarsTest(unittest.TestCase):
    def test_health_recent_and_archived(self) -> None:
        self.assertEqual(
            stars.health_score({
                "pushed_at": "2099-01-01T00:00:00Z",
                "stargazers_count": 100,
                "forks": 10,
                "description": "x",
                "topics": ["video"],
            })["tier"],
            "green",
        )
        archived = stars.health_score({"pushed_at": "2026-01-01T00:00:00Z", "archived": True})
        self.assertEqual(archived["tier"], "red")

    def test_assemble_entries_fast(self) -> None:
        repo = {
            "full_name": "owner/video-tool",
            "name": "video-tool",
            "html_url": "https://github.com/owner/video-tool",
            "description": "视频生成工具",
            "language": "Python",
            "stargazers_count": 123,
            "forks": 4,
            "topics": ["video"],
            "pushed_at": "2026-10-22T00:00:00Z",
        }
        known = {}
        result = stars.assemble_entries(
            [repo], known, {}, {"video": "AI 视频创作"}, None, cats=[{"key": "video", "label": "AI 视频创作"}], fast=True
        )
        self.assertEqual(result[0]["full_name"], "owner/video-tool")
        self.assertEqual(result[0]["category"], "video")
        self.assertEqual(known["owner/video-tool"], "video")


if __name__ == "__main__":
    unittest.main()

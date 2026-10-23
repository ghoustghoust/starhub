from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from starhub_pipeline.render.index import build_index_html


class RenderTest(unittest.TestCase):
    def test_replaces_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template.html"
            template.write_text(
                "<script>const CATS=__CATS__;const L=__LANGS__;const F=__FAVS__;"
                "const A='__API_BASE__';const V='__ASSET_VERSION__';const T=__TRENDING__;"
                "const FEED=__FEED__;const R='__REPO_URL__';</script>__ATTENTION__ __UPDATED__ __AI_SUMMARY__",
                encoding="utf-8",
            )
            html = build_index_html(
                [{"key": "tools", "label": "</script>"}],
                updated="2026-10-23 10:00",
                template_path=template,
                api_base="https://api.example.com",
            )
            self.assertIn("https://api.example.com", html)
            self.assertIn("https://github.com/ghoustghoust/starhub", html)
            self.assertIn("202610231000", html)
            self.assertIn("\\u003c/script", html)
            self.assertNotIn("__CATS__", html)


if __name__ == "__main__":
    unittest.main()

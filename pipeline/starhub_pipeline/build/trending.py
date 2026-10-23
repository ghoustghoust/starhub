"""Trending board payload shared by the build and the page."""

TRENDING_BOARD = "trending_board.json"


def trending_board_payload(trending, ai_summary_html, updated):
    return {
        "trending": trending or {},
        "ai_summary_html": ai_summary_html or "",
        "updated": updated or "",
    }

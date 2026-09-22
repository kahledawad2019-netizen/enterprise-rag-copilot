"""
Streamlit smoke tests.

These use `streamlit.testing.v1.AppTest`, which runs a page's script in-process
without a browser. They are smoke tests by design: the aim is to catch a page
that raises on load, an import that broke during a refactor, or a secret that
crept into the rendered output — not to assert on layout.

`AppTest` opens the embedded Qdrant index, which is single-process, so these
tests must not run while the Streamlit app or an ingestion job is running.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"

pytest.importorskip("streamlit.testing.v1", reason="Streamlit AppTest is unavailable")

from streamlit.testing.v1 import AppTest  # noqa: E402

# The pages directory must be importable, because each page imports helpers
# from streamlit_app.
sys.path.insert(0, str(APP_DIR))

PAGES = [
    "streamlit_app.py",
    "pages/2_Document_Explorer.py",
    "pages/5_Evaluation.py",
    "pages/7_System_Configuration.py",
]

# Pages that need a live index or database are exercised separately and marked
# integration, so a machine without them still gets useful coverage.
SERVICE_PAGES = [
    "pages/1_Copilot_Chat.py",
    "pages/3_Retrieval_Debugger.py",
    "pages/4_SQL_Analytics.py",
    "pages/6_Traces.py",
]


def run_page(relative: str, timeout: int = 180) -> AppTest:
    app = AppTest.from_file(str(APP_DIR / relative), default_timeout=timeout)
    app.run()
    return app


class TestPagesLoad:
    @pytest.mark.parametrize("page", PAGES)
    def test_page_renders_without_raising(self, page: str) -> None:
        app = run_page(page)
        assert not app.exception, (
            f"{page} raised on load: "
            f"{[str(e.value)[:300] for e in app.exception]}"
        )

    @pytest.mark.integration
    @pytest.mark.parametrize("page", SERVICE_PAGES)
    def test_service_page_renders(self, page: str) -> None:
        try:
            app = run_page(page)
        except Exception as exc:
            pytest.skip(f"page needs a service that is unavailable: {exc}")
        assert not app.exception, (
            f"{page} raised on load: "
            f"{[str(e.value)[:300] for e in app.exception]}"
        )


class TestNoSecretsRendered:
    """A page must never render a credential, however convenient it would be."""

    def _rendered_text(self, app: AppTest) -> str:
        parts: list[str] = []
        for collection in (app.markdown, app.caption, app.code, app.text, app.json):
            for element in collection:
                parts.append(str(getattr(element, "value", "")))
        return " ".join(parts)

    def test_configuration_page_masks_the_password(self) -> None:
        app = run_page("pages/7_System_Configuration.py")
        assert not app.exception
        rendered = self._rendered_text(app)

        # The connection string is shown, but never with a live password.
        assert "PWD=" not in rendered or "********" in rendered
        for forbidden in ("SuperSecret", "hunter2", "ChangeMe_Str0ng"):
            assert forbidden not in rendered

    def test_home_page_does_not_render_a_connection_string(self) -> None:
        app = run_page("streamlit_app.py")
        assert not app.exception
        rendered = self._rendered_text(app)
        assert "PWD=" not in rendered
        assert "Trusted_Connection" not in rendered


class TestHomePageContent:
    def test_shows_the_project_title(self) -> None:
        app = run_page("streamlit_app.py")
        titles = " ".join(str(t.value) for t in app.title)
        assert "Copilot" in titles

    def test_offers_example_questions(self) -> None:
        app = run_page("streamlit_app.py")
        rendered = " ".join(str(m.value) for m in app.markdown)
        assert "refund policy" in rendered.lower()

    def test_sidebar_exposes_the_user_switcher(self) -> None:
        """Switching user is how the permission model is demonstrated."""
        app = run_page("streamlit_app.py")
        labels = [s.label for s in app.selectbox]
        assert "User" in labels

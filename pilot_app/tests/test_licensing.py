"""Licensing is a real invariant, not decoration.

The project is AGPL-3.0 and the copyright notice is what makes the licence
attributable to a person rather than to nobody. It is also easy to lose by
accident: the notice lives in a docstring and a markdown file, neither of which
anything else would notice disappearing.
"""

from __future__ import annotations

import os
import pathlib
import unittest
from unittest import mock

import pilot_app
from pilot_app import web

ROOT = pathlib.Path(pilot_app.__file__).resolve().parent.parent
HOLDER = "余剑篪"


class LicenceTests(unittest.TestCase):
    def test_the_agpl_text_is_present_and_complete(self):
        licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
        for marker in ("GNU AFFERO GENERAL PUBLIC LICENSE",
                       "Version 3, 19 November 2007",
                       "TERMS AND CONDITIONS",
                       "END OF TERMS AND CONDITIONS"):
            self.assertIn(marker, licence)
        # Sanity-check the length so a truncated download cannot pass by
        # containing the right headings.
        self.assertGreater(len(licence), 30000)

    def test_the_copyright_holder_is_named_in_the_package(self):
        self.assertIn(HOLDER, pilot_app.__doc__ or "")
        self.assertIn("GNU Affero General Public License", pilot_app.__doc__ or "")

    def test_the_copyright_holder_is_named_in_the_readme(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(HOLDER, readme)
        self.assertIn("AGPL-3.0", readme)

    def test_the_release_package_ships_the_licence(self):
        """Shipping the code without its licence is the one thing AGPL is
        explicit about, and the first version of the build script omitted both
        LICENSE and the README."""
        script = (ROOT / "pilot_app" / "build_release.sh").read_text(encoding="utf-8")
        self.assertIn("LICENSE", script)
        self.assertIn("README.md", script)


if __name__ == "__main__":
    unittest.main()


class SourceOfferTests(unittest.TestCase):
    """AGPL-3.0 section 13: a network service must offer its own source.

    That obligation is easy to satisfy on paper and forget in practice, because
    it only exists when somebody else is using the thing. So the offer is a
    footer link on the page users actually see, driven by a per-installation
    setting -- and, like every other promise in this project, it has a test.
    """

    def setUp(self):
        self.saved = os.environ.get("INFE_PILOT_SOURCE_URL")
        os.environ.pop("INFE_PILOT_SOURCE_URL", None)

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("INFE_PILOT_SOURCE_URL", None)
        else:
            os.environ["INFE_PILOT_SOURCE_URL"] = self.saved

    def test_no_repository_configured_renders_nothing(self):
        self.assertEqual(web.source_url(), "")
        self.assertEqual(web.render_source_link(), "")

    def test_a_configured_repository_becomes_a_link(self):
        os.environ["INFE_PILOT_SOURCE_URL"] = "https://github.com/example/cityu-mail-pilot"
        link = web.render_source_link()
        self.assertIn('href="https://github.com/example/cityu-mail-pilot"', link)
        self.assertIn("AGPL-3.0", link)
        self.assertIn('rel="noopener"', link)

    def test_a_non_http_value_is_refused_rather_than_rendered(self):
        """A `javascript:` value would run in every visitor's browser."""
        for bad in ("javascript:alert(1)", "data:text/html,x", "github.com/x", ""):
            os.environ["INFE_PILOT_SOURCE_URL"] = bad
            self.assertEqual(web.source_url(), "", bad)
            self.assertEqual(web.render_source_link(), "", bad)

    def test_the_landing_page_carries_the_link_when_configured(self):
        """Rendered through the real function, with a stub database.

        The landing page reads the live pilot count, so rendering it needs
        storage. Stubbing `get_db` keeps this test about the link instead of
        about where the database lives -- and it means the assertion holds on a
        machine where the service is not installed.
        """
        os.environ["INFE_PILOT_SOURCE_URL"] = "https://github.com/example/cityu-mail-pilot"

        class StubDatabase:
            @staticmethod
            def landing_user_count():
                return 1

            @staticmethod
            def public_announcements(limit):
                return []

        with mock.patch.object(web, "get_db", return_value=StubDatabase()):
            page = web.render_landing_page(
                ROOT / "pilot_app" / "static" / "landing.html").decode("utf-8")
        self.assertIn("github.com/example/cityu-mail-pilot", page)
        self.assertNotIn("{{SOURCE_LINK}}", page, "占位符必须被替换掉")

    def test_the_landing_page_renders_without_a_repository_configured(self):
        class StubDatabase:
            @staticmethod
            def landing_user_count():
                return 2

            @staticmethod
            def public_announcements(limit):
                return []

        with mock.patch.object(web, "get_db", return_value=StubDatabase()):
            page = web.render_landing_page(
                ROOT / "pilot_app" / "static" / "landing.html").decode("utf-8")
        self.assertNotIn("{{SOURCE_LINK}}", page)
        self.assertNotIn("源代码", page)

    def test_the_shell_has_somewhere_to_put_the_link(self):
        """The app is a static file, so the footer slot must exist for app.js."""
        shell = (ROOT / "pilot_app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="source-link"', shell)
        script = (ROOT / "pilot_app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("renderSourceLink", script)

"""The app itself, built and driven.

Run under the WASM port by `node tools/wasm/run.mjs`. app.py reaches the firmware at
import, and `socket`, `select`, `wifi` and `secrets` come from tools/wasm/shims: nothing
here touches a network, and anything that tried would raise.
"""

import unittest

import app


class Entry(unittest.TestCase):
    def test_importing_the_app_does_not_start_it(self):
        """__init__.py calls main(), so importing app.py starts nothing."""
        self.assertTrue(callable(app.main), "no main() to call")
        self.assertIsNone(app._app, "main() ran at import")


class Built(unittest.TestCase):
    def setUp(self):
        self.app = app.App()

    def test_a_badge_with_no_host_starts_on_the_first_page(self):
        self.assertEqual(self.app.page_index, 0)
        self.assertIsNone(self.app.layout, "a badge with no host has a layout")
        self.assertEqual(self.app.layout_rev, app.NO_REV)
        self.assertTrue(self.app.dirty, "the first frame would not be drawn")


if __name__ == "__main__":
    unittest.main()

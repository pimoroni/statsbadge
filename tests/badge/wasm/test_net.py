"""The badge's HTTP client, against a real statsbadge.

Run under the WASM port by `node tools/wasm/run.mjs`, which starts the same server
tests/test_server.py drives and pairs one badge with it. The socket is node's, through
tools/wasm/shims - the requests, the signing and the parsing are the badge's own.

`step()` is advanced from the draw loop a slice at a time, so these drive it the way a
frame would: call it until the request comes back done.
"""

import json
import unittest

import hostinfo
import net

STEP_LIMIT = 400

# One server for the run, rejecting a counter it has already seen. Each client
# starts well above the last, the way a badge that has been away comes back.
_from = [1000]


def client(secret=None):
    """A Client pointed at the runner's server, paired the way an enrolment leaves it."""
    if hostinfo.HOST is None:
        return None
    _from[0] += 100
    config = net.Config()
    config.badge_id = hostinfo.HOST["badge_id"]
    config.remember("wasm", "127.0.0.1", hostinfo.HOST["port"],
                    secret or hostinfo.HOST["secret"], name="the runner", seq=_from[0])
    return net.Client(config)


class Requests(unittest.TestCase):
    def setUp(self):
        self.client = client()
        if self.client is None:
            self.skipTest("no host was served")

    def tearDown(self):
        self.client.close()

    def finish(self):
        """Advance the request until it is done, as the draw loop would."""
        for _ in range(STEP_LIMIT):
            if self.client.step():
                return True
        return False

    def test_a_signed_request_is_answered(self):
        self.client.get("/v1/stats")
        self.assertTrue(self.finish(), "the request never finished")
        self.assertEqual(self.client.status, net.DONE, self.client.error)
        self.assertEqual(self.client.http_status, 200)
        frame = json.loads(self.client.body)
        self.assertTrue("cpu" in frame, sorted(frame))

    def test_the_connection_is_kept_for_the_next_one(self):
        """A poll a second on a new connection each time is a handshake a second."""
        self.client.get("/v1/stats")
        self.finish()
        held = self.client.sock
        self.client.get("/v1/layout")
        self.assertTrue(self.finish(), "the second request never finished")
        self.assertEqual(self.client.http_status, 200)
        self.assertTrue(self.client.sock is held, "it reconnected for the second request")

    def test_the_counter_goes_up_and_the_host_takes_it(self):
        for _ in range(3):
            self.client.get("/v1/stats")
            self.assertTrue(self.finish())
            self.assertEqual(self.client.http_status, 200, self.client.body)

    def test_a_replayed_counter_is_refused(self):
        """The host rejects a counter it has seen, which is the whole point of signing."""
        self.client.get("/v1/stats")
        self.finish()
        self.client.config.seq = self.client.config.seq - 1
        self.client.get("/v1/stats")
        self.assertTrue(self.finish())
        self.assertEqual(self.client.http_status, 401, self.client.body)
        self.assertTrue(b"replay" in self.client.body, self.client.body)

    def test_a_wrong_secret_is_refused(self):
        self.client.close()
        self.client = client(secret="0" * 64)
        self.client.get("/v1/stats")
        self.assertTrue(self.finish())
        self.assertEqual(self.client.http_status, 401)

    def test_a_layout_comes_back_as_the_badge_reads_it(self):
        self.client.get("/v1/layout")
        self.assertTrue(self.finish())
        layout = json.loads(self.client.body)
        self.assertTrue(layout.get("pages"), sorted(layout))
        self.assertTrue("theme" in layout and "palette" in layout, sorted(layout))

    def test_a_command_nobody_bound_is_refused(self):
        """The POST path, end to end, without running anything on the machine: the
        runner binds no buttons, and the host only runs what a layout bound.
        """
        self.client.post("/v1/command", {"cmd": "media_next"})
        self.assertTrue(self.finish())
        self.assertEqual(self.client.http_status, 403, self.client.body)
        self.assertTrue(b"not bound" in self.client.body, self.client.body)


class NoHostThere(unittest.TestCase):
    """A port nothing is listening on, which is a PC that went to sleep."""

    def setUp(self):
        if hostinfo.HOST is None:
            self.skipTest("no host was served")
        config = net.Config()
        config.badge_id = hostinfo.HOST["badge_id"]
        # One above the runner's, where nothing is bound.
        config.remember("gone", "127.0.0.1", hostinfo.HOST["port"] + 1, "s" * 64)
        self.client = net.Client(config)

    def tearDown(self):
        self.client.close()

    def test_the_draw_loop_is_never_held(self):
        """A blocking connect sits in the handshake until lwIP gives up, with the screen
        inside that call."""
        self.client.get("/v1/stats")
        steps = 0
        while steps < STEP_LIMIT and not self.client.step():
            steps += 1
        self.assertTrue(steps > 0, "the whole connect happened inside one step")
        self.assertEqual(self.client.status, net.FAILED, self.client.status)
        self.assertTrue(self.client.error, "nothing to show on the notice screen")


if __name__ == "__main__":
    unittest.main()

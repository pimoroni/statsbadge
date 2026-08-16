"""Starting up: the trust store a packaged app has to be given."""

import os
import sys


def test_a_bundle_with_no_trust_store_is_given_one():
    """A process that loads no roots is pointed at certifi's bundle."""
    # Inside a packaged app there are no roots at all, so every HTTPS request an extension
    # makes fails to find an issuer.
    import ssl

    from statsbadge import __main__ as cli

    class Store:
        """A context with as many roots as it is told: the count this host happens to
        have is not the thing under test, and uv's Python on Linux loads none."""

        def __init__(self, roots):
            self.roots = roots

        def cert_store_stats(self):
            return {"x509": self.roots, "crl": 0, "x509_ca": self.roots}

    fake = type(sys)("certifi")
    fake.where = lambda: os.path.join("nowhere", "cacert.pem")

    was_paths = ssl.get_default_verify_paths
    was_context, was_certifi = ssl.create_default_context, sys.modules.get("certifi")
    was_file, was_dir = os.environ.pop("SSL_CERT_FILE", None), os.environ.pop("SSL_CERT_DIR", None)
    try:
        nowhere = ssl.DefaultVerifyPaths(None, None, "", None, "", None)
        ssl.get_default_verify_paths = lambda: nowhere
        ssl.create_default_context = lambda *_args, **_kwargs: Store(0)
        sys.modules["certifi"] = fake
        assert cli.trust_store() == fake.where()
        assert os.environ["SSL_CERT_FILE"] == fake.where()

        # Asked again with one already set, it leaves it alone.
        os.environ["SSL_CERT_FILE"] = "somewhere/else.pem"
        assert cli.trust_store() is None

        # A machine with roots of its own is not touched, wherever it keeps them:
        # Windows names no file at all and loads them from the system store.
        del os.environ["SSL_CERT_FILE"]
        ssl.create_default_context = lambda *_args, **_kwargs: Store(128)
        assert cli.trust_store() is None
        assert "SSL_CERT_FILE" not in os.environ

        # And Linux, which names a directory and loads nothing from it: a directory is
        # searched per verification, so the count implies nothing about the contents.
        ssl.create_default_context = lambda *_args, **_kwargs: Store(0)
        listed = ssl.DefaultVerifyPaths(None, os.path.dirname(__file__), "", None, "", None)
        ssl.get_default_verify_paths = lambda: listed
        assert cli.trust_store() is None
        assert "SSL_CERT_FILE" not in os.environ
    finally:
        ssl.create_default_context, ssl.get_default_verify_paths = was_context, was_paths
        if was_certifi is None:
            sys.modules.pop("certifi", None)
        else:
            sys.modules["certifi"] = was_certifi
        for key, value in (("SSL_CERT_FILE", was_file), ("SSL_CERT_DIR", was_dir)):
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

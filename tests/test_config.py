import unittest
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from dreamz_portal import normalize_database_uri


class ConfigTests(unittest.TestCase):
    def test_normalize_empty_database_uri_uses_default_sqlite_path(self):
        self.assertEqual(
            normalize_database_uri(None, "C:/app/instance/app.db"),
            "sqlite:///C:/app/instance/app.db",
        )

    def test_normalize_relative_sqlite_database_uri(self):
        self.assertEqual(
            normalize_database_uri("sqlite:///instance/app.db", "C:/ignored/app.db"),
            f"sqlite:///{os.path.abspath('instance/app.db').replace('\\', '/')}",
        )

    def test_normalize_keeps_memory_and_non_sqlite_uris(self):
        self.assertEqual(normalize_database_uri("sqlite:///:memory:", "C:/app.db"), "sqlite:///:memory:")
        self.assertEqual(
            normalize_database_uri("postgresql://user:pass@db/app", "C:/app.db"),
            "postgresql://user:pass@db/app",
        )


if __name__ == "__main__":
    unittest.main()

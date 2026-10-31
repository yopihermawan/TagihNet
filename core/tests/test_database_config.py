import tempfile
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from config.database import database_config

VALUES = {"TAGIHNET_DB_ENGINE": "postgresql", "TAGIHNET_DB_NAME": "tagihnet_test",
    "TAGIHNET_DB_USER": "tagihnet_app", "TAGIHNET_DB_HOST": "db.example.invalid",
    "TAGIHNET_DB_PASSWORD": "Fictitious-test-password"}


class DatabaseConfigurationTests(SimpleTestCase):
    def test_default_is_sqlite_and_does_not_select_postgres_from_unrelated_values(self):
        for env in [{}, {"TAGIHNET_DB_HOST": "unrelated.example.invalid"}]:
            config = database_config(env, Path("/tmp/example"), True)
            self.assertEqual(config["ENGINE"], "django.db.backends.sqlite3")
            self.assertEqual(config["NAME"], Path("/tmp/example/tagihnet.sqlite3"))

    def test_postgres_defaults_verify_full_and_bounded_connection_and_query_timeouts(self):
        config = database_config(VALUES, Path("/tmp/example"), False)
        self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(config["OPTIONS"]["sslmode"], "verify-full")
        self.assertEqual(config["OPTIONS"]["connect_timeout"], 5)
        self.assertIn("lock_timeout=5000", config["OPTIONS"]["options"])
        self.assertIn("statement_timeout=30000", config["OPTIONS"]["options"])

    def test_unknown_engine_is_an_error_without_silent_sqlite_fallback(self):
        for engine in ["postgres", "mysql", "", "unknown"]:
            with self.assertRaises(ImproperlyConfigured):
                database_config({"TAGIHNET_DB_ENGINE": engine}, Path("/tmp"), True)

    def test_missing_required_postgres_settings_do_not_fall_back(self):
        for field in ["NAME", "USER", "HOST", "PASSWORD"]:
            env = {key: value for key, value in VALUES.items() if key != "TAGIHNET_DB_"+field}
            with self.assertRaises(ImproperlyConfigured):
                database_config(env, Path("/tmp"), True)

    def test_name_user_and_port_validation(self):
        for key, value in [("NAME", "database;query"), ("USER", ""), ("USER", "x"*64),
                ("PORT", "0"), ("PORT", "65536"), ("PORT", "not-number")]:
            with self.assertRaises(ImproperlyConfigured):
                database_config({**VALUES, "TAGIHNET_DB_"+key: value}, Path("/tmp"), True)

    def test_ssl_can_only_be_disabled_explicitly_for_debug_lab(self):
        env = {**VALUES, "TAGIHNET_DB_SSLMODE": "disable"}
        self.assertEqual(database_config(env, Path("/tmp"), True)["OPTIONS"]["sslmode"], "disable")
        with self.assertRaises(ImproperlyConfigured):
            database_config(env, Path("/tmp"), False)
        for mode in ["prefer", "require", "allow", "invalid"]:
            with self.assertRaises(ImproperlyConfigured):
                database_config({**VALUES, "TAGIHNET_DB_SSLMODE": mode}, Path("/tmp"), True)

    def test_private_password_file_and_root_certificate_selector(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"password"
            path.write_text("Fictitious-file-password\n")
            env = {**VALUES, "TAGIHNET_DB_PASSWORD": "", "TAGIHNET_DB_PASSWORD_FILE": str(path),
                "TAGIHNET_DB_SSLROOTCERT": "/private/root-ca.pem"}
            config = database_config(env, Path("/tmp"), False)
            self.assertEqual(config["PASSWORD"], "Fictitious-file-password")
            self.assertEqual(config["OPTIONS"]["sslrootcert"], "/private/root-ca.pem")

    def test_ambiguous_missing_and_empty_password_file_errors_do_not_include_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"password"
            path.write_text("")
            for env in [{**VALUES, "TAGIHNET_DB_PASSWORD_FILE": str(path)},
                    {**VALUES, "TAGIHNET_DB_PASSWORD": "", "TAGIHNET_DB_PASSWORD_FILE": str(path)},
                    {**VALUES, "TAGIHNET_DB_PASSWORD": "", "TAGIHNET_DB_PASSWORD_FILE": str(path)+"-missing"}]:
                with self.assertRaises(ImproperlyConfigured) as error:
                    database_config(env, Path("/tmp"), True)
                self.assertNotIn(VALUES["TAGIHNET_DB_PASSWORD"], str(error.exception))

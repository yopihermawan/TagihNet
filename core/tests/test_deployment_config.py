import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase

from scripts.check_deployment_config import ROOT, check
from scripts.staging_lab import prepare


class DeploymentConfigurationTests(SimpleTestCase):
    hostname = "billing.example.test"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        workspace = TemporaryDirectory()
        cls.addClassCleanup(workspace.cleanup)
        cls.directory = Path(workspace.name)
        with redirect_stdout(io.StringIO()):
            prepare(cls.directory / "certificates")
        keys = cls.directory / "certificates" / "secrets"
        cls.keys = keys
        cls.environment = {
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "TAGIHNET_DEBUG": "0",
            "TAGIHNET_SIMULATION": "0",
            "TAGIHNET_ALLOWED_HOSTS": f"{cls.hostname},localhost,[::1]",
            "TAGIHNET_DATA_DIR": str(cls.directory / "untouched-data"),
            "TAGIHNET_SECRET_KEY_FILE": str(keys / "session-key"),
            "TAGIHNET_MFA_KEY_FILE": str(keys / "mfa-key"),
            "TAGIHNET_DB_ENGINE": "postgresql",
            "TAGIHNET_DB_HOST": "db.example.test",
            "TAGIHNET_DB_NAME": "tagihnet",
            "TAGIHNET_DB_USER": "tagihnet_runtime",
            "TAGIHNET_DB_PASSWORD_FILE": str(keys / "runtime-password"),
            "TAGIHNET_DB_SSLROOTCERT": str(keys / "ca.crt"),
            "TAGIHNET_EMAIL_HOST": "smtp.example.test",
            "TAGIHNET_EMAIL_PORT": "2525",
            "TAGIHNET_EMAIL_USER": "tagihnet-reset",
            "TAGIHNET_EMAIL_PASSWORD_FILE": str(keys / "bootstrap-password"),
            "TAGIHNET_FROM_EMAIL": "TagihNet <reset@example.test>",
        }

    def test_explicit_deployment_configuration_passes_offline(self):
        with patch("django.setup") as setup, patch("socket.create_connection") as connect:
            report = check(self.environment, self.hostname)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(report["checks"].values()))
        self.assertFalse(report["production_ready"])
        self.assertFalse(Path(self.environment["TAGIHNET_DATA_DIR"]).exists())
        setup.assert_not_called()
        connect.assert_not_called()

    def test_bad_settings_are_reported_as_fixed_check_names(self):
        cases = [
            ("DJANGO_SETTINGS_MODULE", "config.staging_settings", "settings_module"),
            ("TAGIHNET_DEBUG", "1", "debug_off"),
            ("TAGIHNET_SIMULATION", "1", "simulation_off"),
            ("TAGIHNET_SECURE_COOKIES", "0", "secure_cookies"),
            ("TAGIHNET_ALLOWED_HOSTS", "*," + self.hostname, "allowed_hosts"),
            ("TAGIHNET_ALLOWED_HOSTS", self.hostname + ":443", "allowed_hosts"),
            ("TAGIHNET_ALLOWED_HOSTS", "others.example.test", "target_hostname"),
            ("TAGIHNET_SECRET_KEY", "Conflicting-private-key", "session_key"),
            ("TAGIHNET_MFA_KEY_FILE", str(self.keys / "runtime-password"), "mfa_key"),
            ("TAGIHNET_DB_ENGINE", "sqlite", "postgresql"),
            ("TAGIHNET_DB_SSLMODE", "disable", "postgresql"),
            ("TAGIHNET_DB_HOST", "/run/postgresql", "postgresql"),
            ("TAGIHNET_DB_SSLROOTCERT", str(self.keys / "missing-ca.pem"), "postgresql_ca"),
            ("TAGIHNET_DB_SSLROOTCERT", str(self.keys / "runtime-password"), "postgresql_ca"),
            ("TAGIHNET_DB_SSLROOTCERT", str(self.keys / "postgres.crt"), "postgresql_ca"),
            ("TAGIHNET_EMAIL_HOST", "https://outside.example.test", "smtp_endpoint"),
            ("TAGIHNET_EMAIL_PORT", "65536", "smtp_endpoint"),
            ("TAGIHNET_EMAIL_USER", "", "smtp_authentication"),
            ("TAGIHNET_FROM_EMAIL", "TagihNet <noreply@example.invalid>", "smtp_sender"),
            ("TAGIHNET_FROM_EMAIL", "reset@example.test\nBcc: other@example.test", "smtp_sender"),
        ]
        for variable, value, expected in cases:
            with self.subTest(variable=variable, code=expected):
                report = check({**self.environment, variable: value}, self.hostname)
                self.assertEqual(report["status"], "failed")
                self.assertIn(expected, report["failures"])
                self.assertNotIn("Conflicting-private-key", json.dumps(report))

    def test_explicit_local_smtp_relay_can_use_no_credentials(self):
        environment = {**self.environment, "TAGIHNET_EMAIL_HOST": "localhost",
                       "TAGIHNET_EMAIL_USER": "", "TAGIHNET_EMAIL_PASSWORD_FILE": ""}
        self.assertEqual(check(environment, self.hostname)["status"], "passed")

    def test_cli_status_and_stdout_keep_values_and_paths_private(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("TAGIHNET_")}
        command = [sys.executable, str(ROOT / "scripts/check_deployment_config.py"), "--hostname", self.hostname]
        for changes, status, exit_code in (({}, "passed", 0), ({"TAGIHNET_DEBUG": "1"}, "failed", 1)):
            result = subprocess.run(command, env={**env, **self.environment, **changes},
                                    capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, exit_code)
            self.assertEqual(json.loads(result.stdout)["status"], status)
            self.assertEqual(result.stderr, "")
            for value in (self.hostname, str(self.directory), "tagihnet_runtime", "smtp.example.test",
                          (self.keys / "runtime-password").read_text()):
                self.assertNotIn(value, result.stdout)
        self.assertFalse(Path(self.environment["TAGIHNET_DATA_DIR"]).exists())

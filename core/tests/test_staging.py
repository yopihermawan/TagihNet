import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from config.deployment import validate_staging
from config.secret_files import read_secret
from core.models import AuditEvent, Membership, Tenant

VALID = {"TAGIHNET_DEBUG":"0","TAGIHNET_SIMULATION":"1","TAGIHNET_DB_ENGINE":"postgresql",
         "TAGIHNET_DB_SSLMODE":"verify-full","TAGIHNET_ALLOWED_HOSTS":"localhost,stage.example.invalid",
         "TAGIHNET_DB_SSLROOTCERT":"/private/ca.crt"}


class StagingConfigurationTests(SimpleTestCase):
    def test_explicit_safe_profile(self):
        validate_staging(VALID)

    def test_debug_simulation_engine_and_tls_cannot_downgrade(self):
        for key,value in (("TAGIHNET_DEBUG","1"),("TAGIHNET_SIMULATION","0"),
                          ("TAGIHNET_DB_ENGINE","sqlite"),("TAGIHNET_DB_SSLMODE","disable")):
            for env in ({**VALID,key:value},{k:v for k,v in VALID.items() if k!=key}):
                with self.subTest(key=key),self.assertRaises(ImproperlyConfigured):
                    validate_staging(env)

    def test_wildcard_missing_url_port_and_bad_hosts_rejected(self):
        for host in ("","*",".example.com","https://example.com","example.com:443","bad_host","localhost,"," localhost","bad\nhost"):
            with self.subTest(host=host),self.assertRaises(ImproperlyConfigured):
                validate_staging({**VALID,"TAGIHNET_ALLOWED_HOSTS":host})

    def test_secure_cookies_and_ca_required(self):
        for changes in ({"TAGIHNET_SECURE_COOKIES":"0"},{"TAGIHNET_DB_SSLROOTCERT":""}):
            with self.assertRaises(ImproperlyConfigured):
                validate_staging({**VALID,**changes})

    def test_secret_value_or_file_without_environment_mutation(self):
        self.assertEqual(read_secret({},"SECRET"),"")
        self.assertEqual(read_secret({"SECRET":"value"},"SECRET"),"value")
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"secret"
            path.write_text("file-value\n")
            env={"SECRET_FILE":str(path)}
            self.assertEqual(read_secret(env,"SECRET"),"file-value")
            self.assertNotIn("SECRET",env)
            with self.assertRaises(ImproperlyConfigured):
                read_secret({**env,"SECRET":"private-value"},"SECRET")

    def test_bad_secret_files_fail_without_disclosing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"secret"
            for data in (b"",b"\xff",b"a"*4097):
                path.write_bytes(data)
                with self.assertRaises(ImproperlyConfigured) as error:
                    read_secret({"SECRET_FILE":str(path)},"SECRET")
                self.assertNotIn(str(path),str(error.exception))
            with self.assertRaises(ImproperlyConfigured):
                read_secret({"SECRET_FILE":str(path)+"-missing"},"SECRET")


class BootstrapISPTests(TestCase):
    password="Bootstrap-private-test-2026!"

    def invoke(self,**changes):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"password"
            path.write_text(changes.pop("password",self.password))
            output=StringIO()
            call_command("bootstrap_isp",isp_name="ISP Fiktif Bootstrap",username="bootstrap-owner",
                email="bootstrap@example.invalid",password_file=path,stdout=output,**changes)
            return output.getvalue()

    def test_first_owner_has_no_superuser_or_demo_accounts(self):
        output=self.invoke()
        user=get_user_model().objects.get()
        self.assertTrue(user.check_password(self.password))
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertEqual(Membership.objects.get().role,"owner")
        self.assertEqual(Tenant.objects.count(),1)
        self.assertEqual(AuditEvent.objects.get().action,"access.bootstrapped")
        self.assertNotIn(self.password,output)
        self.assertEqual(self.client.post('/login/',{'username':user.username,'password':self.password}).url,'/mfa/setup/')

    def test_repeat_refuses_without_reset_or_second_tenant(self):
        self.invoke()
        before=get_user_model().objects.get().password
        with self.assertRaises(CommandError): self.invoke(password="Another-private-test-2026!")
        self.assertEqual(Tenant.objects.count(),1)
        self.assertEqual(get_user_model().objects.get().password,before)

    def test_existing_tenant_or_user_blocks_bootstrap(self):
        Tenant.objects.create(name="Existing")
        with self.assertRaises(CommandError): self.invoke()
        self.assertFalse(get_user_model().objects.exists())
        Tenant.objects.all().delete()
        get_user_model().objects.create_user(username="existing")
        with self.assertRaises(CommandError): self.invoke()
        self.assertFalse(Tenant.objects.exists())

    def test_weak_password_rejected_atomically(self):
        with self.assertRaises(CommandError): self.invoke(password="123")
        self.assertFalse(Tenant.objects.exists())
        self.assertFalse(get_user_model().objects.exists())

    def test_audit_failure_rolls_back_bootstrap(self):
        with patch("core.management.commands.bootstrap_isp.audit",side_effect=RuntimeError("audit failed")),self.assertRaises(RuntimeError):
            self.invoke()
        self.assertFalse(Tenant.objects.exists())
        self.assertFalse(get_user_model().objects.exists())

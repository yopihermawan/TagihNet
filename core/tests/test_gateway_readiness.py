import json
from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.db.migrations.exceptions import InconsistentMigrationHistory
from django.test import TestCase, override_settings
from core.gateway import reserve_order
from core.gateway_readiness import readiness_report
from core.billing import issue_invoice
from core.models import AuditEvent, GatewayConfig, GatewayOrder, Membership, NetworkJob, Payment
from .auth_helpers import verified_login
from .test_payment_gateway import KEY, account_keys
from .test_workflow import fixtures


@override_settings(SIMULATION=True, PUBLIC_ORIGIN="https://billing.example.test", ALLOWED_HOSTS=["testserver", "billing.example.test"])
class GatewayReadinessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        GatewayConfig.objects.create(tenant=cls.tenants[0], merchant_id="G-TEST", enabled=True)

    def setUp(self):
        self.keys = override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(self.tenants[0]))
        self.keys.enable()
        self.addCleanup(self.keys.disable)
        verified_login(self.client, self.owners[0])

    def report(self, tenant=None):
        return readiness_report((tenant or self.tenants[0]).pk)

    def state(self, report, code):
        return next(check["state"] for check in report["checks"] if check["code"] == code)

    def command(self, *, success, **options):
        out, err = StringIO(), StringIO()
        if success:
            call_command("check_payment_gateway", tenant=self.tenants[0].pk, stdout=out, stderr=err, **options)
        else:
            with self.assertRaises(CommandError):
                call_command("check_payment_gateway", tenant=self.tenants[0].pk, stdout=out, stderr=err, **options)
        return out.getvalue()

    def test_complete_local_configuration_is_not_remote_verification(self):
        report = self.report()
        self.assertTrue(report["configuration_ready"])
        self.assertTrue(report["checkout_configured"])
        self.assertTrue(report["public_urls_configured"])
        self.assertEqual(report["remote_verification"], "not_performed")
        self.assertEqual(len(report["checks"]), 7)
        self.assertEqual({check["state"] for check in report["checks"]}, {"ready"})
        self.assertEqual(report["checked_at"].utcoffset(), timedelta(hours=7))

    def test_missing_keys_are_blocked_and_binding_is_unknown(self):
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS={}):
            report = self.report()
        self.assertFalse(report["configuration_ready"])
        self.assertEqual(self.state(report, "credentials"), "blocked")
        self.assertEqual(self.state(report, "merchant_binding"), "unknown")

    def test_production_keys_duplicate_merchant_or_bad_entry_do_not_echo_values(self):
        secret = "Mid-server-PRIVATE-DO-NOT-PRINT"
        variants = [
            {str(self.tenants[0].pk): {"merchant_id": "G-TEST", "server_key": secret}},
            {**account_keys(self.tenants[0]), str(self.tenants[1].pk): {"merchant_id": "G-TEST", "server_key": KEY}},
            {str(self.tenants[0].pk): "PRIVATE INVALID ENTRY"},
        ]
        for keys in variants:
            with self.subTest(keys=bool(keys)), override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=keys):
                report = self.report()
                output = self.command(success=False, as_json=True)
            self.assertEqual(self.state(report, "credentials"), "blocked")
            self.assertNotIn(secret, output)
            self.assertNotIn("PRIVATE INVALID ENTRY", output)
            self.assertNotIn(KEY, output)

    def test_mode_disabled_does_not_claim_credentials_were_checked(self):
        with override_settings(SIMULATION=False):
            report = self.report()
        self.assertEqual(self.state(report, "simulation"), "blocked")
        self.assertEqual(self.state(report, "credentials"), "unknown")
        self.assertFalse(report["checkout_configured"])

    def test_disabled_checkout_does_not_invalidate_configured_credentials(self):
        GatewayConfig.objects.update(enabled=False)
        report = self.report()
        self.assertEqual(self.state(report, "checkout"), "blocked")
        self.assertEqual(self.state(report, "credentials"), "ready")
        self.assertFalse(report["configuration_ready"])

    def test_missing_config_needs_owner_opt_in_without_writing_config(self):
        GatewayConfig.objects.all().delete()
        report = self.report()
        self.assertEqual(self.state(report, "checkout"), "blocked")
        self.assertEqual(self.state(report, "merchant_binding"), "ready")
        self.assertFalse(GatewayConfig.objects.exists())

    def test_changed_merchant_in_saved_config_blocks_readiness(self):
        GatewayConfig.objects.update(merchant_id="G-OLD")
        report = self.report()
        self.assertEqual(self.state(report, "merchant_binding"), "blocked")
        self.assertNotIn("G-OLD", json.dumps(report, default=str))
        response = self.client.post("/settings/payment-gateway/", {"enabled": "on", "version": 1, "reviewed": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.report()["configuration_ready"])

    def test_old_order_merchant_blocks_switch_even_if_saved_config_matches(self):
        invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026, 8, 1), self.owners[0])
        order = reserve_order(self.owners[0], invoice.pk)
        GatewayOrder.objects.filter(pk=order.pk).update(merchant_id="G-OLD-ORDER")
        report = self.report()
        self.assertEqual(self.state(report, "merchant_binding"), "blocked")
        self.assertFalse(report["configuration_ready"])

    def test_foreign_merchant_history_cannot_block_current_tenant(self):
        invoice, _ = issue_invoice(self.tenants[1], self.customers[1].pk, date(2026, 8, 1), self.owners[1])
        GatewayOrder.objects.create(tenant=self.tenants[1], invoice=invoice, created_by=self.owners[1], merchant_id="G-FOREIGN", amount=invoice.total)
        self.assertTrue(self.report()["configuration_ready"])

    def test_missing_origin_preserves_checkout_configured_but_not_public_urls(self):
        with override_settings(PUBLIC_ORIGIN=""):
            report = self.report()
        self.assertTrue(report["checkout_configured"])
        self.assertFalse(report["public_urls_configured"])
        self.assertEqual(self.state(report, "origin"), "blocked")
        self.assertEqual(self.state(report, "allowed_host"), "unknown")

    def test_invalid_origin_is_redacted_and_cannot_be_ready(self):
        with override_settings(PUBLIC_ORIGIN="https://PRIVATE_USER:PRIVATE_PASS@billing.example.test"):
            report = self.report()
            output = self.command(success=False, as_json=True)
        self.assertEqual(self.state(report, "origin"), "blocked")
        self.assertNotIn("PRIVATE_PASS", output)
        self.assertNotIn("PRIVATE_USER", output)

    def test_loopback_http_or_https_is_local_only_including_ipv6(self):
        for origin, hosts in [("http://127.0.0.1:8000", ["127.0.0.1"]), ("https://localhost", ["localhost"]),
                ("http://[::1]:8000", ["[::1]"]), ("https://127.0.0.1", ["127.0.0.1"])]:
            with self.subTest(origin=origin), override_settings(DEBUG=True, PUBLIC_ORIGIN=origin, ALLOWED_HOSTS=hosts):
                report = self.report()
            self.assertEqual(self.state(report, "origin"), "local")
            self.assertEqual(self.state(report, "allowed_host"), "ready")
            self.assertFalse(report["configuration_ready"])

    def test_host_mismatch_and_valid_django_parent_domain_rule(self):
        with override_settings(ALLOWED_HOSTS=["testserver"]):
            report = self.report()
        self.assertEqual(self.state(report, "allowed_host"), "blocked")
        with override_settings(ALLOWED_HOSTS=[".example.test"]):
            self.assertTrue(self.report()["configuration_ready"])

    def test_idna_hostname_is_checked_as_browser_host(self):
        with override_settings(PUBLIC_ORIGIN="https://bülling.example.test", ALLOWED_HOSTS=["xn--blling-3ya.example.test"]):
            report = self.report()
        self.assertEqual(self.state(report, "allowed_host"), "ready")

    def test_pending_schema_does_not_read_tenant_config_or_create_tables(self):
        with patch("core.gateway_readiness.schema_pending_count", return_value=1), patch.object(GatewayConfig.objects, "filter") as config:
            report = self.report()
        config.assert_not_called()
        self.assertEqual(report["checks"][0]["code"], "schema_pending")
        self.assertIsNone(report["checkout_configured"])
        self.assertIsNone(report["public_urls_configured"])

    def test_database_error_and_inconsistent_schema_are_redacted(self):
        for error, code in [(OperationalError("PRIVATE DSN PASSWORD"), "database_unavailable"),
                (InconsistentMigrationHistory("PRIVATE CONNECTION"), "schema_inconsistent")]:
            with self.subTest(code=code), patch("core.gateway_readiness.schema_pending_count", side_effect=error):
                report = self.report()
                output = self.command(success=False, as_json=True)
            self.assertEqual(report["checks"][0]["code"], code)
            self.assertNotIn("PRIVATE", output)
            self.assertFalse(report["configuration_ready"])

    def test_late_database_error_cannot_leave_partial_success_report(self):
        with patch.object(GatewayConfig.objects, "filter", side_effect=OperationalError("PRIVATE SQL")):
            report = self.report()
        self.assertFalse(report["configuration_ready"])
        self.assertIsNone(report["checkout_configured"])
        self.assertEqual([check["code"] for check in report["checks"]], ["database_unavailable"])

    def test_unknown_or_invalid_tenant_is_not_a_success(self):
        report = readiness_report(999999)
        self.assertEqual(report["checks"][-1]["code"], "tenant_unknown")
        self.assertFalse(report["configuration_ready"])
        for value in [None, True, 0, -1, "1"]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                readiness_report(value)

    def test_cli_text_json_and_exit_outcome_do_not_claim_live_connection(self):
        text = self.command(success=True)
        self.assertIn("Tidak ada request ke Midtrans", text)
        report = json.loads(self.command(success=True, as_json=True))
        self.assertTrue(report["configuration_ready"])
        self.assertEqual(report["remote_verification"], "not_performed")
        self.assertTrue(report["checked_at"].endswith("+07:00"))
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS={}):
            blocked = json.loads(self.command(success=False, as_json=True))
        self.assertFalse(blocked["configuration_ready"])
        self.assertNotIn(KEY, text)

    def test_cli_requires_explicit_positive_tenant(self):
        for tenant in [0, -1, True]:
            with self.subTest(tenant=tenant), self.assertRaises(CommandError):
                call_command("check_payment_gateway", tenant=tenant, stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("check_payment_gateway", stdout=StringIO())

    def test_owner_scope_client_input_and_other_roles(self):
        response = self.client.get("/settings/payment-gateway/", {"tenant": self.tenants[1].pk})
        self.assertContains(response, "Kesiapan konfigurasi")
        self.assertTrue(response.context["readiness"]["configuration_ready"])
        self.assertEqual(response.context["readiness"]["tenant_id"], self.tenants[0].pk)
        self.assertNotContains(response, KEY)
        verified_login(self.client, self.owners[1])
        response = self.client.get("/settings/payment-gateway/")
        self.assertFalse(response.context["readiness"]["configuration_ready"])
        for role in ["finance", "customer"]:
            user = get_user_model().objects.create_user(username="readiness-"+role)
            Membership.objects.create(user=user, tenant=self.tenants[0], role=role, customer=self.customers[0] if role == "customer" else None)
            verified_login(self.client, user)
            self.assertEqual(self.client.get("/settings/payment-gateway/").status_code, 403)

    def test_diagnostics_never_contacts_provider_mutates_data_or_creates_checkout(self):
        models = [GatewayConfig, GatewayOrder, Payment, NetworkJob, AuditEvent]
        before = [model.objects.count() for model in models]
        with patch("core.midtrans.SandboxClient.request") as request:
            self.report()
            self.command(success=True, as_json=True)
            self.client.get("/settings/payment-gateway/")
        request.assert_not_called()
        self.assertEqual([model.objects.count() for model in models], before)

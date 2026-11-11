import csv
import io
import time
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from core.access import save_role
from core.billing import issue_invoice, record_payment
from core.gateway import apply_status, reserve_order
from core.gateway_refunds import reconcile_refund
from core.gateway_report_views import csv_cell
from core.gateway_reports import WIB, ledger_report, validate_period
from core.models import AccessRole, AuditEvent, GatewayConfig, GatewayOrder, Invoice, Membership, NetworkJob, Payment, PaymentReversal
from .auth_helpers import verified_login
from .test_gateway_refunds import REASON, refund_payload
from .test_payment_gateway import account_keys, status_payload
from .test_workflow import fixtures

DAY = date(2026, 11, 11)
START = datetime(2026, 11, 11, tzinfo=WIB)


class ReportPeriodTests(SimpleTestCase):
    def test_inclusive_wib_dates_and_maximum_31_days(self):
        lower, upper = validate_period(date(2026, 10, 12), DAY)
        self.assertEqual(lower.isoformat(), "2026-10-12T00:00:00+07:00")
        self.assertEqual(upper.isoformat(), "2026-11-12T00:00:00+07:00")
        self.assertEqual(validate_period(DAY, DAY), (START, START+timedelta(days=1)))

    def test_invalid_reversed_32_day_or_overflow_period_is_rejected(self):
        for start, end in [(DAY, DAY-timedelta(days=1)), (date(2026, 10, 11), DAY), (date.max, date.max),
                (None, DAY), ("2026-11-11", DAY), (START, START)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValidationError):
                validate_period(start, end)

    def test_csv_text_controls_and_formulas_are_neutralized_but_numbers_remain_numeric(self):
        for text in ["=HYPERLINK(1)", "+cmd", "-cmd", "@SUM(1)", " \t=SUM(1)", "\ufeff \ufeff=SUM(1)", "\u00a0@SUM(1)", "\x00=SUM(1)"]:
            self.assertTrue(csv_cell(text).startswith("'"))
        self.assertEqual(csv_cell(-249750), -249750)
        self.assertEqual(csv_cell("Nama\nPelanggan"), "Nama Pelanggan")
        self.assertEqual(csv_cell(None), "")


@override_settings(SIMULATION=True)
class GatewayReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.invoice, _ = issue_invoice(cls.tenants[0], cls.customers[0].pk, date(2026, 8, 1), cls.owners[0])
        GatewayConfig.objects.create(tenant=cls.tenants[0], merchant_id="G-TEST", enabled=True)
        GatewayConfig.objects.create(tenant=cls.tenants[1], merchant_id="G-OTHER", enabled=True)

    def setUp(self):
        keys = account_keys(self.tenants[0])
        keys[str(self.tenants[1].pk)] = {"merchant_id": "G-OTHER", "server_key": "SB-Mid-server-report-other-fixture"}
        self.keys = override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=keys)
        self.keys.enable()
        self.addCleanup(self.keys.disable)
        self.order = reserve_order(self.owners[0], self.invoice.pk)
        self.order = apply_status(self.order.pk, status_payload(self.order))
        Payment.objects.filter(pk=self.order.payment_id).update(created_at=START)
        verified_login(self.client, self.owners[0])
        self.query = {"start": DAY.isoformat(), "end": DAY.isoformat()}

    def report(self, start=DAY, end=DAY, actor=None):
        return ledger_report(actor or self.owners[0], start, end)

    def refund(self, when=START+timedelta(hours=12)):
        with patch("core.gateway_refunds.SandboxClient.status", return_value=refund_payload(self.order)):
            result = reconcile_refund(self.owners[0], self.order.pk, uuid.uuid4(), REASON, time.time(), 1)
        PaymentReversal.objects.filter(pk=result.reversal_id).update(created_at=when)
        return result

    def parsed_csv(self, **query):
        response = self.client.get("/payments/reports/export.csv", {**self.query, **query})
        self.assertEqual(response.status_code, 200)
        return response, list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))

    def test_original_payment_remains_in_original_period_after_later_refund(self):
        self.refund(START+timedelta(days=1))
        original = self.report()
        self.assertEqual(original["totals"], {"payment": self.order.amount, "refund": 0, "reversal": 0, "net": self.order.amount})
        later = self.report(DAY+timedelta(days=1), DAY+timedelta(days=1))
        self.assertEqual(later["totals"], {"payment": 0, "refund": self.order.amount, "reversal": 0, "net": -self.order.amount})
        self.assertEqual(original["rows"][0]["status"], "refunded")
        self.assertEqual(later["rows"][0]["signed_amount"], -self.order.amount)

    def test_payment_and_refund_are_two_entries_with_zero_net(self):
        self.refund()
        report = self.report()
        self.assertEqual(report["counts"], {"payment": 1, "refund": 1, "reversal": 0})
        self.assertEqual(report["totals"]["net"], 0)
        self.assertEqual(len(report["rows"]), 2)
        self.assertEqual(sum(row["signed_amount"] for row in report["rows"]), 0)

    def test_one_union_statement_reads_both_entry_types_without_followup_amount_queries(self):
        self.refund()
        with CaptureQueriesContext(connection) as queries:
            self.report()
        ledger_queries = [item["sql"] for item in queries if "core_gatewayorder" in item["sql"].lower()]
        self.assertEqual(len(ledger_queries), 1)
        self.assertIn("UNION ALL", ledger_queries[0])

    def test_boundary_start_is_included_and_following_midnight_is_excluded(self):
        self.refund(START+timedelta(days=1))
        self.assertEqual(self.report()["counts"], {"payment": 1, "refund": 0, "reversal": 0})
        Payment.objects.filter(pk=self.order.payment_id).update(created_at=START-timedelta(microseconds=1))
        self.assertEqual(len(self.report()["rows"]), 0)
        PaymentReversal.objects.update(created_at=START+timedelta(days=1)-timedelta(microseconds=1))
        self.assertEqual(self.report()["totals"]["net"], -self.order.amount)

    @override_settings(TIME_ZONE="UTC")
    def test_wib_is_explicit_even_if_active_timezone_is_different(self):
        with timezone.override("America/New_York"):
            report = self.report()
            response = self.client.get("/payments/reports/", self.query)
        self.assertEqual(report["counts"]["payment"], 1)
        self.assertContains(response, "11 Nov 2026 00:00:00")
        _, rows = self.parsed_csv()
        self.assertEqual(rows[0]["waktu_ledger_wib"], "2026-11-11 00:00:00")

    def test_replayed_status_and_unrecorded_refund_do_not_duplicate_or_reduce_ledger(self):
        for _ in range(3):
            apply_status(self.order.pk, status_payload(self.order))
        apply_status(self.order.pk, refund_payload(self.order))
        report = self.report()
        self.assertEqual(report["counts"]["payment"], 1)
        self.assertEqual(report["totals"]["net"], self.order.amount)
        self.assertEqual(report["rows"][0]["status"], "review")
        result = self.refund()
        with patch("core.gateway_refunds.SandboxClient.status") as provider:
            reconcile_refund(self.owners[0], self.order.pk, result.request_key, REASON, time.time(), 1)
        provider.assert_not_called()
        self.assertEqual(self.report()["counts"]["refund"], 1)

    def test_generic_reversal_still_reduces_ledger_without_claiming_provider_refund(self):
        PaymentReversal.objects.create(payment_id=self.order.payment_id, recorded_by=self.owners[0], created_at=START,
            reason="Legacy correction fixture without verified refund resolution")
        report = self.report()
        self.assertEqual(report["totals"]["net"], 0)
        self.assertEqual(report["counts"], {"payment": 1, "refund": 0, "reversal": 1})
        self.assertContains(self.client.get("/payments/reports/", self.query), "tanpa bukti refund penuh yang sesuai")

    def test_mismatched_refund_proof_is_a_correction_to_review(self):
        resolution = self.refund()
        resolution.amount = 1
        resolution.save(update_fields=["amount"])
        report = self.report()
        self.assertEqual(report["counts"]["refund"], 0)
        self.assertEqual(report["counts"]["reversal"], 1)
        self.assertEqual(report["totals"]["net"], 0)

    def test_manual_payment_and_pending_checkout_are_excluded(self):
        pending, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026, 9, 1), self.owners[0])
        reserve_order(self.owners[0], pending.pk)
        manual, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026, 10, 1), self.owners[0])
        payment, _ = record_payment(self.tenants[0], manual.pk, 100000, uuid.uuid4(), self.owners[0])
        Payment.objects.filter(pk=payment.pk).update(created_at=START)
        GatewayOrder.objects.create(tenant=self.tenants[0], invoice=manual, created_by=self.owners[0], merchant_id="G-TEST",
            amount=payment.amount, payment=payment, status="settled")
        self.assertEqual(self.report()["counts"]["payment"], 1)

    def test_foreign_tenant_fields_cannot_select_other_financial_data(self):
        other_invoice, _ = issue_invoice(self.tenants[1], self.customers[1].pk, DAY.replace(day=1), self.owners[1])
        other = reserve_order(self.owners[1], other_invoice.pk)
        other = apply_status(other.pk, status_payload(other, merchant_id="G-OTHER"))
        Payment.objects.filter(pk=other.payment_id).update(created_at=START)
        response = self.client.get("/payments/reports/", {**self.query, "tenant": self.tenants[1].pk})
        self.assertNotContains(response, other.order_id)
        _, rows = self.parsed_csv(tenant=self.tenants[1].pk)
        self.assertEqual([row["order_gateway"] for row in rows], [self.order.order_id])

    def test_broken_cross_tenant_or_invoice_links_cannot_expose_ledger(self):
        other, _ = issue_invoice(self.tenants[1], self.customers[1].pk, DAY.replace(day=1), self.owners[1])
        changes = [(GatewayOrder, self.order.pk, "tenant_id", self.tenants[1].pk, self.tenants[0].pk),
            (GatewayOrder, self.order.pk, "invoice_id", other.pk, self.invoice.pk),
            (Payment, self.order.payment_id, "tenant_id", self.tenants[1].pk, self.tenants[0].pk),
            (Payment, self.order.payment_id, "invoice_id", other.pk, self.invoice.pk)]
        for model, pk, field, invalid, original in changes:
            with self.subTest(field=field, model=model):
                model.objects.filter(pk=pk).update(**{field: invalid})
                self.assertEqual(self.report()["rows"], [])
                self.assertEqual(self.report(actor=self.owners[1])["rows"], [])
                model.objects.filter(pk=pk).update(**{field: original})

    def test_owner_finance_only_by_default_and_anonymous_redirected(self):
        self.client.logout()
        self.assertEqual(self.client.get("/payments/reports/").status_code, 302)
        for role in ["finance", "ops", "cashier", "noc", "customer", "technician"]:
            user = get_user_model().objects.create_user(username="report-"+role)
            Membership.objects.create(user=user, tenant=self.tenants[0], role=role, customer=self.customers[0] if role == "customer" else None)
            verified_login(self.client, user)
            expected = 200 if role == "finance" else 403
            with self.subTest(role=role):
                self.assertEqual(self.client.get("/payments/reports/", self.query).status_code, expected)
                self.assertEqual(self.client.get("/payments/reports/export.csv", self.query).status_code, expected)

    def test_custom_role_requires_invoice_access_and_can_be_revoked(self):
        with self.assertRaises(ValidationError):
            save_role(self.owners[0], "Ledger reader", {"gateway_reports"})
        role = save_role(self.owners[0], "Ledger reader", {"gateway_reports", "invoices"})
        user = get_user_model().objects.create_user(username="report-custom")
        Membership.objects.create(user=user, tenant=self.tenants[0], role="finance", access_role=role)
        verified_login(self.client, user)
        self.assertEqual(self.client.get("/payments/reports/", self.query).status_code, 200)
        save_role(self.owners[0], "Ledger reader", {"invoices"}, role.pk)
        response = self.client.get("/payments/reports/export.csv", self.query)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/login/"))

    def test_domain_rejects_corrupt_custom_role_without_invoice_dependency(self):
        role = AccessRole.objects.create(tenant=self.tenants[0], name="Broken fixture", capabilities=["gateway_reports"])
        user = get_user_model().objects.create_user(username="report-broken")
        Membership.objects.create(user=user, tenant=self.tenants[0], role="finance", access_role=role)
        with self.assertRaises(PermissionDenied):
            self.report(actor=user)

    def test_views_are_get_only_and_require_an_authorized_mfa_session(self):
        self.assertEqual(self.client.post("/payments/reports/", self.query).status_code, 405)
        self.assertEqual(self.client.post("/payments/reports/export.csv", self.query).status_code, 405)
        session = self.client.session
        session["access_version"] = 999
        session.save()
        self.assertEqual(self.client.get("/payments/reports/export.csv", self.query).status_code, 302)

    def test_invalid_periods_do_not_emit_partial_csv_or_success_totals(self):
        for query in [{"start": "bad"}, {"end": ""}, {"start": "2026-11-12"}, {"start": "2026-10-11"}, {"start": "9999-12-31", "end": "9999-12-31"}]:
            for path in ["/payments/reports/", "/payments/reports/export.csv"]:
                response = self.client.get(path, {**self.query, **query})
                self.assertEqual(response.status_code, 400)
                self.assertNotContains(response, "Perubahan bersih ledger", status_code=400)
                self.assertNotIn("Content-Disposition", response)

    def test_limit_rejects_entire_report_and_export_instead_of_truncating(self):
        self.refund()
        with patch("core.gateway_reports.MAX_ENTRIES", 2):
            self.assertEqual(len(self.report()["rows"]), 2)
        with patch("core.gateway_reports.MAX_ENTRIES", 1):
            with self.assertRaises(ValidationError):
                self.report()
            response = self.client.get("/payments/reports/export.csv", self.query)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "tidak dipotong sebagian", status_code=400)
        self.assertFalse(AuditEvent.objects.filter(action="gateway.report_exported").exists())

    def test_empty_report_and_header_only_csv_are_valid(self):
        response, rows = self.parsed_csv(start="2026-11-01", end="2026-11-01")
        self.assertEqual(rows, [])
        self.assertIn("2026-11-01_2026-11-01", response["Content-Disposition"])
        self.assertEqual(self.report(date(2026, 11, 1), date(2026, 11, 1))["totals"]["net"], 0)

    def test_default_is_seven_wib_dates_and_page_does_not_filter_csv(self):
        self.refund()
        with patch("core.gateway_report_views.timezone.localdate", return_value=DAY):
            response = self.client.get("/payments/reports/", {"page": "invalid", "size": "invalid"})
        self.assertEqual(response.context["report"]["start"], DAY-timedelta(days=6))
        self.assertEqual(response.context["report"]["end"], DAY)
        self.assertEqual(response.context["page"].paginator.per_page, 10)
        _, rows = self.parsed_csv(page="999", size="1")
        self.assertEqual(len(rows), 2)

    def test_csv_and_html_totals_match_and_export_has_scope_audit(self):
        self.refund()
        report = self.report()
        response, rows = self.parsed_csv()
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(sum(int(row["perubahan_bersih_rupiah"]) for row in rows), report["totals"]["net"])
        self.assertEqual(sum(int(row["pembayaran_rupiah"]) for row in rows), report["totals"]["payment"])
        self.assertEqual(sum(int(row["refund_penuh_rupiah"]) for row in rows), report["totals"]["refund"])
        self.assertEqual({row["periode_awal_wib"] for row in rows}, {DAY.isoformat()})
        self.assertEqual({row["faktur"] for row in rows}, {self.invoice.number})
        event = AuditEvent.objects.get(action="gateway.report_exported")
        self.assertEqual(event.tenant, self.tenants[0])
        self.assertIn("2 entri", event.detail)

    def test_export_never_contains_private_checkout_keys_proof_or_owner_notes(self):
        self.order.checkout_url = "https://app.sandbox.midtrans.com/snap/v4/redirection/private-fixture"
        self.order.save(update_fields=["checkout_url"])
        resolution = self.refund()
        resolution.reason = "PRIVATE OWNER REASON"
        resolution.save(update_fields=["reason"])
        response, _ = self.parsed_csv()
        for private in ["private-fixture", "SB-Mid-server-", "PRIVATE OWNER REASON", resolution.proof_digest,
                str(Payment.objects.get(pk=self.order.payment_id).event_key)]:
            self.assertNotIn(private, response.content.decode())

    def test_customer_text_is_escaped_in_html_and_csv_formula_is_neutralized(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(customer_name="=HYPERLINK(\"https://evil.test\")<script>alert(1)</script>")
        response = self.client.get("/payments/reports/", self.query)
        self.assertNotContains(response, "<script>alert(1)</script>")
        _, rows = self.parsed_csv()
        self.assertTrue(rows[0]["pelanggan"].startswith("'=HYPERLINK"))

    def test_same_timestamp_order_is_stable_and_summary_ignores_page(self):
        self.refund(START)
        first, second = self.report(), self.report()
        self.assertEqual([row["reference"] for row in first["rows"]], [row["reference"] for row in second["rows"]])
        for month in range(1, 13):
            invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2027, month, 1), self.owners[0])
            order = reserve_order(self.owners[0], invoice.pk)
            order = apply_status(order.pk, status_payload(order))
            Payment.objects.filter(pk=order.payment_id).update(created_at=START)
        first_page = self.client.get("/payments/reports/", {**self.query, "page": "1", "size": "10"})
        second_page = self.client.get("/payments/reports/", {**self.query, "page": "2", "size": "10"})
        first_rows, second_rows = list(first_page.context["page"]), list(second_page.context["page"])
        self.assertEqual((len(first_rows), len(second_rows)), (10, 4))
        self.assertEqual(len({row["reference"] for row in first_rows+second_rows}), 14)
        self.assertEqual(first_page.context["report"]["totals"], second_page.context["report"]["totals"])
        response = self.client.get("/payments/reports/", {**self.query, "page": "999", "size": "25"})
        self.assertEqual(response.context["report"]["counts"]["refund"], 1)
        self.assertEqual(response.context["page"].paginator.per_page, 25)

    def test_report_no_network_no_financial_writes_and_failed_status_stays_labeled(self):
        GatewayOrder.objects.filter(pk=self.order.pk).update(last_checked_at=timezone.now()+timedelta(seconds=1))
        GatewayConfig.objects.update(enabled=False)
        before = (Payment.objects.count(), PaymentReversal.objects.count(), NetworkJob.objects.count())
        with override_settings(SIMULATION=False, MIDTRANS_SANDBOX_ACCOUNT_KEYS={}), patch("core.gateway.SandboxClient.status") as status, patch("core.gateway.SandboxClient.create") as create:
            response = self.client.get("/payments/reports/", self.query)
            self.parsed_csv()
        self.assertContains(response, "Pemeriksaan terakhir gagal")
        status.assert_not_called()
        create.assert_not_called()
        self.assertEqual((Payment.objects.count(), PaymentReversal.objects.count(), NetworkJob.objects.count()), before)

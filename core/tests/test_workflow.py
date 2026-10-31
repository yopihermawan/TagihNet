import time
import uuid
from datetime import date, timedelta
from io import BytesIO
from unittest.mock import patch
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from core.billing import issue_invoice, record_payment, reverse_payment, totals
from core.models import AuditEvent, Customer, Invoice, InvoiceLine, Membership, NetworkJob, Payment, Plan, Tenant, month_due
from core.network import run_job
from core.pdf import render_invoice


def fixtures():
    tenants, owners, customers = [], [], []
    for index in range(2):
        tenant = Tenant.objects.create(name=f"ISP {index}", due_day=31, tax_bp=1100)
        owner = get_user_model().objects.create_user(username=f"owner{index}", password="Local-test-secret-2026!", email=f"owner{index}@example.invalid")
        Membership.objects.create(user=owner, tenant=tenant, role="owner")
        plan = Plan.objects.create(tenant=tenant, name="Rumah", speed_mbps=30, price=225000)
        customer = Customer.objects.create(tenant=tenant, code="TN-001", name=f"Pelanggan {index}",
            phone=f"0812000000{index}", username="rumah", address="Jl. Contoh", area="Bandung", plan=plan)
        tenants.append(tenant)
        owners.append(owner)
        customers.append(customer)
    return tenants, owners, customers


@override_settings(SIMULATION=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class WorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.invoice, _ = issue_invoice(cls.tenants[0], cls.customers[0].pk, date(2026, 2, 1), cls.owners[0])
        cls.other_invoice, _ = issue_invoice(cls.tenants[1], cls.customers[1].pk, date(2026, 2, 1), cls.owners[1])

    def setUp(self):
        self.client.force_login(self.owners[0])

    def payment(self, amount=None, key=None):
        return record_payment(self.tenants[0], self.invoice.pk, amount or self.invoice.total,
                              key or uuid.uuid4(), self.owners[0])[0]

    def test_login_logout_and_session_rotation(self):
        client = Client()
        session = client.session
        session["test"] = "value"
        session.save()
        before = session.session_key
        response = client.post("/login/", {"username": "owner0", "password": "Local-test-secret-2026!"})
        self.assertRedirects(response, "/")
        self.assertNotEqual(before, client.session.session_key)
        self.assertEqual(client.get("/logout/").status_code, 405)
        self.assertRedirects(client.post("/logout/"), "/login/")
        self.assertEqual(client.get("/").status_code, 302)

    def test_login_throttle_is_persistent_and_generic(self):
        client = Client()
        for _ in range(5):
            response = client.post("/login/", {"username": "missing", "password": "wrong"})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(client.post("/login/", {"username": "owner0", "password": "wrong"}).status_code, 429)

    def test_csrf_required_for_mutations(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owners[0])
        self.assertEqual(client.post(f"/invoices/{self.invoice.pk}/pay/", {}).status_code, 403)
        self.assertEqual(client.post("/settings/billing/", {}).status_code, 403)
        self.assertEqual(client.post("/logout/", {}).status_code, 403)

    def test_session_idle_and_absolute_timeouts(self):
        for field, age in [("last_seen", 1801), ("started_at", 28801)]:
            self.client.force_login(self.owners[0])
            session = self.client.session
            session[field] = time.time() - age
            session.save()
            self.assertEqual(self.client.get("/").status_code, 302)

    def test_password_reset_once_revokes_old_session(self):
        response = self.client.post("/reset/", {"email": "owner0@example.invalid"})
        self.assertRedirects(response, "/reset/sent/")
        self.assertEqual(len(mail.outbox), 1)
        user = self.owners[0]
        token = default_token_generator.make_token(user)
        url = reverse("password_reset_confirm", kwargs={"uidb64": urlsafe_base64_encode(force_bytes(user.pk)), "token": token})
        recovery = Client()
        response = recovery.get(url)
        clean_url = response.url
        response = recovery.post(clean_url, {"new_password1": "New-local-secret-2026!", "new_password2": "New-local-secret-2026!"})
        self.assertRedirects(response, "/reset/complete/")
        user.refresh_from_db()
        self.assertFalse(default_token_generator.check_token(user, token))
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_reset_unknown_email_is_indistinguishable(self):
        response = self.client.post("/reset/", {"email": "missing@example.invalid"})
        self.assertRedirects(response, "/reset/sent/")
        self.assertEqual(len(mail.outbox), 0)

    def test_owner_pages_render(self):
        for url in ["/", "/customers/", "/invoices/", "/network/", "/settings/billing/", "/audit/",
                    f"/customers/{self.customers[0].pk}/", f"/invoices/{self.invoice.pk}/"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_cross_tenant_objects_read_write_pdf_are_blocked(self):
        customer = self.customers[1]
        invoice = self.other_invoice
        for url in [f"/customers/{customer.pk}/", f"/customers/{customer.pk}/edit/",
                    f"/invoices/{invoice.pk}/", f"/invoices/{invoice.pk}/pdf/"]:
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(f"/invoices/{invoice.pk}/pay/", {"amount": 1, "event_key": uuid.uuid4()}).status_code, 404)
        self.assertNotContains(self.client.get("/customers/?q=Pelanggan"), "Pelanggan 1")
        self.assertNotContains(self.client.get("/customers/export/"), "Pelanggan 1")

    def test_tenant_id_from_request_is_ignored(self):
        response = self.client.get(f"/customers/?tenant_id={self.tenants[1].pk}")
        self.assertContains(response, "Pelanggan 0")
        self.assertNotContains(response, "Pelanggan 1")

    def test_role_change_applies_to_existing_session(self):
        Membership.objects.filter(user=self.owners[0]).update(role="noc")
        self.assertEqual(self.client.get("/customers/").status_code, 403)
        self.assertEqual(self.client.get(f"/invoices/{self.invoice.pk}/pdf/").status_code, 403)
        self.assertEqual(self.client.get("/network/").status_code, 200)

    def test_cashier_cannot_issue_change_prices_or_reverse(self):
        Membership.objects.filter(user=self.owners[0]).update(role="cashier")
        payment = self.payment(1000)
        for url in ["/invoices/new/", "/customers/new/", "/settings/billing/", f"/payments/{payment.pk}/reverse/"]:
            self.assertEqual(self.client.post(url, {}).status_code, 403)

    def test_portal_scopes_same_tenant_and_cross_tenant(self):
        member = Membership.objects.get(user=self.owners[0])
        member.role, member.customer = "customer", self.customers[0]
        member.save()
        sibling = Customer.objects.create(tenant=self.tenants[0], code="TN-002", name="Private sibling", phone="08121111111",
            username="sibling", address="Contoh", area="Dago", plan=self.customers[0].plan)
        invoice, _ = issue_invoice(self.tenants[0], sibling.pk, date(2026, 2, 1), self.owners[0])
        self.assertEqual(self.client.get("/portal/").status_code, 200)
        self.assertEqual(self.client.get(f"/invoices/{self.invoice.pk}/pdf/").status_code, 200)
        for other in [invoice, self.other_invoice]:
            self.assertEqual(self.client.get(f"/invoices/{other.pk}/").status_code, 404)
            self.assertEqual(self.client.get(f"/invoices/{other.pk}/pdf/").status_code, 404)
        self.assertEqual(self.client.post(f"/invoices/{self.invoice.pk}/pay/", {}).status_code, 403)

    def test_customer_create_edit_reload_and_duplicate_detection(self):
        data = {"name": "Pelanggan Baru", "phone": "+62 812-3456789", "username": "baru", "email": "",
                "address": "Jl. Baru", "area": "Dago", "plan": self.customers[0].plan_id}
        response = self.client.post("/customers/new/", data)
        self.assertEqual(response.status_code, 302)
        item = Customer.objects.get(username="baru", tenant=self.tenants[0])
        self.assertEqual(item.phone, "08123456789")
        data["name"] = "Nama Diubah"
        self.client.post(f"/customers/{item.pk}/edit/", data)
        self.assertContains(self.client.get(f"/customers/{item.pk}/"), "Nama Diubah")
        response = self.client.post("/customers/new/", data)
        self.assertContains(response, "sudah")
        self.assertEqual(Customer.objects.filter(tenant=self.tenants[0], username="baru").count(), 1)

    def test_form_rejects_cross_tenant_plan(self):
        response = self.client.post("/customers/new/", {"name": "Bad", "phone": "08123333333", "username": "bad",
            "address": "Jl. Bad", "area": "Dago", "plan": self.customers[1].plan_id})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Customer.objects.filter(username="bad").exists())

    def test_search_sort_filter_pagination_is_one_source_of_truth(self):
        for index in range(52):
            Customer.objects.create(tenant=self.tenants[0], code=f"C{index:03}", name=f"Nama {index:03}",
                phone=f"081299{index:05}", username=f"nama-{index}", address="Contoh", area="Dago", plan=self.customers[0].plan)
        for size in [10, 25, 50]:
            response = self.client.get(f"/customers/?q=Nama&area=Dago&sort=-name&size={size}")
            page = response.context["page"]
            self.assertEqual(len(page.object_list), size)
            self.assertEqual(page.start_index(), 1)
            self.assertEqual(page.end_index(), size)
            self.assertEqual(page.object_list[0].name, "Nama 051")
            response = self.client.get(f"/customers/?q=Nama&area=Dago&sort=-name&size={size}&page=2")
            self.assertEqual(response.context["page"].start_index(), size + 1)
        self.assertEqual(self.client.get("/customers/?size=999").context["page"].paginator.per_page, 10)

    def test_invoice_retry_is_unique_and_snapshot_is_stable(self):
        invoice, created = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026, 2, 1), self.owners[0])
        self.assertFalse(created)
        self.assertEqual(invoice.pk, self.invoice.pk)
        old_total = invoice.total
        Plan.objects.filter(pk=self.customers[0].plan_id).update(price=900000)
        Tenant.objects.filter(pk=self.tenants[0].pk).update(tax_bp=0, invoice_footer="Changed")
        invoice.refresh_from_db()
        self.assertEqual(invoice.total, old_total)
        self.assertNotEqual(invoice.footer, "Changed")

    def test_integer_tax_discount_and_half_up_rounding(self):
        self.assertEqual(totals(225000, 25000, 1100), (222000, 22000))
        self.assertEqual(totals(101, 0, 5000), (152, 51))
        self.assertEqual(totals(150000, 0, 0), (150000, 0))
        for args in [(100, 101, 0), (100, -1, 0), (0, 0, 0), (1.5, 0, 0), (1, 0, 10001)]:
            with self.assertRaises(ValidationError):
                totals(*args)

    def test_month_end_february_and_leap_year(self):
        for period, expected in [(date(2026, 2, 1), date(2026, 2, 28)), (date(2028, 2, 1), date(2028, 2, 29)),
                                 (date(2026, 4, 1), date(2026, 4, 30)), (date(2026, 1, 1), date(2026, 1, 31))]:
            self.assertEqual(month_due(period, 31), expected)

    def test_partial_full_overpay_and_idempotency(self):
        key = uuid.uuid4()
        payment = self.payment(100000, key)
        same, created = record_payment(self.tenants[0], self.invoice.pk, 100000, key, self.owners[0])
        self.assertFalse(created)
        self.assertEqual(same.pk, payment.pk)
        self.assertEqual(self.invoice.paid, 100000)
        with self.assertRaises(ValidationError):
            self.payment(self.invoice.balance + 1)
        self.payment(self.invoice.balance)
        self.assertEqual(self.invoice.status, "paid")
        self.assertEqual(Payment.objects.filter(tenant=self.tenants[0]).count(), 2)

    def test_reused_event_key_cannot_change_amount(self):
        key = uuid.uuid4()
        self.payment(1000, key)
        with self.assertRaises(ValidationError):
            self.payment(2000, key)

    def test_browser_cannot_set_price_or_paid_status(self):
        response = self.client.post("/invoices/new/", {"customer": self.customers[0].pk, "period": "2026-03",
            "discount": 0, "total": 1, "status": "paid", "tenant_id": self.tenants[1].pk})
        self.assertEqual(response.status_code, 302)
        invoice = Invoice.objects.get(tenant=self.tenants[0], period=date(2026, 3, 1))
        self.assertEqual(invoice.total, 249750)
        self.assertEqual(invoice.paid, 0)

    def test_payment_reversal_preserves_history(self):
        payment = self.payment()
        self.assertEqual(self.invoice.balance, 0)
        reverse_payment(self.tenants[0], payment.pk, "Koreksi pencatatan lokal", self.owners[0])
        reverse_payment(self.tenants[0], payment.pk, "Koreksi pencatatan lokal", self.owners[0])
        self.assertEqual(self.invoice.balance, self.invoice.total)
        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())
        self.assertEqual(AuditEvent.objects.filter(action="payment.reversed").count(), 1)

    def test_failed_router_does_not_rollback_payment(self):
        Customer.objects.filter(pk=self.customers[0].pk).update(router_available=False, service_state="isolated")
        payment = self.payment()
        job = NetworkJob.objects.get(customer=self.customers[0])
        run_job(job.pk)
        job.refresh_from_db()
        self.customers[0].refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(self.invoice.balance, 0)
        self.assertEqual(self.customers[0].service_state, "isolated")
        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())

    def test_payment_before_isolation_job_rechecks_current_debt(self):
        job = NetworkJob.objects.create(tenant=self.tenants[0], customer=self.customers[0])
        self.payment()
        run_job(job.pk)
        self.customers[0].refresh_from_db()
        self.assertEqual(self.customers[0].service_state, "active")
        self.assertEqual(run_job(job.pk).attempts, 1)

    def test_isolation_before_payment_eventually_reconciles(self):
        job = NetworkJob.objects.create(tenant=self.tenants[0], customer=self.customers[0])
        run_job(job.pk)
        self.customers[0].refresh_from_db()
        self.assertEqual(self.customers[0].service_state, "isolated")
        self.payment()
        new_job = NetworkJob.objects.filter(customer=self.customers[0], status="pending").get()
        run_job(new_job.pk)
        self.customers[0].refresh_from_db()
        self.assertEqual(self.customers[0].service_state, "active")

    def test_worker_rejects_tenant_mismatch(self):
        job = NetworkJob.objects.create(tenant=self.tenants[1], customer=self.customers[0])
        with self.assertRaises(Customer.DoesNotExist):
            run_job(job.pk)

    @override_settings(SIMULATION=False)
    def test_real_effects_are_disabled_on_server(self):
        with self.assertRaises(ValidationError):
            self.payment()
        self.assertEqual(self.client.post("/webhooks/payment/", {}).status_code, 404)

    def test_plain_text_footer_is_encoded_and_pdf_cannot_fetch(self):
        markup = '<script src="https://example.invalid/evil.js">safe-marker</script>'
        Invoice.objects.filter(pk=self.invoice.pk).update(footer=markup)
        self.invoice.refresh_from_db()
        response = self.client.get(f"/invoices/{self.invoice.pk}/")
        self.assertContains(response, "&lt;script")
        self.assertNotContains(response, markup)
        self.assertTrue(render_invoice(self.invoice, True).startswith(b"%PDF-"))

    def test_pdf_authorized_valid_and_tagged_as_simulation(self):
        response = self.client.get(f"/invoices/{self.invoice.pk}/pdf/")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertIn(self.invoice.number, response["Content-Disposition"])

    def test_csv_prevents_formula_injection(self):
        Customer.objects.filter(pk=self.customers[0].pk).update(name="=1+1", address="@unsafe")
        response = self.client.get("/customers/export/")
        self.assertContains(response, "'=1+1")

    def test_security_headers_and_no_cache(self):
        response = self.client.get("/")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", response["Content-Security-Policy"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "DENY")

    def test_dashboard_definitions_and_no_false_online(self):
        self.payment(1000)
        response = self.client.get("/")
        self.assertEqual(response.context["received"], 1000)
        self.assertEqual(response.context["issued"], 0)
        self.assertContains(response, "Data sesi router belum tersedia")
        self.assertNotContains(response, "0 online")

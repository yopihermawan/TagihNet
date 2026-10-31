import csv
import io
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, close_old_connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone
from core.access import save_role
from core.customer_import import MAX_BYTES, cancel_import, confirm_import, preview_import
from core.models import AuditEvent, Customer, CustomerImportBatch, CustomerImportRow, Invoice, Membership, NetworkJob, Plan
from .auth_helpers import verified_login
from .test_workflow import fixtures

HEADERS = ["nama", "email", "telepon", "username", "paket_id", "wilayah", "alamat"]


def source(tenant, count=1, **changes):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(HEADERS)
    for index in range(count):
        record = {"nama": f"Pelanggan CSV {index}", "email": f"csv{index}@example.invalid", "telepon": f"0812999{index:05d}",
            "username": f"CSV-{index}", "paket_id": tenant.plan_set.first().pk, "wilayah": "Wilayah CSV", "alamat": "Alamat, fiktif\nBaris kedua"}
        record.update(changes)
        writer.writerow([record[key] for key in HEADERS])
    return stream.getvalue().encode()


class CustomerImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()

    def setUp(self):
        verified_login(self.client, self.owners[0])

    def preview(self, data=None, **changes):
        return preview_import(self.owners[0], data if data is not None else source(self.tenants[0], **changes), uuid.uuid4())

    def confirm(self, batch, proof=None, version=1):
        return confirm_import(self.owners[0], batch.pk, time.time() if proof is None else proof, version)

    def test_preview_normalizes_and_has_no_customer_financial_or_network_effect(self):
        batch = self.preview(telepon="+62 812-9999988")
        row = batch.rows.get()
        self.assertEqual((row.status, row.data["phone"], row.data["username"]), ("valid", "08129999988", "csv-0"))
        self.assertEqual(Customer.objects.count(), 2)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(NetworkJob.objects.exists())
        self.assertEqual(batch.expires_at-batch.created_at, timedelta(minutes=15))
        self.assertNotIn("08129999988", AuditEvent.objects.get(action="customer_import.preview").detail)

    def test_atomic_confirmation_creates_pending_customers_only_and_replays_once(self):
        batch = self.preview(source(self.tenants[0], count=3))
        self.confirm(batch)
        self.confirm(batch)
        created = list(batch.rows.select_related("customer"))
        self.assertEqual(len(created), 3)
        self.assertTrue(all(row.status == "created" and row.customer.service_state == "pending" for row in created))
        self.assertEqual(Customer.objects.count(), 5)
        self.assertEqual(Membership.objects.count(), 2)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(NetworkJob.objects.exists())
        self.assertEqual(AuditEvent.objects.filter(action="customer_import.completed").count(), 1)

    def test_duplicate_upload_key_returns_same_preview_and_rejects_different_file(self):
        data, key = source(self.tenants[0]), uuid.uuid4()
        first = preview_import(self.owners[0], data, key)
        second = preview_import(self.owners[0], data, key)
        self.assertEqual(first.pk, second.pk)
        with self.assertRaises(ValidationError):
            preview_import(self.owners[0], source(self.tenants[0], nama="Berkas berbeda"), key)
        self.assertEqual(CustomerImportBatch.objects.count(), 1)

    def test_utf8_bom_quoted_multiline_and_reordered_headers_are_supported(self):
        data = source(self.tenants[0])
        batch = self.preview(b"\xef\xbb\xbf"+data)
        self.assertEqual(batch.rows.get().data["address"], "Alamat, fiktif\nBaris kedua")
        rows = list(csv.reader(io.StringIO(data.decode())))
        stream = io.StringIO()
        csv.writer(stream).writerows([list(reversed(row)) for row in rows])
        self.assertEqual(self.preview(stream.getvalue().encode()).rows.get().status, "valid")

    def test_reject_non_csv_encoding_control_unknown_duplicate_or_missing_headers(self):
        valid = source(self.tenants[0])
        for data in [b"", b"\xff\xfe", b"\x89PNG\r\n", valid+b"\x00", valid.replace(b"nama,", b"tenant,"),
                valid.replace(b"email,", b"nama,"), b"nama,telepon\nContoh,08129999999", b"nama;email;telepon", b'"unclosed']:
            with self.assertRaises(ValidationError):
                self.preview(data)
        self.assertFalse(CustomerImportBatch.objects.exists())

    def test_byte_row_and_column_limits_fail_before_preview_is_saved(self):
        for data in [b"x"*(MAX_BYTES+1), source(self.tenants[0], count=201),
                b",".join(header.encode() for header in HEADERS)+b"\nwrong,columns\n"]:
            with self.assertRaises(ValidationError):
                self.preview(data)
        self.assertFalse(CustomerImportBatch.objects.exists())
        self.assertEqual(self.preview(source(self.tenants[0], count=200)).rows.count(), 200)

    def test_invalid_rows_are_reported_and_block_entire_confirmation(self):
        data = source(self.tenants[0], count=2).decode().replace("081299900001", "not-a-phone")
        batch = self.preview(data.encode())
        self.assertEqual(batch.rows.filter(status="valid").count(), 1)
        self.assertEqual(batch.rows.filter(status="invalid").count(), 1)
        with self.assertRaises(ValidationError):
            self.confirm(batch)
        self.assertEqual(Customer.objects.count(), 2)

    def test_duplicate_phone_and_normalized_username_within_file_are_invalid(self):
        for changes in [{"telepon": "081299900000"}, {"username": "REPEATED"}]:
            batch = self.preview(source(self.tenants[0], count=2, **changes))
            self.assertEqual(batch.rows.filter(status="invalid").count(), 1)
            with self.assertRaises(ValidationError):
                self.confirm(batch)

    def test_existing_phone_and_case_variant_username_are_invalid_in_own_tenant(self):
        for changes in [{"telepon": self.customers[0].phone}, {"username": self.customers[0].username.upper()}]:
            batch = self.preview(**changes)
            self.assertEqual(batch.rows.get().status, "invalid")
            with self.assertRaises(ValidationError):
                self.confirm(batch)

    def test_plan_must_belong_to_tenant_and_other_tenant_data_does_not_leak(self):
        batch = self.preview(paket_id=self.customers[1].plan_id)
        row = batch.rows.get()
        self.assertEqual(row.status, "invalid")
        self.assertEqual(row.plan_snapshot, {})
        self.assertNotIn(self.tenants[1].name, row.error)

    def test_other_tenant_phone_can_be_imported_without_global_duplicate_check(self):
        batch = self.preview(telepon=self.customers[1].phone, username="different-user")
        self.assertEqual(self.confirm(batch).status, "completed")

    def test_changed_plan_marks_preview_stale_without_creating_any_customer(self):
        batch = self.preview()
        Plan.objects.filter(pk=self.customers[0].plan_id).update(price=300000)
        self.assertEqual(self.confirm(batch).status, "stale")
        self.assertEqual(Customer.objects.count(), 2)

    def test_customer_inserted_after_preview_cancels_all_rows(self):
        batch = self.preview(source(self.tenants[0], count=2))
        other = self.preview(source(self.tenants[0], count=1))
        self.confirm(other)
        self.assertEqual(self.confirm(batch).status, "stale")
        self.assertEqual(Customer.objects.count(), 3)
        self.assertFalse(batch.rows.filter(customer__isnull=False).exists())

    def test_database_conflict_rolls_back_all_created_customers_and_row_links(self):
        batch = self.preview(source(self.tenants[0], count=2))
        with patch("core.customer_import.CustomerImportRow.objects.bulk_update", side_effect=IntegrityError("fake constraint conflict")):
            self.assertEqual(self.confirm(batch).status, "stale")
        self.assertEqual(Customer.objects.count(), 2)
        self.assertFalse(batch.rows.filter(customer__isnull=False).exists())

    def test_tampered_preview_does_not_match_fingerprint(self):
        batch = self.preview()
        row = batch.rows.get()
        CustomerImportRow.objects.filter(pk=row.pk).update(data={**row.data, "phone": "081234567890"})
        self.assertEqual(self.confirm(batch).status, "stale")
        self.assertEqual(Customer.objects.count(), 2)

    def test_expiry_and_access_version_require_new_preview(self):
        batch = self.preview()
        with patch("core.customer_import.timezone.now", return_value=batch.expires_at+timedelta(seconds=1)):
            self.assertEqual(self.confirm(batch).status, "expired")
        changed = self.preview()
        Membership.objects.filter(user=self.owners[0]).update(session_version=2)
        self.assertEqual(self.confirm(changed, version=2).status, "stale")
        self.assertEqual(Customer.objects.count(), 2)

    def test_confirmation_requires_fresh_factor_and_current_session(self):
        batch = self.preview()
        for proof, version in [(time.time()-301, 1), (time.time()+10, 1), (time.time(), 99)]:
            with self.assertRaises(PermissionDenied):
                self.confirm(batch, proof=proof, version=version)
        self.assertEqual(Customer.objects.count(), 2)

    def test_cancel_and_completed_batch_cannot_be_restarted(self):
        batch = self.preview()
        cancel_import(self.owners[0], batch.pk)
        self.assertEqual(self.confirm(batch).status, "cancelled")
        second = self.preview()
        self.confirm(second)
        self.assertEqual(cancel_import(self.owners[0], second.pk).status, "completed")
        self.assertEqual(Customer.objects.count(), 3)

    def test_twenty_active_preview_quota_and_cancel_releases_a_slot(self):
        for _ in range(20):
            last = self.preview()
        with self.assertRaises(ValidationError):
            self.preview()
        cancel_import(self.owners[0], last.pk)
        self.preview()
        self.assertEqual(CustomerImportBatch.objects.count(), 21)

    def test_username_case_uniqueness_is_enforced_by_database_for_all_entry_points(self):
        customer = self.customers[0]
        with self.assertRaises(IntegrityError), transaction.atomic():
            Customer.objects.create(tenant=customer.tenant, plan=customer.plan, code="CASE-DUP", name="Kasus",
                phone="081234567890", username=customer.username.upper(), area="Lab", address="Alamat fiktif")

    def test_plaintext_preview_report_formula_safety_and_no_raw_upload_storage(self):
        batch = self.preview(nama="=1+1", alamat="<script>csv-marker</script>")
        response = self.client.get(f"/customers/imports/{batch.pk}/")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, '<script>csv-marker</script>')
        report = self.client.get(f"/customers/imports/{batch.pk}/report.csv").content.decode("utf-8-sig")
        self.assertIn("'=1+1", report)
        self.assertIn("attachment;", self.client.get(f"/customers/imports/{batch.pk}/report.csv")["Content-Disposition"])
        self.assertFalse(any(field.get_internal_type() == "FileField" for field in CustomerImportBatch._meta.fields))

    def test_batches_are_private_to_creator_and_tenant_including_report_and_mutations(self):
        batch = self.preview()
        same = get_user_model().objects.create_user(username="same-isp-owner")
        Membership.objects.create(user=same, tenant=self.tenants[0], role="owner")
        for user in [same, self.owners[1]]:
            client = Client()
            verified_login(client, user)
            self.assertEqual(client.get("/customers/imports/").context["page"].paginator.count, 0)
            for suffix in ["", "report.csv", "confirm/", "cancel/"]:
                url = f"/customers/imports/{batch.pk}/"+suffix
                response = client.post(url, {"reviewed": "on"}) if suffix in {"confirm/", "cancel/"} else client.get(url)
                self.assertEqual(response.status_code, 404)

    def test_preset_ops_does_not_imply_import_and_custom_role_requires_dependencies(self):
        with self.assertRaises(ValidationError):
            save_role(self.owners[0], "Impor tidak lengkap", {"import_customers"})
        role = save_role(self.owners[0], "Petugas impor", {"import_customers", "customers", "edit_customers"})
        Membership.objects.filter(user=self.owners[0]).update(role="ops")
        for url in ["/customers/imports/", "/customers/imports/new/", "/customers/imports/template.csv"]:
            self.assertEqual(self.client.get(url).status_code, 403)
        Membership.objects.filter(user=self.owners[0]).update(access_role=role)
        self.assertEqual(self.client.get("/customers/imports/").status_code, 200)
        self.assertEqual(self.preview().rows.count(), 1)

    def test_upload_requires_csrf_valid_extension_size_and_valid_request_key(self):
        client = Client(enforce_csrf_checks=True)
        verified_login(client, self.owners[0])
        self.assertEqual(client.post("/customers/imports/new/", {}).status_code, 403)
        for filename, data in [("file.png", source(self.tenants[0])), ("huge.csv", b"x"*(MAX_BYTES+1))]:
            response = self.client.post("/customers/imports/new/", {"request_key": uuid.uuid4(),
                "csv_file": SimpleUploadedFile(filename, data)})
            self.assertEqual(response.status_code, 200)
        with self.assertRaises(ValidationError):
            preview_import(self.owners[0], source(self.tenants[0]), "not-a-uuid")
        self.assertFalse(CustomerImportBatch.objects.exists())

    def test_http_upload_preview_review_confirmation_replay_and_summary(self):
        response = self.client.post("/customers/imports/new/", {"request_key": uuid.uuid4(),
            "csv_file": SimpleUploadedFile("customers.csv", source(self.tenants[0], count=3), content_type="text/csv")})
        batch = CustomerImportBatch.objects.get()
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(response.url)
        self.assertEqual(detail.context["summary"], {"total": 3, "valid": 3, "invalid": 0, "created": 0})
        self.assertEqual(self.client.post(response.url+"confirm/", {}).status_code, 400)
        self.assertEqual(self.client.post(response.url+"confirm/", {"reviewed": "on"}).status_code, 302)
        self.assertEqual(self.client.post(response.url+"confirm/", {"reviewed": "on"}).status_code, 302)
        self.assertEqual(Customer.objects.count(), 5)
        self.assertEqual(self.client.get(response.url).context["summary"]["created"], 3)

    def test_get_routes_do_not_create_objects_and_confirmation_get_is_rejected(self):
        for url in ["/customers/imports/", "/customers/imports/new/", "/customers/imports/template.csv"]:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertFalse(CustomerImportBatch.objects.exists())
        batch = self.preview()
        self.assertEqual(self.client.get(f"/customers/imports/{batch.pk}/confirm/").status_code, 405)

    def test_expired_factor_returns_to_same_batch_without_importing_early(self):
        batch = self.preview()
        session = self.client.session
        session["mfa_verified_at"] = time.time()-301
        session.save()
        response = self.client.post(f"/customers/imports/{batch.pk}/confirm/", {"reviewed": "on"})
        self.assertEqual(response.url, "/account/security/")
        self.assertEqual(self.client.session["stepup_return"], f"/customers/imports/{batch.pk}/")
        self.assertEqual(Customer.objects.count(), 2)


class CustomerImportConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenants, self.owners, self.customers = fixtures()

    def parallel(self, functions):
        def run(function):
            close_old_connections()
            try:
                return function()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(run, functions))

    def test_two_upload_replays_create_one_preview(self):
        data, key = source(self.tenants[0]), uuid.uuid4()
        results = self.parallel([lambda: preview_import(self.owners[0], data, key).pk]*2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(CustomerImportBatch.objects.count(), 1)

    def test_two_confirmations_of_same_batch_create_once(self):
        batch = preview_import(self.owners[0], source(self.tenants[0]), uuid.uuid4())
        self.parallel([lambda: confirm_import(self.owners[0], batch.pk, time.time(), 1)]*2)
        self.assertEqual(Customer.objects.count(), 3)
        self.assertEqual(batch.rows.filter(status="created").count(), 1)

    def test_overlapping_previews_create_one_and_mark_other_stale(self):
        data = source(self.tenants[0])
        first, second = [preview_import(self.owners[0], data, uuid.uuid4()) for _ in range(2)]
        results = self.parallel([lambda: confirm_import(self.owners[0], first.pk, time.time(), 1).status,
            lambda: confirm_import(self.owners[0], second.pk, time.time(), 1).status])
        self.assertEqual(sorted(results), ["completed", "stale"])
        self.assertEqual(Customer.objects.count(), 3)

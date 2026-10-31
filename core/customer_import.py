"""Private, bounded CSV preview and atomic create-only customer import."""
import csv
import hashlib
import io
import json
import time
import uuid
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from .billing import audit, retry_locked
from .forms import CustomerForm
from .models import Customer, CustomerImportBatch, CustomerImportRow, Tenant
from .operations import member_for
from .permissions import capabilities

MAX_BYTES = 128 * 1024
MAX_ROWS = 200
COLUMNS = {"nama": "name", "email": "email", "telepon": "phone", "username": "username",
           "paket_id": "plan", "wilayah": "area", "alamat": "address"}


def manager(actor):
    member = member_for(actor)
    if not {"import_customers", "edit_customers", "customers"} <= capabilities(member):
        raise PermissionDenied("Izin impor pelanggan tidak tersedia.")
    return member


def csv_records(source):
    if not isinstance(source, bytes) or not 0 < len(source) <= MAX_BYTES:
        raise ValidationError("CSV harus berukuran 1–131072 byte (128 KiB).")
    try:
        text = source.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("Gunakan CSV UTF-8, dengan atau tanpa BOM.") from None
    if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        raise ValidationError("CSV mengandung karakter kontrol yang tidak diizinkan.")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = [cell.strip().lower() for cell in next(reader)]
        if len(header) != len(COLUMNS) or set(header) != set(COLUMNS):
            raise ValidationError("Header harus tepat: " + ",".join(COLUMNS) + ". Tidak menerima kolom tenant/status/sandi.")
        records = []
        for values in reader:
            if not values or not any(value.strip() for value in values):
                continue
            if len(values) != len(header):
                raise ValidationError("Jumlah kolom tidak cocok dengan header. Periksa delimiter dan tanda kutip CSV.")
            if len(records) >= MAX_ROWS:
                raise ValidationError("Maksimal 200 baris data per impor.")
            records.append({COLUMNS[key]: value.strip() for key, value in zip(header, values)})
    except (csv.Error, StopIteration):
        raise ValidationError("Format CSV tidak valid atau header tidak ditemukan.") from None
    if not records:
        raise ValidationError("CSV tidak memiliki baris data.")
    return records


def validate_record(tenant, values):
    values = {**values, "username": values.get("username", "").lower()}
    form = CustomerForm(values, tenant=tenant)
    if not form.is_valid():
        errors = []
        for field, messages in form.errors.items():
            label = form.fields[field].label if field in form.fields else "Data pelanggan"
            errors.extend(f"{label}: {message}" for message in messages)
        # Retain bounded known-column values, never the uploaded file itself.
        return {key: str(value)[:300] for key, value in values.items()}, {}, "; ".join(errors)[:700], None
    data = {key: form.cleaned_data[key] for key in ["name", "email", "phone", "username", "area", "address"]}
    plan = form.cleaned_data["plan"]
    data["plan"] = str(plan.pk)
    plan_snapshot = {"id": plan.pk, "name": plan.name, "speed_mbps": plan.speed_mbps, "price": plan.price}
    if Customer.objects.filter(tenant=tenant).filter(Q(phone=data["phone"]) | Q(username__iexact=data["username"])).exists():
        return data, plan_snapshot, "Telepon atau username sudah digunakan pelanggan ISP ini.", None
    customer = form.save(commit=False)
    customer.service_state = "pending"
    return data, plan_snapshot, "", customer


def fingerprint(rows):
    values = [{"number": row.row_number, "data": row.data, "plan": row.plan_snapshot,
        "status": row.status, "error": row.error} for row in rows]
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@retry_locked
@transaction.atomic
def preview_import(actor, source, request_key):
    member = manager(actor)
    # Serialize preview quota, duplicate requests, and imports without locking child FK inserts.
    Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    member = manager(actor)
    try:
        request_key = uuid.UUID(str(request_key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Kunci permintaan pratinjau tidak valid.") from None
    records = csv_records(source)
    source_digest = hashlib.sha256(source).hexdigest()
    prior = CustomerImportBatch.objects.filter(tenant=member.tenant, created_by=actor, request_key=request_key).first()
    if prior:
        if prior.source_digest != source_digest:
            raise ValidationError("Kunci permintaan sudah digunakan untuk CSV berbeda.")
        return prior
    now = timezone.now()
    if CustomerImportBatch.objects.filter(tenant=member.tenant, created_by=actor, status="preview", expires_at__gte=now).count() >= 20:
        raise ValidationError("Maksimal 20 pratinjau aktif. Batalkan pratinjau lama sebelum mengunggah lagi.")
    rows, phones, usernames = [], set(), set()
    for number, record in enumerate(records, start=2):
        data, plan, error, _ = validate_record(member.tenant, record)
        if not error:
            if data["phone"] in phones or data["username"] in usernames:
                error = "Telepon atau username duplikat dalam CSV. Tidak ada baris yang akan diimpor sampai seluruh kesalahan diperbaiki."
            phones.add(data["phone"])
            usernames.add(data["username"])
        rows.append(CustomerImportRow(row_number=number, data=data, plan_snapshot=plan,
            status="invalid" if error else "valid", error=error))
    batch = CustomerImportBatch.objects.create(tenant=member.tenant, created_by=actor, request_key=request_key,
        authorization_version=member.session_version, source_digest=source_digest, fingerprint=fingerprint(rows),
        created_at=now, expires_at=now+timedelta(minutes=15))
    for row in rows:
        row.batch = batch
    CustomerImportRow.objects.bulk_create(rows)
    audit(member.tenant, actor, "customer_import.preview", batch.pk, f"{len(rows)} baris; belum ada pelanggan ditambahkan.", batch.pk)
    return batch


def own_batch(actor, batch_id, lock=False):
    member = manager(actor)
    query = CustomerImportBatch.objects.select_for_update() if lock else CustomerImportBatch.objects
    return query.get(pk=batch_id, tenant=member.tenant, created_by=actor)


def finish(batch, actor, status, reason):
    batch.status, batch.reason, batch.finished_at = status, reason, timezone.now()
    batch.save(update_fields=["status", "reason", "finished_at"])
    audit(batch.tenant, actor, "customer_import."+status, batch.pk, reason, batch.pk)
    return batch


@retry_locked
@transaction.atomic
def confirm_import(actor, batch_id, verified_at, access_version):
    member = manager(actor)
    Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    member = manager(actor)
    if member.session_version != access_version or not 0 <= time.time()-verified_at <= 300:
        raise PermissionDenied("MFA terbaru dan sesi yang berlaku diperlukan untuk konfirmasi impor.")
    batch = own_batch(actor, batch_id, lock=True)
    if batch.status != "preview":
        return batch
    if batch.expires_at < timezone.now():
        return finish(batch, actor, "expired", "Pratinjau kedaluwarsa. Unggah ulang CSV untuk pemeriksaan terbaru.")
    if batch.authorization_version != member.session_version:
        return finish(batch, actor, "stale", "Hak akses berubah setelah pratinjau. Buat pratinjau baru.")
    rows = list(batch.rows.all())
    if not rows or len(rows) > MAX_ROWS or fingerprint(rows) != batch.fingerprint:
        return finish(batch, actor, "stale", "Rincian pratinjau tidak cocok. Buat pratinjau baru.")
    if any(row.status != "valid" for row in rows):
        raise ValidationError("Semua kesalahan CSV harus diperbaiki sebelum konfirmasi. Belum ada pelanggan ditambahkan.")
    customers = []
    for row in rows:
        data, plan, error, customer = validate_record(member.tenant, row.data)
        if error or data != row.data or plan != row.plan_snapshot:
            return finish(batch, actor, "stale", "Pelanggan duplikat, paket atau data berubah. Buat pratinjau baru; tidak ada baris ditambahkan.")
        customer.code = "TN-"+uuid.uuid4().hex[:12].upper()
        customers.append(customer)
    try:
        with transaction.atomic():
            Customer.objects.bulk_create(customers)
            for row, customer in zip(rows, customers):
                row.customer, row.status = customer, "created"
            CustomerImportRow.objects.bulk_update(rows, ["customer", "status"])
    except IntegrityError:
        return finish(batch, actor, "stale", "Konflik data saat penyimpanan. Seluruh impor dibatalkan; buat pratinjau baru.")
    return finish(batch, actor, "completed", f"{len(rows)} pelanggan ditambahkan atomik dengan status Menunggu provisioning. Tanpa faktur, akun portal atau job router.")


@retry_locked
@transaction.atomic
def cancel_import(actor, batch_id):
    batch = own_batch(actor, batch_id, lock=True)
    if batch.status == "preview":
        return finish(batch, actor, "cancelled", "Pratinjau dibatalkan. Tidak ada pelanggan ditambahkan.")
    return batch

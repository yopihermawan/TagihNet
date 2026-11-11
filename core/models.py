import calendar
import uuid
from datetime import date
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone


class Tenant(models.Model):
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=250, default="Indonesia")
    tax_bp = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(10000)])
    invoice_footer = models.TextField(max_length=1000, default="Terima kasih telah mempercayakan koneksi Anda kepada kami.")
    due_day = models.PositiveSmallIntegerField(default=20, validators=[MinValueValidator(1), MaxValueValidator(31)])
    sla_low_hours = models.PositiveSmallIntegerField(default=72, validators=[MinValueValidator(1), MaxValueValidator(720)])
    sla_normal_hours = models.PositiveSmallIntegerField(default=24, validators=[MinValueValidator(1), MaxValueValidator(720)])
    sla_high_hours = models.PositiveSmallIntegerField(default=8, validators=[MinValueValidator(1), MaxValueValidator(720)])
    sla_urgent_hours = models.PositiveSmallIntegerField(default=2, validators=[MinValueValidator(1), MaxValueValidator(720)])

    def __str__(self):
        return self.name


class Plan(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=80)
    speed_mbps = models.PositiveIntegerField()
    price = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(100000000)])

    def __str__(self):
        return f"{self.name} · {self.speed_mbps} Mbps"


class Customer(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    code = models.CharField(max_length=24)
    name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20)
    address = models.CharField(max_length=250)
    area = models.CharField(max_length=80)
    username = models.SlugField(max_length=64)
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    service_state = models.CharField(max_length=16, choices=[("active", "Aktif"), ("isolated", "Diisolir"), ("pending", "Menunggu provisioning")], default="active")
    router_available = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="customer_code_per_tenant"),
            models.UniqueConstraint(fields=["tenant", "username"], name="customer_username_per_tenant"),
            models.UniqueConstraint(models.F("tenant"), models.functions.Lower("username"), name="customer_username_ci_per_tenant"),
            models.UniqueConstraint(fields=["tenant", "phone"], name="customer_phone_per_tenant"),
        ]

    def __str__(self):
        return f"{self.code} · {self.name}"


class CustomerImportBatch(models.Model):
    STATES = [("preview", "Pratinjau"), ("completed", "Selesai"), ("cancelled", "Dibatalkan"),
              ("stale", "Perlu pratinjau baru"), ("expired", "Kedaluwarsa")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    request_key = models.UUIDField()
    authorization_version = models.PositiveIntegerField()
    source_digest = models.CharField(max_length=64)
    fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATES, default="preview")
    reason = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["tenant", "created_by", "request_key"], name="import_request_per_creator")]


class CustomerImportRow(models.Model):
    STATES = [("valid", "Siap diimpor"), ("invalid", "Perlu diperbaiki"), ("created", "Ditambahkan")]
    batch = models.ForeignKey(CustomerImportBatch, related_name="rows", on_delete=models.PROTECT)
    row_number = models.PositiveIntegerField()
    data = models.JSONField()
    plan_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATES)
    error = models.CharField(max_length=700, blank=True)
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ["row_number", "pk"]
        constraints = [models.UniqueConstraint(fields=["batch", "row_number"], name="one_import_row_number"),
            models.CheckConstraint(condition=(Q(status="created", customer__isnull=False)
                | Q(status__in=["valid", "invalid"], customer__isnull=True)), name="import_row_customer_matches_state")]


class AccessRole(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=80)
    capabilities = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="role_name_per_tenant")]

    def __str__(self):
        return self.name


class Membership(models.Model):
    ROLES = [("owner", "Owner ISP"), ("ops", "Admin operasional"), ("finance", "Keuangan"),
             ("cashier", "Kasir"), ("noc", "NOC"), ("customer", "Pelanggan"), ("technician", "Teknisi")]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    role = models.CharField(max_length=16, choices=ROLES)
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.PROTECT)
    access_role = models.ForeignKey(AccessRole, null=True, blank=True, on_delete=models.PROTECT)
    session_version = models.PositiveIntegerField(default=1)

    @property
    def access_label(self):
        return self.access_role.name if self.access_role_id else self.get_role_display()


class MFADevice(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    encrypted_secret = models.TextField()
    last_step = models.BigIntegerField(default=-1)
    recovery_hashes = models.JSONField(default=list)
    revision = models.UUIDField(default=uuid.uuid4)
    created_at = models.DateTimeField(default=timezone.now)


class AuthRateBucket(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    attempts = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()


class Invoice(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    period = models.DateField(help_text="Tanggal pertama bulan layanan")
    issued_at = models.DateTimeField(default=timezone.now)
    due_date = models.DateField()
    customer_name = models.CharField(max_length=100)
    customer_address = models.CharField(max_length=250)
    subtotal = models.PositiveBigIntegerField()
    discount = models.PositiveBigIntegerField(default=0)
    tax_bp = models.PositiveIntegerField(default=0)
    tax_amount = models.PositiveBigIntegerField(default=0)
    total = models.PositiveBigIntegerField()
    footer = models.TextField(max_length=1000)

    class Meta:
        ordering = ["-issued_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "customer", "period"], name="one_monthly_invoice"),
            models.CheckConstraint(condition=Q(discount__lte=models.F("subtotal")), name="valid_discount"),
            models.CheckConstraint(condition=Q(total__gt=0), name="positive_invoice"),
        ]

    @property
    def number(self):
        return f"TN-{self.period:%Y%m}-{self.pk:05d}"

    @property
    def paid(self):
        return self.payments.filter(reversal__isnull=True).aggregate(value=Sum("amount"))["value"] or 0

    @property
    def balance(self):
        return self.total - self.paid

    @property
    def status(self):
        if self.balance == 0:
            return "paid"
        if self.due_date < timezone.localdate():
            return "overdue"
        return "partial" if self.paid else "issued"

    @property
    def status_label(self):
        return {"paid": "Lunas", "overdue": "Menunggak", "partial": "Dibayar sebagian", "issued": "Terbit"}[self.status]


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, related_name="lines", on_delete=models.PROTECT)
    description = models.CharField(max_length=250)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.PositiveBigIntegerField()

    @property
    def total(self):
        return self.quantity * self.unit_price


class BillingBatch(models.Model):
    STATES = [("preview", "Pratinjau"), ("queued", "Menunggu worker"), ("processing", "Diproses"),
              ("completed", "Selesai"), ("stale", "Data berubah"), ("expired", "Kedaluwarsa"),
              ("blocked", "Dihentikan untuk ditinjau"), ("cancelled", "Dibatalkan")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    period = models.DateField()
    filters = models.JSONField(default=dict)
    discount = models.PositiveIntegerField(default=0)
    fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATES, default="preview")
    authorization_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=250, blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["status", "created_at"], name="billing_batch_queue")]


class BillingBatchItem(models.Model):
    batch = models.ForeignKey(BillingBatch, related_name="items", on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    snapshot = models.JSONField()
    status = models.CharField(max_length=12, choices=[("pending", "Akan diterbitkan"), ("created", "Diterbitkan"),
                                                   ("skipped", "Sudah ada · dilewati")], default="pending")
    invoice = models.ForeignKey(Invoice, null=True, blank=True, on_delete=models.PROTECT)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["batch", "customer"], name="customer_once_per_batch")]


class Payment(models.Model):
    source = models.CharField(max_length=24, default="simulation", choices=[("simulation", "Simulasi manual"), ("midtrans_sandbox", "Midtrans Sandbox")])
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    invoice = models.ForeignKey(Invoice, related_name="payments", on_delete=models.PROTECT)
    amount = models.PositiveBigIntegerField()
    event_key = models.UUIDField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "event_key"], name="unique_payment_event"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="positive_payment"),
        ]


class PaymentReversal(models.Model):
    payment = models.OneToOneField(Payment, related_name="reversal", on_delete=models.PROTECT)
    reason = models.CharField(max_length=250)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)


class GatewayConfig(models.Model):
    tenant = models.OneToOneField(Tenant, on_delete=models.PROTECT)
    merchant_id = models.CharField(max_length=64)
    enabled = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(default=timezone.now)


class GatewayReconciliationPolicy(models.Model):
    STATES = [("never", "Belum dijalankan"), ("idle", "Tidak ada order untuk diperiksa"),
        ("completed", "Pemeriksaan selesai"), ("partial", "Sebagian belum terverifikasi"),
        ("failed", "Gateway belum dapat diverifikasi"), ("blocked", "Perlu persetujuan ulang")]
    tenant = models.OneToOneField(Tenant, on_delete=models.PROTECT)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    authorization_version = models.PositiveIntegerField(default=0)
    merchant_id = models.CharField(max_length=64, blank=True)
    enabled = models.BooleanField(default=False)
    include_settled = models.BooleanField(default=False)
    interval_seconds = models.PositiveIntegerField(default=60)
    version = models.PositiveIntegerField(default=1)
    approved_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=16, choices=STATES, default="never")
    last_verified = models.PositiveIntegerField(default=0)
    last_unavailable = models.PositiveIntegerField(default=0)
    failure_streak = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(interval_seconds__in=[60, 300, 900]), name="gateway_policy_interval"),
            models.CheckConstraint(condition=(Q(lease_token__isnull=True, lease_until__isnull=True)
                | Q(lease_token__isnull=False, lease_until__isnull=False)), name="gateway_policy_lease_pair"),
        ]


class GatewayOrder(models.Model):
    STATES = [("creating", "Menyiapkan checkout"), ("pending", "Menunggu pembayaran"),
        ("settled", "Pembayaran tercatat"), ("deny", "Ditolak"), ("cancel", "Dibatalkan gateway"),
        ("expire", "Kedaluwarsa di gateway"), ("review", "Perlu rekonsiliasi")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    invoice = models.ForeignKey(Invoice, related_name="gateway_orders", on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    merchant_id = models.CharField(max_length=64)
    amount = models.PositiveBigIntegerField()
    status = models.CharField(max_length=16, choices=STATES, default="creating")
    checkout_url = models.URLField(max_length=500, blank=True)
    transaction_id = models.CharField(max_length=64, blank=True)
    payment = models.OneToOneField(Payment, null=True, blank=True, on_delete=models.PROTECT)
    reason = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)

    @property
    def verification_failed(self):
        return bool(self.last_checked_at and (not self.last_verified_at or self.last_checked_at > self.last_verified_at))

    @property
    def order_id(self):
        return f"TN-SBX-{self.pk.hex}"

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="positive_gateway_order"),
            models.UniqueConstraint(fields=["invoice"], condition=Q(status__in=["creating", "pending"], payment__isnull=True), name="one_open_gateway_order"),
            models.UniqueConstraint(fields=["merchant_id", "transaction_id"], condition=~Q(transaction_id=""), name="unique_gateway_transaction"),
        ]


class GatewayEvent(models.Model):
    order = models.ForeignKey(GatewayOrder, related_name="events", on_delete=models.PROTECT)
    fingerprint = models.CharField(max_length=64)
    gateway_status = models.CharField(max_length=32)
    outcome = models.CharField(max_length=32)
    created_at = models.DateTimeField(default=timezone.now)

    @property
    def status_label(self):
        return {"pending": "Menunggu pembayaran", "capture": "Pembayaran kartu diproses", "settlement": "Pembayaran lunas",
            "deny": "Ditolak", "cancel": "Dibatalkan gateway", "expire": "Kedaluwarsa di gateway",
            "refund": "Pengembalian dana", "partial_refund": "Pengembalian sebagian dana",
            "chargeback": "Sengketa pembayaran", "partial_chargeback": "Sengketa sebagian pembayaran",
            "failure": "Transaksi gagal"}.get(self.gateway_status, self.gateway_status)

    @property
    def outcome_label(self):
        return dict(GatewayOrder.STATES).get(self.outcome, self.outcome)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [models.UniqueConstraint(fields=["order", "fingerprint"], name="unique_gateway_event")]


class GatewayExpiry(models.Model):
    STATES = [("requested", "Diminta"), ("confirmed", "Dikonfirmasi berakhir"),
        ("unavailable", "Belum dapat dipastikan"), ("blocked", "Tidak dapat diakhiri")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(GatewayOrder, related_name="expiry_requests", on_delete=models.PROTECT)
    request_key = models.UUIDField()
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=250)
    status = models.CharField(max_length=16, choices=STATES, default="requested")
    result = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["order", "request_key"], name="gateway_expiry_request_once")]


class NetworkJob(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=[("pending", "Menunggu"), ("confirmed", "Terkonfirmasi (simulasi)"), ("failed", "Gagal")], default="pending")
    attempts = models.PositiveIntegerField(default=0)
    result = models.CharField(max_length=250, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    run_after = models.DateTimeField(default=timezone.now)


class Ticket(models.Model):
    PRIORITIES = [("low", "Rendah"), ("normal", "Normal"), ("high", "Tinggi"), ("urgent", "Mendesak")]
    CATEGORIES = [("connection", "Koneksi"), ("billing", "Penagihan"), ("installation", "Pemasangan"), ("other", "Lainnya")]
    STATES = [("open", "Terbuka"), ("in_progress", "Dalam penanganan"), ("waiting_customer", "Menunggu pelanggan"),
              ("resolved", "Terselesaikan"), ("closed", "Ditutup")]
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    assigned_to = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.PROTECT)
    request_key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    title = models.CharField(max_length=140)
    description = models.TextField(max_length=4000)
    priority = models.CharField(max_length=16, choices=PRIORITIES, default="normal")
    category = models.CharField(max_length=16, choices=CATEGORIES)
    status = models.CharField(max_length=20, choices=STATES, default="open")
    version = models.PositiveIntegerField(default=1)
    sla_policy = models.JSONField()
    sla_cycle = models.PositiveIntegerField(default=1)
    sla_due_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-updated_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["tenant", "request_key"], name="ticket_request_per_tenant")]

    @property
    def number(self):
        return f"TN-T-{self.pk:05d}"

    @property
    def sla_label(self):
        if self.resolved_at:
            return "Selesai tepat SLA" if self.resolved_at <= self.sla_due_at else "Selesai melewati SLA"
        return "Melewati SLA" if timezone.now() > self.sla_due_at else "Dalam SLA"


class Installation(models.Model):
    STATES = [("prospect", "Prospek"), ("survey", "Survei"), ("scheduled", "Terjadwal"), ("installing", "Pengerjaan"),
              ("installed", "Terpasang"), ("activated", "Teraktivasi administrasi"), ("cancelled", "Dibatalkan")]
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    request_key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20)
    username = models.SlugField(max_length=64)
    address = models.CharField(max_length=250)
    area = models.CharField(max_length=80)
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    assigned_to = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.PROTECT)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATES, default="prospect")
    version = models.PositiveIntegerField(default=1)
    customer = models.OneToOneField(Customer, null=True, blank=True, on_delete=models.PROTECT)
    invoice = models.OneToOneField(Invoice, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-updated_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["tenant", "request_key"], name="installation_request_per_tenant")]

    @property
    def number(self):
        return f"TN-PSB-{self.pk:05d}"


class WorkEvent(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    ticket = models.ForeignKey(Ticket, null=True, blank=True, related_name="events", on_delete=models.PROTECT)
    installation = models.ForeignKey(Installation, null=True, blank=True, related_name="events", on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=24)
    body = models.TextField(max_length=4000)
    internal = models.BooleanField(default=True)
    request_key = models.UUIDField(null=True, blank=True)
    signature = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "request_key"], name="work_event_request_per_tenant"),
            models.CheckConstraint(condition=(Q(ticket__isnull=False, installation__isnull=True)
                | Q(ticket__isnull=True, installation__isnull=False)), name="one_work_event_target"),
        ]


class ReminderPolicy(models.Model):
    tenant = models.OneToOneField(Tenant, on_delete=models.PROTECT)
    billing_enabled = models.BooleanField(default=False)
    sla_enabled = models.BooleanField(default=False)
    lead_days = models.PositiveSmallIntegerField(default=3, validators=[MaxValueValidator(14)])
    billing_hour = models.PositiveSmallIntegerField(default=9, validators=[MaxValueValidator(23)])
    scenario = models.CharField(max_length=16, choices=[("normal", "Normal"), ("unavailable", "Gangguan simulasi")], default="normal")
    authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    authorization_version = models.PositiveIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(default=timezone.now)


class Reminder(models.Model):
    KINDS = [("billing", "Jatuh tempo faktur"), ("sla", "SLA tiket")]
    STATES = [("pending", "Menunggu"), ("simulated", "Tercatat simulasi"), ("skipped", "Dilewati"),
              ("blocked", "Tertahan · perlu persetujuan ulang"), ("failed", "Gagal")]
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    kind = models.CharField(max_length=12, choices=KINDS)
    invoice = models.ForeignKey(Invoice, null=True, blank=True, on_delete=models.PROTECT)
    ticket = models.ForeignKey(Ticket, null=True, blank=True, on_delete=models.PROTECT)
    business_date = models.DateField()
    deadline_snapshot = models.DateTimeField(null=True, blank=True)
    sla_cycle = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=STATES, default="pending")
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    recipient = models.CharField(max_length=160, blank=True)
    body = models.TextField(max_length=2000, blank=True)
    reason = models.CharField(max_length=250, blank=True)
    policy_version_planned = models.PositiveIntegerField()
    policy_version_processed = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "invoice", "business_date"], name="one_invoice_reminder_per_day"),
            models.UniqueConstraint(fields=["tenant", "ticket", "sla_cycle", "deadline_snapshot"], name="one_sla_reminder_per_deadline"),
            models.CheckConstraint(condition=(Q(kind="billing", invoice__isnull=False, ticket__isnull=True, deadline_snapshot__isnull=True, sla_cycle=0)
                | Q(kind="sla", invoice__isnull=True, ticket__isnull=False, deadline_snapshot__isnull=False, sla_cycle__gte=1)), name="reminder_target_matches_kind"),
        ]


class MonitoringDevice(models.Model):
    SCENARIOS = [("normal", "Sesi contoh online/offline"), ("empty", "Tidak ada sesi"),
                 ("stale", "Data kedaluwarsa"), ("unavailable", "Router tidak dapat dijangkau")]
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=80)
    location = models.CharField(max_length=80, blank=True)
    enabled = models.BooleanField(default=False)
    scenario = models.CharField(max_length=16, choices=SCENARIOS, default="normal")
    version = models.PositiveIntegerField(default=1)
    authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    authorization_version = models.PositiveIntegerField(default=0)
    next_poll_at = models.DateTimeField(null=True, blank=True, default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name", "pk"]
        indexes = [models.Index(fields=["enabled", "next_poll_at"], name="monitor_due_idx")]
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="monitor_name_per_tenant")]


class MonitoringPoll(models.Model):
    STATES = [("success", "Snapshot tersimpan"), ("failed", "Gagal diambil"), ("blocked", "Persetujuan tertahan")]
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    device = models.ForeignKey(MonitoringDevice, related_name="polls", on_delete=models.PROTECT)
    device_version = models.PositiveIntegerField()
    bucket = models.BigIntegerField()
    scenario = models.CharField(max_length=16, choices=MonitoringDevice.SCENARIOS)
    status = models.CharField(max_length=16, choices=STATES)
    observed_at = models.DateTimeField(null=True)
    sessions = models.JSONField(default=list, blank=True)
    reason = models.CharField(max_length=250)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["device", "-created_at", "-id"], name="monitor_latest_idx")]
        constraints = [models.UniqueConstraint(fields=["device", "device_version", "bucket"], name="one_monitor_poll_per_minute"),
            models.CheckConstraint(condition=(Q(status="success", observed_at__isnull=False)
                | Q(status__in=["failed", "blocked"], observed_at__isnull=True)), name="monitor_snapshot_requires_time")]


class AuditEvent(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    target = models.CharField(max_length=80)
    detail = models.CharField(max_length=250, blank=True)
    correlation_id = models.UUIDField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]


class AuthAttempt(models.Model):
    key = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)


def month_due(period, day):
    return date(period.year, period.month, min(day, calendar.monthrange(period.year, period.month)[1]))

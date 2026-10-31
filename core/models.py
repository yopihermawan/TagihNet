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
    service_state = models.CharField(max_length=16, choices=[("active", "Aktif"), ("isolated", "Diisolir")], default="active")
    router_available = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="customer_code_per_tenant"),
            models.UniqueConstraint(fields=["tenant", "username"], name="customer_username_per_tenant"),
            models.UniqueConstraint(fields=["tenant", "phone"], name="customer_phone_per_tenant"),
        ]

    def __str__(self):
        return f"{self.code} · {self.name}"


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
             ("cashier", "Kasir"), ("noc", "NOC"), ("customer", "Pelanggan")]
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


class Payment(models.Model):
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


class NetworkJob(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=[("pending", "Menunggu"), ("confirmed", "Terkonfirmasi (simulasi)"), ("failed", "Gagal")], default="pending")
    attempts = models.PositiveIntegerField(default=0)
    result = models.CharField(max_length=250, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    run_after = models.DateTimeField(default=timezone.now)


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

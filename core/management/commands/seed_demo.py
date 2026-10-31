import os
import secrets
import uuid
from datetime import timedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from core.billing import issue_invoice, record_payment
from core.models import Customer, Membership, NetworkJob, Plan, Tenant

class Command(BaseCommand):
    help = "Buat data fiktif khusus lokal secara idempoten."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG or not settings.SIMULATION:
            raise CommandError("Seed ditolak: DEBUG=1 dan SIMULATION=1 diperlukan.")
        password = os.getenv("TAGIHNET_DEMO_PASSWORD") or secrets.token_urlsafe(18)
        if len(password) < 12:
            raise CommandError("Kata sandi demo minimal 12 karakter.")
        names = ["Aditya Pratama", "Aisyah Rahma", "Bagas Saputra", "Citra Lestari", "Dewi Anggraini",
                 "Dimas Setiawan", "Eka Wulandari", "Fajar Nugraha", "Farah Nabila", "Gilang Ramadhan",
                 "Hana Putri", "Indra Wijaya", "Intan Permata", "Joko Santoso", "Kartika Sari", "Lukman Hakim",
                 "Maya Safitri", "Nadia Maharani", "Oki Firmansyah", "Putri Amelia", "Rafi Maulana", "Ratna Dewi",
                 "Rizky Ananda", "Salsa Aulia", "Taufik Hidayat", "Vina Melati", "Wahyu Prakoso", "Yulia Astuti"]
        first = timezone.localdate().replace(day=1)
        previous = (first - timedelta(days=1)).replace(day=1)
        for tenant_index, tenant_name in enumerate(["Nusantara Net", "Cakrawala Net"]):
            owner_name = "owner" if tenant_index == 0 else "owner2"
            owner, created = get_user_model().objects.get_or_create(username=owner_name, defaults={"email": f"{owner_name}@example.invalid", "first_name": "Operator"})
            if created:
                owner.set_password(password)
                owner.save()
                self.stdout.write(f"Akun lokal dibuat: {owner_name}")
            if not created:
                if not Membership.objects.filter(user=owner, role="owner").exists():
                    raise CommandError("Username seed sudah digunakan akun lain. Tidak ada hak akses yang diubah.")
                tenant = owner.membership.tenant
            else:
                tenant = Tenant.objects.create(name=tenant_name, address="Jl. Koneksi No. 8, Bandung · Data fiktif", due_day=20)
            Membership.objects.get_or_create(user=owner, defaults={"tenant": tenant, "role": "owner"})
            plans = [Plan.objects.get_or_create(tenant=tenant, name=name, defaults={"speed_mbps": speed, "price": price})[0]
                     for name, speed, price in [("Rumah Hemat", 15, 150000), ("Rumah Nyaman", 30, 225000), ("Rumah Prima", 50, 325000)]]
            for index, name in enumerate(names if tenant_index == 0 else ["Pelanggan ISP Kedua", "Pelanggan Uji B"]):
                customer, created = Customer.objects.get_or_create(tenant=tenant, code=f"TN-{index+1:04d}", defaults={
                    "name": name, "email": f"pelanggan{index}@example.invalid", "phone": f"08120{tenant_index}{index:06d}",
                    "address": f"Jl. Contoh No. {index+1}, Bandung", "area": ["Antapani", "Dago", "Cibiru"][index % 3],
                    "username": f"rumah-{index+1:03d}", "plan": plans[index % 3], "router_available": index != 5})
                if created:
                    invoice, _ = issue_invoice(tenant, customer.pk, first, owner)
                    if index % 3 != 0:
                        record_payment(tenant, invoice.pk, invoice.total if index % 3 == 1 else invoice.total // 2,
                                       uuid.uuid4(), owner, "Seed fiktif untuk demonstrasi")
                    if index < 5:
                        issue_invoice(tenant, customer.pk, previous, owner)
            if tenant_index == 0:
                for username, role in [("keuangan", "finance"), ("kasir", "cashier"), ("noc", "noc"), ("pelanggan", "customer")]:
                    user, created = get_user_model().objects.get_or_create(username=username, defaults={"email": f"{username}@example.invalid"})
                    if created:
                        user.set_password(password)
                        user.save()
                    Membership.objects.get_or_create(user=user, defaults={"tenant": tenant, "role": role,
                        "customer": tenant.customer_set.order_by("id").first() if role == "customer" else None})
        self.stdout.write(self.style.SUCCESS("Seed selesai. Data yang sudah ada tidak diubah; kata sandi lama tidak direset."))
        if not os.getenv("TAGIHNET_DEMO_PASSWORD"):
            self.stdout.write(f"Kata sandi untuk akun BARU pada eksekusi ini: {password}")

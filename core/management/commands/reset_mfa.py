"""Offline recovery requires a trusted host administrator and verified identity."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from core.billing import audit
from core.models import MFADevice, Membership


class Command(BaseCommand):
    help = "Reset MFA offline setelah identitas diverifikasi. Mencabut sesi; tidak mengganti kata sandi."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--confirm-user-id", type=int, required=True)
        parser.add_argument("--reason", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        if not 20 <= len(options["reason"].strip()) <= 250:
            raise CommandError("Alasan verifikasi identitas wajib 20–250 karakter; jangan sertakan secret.")
        member = Membership.objects.select_for_update(of=("self",)).select_related("user").filter(
            user__username=options["username"], user_id=options["confirm_user_id"]).first()
        if not member:
            raise CommandError("Username dan ID konfirmasi tidak cocok. Tidak ada perubahan.")
        if not MFADevice.objects.filter(user=member.user).exists():
            raise CommandError("MFA tidak terdaftar. Tidak ada perubahan.")
        MFADevice.objects.filter(user=member.user).delete()
        Membership.objects.filter(pk=member.pk).update(session_version=F("session_version") + 1)
        audit(member.tenant, None, "auth.mfa_reset_offline", member.user_id, options["reason"].strip())
        self.stdout.write(self.style.SUCCESS("MFA direset, sesi dicabut. Operator wajib mendaftar ulang saat login berikutnya."))

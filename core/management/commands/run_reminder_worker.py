import time
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError
from django.db.models import F, Q
from django.utils import timezone
from core.models import Reminder, ReminderPolicy
from core.reminders import plan_reminders, process_reminder


class Command(BaseCommand):
    help = "Rencanakan dan proses pengingat simulasi untuk kebijakan opt-in; tanpa pesan eksternal."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not settings.SIMULATION:
            raise CommandError("Worker pengingat ditolak di luar mode simulasi.")
        if not 1 <= options["limit"] <= 500:
            raise CommandError("--limit harus 1–500.")
        try:
            while True:
                failed = False
                policies = list(ReminderPolicy.objects.filter(Q(billing_enabled=True) | Q(sla_enabled=True))
                    .order_by(F("last_checked_at").asc(nulls_first=True), "pk").values_list("pk", flat=True)[:10])
                for policy_id in policies:
                    try:
                        count = plan_reminders(policy_id, options["limit"])
                        self.stdout.write(f"policy={policy_id} planned={count}")
                    except OperationalError:
                        failed = True
                        self.stderr.write(f"policy={policy_id} database_busy; retry later")
                ids = list(Reminder.objects.filter(status__in=["pending", "failed"], attempts__lt=3,
                    next_attempt_at__lte=timezone.now()).order_by("created_at", "pk").values_list("pk", flat=True)[:options["limit"]])
                for reminder_id in ids:
                    try:
                        reminder = process_reminder(reminder_id)
                        self.stdout.write(f"reminder={reminder_id} status={reminder.status} attempts={reminder.attempts}")
                        failed |= reminder.status == "failed"
                    except (OperationalError, ValidationError):
                        failed = True
                        self.stderr.write(f"reminder={reminder_id} not_processed; inspect policy and retry later")
                if options["once"]:
                    if failed:
                        raise CommandError("Sebagian pengingat belum berhasil diproses. Progres tersimpan; periksa status simulasi.")
                    return
                time.sleep(5)
        except KeyboardInterrupt:
            self.stdout.write("Worker pengingat dihentikan.")

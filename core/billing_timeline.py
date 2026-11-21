"""Illustrate the billing workers' date rules using a sample service month."""
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings

from .billing_scheduler import consent_valid
from .isolation import isolation_date
from .models import BillingSchedule, ReminderPolicy, month_due
from .reminders import policy_authorized


@dataclass(frozen=True)
class TimelineRules:
    issue_day: int | None
    issue_note: str
    reminder_days: int | None
    reminder_hour: str
    reminder_note: str
    simulation: bool


def rules_for(tenant):
    schedule = BillingSchedule.objects.filter(tenant=tenant).select_related("authorized_by").first()
    reminder = ReminderPolicy.objects.filter(tenant=tenant).select_related("authorized_by").first()
    simulated = settings.SIMULATION
    issue_day = None
    issue_note = "Jadwal bulanan nonaktif. Contoh memakai penerbitan manual pada awal bulan."
    if schedule and schedule.enabled:
        if not simulated:
            issue_note = "Jadwal hanya berjalan dalam simulasi. Contoh memakai penerbitan manual pada awal bulan."
        elif not consent_valid(schedule):
            issue_note = "Jadwal menunggu persetujuan ulang. Contoh memakai penerbitan manual pada awal bulan."
        else:
            issue_day = schedule.day
            issue_note = "Tanggal mulai antre sesuai jadwal bulanan. Penerbitan aktual mengikuti proses worker."

    reminder_days = None
    reminder_hour = ""
    reminder_note = "Pengingat billing nonaktif."
    if reminder and reminder.billing_enabled:
        if not simulated:
            reminder_note = "Pengingat hanya berjalan dalam simulasi."
        elif not policy_authorized(reminder):
            reminder_note = "Pengingat menunggu persetujuan ulang."
        else:
            reminder_days = reminder.lead_days
            reminder_hour = f"{reminder.billing_hour:02d}:00"
            reminder_note = "Pengingat simulasi dapat masuk antrean sekali per hari selama faktur masih bersaldo."
            if reminder.scenario == "unavailable":
                reminder_note += " Kebijakan sedang memakai skenario gangguan."
    return TimelineRules(issue_day, issue_note, reminder_days, reminder_hour, reminder_note, simulated)


def calendar_dates(period, due_day, rules, grace_days=0):
    issue = month_due(period, rules.issue_day or 1)
    due = month_due(period, due_day)
    reminder = None
    if rules.reminder_days is not None:
        reminder = max(issue, due - timedelta(days=rules.reminder_days))
    isolation = max(issue, isolation_date(due, grace_days)) if rules.simulation else None
    return {"issue": issue, "reminder": reminder, "due": due, "isolation": isolation,
            "grace_days": grace_days, "past_due_at_issue": issue > due}

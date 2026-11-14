import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils.html import strip_tags
from core.access import save_role
from core.models import AuditEvent, Customer, Invoice, Membership, MonitoringDevice, MonitoringPoll, NetworkJob
from core.monitoring import collect_poll, overview, save_device, snapshots
from .auth_helpers import verified_login
from .test_workflow import fixtures

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=ZoneInfo("Asia/Jakarta"))
VALUES = {"name": "NAS contoh", "location": "Lokasi fiktif", "enabled": True, "scenario": "normal"}


@override_settings(SIMULATION=True)
class MonitoringTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()

    def setUp(self):
        verified_login(self.client, self.owners[0])
        clock = patch("core.monitoring.timezone.now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def device(self, **values):
        return save_device(self.owners[0], {**VALUES, **values}, 1, time.time(), 1)

    def edit(self, device, **values):
        data = {key: getattr(device, key) for key in VALUES}
        return save_device(self.owners[0], {**data, **values}, device.version, time.time(), 1, device.pk)

    def row(self, device):
        return snapshots(MonitoringDevice.objects.filter(pk=device.pk))[0]

    def test_default_off_and_get_does_not_create_or_collect(self):
        self.assertFalse(MonitoringDevice().enabled)
        self.assertEqual(self.client.get("/network/monitoring/").status_code, 200)
        self.client.get("/network/monitoring/new/")
        call_command("collect_monitoring", stdout=StringIO())
        self.assertFalse(MonitoringDevice.objects.exists())
        self.assertFalse(MonitoringPoll.objects.exists())
        self.assertIsNone(overview(self.tenants[0])["online"])

    def test_success_snapshot_has_counts_and_does_not_change_service_or_financial_records(self):
        device = self.device()
        poll = collect_poll(device.pk)
        self.assertEqual((poll.status, len(poll.sessions)), ("success", 3))
        row = self.row(device)
        self.assertEqual((row["state"], row["online"], row["offline"]), ("fresh", 2, 1))
        self.assertEqual(overview(self.tenants[0])["online"], 2)
        self.assertEqual(Customer.objects.count(), 2)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(NetworkJob.objects.exists())
        self.assertEqual(self.customers[0].service_state, "active")
        self.assertNotIn("demo-ppp", AuditEvent.objects.filter(action="monitoring.success").get().detail)

    def test_empty_success_is_zero_not_unknown(self):
        device = self.device(scenario="empty")
        collect_poll(device.pk)
        self.assertEqual(overview(self.tenants[0])["online"], 0)
        self.assertEqual(self.row(device)["state"], "fresh")
        response = self.client.get(f"/network/monitoring/{device.pk}/")
        self.assertContains(response, "Snapshot berhasil: tidak ada sesi")

    def test_failure_without_snapshot_is_unknown_never_zero(self):
        device = self.device(scenario="unavailable")
        poll = collect_poll(device.pk)
        self.assertEqual((poll.status, poll.sessions, poll.observed_at), ("failed", [], None))
        self.assertIsNone(overview(self.tenants[0])["online"])
        self.assertEqual(self.row(device)["state"], "failed")

    def test_failure_retains_last_success_as_excluded_history(self):
        device = self.device()
        success = collect_poll(device.pk)
        with patch("core.monitoring.timezone.now", return_value=NOW+timedelta(seconds=60)), patch(
                "core.monitoring.SimulationMonitor.read", side_effect=ConnectionError("fake-secret-must-not-leak")):
            failure = collect_poll(device.pk)
            row = self.row(device)
            self.assertEqual((row["state"], row["snapshot"].pk, row["online"]), ("failed", success.pk, 2))
            self.assertIsNone(overview(self.tenants[0])["online"])
        self.assertNotIn("fake-secret", failure.reason)
        self.assertEqual(MonitoringPoll.objects.count(), 2)

    def test_stale_scenario_and_natural_age_expiry_are_excluded(self):
        stale = self.device(scenario="stale")
        collect_poll(stale.pk)
        self.assertEqual(self.row(stale)["state"], "stale")
        fresh = self.device(name="NAS segar")
        collect_poll(fresh.pk)
        with patch("core.monitoring.timezone.now", return_value=NOW+timedelta(seconds=120)):
            self.assertEqual(self.row(fresh)["state"], "fresh")
        with patch("core.monitoring.timezone.now", return_value=NOW+timedelta(seconds=121)):
            self.assertEqual(self.row(fresh)["state"], "stale")
            self.assertIsNone(overview(self.tenants[0])["online"])

    def test_aggregate_uses_only_fresh_own_tenant_and_reports_partial_scope(self):
        fresh = self.device()
        collect_poll(fresh.pk)
        collect_poll(self.device(name="NAS gagal", scenario="unavailable").pk)
        other = save_device(self.owners[1], VALUES, 1, time.time(), 1)
        collect_poll(other.pk)
        summary = overview(self.tenants[0])
        self.assertEqual((summary["online"], summary["fresh"], summary["excluded"], summary["total"]), (2, 1, 1, 2))

    def test_poll_repeat_and_restart_do_not_duplicate_within_interval(self):
        device = self.device()
        first = collect_poll(device.pk)
        self.assertEqual(collect_poll(device.pk).pk, first.pk)
        call_command("collect_monitoring", stdout=StringIO())
        self.assertEqual(MonitoringPoll.objects.count(), 1)
        with patch("core.monitoring.timezone.now", return_value=NOW+timedelta(seconds=60)):
            self.assertNotEqual(collect_poll(device.pk).pk, first.pk)

    def test_new_configuration_requires_new_snapshot_and_stale_form_is_rejected(self):
        device = self.device()
        collect_poll(device.pk)
        changed = self.edit(device, location="Lokasi baru")
        self.assertEqual(self.row(changed)["state"], "pending")
        self.assertIsNone(overview(self.tenants[0])["online"])
        with self.assertRaises(ValidationError):
            self.edit(device, location="Form kedaluwarsa")
        with self.assertRaises(ValidationError):
            collect_poll(device.pk, self.owners[0], device.version)
        self.assertEqual(collect_poll(changed.pk).device_version, changed.version)

    def test_disabling_preserves_history_and_stops_collection(self):
        device = self.device()
        collect_poll(device.pk)
        disabled = self.edit(device, enabled=False)
        self.assertIsNone(disabled.next_poll_at)
        with self.assertRaises(ValidationError):
            collect_poll(device.pk)
        self.assertEqual(self.row(disabled)["state"], "disabled")
        self.assertIsNone(overview(self.tenants[0])["online"])
        self.assertEqual(MonitoringPoll.objects.count(), 1)

    def test_permission_change_blocks_and_fresh_consent_resumes_new_version(self):
        device = self.device()
        Membership.objects.filter(user=self.owners[0]).update(session_version=2)
        blocked = collect_poll(device.pk)
        device.refresh_from_db()
        self.assertEqual(blocked.status, "blocked")
        self.assertIsNone(device.next_poll_at)
        self.assertEqual(collect_poll(device.pk).pk, blocked.pk)
        renewed = save_device(self.owners[0], VALUES, device.version, time.time(), 2, device.pk)
        self.assertEqual(collect_poll(renewed.pk).status, "success")
        self.assertEqual(MonitoringPoll.objects.count(), 2)

    def test_revoked_consent_excludes_fresh_snapshot_before_worker_runs(self):
        device = self.device()
        collect_poll(device.pk)
        get_user_model().objects.filter(pk=self.owners[0].pk).update(is_active=False)
        self.assertEqual(self.row(device)["state"], "blocked")
        self.assertIsNone(overview(self.tenants[0])["online"])

    def test_configuration_needs_current_mfa_and_session_version(self):
        for proof, version in [(time.time()-301, 1), (time.time()+60, 1), (time.time(), 99)]:
            with self.assertRaises(PermissionDenied):
                save_device(self.owners[0], VALUES, 1, proof, version)
        self.assertFalse(MonitoringDevice.objects.exists())

    def test_validate_configuration_limits_and_rejects_target_or_secret_fields(self):
        for changes in [{"name": ""}, {"name": "x"*81}, {"scenario": "live"}, {"enabled": "1"},
                        {"host": "169.254.169.254"}, {"password": "do-not-store"}]:
            with self.assertRaises(ValidationError):
                self.device(**changes)
        self.assertFalse(MonitoringDevice.objects.exists())

    def test_duplicate_name_is_tenant_scoped_and_device_limit_is_bounded(self):
        self.device()
        with self.assertRaises(ValidationError):
            self.device()
        save_device(self.owners[1], VALUES, 1, time.time(), 1)
        MonitoringDevice.objects.bulk_create([MonitoringDevice(tenant=self.tenants[0], name=f"NAS {n}") for n in range(49)])
        with self.assertRaises(ValidationError):
            self.device(name="NAS ke 51")

    def test_bad_or_future_snapshot_is_failed_without_persisting_untrusted_data(self):
        bad = [(NOW+timedelta(seconds=1), []), (NOW.replace(tzinfo=None), []), (NOW-timedelta(days=2), []),
            (NOW, [{}]), (NOW, [{"username": "demo", "state": "online", "uptime_seconds": -1}]),
            (NOW, [{"username": "demo", "state": "offline", "uptime_seconds": 1}]),
            (NOW, [{"username": "demo", "state": "online", "uptime_seconds": True}]),
            (NOW, [{"username": "demo", "state": "online", "uptime_seconds": 1}]*2), (NOW, [{}]*101)]
        for index, value in enumerate(bad):
            device = self.device(name=f"NAS invalid {index}")
            with patch("core.monitoring.SimulationMonitor.read", return_value=value):
                poll = collect_poll(device.pk)
            self.assertEqual((poll.status, poll.sessions, poll.observed_at), ("failed", [], None))

    def test_scope_noc_read_and_collect_but_not_configuration(self):
        device = self.device()
        Membership.objects.filter(user=self.owners[0]).update(role="noc")
        for url in ["/network/monitoring/", f"/network/monitoring/{device.pk}/"]:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(f"/network/monitoring/{device.pk}/edit/").status_code, 403)
        self.assertEqual(self.client.post("/network/monitoring/new/", {}).status_code, 403)
        # A separate owner's approval is needed; changing this owner's role invalidated consent.
        response = self.client.post(f"/network/monitoring/{device.pk}/collect/", {"version": device.version})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MonitoringPoll.objects.get().status, "blocked")
        response = self.client.get(f"/network/monitoring/{device.pk}/")
        # Random masked CSRF attributes can contain "Rp"; inspect visible text.
        self.assertNotIn("Rp", strip_tags(response.content.decode()))
        self.assertNotContains(response, self.customers[0].phone)

    def test_other_tenant_ids_and_unauthorized_roles_are_blocked(self):
        device = self.device()
        other = Client()
        verified_login(other, self.owners[1])
        for suffix in ["", "edit/", "collect/"]:
            url = f"/network/monitoring/{device.pk}/"+suffix
            self.assertEqual(other.post(url, {"version": device.version}).status_code, 404 if suffix else 405)
            if suffix != "collect/":
                self.assertEqual(other.get(url).status_code, 404)
        self.assertEqual(other.get("/network/monitoring/").context["summary"]["total"], 0)
        for role in ["ops", "finance", "cashier", "technician"]:
            Membership.objects.filter(user=self.owners[0]).update(role=role)
            self.assertEqual(self.client.get(f"/network/monitoring/{device.pk}/").status_code, 403)

    def test_custom_config_role_requires_network_and_revocation_blocks_worker(self):
        with self.assertRaises(ValidationError):
            save_role(self.owners[0], "Konfigurator tidak lengkap", {"manage_monitoring"})
        role = save_role(self.owners[0], "Konfigurator", {"network", "manage_monitoring"})
        user = get_user_model().objects.create_user(username="monitor-manager")
        member = Membership.objects.create(user=user, tenant=self.tenants[0], role="noc", access_role=role)
        device = save_device(user, VALUES, 1, time.time(), member.session_version)
        save_role(self.owners[0], role.name, {"network"}, role_id=role.pk)
        self.assertEqual(collect_poll(device.pk).status, "blocked")

    def test_plaintext_escaping_and_filters_preserve_summary_scope(self):
        device = self.device(name='<script>NAS-marker</script>')
        collect_poll(device.pk)
        self.device(name="NAS lain")
        response = self.client.get("/network/monitoring/?q=marker&size=25")
        self.assertEqual(response.context["page"].paginator.count, 1)
        self.assertEqual(response.context["summary"]["total"], 2)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, '<script>NAS-marker</script>')

    def test_csrf_review_stale_form_and_expired_factor_on_http(self):
        client = Client(enforce_csrf_checks=True)
        verified_login(client, self.owners[0])
        self.assertEqual(client.post("/network/monitoring/new/", {}).status_code, 403)
        data = {**VALUES, "version": 1}
        self.client.post("/network/monitoring/new/", data)
        self.assertFalse(MonitoringDevice.objects.exists())
        response = self.client.post("/network/monitoring/new/", {**data, "reviewed": "on"})
        device = MonitoringDevice.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.post(f"/network/monitoring/{device.pk}/edit/", {**data, "reviewed": "on"}), "Data sudah berubah")
        session = self.client.session
        session["mfa_verified_at"] = time.time()-301
        session.save()
        response = self.client.post(f"/network/monitoring/{device.pk}/edit/", {**data, "reviewed": "on"})
        self.assertEqual(response.url, "/account/security/")
        self.assertEqual(self.client.session["stepup_return"], f"/network/monitoring/{device.pk}/edit/")
        self.assertEqual(MonitoringDevice.objects.get().version, device.version)

    @override_settings(SIMULATION=False)
    def test_live_mode_is_fail_closed(self):
        with self.assertRaises(ValidationError):
            self.device()
        device = self.device(enabled=False)
        with self.assertRaises(ValidationError):
            collect_poll(device.pk)
        for command in ["collect_monitoring", "run_simulation_worker"]:
            with self.assertRaises(CommandError):
                call_command(command, stdout=StringIO())
        self.assertFalse(MonitoringPoll.objects.exists())

    def test_command_failure_exit_due_limit_and_integration_with_network_worker(self):
        self.device(scenario="unavailable")
        with self.assertRaises(CommandError):
            call_command("run_simulation_worker", once=True, stdout=StringIO(), stderr=StringIO())
        self.assertEqual(MonitoringPoll.objects.get().status, "failed")
        for limit in [0, 501]:
            with self.assertRaises(CommandError):
                call_command("collect_monitoring", limit=limit, stdout=StringIO())
        for index in range(3):
            self.device(name=f"NAS due {index}")
        call_command("collect_monitoring", limit=1, stdout=StringIO())
        self.assertEqual(MonitoringPoll.objects.count(), 2)


class MonitoringConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenants, self.owners, self.customers = fixtures()

    def parallel(self, tasks):
        def run(task):
            close_old_connections()
            try:
                return task()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(run, tasks))

    def test_two_collectors_commit_only_one_snapshot_in_a_minute(self):
        with patch("core.monitoring.timezone.now", return_value=NOW):
            device = save_device(self.owners[0], VALUES, 1, time.time(), 1)
            polls = self.parallel([lambda: collect_poll(device.pk), lambda: collect_poll(device.pk)])
        self.assertEqual(polls[0].pk, polls[1].pk)
        self.assertEqual(MonitoringPoll.objects.count(), 1)

    def test_two_configuration_updates_do_not_lose_changes(self):
        device = save_device(self.owners[0], VALUES, 1, time.time(), 1)
        def update(name):
            try:
                save_device(self.owners[0], {**VALUES, "name": name}, device.version, time.time(), 1, device.pk)
                return True
            except ValidationError:
                return False
        results = self.parallel([lambda: update("NAS A"), lambda: update("NAS B")])
        self.assertEqual(sorted(results), [False, True])

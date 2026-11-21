import uuid
from datetime import date, datetime, timedelta, timezone

from django.test import Client, TestCase
from django.urls import reverse

from core.access import save_role
from core.bulk_billing import create_preview
from core.models import AuditEvent, Membership, NetworkJob

from .auth_helpers import verified_login
from .test_workflow import fixtures


class AuditSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.url = reverse("audit")

    def setUp(self):
        self.actor = self.owners[0]
        verified_login(self.client, self.actor)

    def event(self, action="network.confirmed", target="12", tenant_index=0, **fields):
        defaults = {"actor": self.owners[tenant_index], "correlation_id": uuid.uuid4()}
        return AuditEvent.objects.create(tenant=self.tenants[tenant_index], action=action, target=target,
                                         **{**defaults, **fields})

    def test_tenant_scope_read_permission_and_cache_policy(self):
        own = self.event(detail="Keterangan privat dari ISP satu")
        other = self.event(tenant_index=1, detail="Keterangan privat dari ISP berbeda")
        response = self.client.get(self.url, {"tenant_id": other.tenant_id})
        self.assertContains(response, own.detail)
        self.assertNotContains(response, other.detail)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.client.head(self.url).status_code, 200)
        self.assertEqual(self.client.post(self.url).status_code, 405)
        self.assertEqual(Client().get(self.url).status_code, 302)
        custom = save_role(self.actor, "Pemeriksa ISP", {"audit"})
        Membership.objects.filter(user=self.actor).update(role="noc")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        Membership.objects.filter(user=self.actor).update(access_role=custom)
        self.assertContains(self.client.get(self.url), own.detail)
        self.assertNotContains(self.client.get(self.url), other.detail)

    def test_action_group_actor_and_exact_target_combine(self):
        match = self.event("network.router_simulation_changed", detail="Catatan <b>terverifikasi</b>")
        self.event("network", detail="Aksi pada akar kelompok")
        self.event("networking.configured")
        self.event("network.failed", "123")
        self.event("network.skipped", actor=None)
        self.event("network.router_simulation_changed", tenant_index=1)
        response = self.client.get(self.url, {"action": "NETWORK", "actor": "OWNER0", "target": "12"})
        self.assertEqual({event.action for event in response.context["page"]}, {match.action, "network"})
        self.assertContains(response, "Catatan &lt;b&gt;terverifikasi&lt;/b&gt;")
        response = self.client.get(self.url, {"action": match.action})
        self.assertEqual(list(response.context["page"]), [match])

    def test_date_filters_use_inclusive_wib_boundaries(self):
        midnight = datetime(2026, 10, 31, 17, tzinfo=timezone.utc)
        before = self.event(created_at=midnight - timedelta(microseconds=1))
        first = self.event(created_at=midnight)
        last = self.event(created_at=midnight + timedelta(days=1, microseconds=-1))
        after = self.event(created_at=midnight + timedelta(days=1))
        response = self.client.get(self.url, {"start": "2026-11-01", "end": "2026-11-01"})
        self.assertEqual(list(response.context["page"]), [last, first])
        self.assertContains(response, "01 Nov 2026 00:00:00")
        self.assertEqual(list(self.client.get(self.url, {"end": "2026-11-01"}).context["page"]), [last, first, before])
        self.assertEqual(list(self.client.get(self.url, {"start": "2026-11-01"}).context["page"]), [after, last, first])
        response = self.client.get(self.url, {"start": "0001-01-01", "end": "9999-12-31"})
        self.assertEqual(list(response.context["page"]), [after, last, first, before])

    def test_correlation_trace_is_exact_tenant_scoped_and_read_only(self):
        actor_event = self.event("billing.queued")
        worker_event = self.event("billing.completed", correlation_id=actor_event.correlation_id, actor=None)
        self.event("billing.completed", correlation_id=actor_event.correlation_id, tenant_index=1)
        self.event("billing.completed")
        before = list(AuditEvent.objects.order_by("pk").values())
        url = reverse("audit", query={"correlation_id": actor_event.correlation_id})
        response = self.client.get(url)
        self.assertEqual(list(response.context["page"]), [worker_event, actor_event])
        self.assertContains(response, "Worker sistem")
        self.assertContains(response, f'href="{url}"', count=2)
        self.assertEqual(list(AuditEvent.objects.order_by("pk").values()), before)

    def test_invalid_filters_never_fall_back_to_unfiltered_records(self):
        event = self.event(detail="Catatan yang tidak boleh muncul pada filter tidak valid")
        for data in ({"correlation_id": "abc"}, {"action": "network.%"}, {"actor": "x" * 151},
                     {"start": "2026-11-31"}, {"start": "2026-11-19", "end": "2026-11-18"}):
            with self.subTest(data=data):
                response = self.client.get(self.url, data)
                self.assertContains(response, "errorlist", status_code=400)
                self.assertNotContains(response, event.detail, status_code=400)
        self.assertContains(self.client.get(self.url, {"action": "plan"}), "Tidak ada aktivitas yang cocok")

    def test_pagination_keeps_filters_and_breaks_timestamp_ties(self):
        instant = datetime(2026, 11, 1, tzinfo=timezone.utc)
        events = [self.event(target=str(index), created_at=instant) for index in range(12)]
        self.event("billing.queued", created_at=instant)
        first = self.client.get(self.url, {"action": "network", "size": 10})
        self.assertEqual(list(first.context["page"]), events[2:][::-1])
        self.assertContains(first, 'href="?action=network&amp;size=10&amp;page=2"')
        second = self.client.get(self.url, {"action": "network", "size": 10, "page": 2})
        self.assertEqual(list(second.context["page"]), events[:2][::-1])

    def test_job_and_batch_links_open_their_audit_entries(self):
        job = NetworkJob.objects.create(tenant=self.tenants[0], customer=self.customers[0])
        job_url = reverse("network_job_detail", args=[job.pk])
        router = self.event("network.router_simulation_changed", target=str(job.customer_id))
        job_audit = reverse("audit", query={"action": router.action, "target": job.customer_id})
        self.assertContains(self.client.get(job_url), job_audit.replace("&", "&amp;"))
        self.assertEqual(list(self.client.get(job_audit).context["page"]), [router])
        batch = create_preview(self.actor, date(2026, 11, 1), {"state": "active", "plan": None, "area": ""})
        batch_url = reverse("billing_batch_detail", args=[batch.pk])
        batch_audit = reverse("audit", query={"correlation_id": batch.correlation_id})
        self.assertContains(self.client.get(batch_url), batch_audit)
        self.assertEqual([event.action for event in self.client.get(batch_audit).context["page"]], ["billing.preview_created"])
        custom = save_role(self.actor, "Operator batch", {"network", "bulk_issue", "issue", "customers", "invoices"})
        Membership.objects.filter(user=self.actor).update(role="ops", access_role=custom)
        self.assertNotContains(self.client.get(batch_url), "Jejak audit batch")
        self.assertNotContains(self.client.get(job_url), "Jejak audit router")

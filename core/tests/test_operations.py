import os
import tempfile
from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from core.models import Customer, Invoice, NetworkJob, Payment, Tenant
from core.network import run_job
from .test_workflow import fixtures

class OperationTests(TestCase):
    @override_settings(DEBUG=True, SIMULATION=True)
    def test_seed_repeat_preserves_counts_passwords_and_renamed_isp(self):
        with patch.dict(os.environ, {"TAGIHNET_DEMO_PASSWORD": "Local-seed-test-2026!"}):
            call_command("seed_demo", stdout=StringIO())
            counts = [model.objects.count() for model in [Tenant, Customer, Invoice, Payment]]
            from django.contrib.auth import get_user_model
            user = get_user_model().objects.get(username="owner")
            before = user.password
            tenant = user.membership.tenant
            tenant.name = "Nama ISP Diubah"
            tenant.save()
            call_command("seed_demo", stdout=StringIO())
            self.assertEqual(counts, [model.objects.count() for model in [Tenant, Customer, Invoice, Payment]])
            user.refresh_from_db()
            self.assertEqual(user.password, before)
            tenant.refresh_from_db()
            self.assertEqual(tenant.name, "Nama ISP Diubah")

    @override_settings(DEBUG=False, SIMULATION=True)
    def test_seed_is_disabled_outside_local_development(self):
        with self.assertRaises(CommandError):
            call_command("seed_demo", stdout=StringIO())

    @override_settings(SIMULATION=True)
    def test_worker_respects_backoff_and_max_attempts(self):
        tenants, _, customers = fixtures()
        customers[0].router_available = False
        customers[0].save()
        job = NetworkJob.objects.create(tenant=tenants[0], customer=customers[0])
        self.assertEqual(run_job(job.pk).attempts, 1)
        self.assertEqual(run_job(job.pk).attempts, 1)
        for expected in [2, 3]:
            NetworkJob.objects.filter(pk=job.pk).update(run_after=timezone.now() - timedelta(seconds=1))
            self.assertEqual(run_job(job.pk).attempts, expected)
        NetworkJob.objects.filter(pk=job.pk).update(run_after=timezone.now() - timedelta(seconds=1))
        self.assertEqual(run_job(job.pk).attempts, 3)

import time
import uuid
from datetime import date
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from core.access import save_role
from core.billing import issue_invoice
from core.bulk_billing import confirm_batch, create_preview, process_batch
from core.customer_import import confirm_import, preview_import
from core.models import AccessRole, AuditEvent, Customer, Invoice, Membership, NetworkJob, Plan
from core.operations import activation_quote, activate_installation
from core.plans import confirm_change, impact, read_review, review_change, values_for
from .auth_helpers import verified_login
from .test_customer_import import source
from .test_operations_workflows import installed_order, technician
from .test_workflow import fixtures


class PlanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()

    def setUp(self):
        self.actor = self.owners[0]
        self.plan = self.customers[0].plan
        verified_login(self.client, self.actor)

    def review(self, **changes):
        return review_change(self.actor, {**values_for(self.plan), **changes}, self.plan.version, self.plan.pk)[0]

    def confirm(self, token):
        return confirm_change(self.actor, token, time.time(), 1)

    def test_gets_and_preview_have_no_effects(self):
        self.assertEqual(self.client.get('/plans/').status_code, 200)
        self.assertEqual(self.client.get('/plans/new/').status_code, 200)
        response = self.client.post(f'/plans/{self.plan.pk}/edit/', {**values_for(self.plan), 'price': 300000, 'version': 1})
        self.assertContains(response, 'Tinjau perubahan paket')
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.price, 225000)
        self.assertFalse(AuditEvent.objects.filter(action__startswith='plan.').exists())
        self.assertEqual(Plan.objects.count(), 2)

    def test_create_replay_has_one_plan_and_one_audit(self):
        token, _ = review_change(self.actor, {'name': 'Baru', 'speed_mbps': 50, 'price': 325000}, 1)
        saved = self.confirm(token)
        self.assertEqual(self.confirm(token).pk, saved.pk)
        self.assertEqual(saved.tenant_id, self.tenants[0].pk)
        self.assertEqual(AuditEvent.objects.filter(action='plan.created').count(), 1)
        self.assertEqual(Plan.objects.count(), 3)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(NetworkJob.objects.exists())

    def test_create_replay_after_edit_never_reverts_price(self):
        token, _ = review_change(self.actor, {'name': 'Baru', 'speed_mbps': 50, 'price': 325000}, 1)
        plan = self.confirm(token)
        edit, _ = review_change(self.actor, {**values_for(plan), 'price': 400000}, plan.version, plan.pk)
        self.confirm(edit)
        self.assertEqual(self.confirm(token).price, 400000)

    def test_change_has_version_and_linked_before_after_audit(self):
        token = self.review(name='Paket "' + 'a'*70, speed_mbps=100000, price=100000000)
        plan = self.confirm(token)
        self.assertEqual(plan.version, 2)
        before = AuditEvent.objects.get(action='plan.before_update')
        after = AuditEvent.objects.get(action='plan.updated')
        self.assertEqual(before.correlation_id, after.correlation_id)
        self.assertIn('Rp225000', before.detail)
        self.assertIn('Rp100000000', after.detail)
        self.assertLessEqual(len(after.detail), 250)

    def test_two_previews_cannot_overwrite_each_other(self):
        first, second = self.review(price=300000), self.review(price=400000)
        self.confirm(first)
        with self.assertRaises(ValidationError):
            self.confirm(second)
        with self.assertRaises(ValidationError):
            self.confirm(first)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.price, 300000)

    def test_direct_data_change_also_invalidates_review(self):
        token = self.review(price=300000)
        Plan.objects.filter(pk=self.plan.pk).update(name='Diubah di luar UI')
        with self.assertRaises(ValidationError):
            self.confirm(token)

    def test_old_form_version_is_rejected_at_preview(self):
        self.confirm(self.review(price=300000))
        with self.assertRaises(ValidationError):
            self.review(price=400000)

    def test_no_change_does_not_advance_version_or_audit(self):
        self.assertEqual(self.confirm(self.review()).version, 1)
        self.assertFalse(AuditEvent.objects.filter(action__startswith='plan.').exists())

    def test_names_are_trimmed_and_duplicate_names_rejected(self):
        token, _ = review_change(self.actor, {'name': '  Baru  ', 'speed_mbps': 1, 'price': 1}, 1)
        self.assertEqual(self.confirm(token).name, 'Baru')
        with self.assertRaises(ValidationError):
            review_change(self.actor, {'name': 'bArU', 'speed_mbps': 1, 'price': 1}, 1)

    def test_duplicate_created_after_preview_is_rejected(self):
        token, _ = review_change(self.actor, {'name': 'Baru', 'speed_mbps': 1, 'price': 1}, 1)
        Plan.objects.create(tenant=self.tenants[0], name='BARU', speed_mbps=10, price=1)
        with self.assertRaises(ValidationError):
            self.confirm(token)

    def test_domain_validation_rejects_invalid_values(self):
        for changes in [{'speed_mbps': 0}, {'speed_mbps': True}, {'speed_mbps': 100001}, {'price': 0},
                        {'price': True}, {'price': 100000001}, {'name': ' '}, {'name': 'a'*81},
                        {'name': 'a\nb'}, {'name': 'a\x7fb'}, {'tenant_id': self.tenants[1].pk}]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.review(**changes)

    def test_bad_signature_and_expiry_are_rejected(self):
        token = self.review(price=300000)
        with self.assertRaises(ValidationError):
            self.confirm(token+'tampered')
        with patch('django.core.signing.time.time', return_value=time.time()+901), self.assertRaises(ValidationError):
            self.confirm(token)

    def test_other_actor_or_tenant_cannot_use_review(self):
        token = self.review(price=300000)
        with self.assertRaises(PermissionDenied):
            confirm_change(self.owners[1], token, time.time(), 1)
        other = get_user_model().objects.create_user('second-owner')
        Membership.objects.create(user=other, tenant=self.tenants[0], role='owner')
        with self.assertRaises(PermissionDenied):
            read_review(other, token)

    def test_cross_tenant_edit_is_404_and_domain_is_scoped(self):
        other = self.customers[1].plan
        self.assertEqual(self.client.get(f'/plans/{other.pk}/edit/').status_code, 404)
        with self.assertRaises(Plan.DoesNotExist):
            review_change(self.actor, values_for(other), other.version, other.pk)
        other.name = 'Paket rahasia ISP lain'; other.save()
        self.assertNotContains(self.client.get('/plans/'), other.name)

    def test_changed_access_and_disabled_actor_are_rechecked(self):
        token = self.review(price=300000)
        Membership.objects.filter(user=self.actor).update(session_version=2)
        with self.assertRaises(PermissionDenied):
            self.confirm(token)
        Membership.objects.filter(user=self.actor).update(session_version=1)
        get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.confirm(token)

    def test_stale_or_future_mfa_and_wrong_access_version_rejected(self):
        token = self.review(price=300000)
        for verified, version in [(time.time()-301, 1), (time.time()+20, 1), (time.time(), 2)]:
            with self.subTest(version=version), self.assertRaises(PermissionDenied):
                confirm_change(self.actor, token, verified, version)

    def test_confirmation_requires_post_review_checkbox_and_csrf(self):
        token = self.review(price=300000)
        self.assertEqual(self.client.get('/plans/confirm/').status_code, 405)
        self.assertEqual(self.client.post('/plans/confirm/', {'token': token}).status_code, 400)
        csrf = Client(enforce_csrf_checks=True); verified_login(csrf, self.actor)
        self.assertEqual(csrf.post('/plans/confirm/', {'token': token, 'reviewed': 'on'}).status_code, 403)
        self.plan.refresh_from_db(); self.assertEqual(self.plan.price, 225000)

    def test_http_confirm_and_stepup_return_path(self):
        token = self.review(price=300000)
        session = self.client.session; session['mfa_verified_at'] = time.time()-301; session.save()
        self.assertRedirects(self.client.post('/plans/confirm/', {'token': token, 'reviewed': 'on'}), '/account/security/')
        self.assertEqual(self.client.session['stepup_return'], f'/plans/{self.plan.pk}/edit/')
        verified_login(self.client, self.actor)
        self.assertRedirects(self.client.post('/plans/confirm/', {'token': token, 'reviewed': 'on'}), '/plans/')
        self.plan.refresh_from_db(); self.assertEqual(self.plan.price, 300000)

    def test_read_only_role_and_denied_presets(self):
        for role in ['ops', 'finance', 'cashier', 'noc', 'technician', 'customer']:
            user = get_user_model().objects.create_user('role-'+role)
            Membership.objects.create(user=user, tenant=self.tenants[0], role=role,
                customer=self.customers[0] if role == 'customer' else None)
            client = Client(); verified_login(client, user)
            self.assertEqual(client.get('/plans/').status_code, 200 if role in {'ops', 'finance'} else 403)
            self.assertEqual(client.get('/plans/new/').status_code, 403)
            self.assertEqual(client.post('/plans/confirm/', {}).status_code, 403)

    def test_custom_permission_dependency_and_revocation(self):
        with self.assertRaises(ValidationError):
            save_role(self.actor, 'Paket saja', {'manage_plans'})
        role = save_role(self.actor, 'Paket', {'customers', 'manage_plans'})
        user = get_user_model().objects.create_user('package-manager')
        Membership.objects.create(user=user, tenant=self.tenants[0], role='ops', access_role=role)
        token, _ = review_change(user, values_for(self.plan), 1, self.plan.pk)
        AccessRole.objects.filter(pk=role.pk).update(capabilities=['customers'])
        with self.assertRaises(PermissionDenied):
            confirm_change(user, token, time.time(), 1)

    def test_invoice_snapshot_is_unchanged_and_new_month_uses_new_price(self):
        old, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026, 1, 1), self.actor)
        old_line = old.lines.get().description
        self.confirm(self.review(price=300000, name='Baru', speed_mbps=50))
        old.refresh_from_db()
        self.assertEqual((old.subtotal, old.lines.get().description), (225000, old_line))
        new, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026, 2, 1), self.actor)
        self.assertEqual((new.subtotal, new.total), (300000, 333000))
        self.assertIn('Baru · 50 Mbps', new.lines.get().description)

    def test_unconfirmed_bulk_preview_is_stale(self):
        batch = create_preview(self.actor, date(2026, 2, 1), {'state': 'active'}, 0)
        self.confirm(self.review(price=300000))
        self.assertEqual(confirm_batch(self.actor, batch.pk, time.time(), 1).status, 'stale')
        self.assertFalse(Invoice.objects.exists())

    def test_approved_bulk_retains_price(self):
        batch = create_preview(self.actor, date(2026, 2, 1), {'state': 'active'}, 0)
        confirm_batch(self.actor, batch.pk, time.time(), 1)
        self.confirm(self.review(price=300000))
        process_batch(batch.pk)
        self.assertEqual(Invoice.objects.get().subtotal, 225000)

    def test_import_preview_is_stale_after_package_change(self):
        batch = preview_import(self.actor, source(self.tenants[0]), uuid.uuid4())
        self.confirm(self.review(price=300000))
        self.assertEqual(confirm_import(self.actor, batch.pk, time.time(), 1).status, 'stale')
        self.assertEqual(Customer.objects.count(), 2)

    def test_activation_quote_is_stale_after_package_change(self):
        tech = technician(self.tenants[0])
        order = installed_order(self.actor, tech)
        _, fingerprint = activation_quote(order, date(2026, 2, 1))
        # The fixture uses this tenant's first plan.
        self.assertEqual(order.plan_id, self.plan.pk)
        self.confirm(self.review(price=300000))
        with self.assertRaises(ValidationError):
            activate_installation(self.actor, order.pk, date(2026, 2, 1), fingerprint, time.time(), 1)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(NetworkJob.objects.exists())

    def test_audit_failure_rolls_back_change(self):
        token = self.review(price=300000)
        with patch('core.plans.audit', side_effect=RuntimeError('test')), self.assertRaises(RuntimeError):
            self.confirm(token)
        self.plan.refresh_from_db()
        self.assertEqual((self.plan.price, self.plan.version), (225000, 1))

    def test_search_pagination_escaping_and_impact_scope(self):
        self.confirm(self.review(name='<script>alert(1)</script>'))
        response = self.client.get('/plans/?q=script&per_page=10')
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertEqual(impact(self.tenants[0], self.plan.pk), {'total': 1, 'active': 1, 'isolated': 0, 'pending': 0})
        self.assertEqual(impact(self.tenants[0], self.customers[1].plan_id)['total'], 0)

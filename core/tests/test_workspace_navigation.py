import uuid
from datetime import date
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from core.access import save_role
from core.billing import issue_invoice, record_payment
from core.models import AuditEvent, Customer, Fund, Invoice, Membership, MFADevice, NetworkJob, Payment, PaymentReversal, Plan
from core.navigation import navigation
from core.permissions import capabilities
from .auth_helpers import verified_login
from .test_workflow import fixtures


class WorkspaceNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):cls.tenants,cls.owners,cls.customers=fixtures()

    def setUp(self):verified_login(self.client,self.owners[0])

    def groups(self,member,path='/',action=''):
        return navigation(member,capabilities(member),path,action)

    def test_owner_groups_are_nonempty_and_only_one_destination_selected(self):
        member=Membership.objects.get(user=self.owners[0])
        for path,expected in [('/','dashboard'),('/customers/','customers'),('/customers/imports/new/','imports'),
                ('/invoices/schedule/confirm/','schedule'),('/network/monitoring/','monitoring'),
                ('/account/password/','security')]:
            groups=self.groups(member,path)
            self.assertTrue(all(group['links'] for group in groups))
            self.assertEqual([link['key'] for group in groups for link in group['links'] if link['selected']],[expected])

    def test_action_forms_select_their_actual_module(self):
        member=Membership.objects.get(user=self.owners[0])
        for action,expected in [('salary','payroll'),('voucher_refund','hotspot'),('endpoint_set','topology'),('campaign_edit','sales'),('fund_create','cash')]:
            groups=self.groups(member,'/business/actions/'+action+'/',action)
            self.assertEqual([link['key'] for group in groups for link in group['links'] if link['selected']],[expected])

    def test_noc_and_sales_have_no_financial_or_customer_destinations(self):
        member=Membership.objects.get(user=self.owners[0])
        for role,expected in [('noc',{'network','monitoring','topology','payslips','profile','security'}),
                              ('sales',{'sales','payslips','profile','security'}),
                              ('customer',{'portal','tickets','profile','security'})]:
            member.role=role
            groups=self.groups(member)
            self.assertEqual({link['key'] for group in groups for link in group['links']},expected)

    def test_context_preserves_custom_role_and_active_group(self):
        owner=self.owners[0]
        role=save_role(owner,'Contacts only',['customers'])
        user=get_user_model().objects.create_user(username='workspace-contacts')
        Membership.objects.create(tenant=self.tenants[0],user=user,role='ops',access_role=role)
        verified_login(self.client,user)
        response=self.client.get('/customers/')
        links=[link for group in response.context['navigation_groups'] for link in group['links']]
        self.assertEqual({link['key'] for link in links},{'customers','plans','payslips','profile','security'})
        self.assertEqual([link['key'] for link in links if link['selected']],['customers'])


class CustomerPreviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants,cls.owners,cls.customers=fixtures()
        cls.invoice,_=issue_invoice(cls.tenants[0],cls.customers[0].pk,date(2026,1,1),cls.owners[0])
        record_payment(cls.tenants[0],cls.invoice.pk,100000,uuid.uuid4(),cls.owners[0],'PRIVATE PAYMENT NOTE')

    def setUp(self):
        verified_login(self.client,self.owners[0]);self.url=f'/customers/{self.customers[0].pk}/preview/'

    def test_preview_is_private_scoped_and_contains_canonical_detail_links(self):
        response=self.client.get(self.url)
        self.assertEqual(response.status_code,200)
        self.assertIn('no-store',response['Cache-Control']);self.assertEqual(response['X-Robots-Tag'],'noindex, nofollow')
        self.assertContains(response,self.customers[0].name)
        self.assertContains(response,'Rp149.750')
        self.assertContains(response,f'/customers/{self.customers[0].pk}/?tab=location')
        self.assertNotContains(response,'PRIVATE PAYMENT NOTE');self.assertNotContains(response,self.invoice.number)
        self.assertNotContains(response,'<!doctype html>')

    def test_other_tenant_missing_and_spoofed_query_parameters(self):
        self.assertEqual(self.client.get(f'/customers/{self.customers[1].pk}/preview/').status_code,404)
        self.assertEqual(self.client.get('/customers/999999/preview/').status_code,404)
        response=self.client.get(self.url,{'tenant':self.tenants[1].pk,'tab':'payments','customer':self.customers[1].pk})
        self.assertContains(response,self.customers[0].name)
        self.assertNotContains(response,self.customers[1].name);self.assertNotContains(response,'PRIVATE PAYMENT NOTE')

    def test_anonymous_and_presets_without_customer_read_are_denied(self):
        self.assertEqual(Client().get(self.url).status_code,302)
        for role in ('cashier','noc','customer','technician','sales'):
            user=get_user_model().objects.create_user(username='preview-'+role)
            Membership.objects.create(tenant=self.tenants[0],user=user,role=role,customer=self.customers[0] if role=='customer' else None)
            client=Client();verified_login(client,user)
            self.assertEqual(client.get(self.url).status_code,403,role)

    def test_contacts_only_role_has_aggregate_receivable_without_invoice_link(self):
        role=save_role(self.owners[0],'Preview contacts',['customers'])
        user=get_user_model().objects.create_user(username='preview-contacts')
        member=Membership.objects.create(tenant=self.tenants[0],user=user,role='ops',access_role=role)
        verified_login(self.client,user)
        response=self.client.get(self.url)
        self.assertContains(response,'Rp149.750');self.assertNotContains(response,'Lihat faktur')
        member.session_version+=1;member.save(update_fields=['session_version'])
        self.assertEqual(self.client.get(self.url).status_code,302)

    def test_preview_escapes_customer_plan_and_address_text(self):
        Customer.objects.filter(pk=self.customers[0].pk).update(name='<script>name()</script>',address='<img src=x onerror=alert(1)>')
        Plan.objects.filter(pk=self.customers[0].plan_id).update(name='<script>plan()</script>')
        response=self.client.get(self.url)
        self.assertContains(response,'&lt;script&gt;name()&lt;/script&gt;')
        self.assertContains(response,'&lt;img src=x onerror=alert(1)&gt;')
        self.assertNotContains(response,'<script>');self.assertNotContains(response,'<img src=x')

    def test_post_is_rejected_and_all_business_and_mfa_rows_are_read_only(self):
        models=(Customer,Invoice,Payment,PaymentReversal,NetworkJob,AuditEvent,MFADevice,Fund)
        before=[list(model.objects.order_by('pk').values()) for model in models]
        self.assertEqual(self.client.post(self.url,{'name':'Injected'}).status_code,405)
        self.assertEqual(self.client.get(self.url).status_code,200)
        self.assertEqual([list(model.objects.order_by('pk').values()) for model in models],before)

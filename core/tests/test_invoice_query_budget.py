import uuid
from datetime import date, timedelta
from django.db import connection
from django.test import TestCase,override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from core.billing import issue_invoice,record_payment,reverse_payment
from core.models import Customer,Invoice
from .auth_helpers import verified_login
from .test_workflow import fixtures


@override_settings(SIMULATION=True)
class InvoiceQueryBudgetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants,cls.owners,cls.customers=fixtures()
        cls.today=timezone.localdate()
        cls.period=cls.today.replace(day=1)
        cls.cases={}
        for n,(state,due,paid,reverse) in enumerate((('issued',0,0,False),('partial',1,50000,False),
                ('paid',-2,'full',False),('overdue',-1,50000,False),('reversed',-3,'full',True))):
            c=Customer.objects.create(tenant=cls.tenants[0],plan=cls.customers[0].plan,code='QUERY-'+state,name=state,username=state,phone='081288880'+str(n),address='Fixture',area='Query')
            i,_=issue_invoice(cls.tenants[0],c.pk,cls.period,cls.owners[0])
            Invoice.objects.filter(pk=i.pk).update(due_date=cls.today+timedelta(days=due))
            if paid:
                p,_=record_payment(cls.tenants[0],i.pk,i.total if paid=='full' else paid,uuid.uuid4(),cls.owners[0])
                if reverse:reverse_payment(cls.tenants[0],p.pk,'Koreksi fixture query',cls.owners[0])
            cls.cases[state]=i.pk
        other,_=issue_invoice(cls.tenants[1],cls.customers[1].pk,cls.period,cls.owners[1])
        record_payment(cls.tenants[1],other.pk,other.total,uuid.uuid4(),cls.owners[1])

    def setUp(self):verified_login(self.client,self.owners[0])

    def query_count(self,path):
        with CaptureQueriesContext(connection) as queries:
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
        return len(queries),response

    def grow(self):
        for n in range(25):
            c=Customer.objects.create(tenant=self.tenants[0],plan=self.customers[0].plan,code=f'GROW-{n}',name=f'Growth {n}',username=f'growth-{n}',phone=f'0812666{n:05}',address='Fixture',area='Query')
            i,_=issue_invoice(self.tenants[0],c.pk,self.period,self.owners[0])
            Invoice.objects.filter(pk=i.pk).update(due_date=self.today-timedelta(days=1))

    def test_dashboard_queries_do_not_grow_with_invoice_count(self):
        before,_=self.query_count('/')
        self.grow();after,_=self.query_count('/')
        self.assertEqual(after,before)
        self.assertLessEqual(after,18)

    def test_invoice_filter_queries_do_not_grow_with_invoice_count(self):
        path=f'/invoices/?state=overdue&size=10&month={self.period:%Y-%m}'
        before,_=self.query_count(path)
        self.grow();after,response=self.query_count(path)
        self.assertEqual(after,before);self.assertLessEqual(after,12)
        self.assertEqual(response.context['page'].paginator.count,27)

    def test_large_page_queries_remain_bounded(self):
        self.grow();count,_=self.query_count('/invoices/?size=50')
        self.assertLessEqual(count,12)

    def test_status_filters_keep_reversal_paid_and_today_semantics(self):
        for state,names in (('paid',('paid',)),('overdue',('overdue','reversed')),('partial',('partial',)),('issued',('issued',))):
            with self.subTest(state=state):
                response=self.client.get('/invoices/',{'state':state,'size':50})
                self.assertEqual({i.pk for i in response.context['page']},{self.cases[n] for n in names})

    def test_dashboard_totals_exclude_reversal_and_other_tenant(self):
        response=self.client.get('/')
        total=Invoice.objects.get(pk=self.cases['issued']).total
        expected_paid=total+100000
        self.assertEqual(response.context['received'],expected_paid)
        self.assertEqual(response.context['payment_count'],3)
        self.assertEqual(response.context['issued'],5*total)
        self.assertEqual(response.context['issued_count'],5)
        self.assertEqual(response.context['receivable'],5*total-expected_paid)
        self.assertEqual(response.context['overdue'],2*total-50000)
        self.assertEqual({i.pk for i in response.context['overdue_invoices']},{self.cases['overdue'],self.cases['reversed']})

    def test_dashboard_drill_down_uses_same_service_month_including_legacy_period_dates(self):
        legacy = Invoice.objects.get(pk=self.cases['overdue'])
        Invoice.objects.filter(pk=legacy.pk).update(period=date(2020, 2, 29))
        Invoice.objects.filter(tenant=self.tenants[1]).update(period=date(2020, 2, 1))

        overview = self.client.get('/', {'period': '2020-02'})
        self.assertContains(overview, 'href="/invoices/?month=2020-02"')
        self.assertContains(overview, 'href="/invoices/?state=overdue"')
        self.assertEqual((overview.context['issued_count'], overview.context['issued']), (1, legacy.total))
        invoices = self.client.get('/invoices/', {'month': '2020-02'})
        self.assertEqual([invoice.pk for invoice in invoices.context['page']], [legacy.pk])

    def test_service_month_combines_with_search_and_status_and_preserves_pagination(self):
        self.grow()
        month = f'{self.period:%Y-%m}'
        filters = {'q': 'GROW', 'month': month, 'state': 'overdue', 'size': '10', 'page': '2'}
        response = self.client.get('/invoices/', filters)
        page = response.context['page']
        self.assertEqual((page.number, page.paginator.count, len(page.object_list)), (2, 25, 10))
        self.assertTrue(all(invoice.customer.code.startswith('GROW-') for invoice in page))
        self.assertContains(response, f'q=GROW&amp;month={month}&amp;state=overdue&amp;size=10&amp;page=3')
        invoice = page.object_list[0]
        detail = reverse('invoice_detail', args=[invoice.pk], query=filters)
        self.assertContains(response, f'href="{escape(detail)}"', count=2)
        back = reverse('invoices', query=filters)
        self.assertContains(self.client.get(detail), f'href="{escape(back)}"')
        empty = self.client.get('/invoices/', {**filters, 'month': '2020-02', 'size': '25'})
        self.assertContains(empty, 'Belum ada faktur yang sesuai')
        self.assertContains(empty, 'href="/invoices/?size=25"')

    def test_invalid_service_month_displays_errors_without_listing_any_invoices(self):
        for month in ('2020-2', '2020-02-01', '9999-13'):
            with self.subTest(month=month):
                response = self.client.get('/invoices/', {'month': month, 'q': 'issued', 'size': '25'})
                self.assertContains(response, 'Gunakan bulan yang valid dengan format YYYY-MM.', status_code=400)
                self.assertContains(response, 'href="#id_month"', status_code=400)
                self.assertNotContains(response, 'Daftar faktur, geser untuk kolom lain', status_code=400)
                self.assertEqual(response.context['invoice_rows'], [])

    def test_dashboard_empty_tenant_shows_actual_zero(self):
        # The second tenant has a paid fixture; remove none of its financial history.
        from django.contrib.auth import get_user_model
        from core.models import Membership,Tenant
        t=Tenant.objects.create(name='Empty fixture')
        user=get_user_model().objects.create_user(username='query-empty',password='Local-empty-fixture-2026!')
        Membership.objects.create(user=user,tenant=t,role='owner')
        verified_login(self.client,user)
        response=self.client.get('/')
        for key in ('issued','received','receivable','overdue','issued_count','payment_count','customer_count','active_customers'):
            self.assertEqual(response.context[key],0)

import uuid
from datetime import timedelta
from django.db import connection
from django.test import TestCase,override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
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
        path='/invoices/?state=overdue&size=10'
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

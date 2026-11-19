import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase

from core.locations import (MAX_TOKEN, SALT, LocationConflict, display, form_token, pair, read_token, save_location)
from core.models import AuditEvent, Customer, Installation, Invoice, Membership, MFADevice, NetworkJob, Tenant, WorkEvent
from core.operations import activate_installation, activation_quote, create_installation, create_ticket, manage_ticket
from .auth_helpers import verified_login
from .test_operations_workflows import prospect_data, technician
from .test_workflow import fixtures


class LocationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants,cls.owners,cls.customers=fixtures()
        cls.tech=technician(cls.tenants[0]);cls.other_tech=technician(cls.tenants[0],'other-location-tech')

    def setUp(self):
        self.actor=self.owners[0];self.customer=self.customers[0]
        verified_login(self.client,self.actor)
        self.order=create_installation(self.actor,{**prospect_data(self.tenants[0]),'assigned_id':self.tech.pk})

    def token(self,kind='customer',actor=None,pk=None):
        return form_token(actor or self.actor,kind,pk or (self.customer.pk if kind=='customer' else self.order.pk))[0]

    def save(self,latitude='-6.917464',longitude='107.619123',*,kind='customer',actor=None,token=None,version=1):
        return save_location(actor or self.actor,kind,self.customer.pk if kind=='customer' else self.order.pk,
            token or self.token(kind,actor),latitude,longitude,version)

    def test_empty_is_unknown_while_zero_zero_is_a_real_pair(self):
        self.assertEqual(pair(None,None),(None,None))
        self.assertEqual(pair('0','-0.000000'),(Decimal('0'),Decimal('0')))
        self.assertFalse(display(self.customer)['present'])
        self.save('0','0');self.customer.refresh_from_db()
        location=display(self.customer);self.assertTrue(location['present'])
        self.assertEqual(location['latitude'],'0.000000');self.assertIn('mlat=0.000000',location['url'])

    def test_boundaries_six_decimals_and_decimal_precision(self):
        for values in (('-90','-180'),('90','180'),('-6.917464','107.619123')):
            self.assertEqual(pair(*values),tuple(Decimal(v).quantize(Decimal('.000001')) for v in values))
        for values in (('90.000001','180'),('0','180.000001'),('-90.000001','0'),('1.0000000','2'),('0','-180.000001')):
            with self.assertRaises(ValidationError):pair(*values)

    def test_nonfinite_float_boolean_long_and_injection_values_refused(self):
        for value in ('NaN','Infinity','-Infinity','sNaN','1e999999',True,1.2,'1'*25,'0;DROP TABLE','<script>'):
            with self.subTest(value_type=type(value).__name__),self.assertRaises(ValidationError):pair(value,'0')

    def test_half_pair_is_refused_at_domain_form_and_database(self):
        for values in ((None,'0'),('0',None)):
            with self.assertRaises(ValidationError):pair(*values)
        response=self.client.post(f'/customers/{self.customer.pk}/location/',{'token':self.token(),'latitude':'0'})
        self.assertEqual(response.status_code,400)
        for model,pk in ((Customer,self.customer.pk),(Installation,self.order.pk)):
            with self.assertRaises(IntegrityError),transaction.atomic():model.objects.filter(pk=pk).update(latitude=1)

    def test_database_range_checks_cover_both_models(self):
        for model,pk in ((Customer,self.customer.pk),(Installation,self.order.pk)):
            for latitude,longitude in ((91,0),(0,181),(-91,0),(0,-181)):
                with self.assertRaises(IntegrityError),transaction.atomic():
                    model.objects.filter(pk=pk).update(latitude=latitude,longitude=longitude)

    def test_get_is_private_and_does_not_update_location_or_audit(self):
        before=Customer.objects.values().get(pk=self.customer.pk)
        response=self.client.get(f'/customers/{self.customer.pk}/location/')
        self.assertContains(response,'Ubah koordinat lokasi')
        self.assertEqual(response['Cache-Control'],'no-store')
        self.assertEqual(Customer.objects.values().get(pk=self.customer.pk),before)
        self.assertFalse(AuditEvent.objects.filter(action='customer.location_updated').exists())

    def test_save_and_clear_only_coordinates_with_metadata_audit(self):
        before=Customer.objects.values().get(pk=self.customer.pk)
        self.assertTrue(self.save());after=Customer.objects.values().get(pk=self.customer.pk)
        self.assertEqual(after,{**before,'latitude':Decimal('-6.917464'),'longitude':Decimal('107.619123')})
        self.assertFalse(Invoice.objects.exists());self.assertFalse(NetworkJob.objects.exists())
        detail=AuditEvent.objects.get(action='customer.location_updated').detail
        self.assertNotIn('107.619123',detail);self.assertNotIn('-6.917464',detail)
        self.assertTrue(self.save(None,None));self.customer.refresh_from_db()
        self.assertIsNone(self.customer.latitude);self.assertIsNone(self.customer.longitude)

    def test_noop_does_not_audit_or_bump_installation_version(self):
        self.assertFalse(self.save(None,None))
        version=self.order.version
        self.assertFalse(self.save(None,None,kind='installation'))
        self.order.refresh_from_db();self.assertEqual(self.order.version,version)
        self.assertFalse(WorkEvent.objects.filter(kind='location').exists())

    def test_stale_pair_and_replay_refused_without_a_second_write(self):
        token=self.token();self.save(token=token)
        with self.assertRaises(LocationConflict):self.save('1','2',token=token)
        response=self.client.post(f'/customers/{self.customer.pk}/location/',{'token':token,'latitude':'1','longitude':'2'})
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.context['form'].data['token'],token)
        self.assertEqual(AuditEvent.objects.filter(action='customer.location_updated').count(),1)

    def test_customer_fingerprint_does_not_claim_permanent_version_history(self):
        token=self.token();self.save();self.save(None,None)
        self.assertTrue(self.save(token=token))

    def test_csrf_required_and_target_fields_ignored(self):
        client=Client(enforce_csrf_checks=True);verified_login(client,self.actor)
        self.assertEqual(client.post(f'/customers/{self.customer.pk}/location/',{'token':self.token(),'latitude':'1','longitude':'2'}).status_code,403)
        response=self.client.post(f'/customers/{self.customer.pk}/location/',{'token':self.token(),'latitude':'1','longitude':'2',
            'tenant':self.tenants[1].pk,'customer':self.customers[1].pk,'service_state':'isolated'})
        self.assertRedirects(response,f'/customers/{self.customer.pk}/?tab=location')
        self.customer.refresh_from_db();self.assertEqual(self.customer.service_state,'active')
        self.customers[1].refresh_from_db();self.assertIsNone(self.customers[1].latitude)

    def test_foreign_tenant_actor_or_object_tokens_refused(self):
        token=self.token()
        verified_login(Client(),self.owners[1])
        with self.assertRaises(PermissionDenied):read_token(self.owners[1],'customer',self.customers[1].pk,token)
        with self.assertRaises(PermissionDenied):read_token(self.actor,'installation',self.order.pk,token)
        self.assertEqual(self.client.get(f'/customers/{self.customers[1].pk}/location/').status_code,404)

    def test_readonly_presets_cannot_edit_customer_coordinates(self):
        for role in ('finance','cashier','noc','technician'):
            Membership.objects.filter(user=self.actor).update(role=role)
            self.assertEqual(self.client.get(f'/customers/{self.customer.pk}/location/').status_code,403)

    def test_access_device_revision_inactive_and_integer_version_checked(self):
        token=self.token()
        for version in (True,999):
            with self.assertRaises(PermissionDenied):self.save(token=token,version=version)
        Membership.objects.filter(user=self.actor).update(session_version=2)
        with self.assertRaises(PermissionDenied):self.save(token=token,version=2)
        Membership.objects.filter(user=self.actor).update(session_version=1)
        MFADevice.objects.filter(user=self.actor).update(revision=uuid.uuid4())
        with self.assertRaises(PermissionDenied):self.save(token=token)
        get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):self.token()

    def test_expiry_tamper_size_format_and_missing_mfa_refused(self):
        token=self.token()
        with patch('django.core.signing.time.time',return_value=time.time()+901):
            with self.assertRaises(ValidationError):self.save(token=token)
        for invalid in ('invalid',token+'x','x'*(MAX_TOKEN+1),signing.dumps([],salt=SALT)):
            with self.assertRaises(ValidationError):self.save(token=invalid)
        MFADevice.objects.filter(user=self.actor).delete()
        with self.assertRaises(PermissionDenied):self.token()

    def test_strict_signed_payload_types(self):
        value=signing.loads(self.token(),salt=SALT)
        for change in ({'v':True},{'object':True},{'assigned':True},{'version':1},{'before':['1','2']},{'before':[None,'0']},{'mfa':'invalid'}):
            with self.assertRaises(ValidationError):self.save(token=signing.dumps({**value,**change},salt=SALT))

    def test_audit_failure_rolls_back_customer_location(self):
        with patch('core.locations.audit',side_effect=RuntimeError),self.assertRaises(RuntimeError):self.save()
        self.customer.refresh_from_db();self.assertIsNone(self.customer.latitude)

    def test_main_customer_form_does_not_restore_old_coordinates(self):
        from core.forms import CustomerForm
        original=CustomerForm.is_valid
        def validate_then_locate(form):
            valid=original(form)
            if valid:self.save()
            return valid
        c=self.customer
        with patch.object(CustomerForm,'is_valid',validate_then_locate):
            response=self.client.post(f'/customers/{c.pk}/edit/',{'name':'Identity edit','email':c.email,'phone':c.phone,
                'username':c.username,'plan':c.plan_id,'nas':'','area':c.area,'address':c.address})
        self.assertEqual(response.status_code,302);c.refresh_from_db()
        self.assertEqual(c.latitude,Decimal('-6.917464'));self.assertEqual(c.name,'Identity edit')

    def test_location_save_does_not_restore_concurrent_service_or_identity(self):
        token=self.token();Customer.objects.filter(pk=self.customer.pk).update(service_state='isolated',name='Current identity')
        self.save(token=token);self.customer.refresh_from_db()
        self.assertEqual(self.customer.service_state,'isolated');self.assertEqual(self.customer.name,'Current identity')

    def test_map_link_uses_fixed_https_host_and_empty_address_search_is_encoded(self):
        self.customer.address='https://evil.invalid/?x=<script>'
        link=display(self.customer)['url']
        self.assertTrue(link.startswith('https://www.openstreetmap.org/search?query='))
        self.assertNotIn('<script>',link)
        self.save();response=self.client.get(f'/customers/{self.customer.pk}/?tab=location')
        self.assertContains(response,'rel="noopener noreferrer"')
        self.assertContains(response,'mlat=-6.917464')
        self.assertNotContains(response,'<iframe');self.assertNotContains(response,'tile.openstreetmap')

    def test_portal_reads_only_own_coordinates_and_cannot_edit(self):
        self.save()
        user=get_user_model().objects.create_user('location-portal')
        Membership.objects.create(user=user,tenant=self.tenants[0],role='customer',customer=self.customer)
        client=Client();verified_login(client,user)
        self.assertContains(client.get('/portal/'),'-6.917464')
        self.assertEqual(client.get(f'/customers/{self.customer.pk}/location/').status_code,403)

    def test_assigned_technician_can_edit_psb_and_no_other_order_or_global_customer(self):
        client=Client();verified_login(client,self.tech.user)
        token=self.token('installation',self.tech.user)
        self.assertTrue(self.save(kind='installation',actor=self.tech.user,token=token))
        self.order.refresh_from_db();self.assertEqual(self.order.version,2)
        self.assertContains(client.get(f'/operations/installations/{self.order.pk}/'),'-6.917464')
        self.assertEqual(client.get(f'/customers/{self.customer.pk}/location/').status_code,403)
        Installation.objects.filter(pk=self.order.pk).update(assigned_to=self.other_tech,version=3)
        self.assertEqual(client.get(f'/operations/installations/{self.order.pk}/location/').status_code,404)
        with self.assertRaises(Installation.DoesNotExist):self.save(kind='installation',actor=self.tech.user,token=token)

    def test_assignment_or_psb_version_change_makes_owner_form_stale(self):
        token=self.token('installation')
        Installation.objects.filter(pk=self.order.pk).update(assigned_to=self.other_tech)
        with self.assertRaises(LocationConflict):self.save(kind='installation',token=token)
        token=self.token('installation');Installation.objects.filter(pk=self.order.pk).update(version=7)
        with self.assertRaises(LocationConflict):self.save(kind='installation',token=token)

    def test_terminal_psb_refuses_location_updates(self):
        token=self.token('installation')
        for status in ('activated','cancelled'):
            Installation.objects.filter(pk=self.order.pk).update(status=status)
            with self.assertRaises(PermissionDenied):self.save(kind='installation',token=token)

    def test_psb_event_failure_rolls_back_coordinates_version_and_event(self):
        token=self.token('installation')
        with patch('core.operations.audit',side_effect=RuntimeError),self.assertRaises(RuntimeError):self.save(kind='installation',token=token)
        self.order.refresh_from_db();self.assertIsNone(self.order.latitude);self.assertEqual(self.order.version,1)
        self.assertFalse(WorkEvent.objects.filter(kind='location').exists())

    def test_activation_copies_coordinates_and_old_quote_becomes_stale(self):
        Installation.objects.filter(pk=self.order.pk).update(status='installed');self.order.refresh_from_db()
        _,old=activation_quote(self.order,date(2026,11,1))
        self.save(kind='installation')
        with self.assertRaises(ValidationError):activate_installation(self.actor,self.order.pk,date(2026,11,1),old,time.time(),1)
        self.order.refresh_from_db();_,current=activation_quote(self.order,date(2026,11,1))
        active=activate_installation(self.actor,self.order.pk,date(2026,11,1),current,time.time(),1)
        self.assertEqual(active.customer.latitude,self.order.latitude);self.assertEqual(active.customer.longitude,self.order.longitude)
        self.assertEqual(active.invoice.total,249750)
        self.assertEqual(activate_installation(self.actor,self.order.pk,date(2026,11,1),current,time.time(),1).customer_id,active.customer_id)
        self.assertEqual(Invoice.objects.count(),1)

    def test_ticket_location_is_read_only_and_assignment_scoped(self):
        self.save();ticket=create_ticket(self.actor,self.customer.pk,'Tiket lokasi','Kendala fiktif','connection')
        ticket=manage_ticket(self.actor,ticket.pk,'normal','connection',self.tech.pk,ticket.version,uuid.uuid4())
        client=Client();verified_login(client,self.tech.user)
        self.assertContains(client.get(f'/operations/tickets/{ticket.pk}/'),'-6.917464')
        other=Client();verified_login(other,self.other_tech.user)
        self.assertEqual(other.get(f'/operations/tickets/{ticket.pk}/').status_code,404)

    def test_lost_scope_before_detail_response_is_rechecked(self):
        from core.operation_views import installation_response
        from django.test import RequestFactory
        request=RequestFactory().get('/');request.user=self.tech.user
        Installation.objects.filter(pk=self.order.pk).update(assigned_to=self.other_tech)
        with self.assertRaises(Installation.DoesNotExist):installation_response(request,self.order)

    def test_busy_response_keeps_original_token_without_write(self):
        from django.db import OperationalError
        token=self.token()
        with patch('core.location_views.save_location',side_effect=OperationalError):
            response=self.client.post(f'/customers/{self.customer.pk}/location/',{'token':token,'latitude':'1','longitude':'2'})
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.context['form'].data['token'],token)
        self.customer.refresh_from_db();self.assertIsNone(self.customer.latitude)


class LocationConcurrencyTests(TransactionTestCase):
    def test_two_customer_location_writes_have_one_winner(self):
        tenants,owners,customers=fixtures();actor=owners[0];verified_login(self.client,actor)
        token=form_token(actor,'customer',customers[0].pk)[0]
        def run(index):
            close_old_connections()
            try:
                try:return save_location(actor,'customer',customers[0].pk,token,str(index+1),'2',1)
                except LocationConflict:return False
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,range(2)))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(AuditEvent.objects.filter(action='customer.location_updated').count(),1)


class LocationMigrationTests(TransactionTestCase):
    def test_forward_migration_preserves_rows_foreign_keys_and_deleted_id_sequences(self):
        old=[('core','0012_plan_availability')];new=[('core','0013_location_coordinates')]
        executor=MigrationExecutor(connection);executor.migrate(old)
        try:
            apps=executor.loader.project_state(old).apps
            T=apps.get_model('core','Tenant');P=apps.get_model('core','Plan');C=apps.get_model('core','Customer')
            O=apps.get_model('core','Installation');U=apps.get_model('auth','User');I=apps.get_model('core','Invoice')
            tenant=T.objects.create(name='Legacy location fixture');plan=P.objects.create(tenant=tenant,name='Legacy',speed_mbps=10,price=100000)
            user=U.objects.create(username='legacy-location')
            customer=C.objects.create(tenant=tenant,plan=plan,code='OLD',name='Legacy',username='old',phone='628129999991',address='Original',area='Original')
            customer_high=max(customer.pk+100,9001)
            deleted=C.objects.create(pk=customer_high,tenant=tenant,plan=plan,code='DELETED',name='Deleted',username='deleted',phone='628129999992',address='Original',area='Original');deleted.delete()
            values={'tenant':tenant,'plan':plan,'created_by':user,'name':'Legacy PSB','phone':'628129999993','username':'old-psb','address':'Original','area':'Original','fingerprint':'original','version':7}
            order=O.objects.create(**values,request_key=uuid.uuid4())
            order_high=max(order.pk+100,9002)
            deleted=O.objects.create(**values,pk=order_high,request_key=uuid.uuid4());deleted.delete()
            invoice=I.objects.create(tenant=tenant,customer=customer,period=date(2026,11,1),due_date=date(2026,11,20),customer_name='Snapshot',customer_address='Snapshot',subtotal=100000,total=100000,footer='Original')
            before={model._meta.db_table:list(model.objects.values()) for model in (T,P,C,O,I)}
            sequences={}
            with connection.cursor() as cursor:
                for table,high in (('core_customer',customer_high),('core_installation',order_high)):
                    if connection.vendor=='sqlite':cursor.execute('SELECT seq FROM sqlite_sequence WHERE name=%s',[table])
                    else:
                        cursor.execute('SELECT setval(pg_get_serial_sequence(%s,%s),%s,true)',[table,'id',high])
                        cursor.execute('SELECT last_value FROM '+table+'_id_seq')
                    sequences[table]=cursor.fetchone()[0]
            executor=MigrationExecutor(connection);executor.migrate(new);apps=executor.loader.project_state(new).apps
            for name in ('Tenant','Plan','Customer','Installation','Invoice'):
                model=apps.get_model('core',name);rows=list(model.objects.values())
                if name in {'Customer','Installation'}:
                    for row in rows:self.assertIsNone(row.pop('latitude'));self.assertIsNone(row.pop('longitude'))
                self.assertEqual(rows,before[model._meta.db_table])
            connection.check_constraints()
            with connection.cursor() as cursor:
                for table,seq in sequences.items():
                    cursor.execute('SELECT seq FROM sqlite_sequence WHERE name=%s',[table]) if connection.vendor=='sqlite' else cursor.execute('SELECT last_value FROM '+table+'_id_seq')
                    self.assertEqual(cursor.fetchone()[0],seq)
        finally:
            latest=MigrationExecutor(connection);latest.migrate(latest.loader.graph.leaf_nodes())

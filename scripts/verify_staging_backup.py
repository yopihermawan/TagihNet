"""Full HTTPS staging recovery drill with ledger, MFA, photos and pending work; no user data."""
import copy
import http.cookiejar
import io
import hashlib
import json
import shutil
import socket
import ssl
import subprocess
import tempfile
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, HTTPCookieProcessor, Request, build_opener

import pyotp
from cryptography.fernet import Fernet
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from core.backup_archive import ArchiveError, encrypt_stream, generate_key, read_key, verify_archive
from staging_lab import compose, manifest, prepare, up
from scripts.staging_backup import EMPTY_SQL, backup, restore, query, database_container
from verify_staging import TestIdentity,verify
from scripts.ops_smoke import verify_source, verify_recovery
from scripts.recovery_package import export_package,verify_package,restore_package
from scripts.preflight_smoke import active as preflight_active,recovery as preflight_recovery


def port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        return listener.getsockname()[1]


def shell(directory,code):
    try:
        return compose(directory,'run','--rm','--no-deps','migrate','python','manage.py','shell','-c',code,capture=True).stdout
    except subprocess.CalledProcessError as error:
        # Synthetic fixtures only; surface the exception summary, never a body or factor.
        raise RuntimeError('Staging fixture/check failed: '+error.stderr.splitlines()[-1]) from None


def fingerprint(directory):
    code='''
import hashlib,json
from django.apps import apps
from django.db import connection
from psycopg import sql
result={}
with connection.cursor() as cursor:
    for model in apps.get_models(include_auto_created=True):
        cursor.execute(sql.SQL('SELECT row_to_json(t)::text FROM {} t ORDER BY {}').format(sql.Identifier(model._meta.db_table),sql.Identifier(model._meta.pk.column)))
        rows=cursor.fetchall()
        result[model._meta.db_table]=[len(rows),hashlib.sha256(json.dumps(rows).encode()).hexdigest()]
    cursor.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename")
    sequences=cursor.fetchall()
    for (name,) in sequences:
        cursor.execute(sql.SQL('SELECT last_value,is_called FROM {}').format(sql.Identifier(name)))
        result['sequence:'+name]=cursor.fetchone()
print('FINGERPRINT:'+json.dumps(result,sort_keys=True))
'''
    return json.loads(next(line.removeprefix('FINGERPRINT:') for line in shell(directory,code).splitlines() if line.startswith('FINGERPRINT:')))


FIXTURE='''
import io,time,uuid
from PIL import Image
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from core.models import Tenant,Plan,Customer,Membership,ReminderPolicy,Reminder
from core.billing import issue_invoice,record_payment,reverse_payment
from core.operations import create_ticket
from core.work_photos import upload_photo
from core.billing_scheduler import review_change,confirm_change,run_schedule
actor=get_user_model().objects.get(username='staging-owner')
access_version=Membership.objects.get(user=actor).session_version
tenant=actor.membership.tenant
tenant.tax_bp=1100;tenant.save()
plan=Plan.objects.create(tenant=tenant,name='Paket Restore',speed_mbps=30,price=225000)
customer=Customer.objects.create(tenant=tenant,plan=plan,code='RESTORE-1',name='Pelanggan Restore Fiktif',username='restore-1',phone='081299998881',address='Alamat Fiktif',area='Restore')
period=timezone.localdate().replace(day=1)
invoice,_=issue_invoice(tenant,customer.pk,period,actor)
record_payment(tenant,invoice.pk,100000,uuid.uuid4(),actor,'Fixture restore')
payment,_=record_payment(tenant,invoice.pk,25000,uuid.uuid4(),actor,'Fixture reversal')
reverse_payment(tenant,payment.pk,'Koreksi pembayaran fixture restore',actor)
other=Tenant.objects.create(name='ISP Kedua Restore')
other_plan=Plan.objects.create(tenant=other,name='Paket ISP Kedua',speed_mbps=20,price=120000)
Customer.objects.create(tenant=other,plan=other_plan,code='RESTORE-2',name='Pelanggan ISP Kedua Fiktif',username='restore-2',phone='081299998882',address='Alamat Kedua',area='Restore')
ticket=create_ticket(actor,customer.pk,'Tiket restore fiktif','Bukti pekerjaan fiktif','connection')
image=io.BytesIO();Image.new('RGB',(20,16),'#13796b').save(image,format='PNG')
upload_photo(actor,ticket.pk,SimpleUploadedFile('fixture.png',image.getvalue(),content_type='image/png'),'Foto restore fiktif',True,ticket.version,uuid.uuid4(),access_version)
values={'enabled':True,'day':1,'state':'active','plan':None,'area':''}
token,_=review_change(actor,values,0)
schedule=confirm_change(actor,token,time.time(),access_version)
future=(period.replace(day=28)+timedelta(days=4)).replace(day=1)
run_schedule(schedule.pk,timezone.now().replace(year=future.year,month=future.month,day=1))
ReminderPolicy.objects.create(tenant=tenant,billing_enabled=True,authorized_by=actor,authorization_version=1)
Reminder.objects.create(tenant=tenant,kind='billing',invoice=invoice,business_date=timezone.localdate(),policy_version_planned=1,recipient='Fixture only',body='No delivery')
assert invoice.balance==149750
from core.plan_availability import review_availability,confirm_availability
token,_=review_availability(actor,plan.pk,plan.version,False)
confirm_availability(actor,token,time.time(),access_version)
plan.refresh_from_db();assert not plan.is_active
'''


def verify_restored_access(target,identity):
    base=f"https://localhost:{manifest(target)['port']}"
    context=ssl.create_default_context(cafile=str(target/'secrets/ca.crt'))
    jar=http.cookiejar.CookieJar()
    for cookie in identity.cookies:jar.set_cookie(copy.copy(cookie))
    client=build_opener(HTTPSHandler(context=context),HTTPCookieProcessor(jar))
    def request(path,form=None):
        req=Request(base+path,data=urlencode(form).encode() if form is not None else None,headers={'Origin':base,'Referer':base+'/'})
        try:
            with client.open(req,timeout=10) as response:return response.status,response.read(),response.url
        except HTTPError as response:return response.code,response.read(),response.url
    assert request('/settings/access/')[2].startswith(base+'/login/')
    request('/login/')
    def csrf():return next(cookie.value for cookie in jar if cookie.name=='csrftoken')
    assert request('/login/',{'username':'staging-owner','password':identity.password,'csrfmiddlewaretoken':csrf()})[2].endswith('/mfa/verify/')
    code=pyotp.TOTP(identity.totp_secret).now()
    status,_,url=request('/mfa/verify/',{'code':code,'csrfmiddlewaretoken':csrf()})
    assert status==200 and url==base+'/'
    assert request('/settings/access/')[0]==200
    assert 'PASS:' in shell(target,"from core.models import Plan,Customer; p=Plan.objects.get(name='Paket Restore'); assert not p.is_active; assert Customer.objects.filter(plan=p).count()==1; print('PASS: archived catalog and legacy assignment restored')")
    values=shell(target,"from core.models import Invoice,Payment,WorkPhoto; import hashlib; print('RESTORED:',Invoice.objects.get().balance,Payment.objects.count(),WorkPhoto.objects.get().pk,WorkPhoto.objects.get().checksum)")
    result=next(line for line in values.splitlines() if line.startswith('RESTORED:')).split()
    assert result[1:3]==['149750','2']
    status,content,_=request(f'/operations/photos/{result[3]}/download/')
    import hashlib
    assert status==200 and hashlib.sha256(content).hexdigest()==result[4]
    # This code was accepted in this target after recovery; immediate replay remains refused there.
    replay=shell(target,"from core.security import verify_factor,decrypt_secret; import pyotp; from core.models import MFADevice; d=MFADevice.objects.get(); assert not verify_factor(d.user,pyotp.TOTP(decrypt_secret(d.encrypted_secret)).at(d.last_step*30)); print('PASS: target OTP replay refused')")
    assert 'PASS:' in replay
    return TestIdentity(identity.password,identity.totp_secret,jar)


def drill(root):
    stacks=[]
    def fresh(name):
        directory=root/name
        prepare(directory,port())
        stacks.append(directory)
        return directory
    try:
        source=fresh('source')
        identity=verify(source)
        verify_source(source)
        preflight_active(source,fingerprint)
        compose(source,'stop','web','network-worker','billing-worker','reminder-worker',capture=True)
        shell(source,FIXTURE)
        before=fingerprint(source)
        assert before['core_payment'][0]==2 and before['core_paymentreversal'][0]==1 and before['core_workphoto'][0]==1
        keyfile=root/'archive-key.json';generate_key(keyfile);key=read_key(keyfile)
        archive=root/'source.tnbk'
        backup(source,archive,key)
        verify_archive(archive,key)
        assert fingerprint(source)==before
        first=archive.read_bytes()
        try: backup(source,archive,key)
        except ArchiveError: pass
        else: raise AssertionError('Existing archive was overwritten')
        assert archive.read_bytes()==first
        print('PASS: encrypted staging backup, non-clobber publication and unchanged source; nonempty ledger/MFA/photo/scheduler/outbox.',flush=True)

        target=fresh('restored')
        wrongfile=root/'wrong-key.json';generate_key(wrongfile)
        try: restore(target,archive,read_key(wrongfile),source/'secrets/mfa-key')
        except ArchiveError: pass
        else: raise AssertionError('Wrong archive key accepted')
        assert not manifest(target)['initialized']
        damaged=root/'damaged.tnbk'
        raw=bytearray(first);raw[-1]^=1;damaged.write_bytes(raw)
        try: restore(target,damaged,key,source/'secrets/mfa-key')
        except ArchiveError: pass
        else: raise AssertionError('Tampered archive accepted')
        assert not manifest(target)['initialized']
        try: restore(source,archive,key,source/'secrets/mfa-key')
        except ArchiveError: pass
        else: raise AssertionError('Source directory accepted as target')
        wrong_mfa=root/'wrong-mfa.key';wrong_mfa.write_bytes(Fernet.generate_key());wrong_mfa.chmod(0o600)
        bad_target=fresh('wrong-mfa')
        try: restore(bad_target,archive,key,wrong_mfa)
        except ArchiveError: pass
        else: raise AssertionError('Wrong MFA material accepted')
        assert manifest(bad_target)['restore_state']=='failed'
        assert fingerprint(bad_target)==before  # Valid SQL committed, key check failed; retained and blocked.
        try: up(bad_target,capture=True)
        except RuntimeError: pass
        else: raise AssertionError('Failed restore started an app')
        corrupt=root/'invalid-dump.tnbk';encrypt_stream(io.BytesIO(b'PGDMPnot-a-real-dump'),corrupt,key,'postgresql')
        broken=fresh('invalid-dump')
        try: restore(broken,corrupt,key,source/'secrets/mfa-key')
        except ArchiveError: pass
        else: raise AssertionError('Malformed dump accepted')
        assert manifest(broken)['restore_state']=='failed'
        assert query(database_container(broken),EMPTY_SQL)=='t'
        print('PASS: wrong key/tamper/source target/wrong MFA/invalid dump refused; failed targets cannot start.',flush=True)

        restore(target,archive,key,source/'secrets/mfa-key')
        assert (target/'secrets/session-key').read_bytes()!=(source/'secrets/session-key').read_bytes()
        assert fingerprint(target)==before
        try: restore(target,archive,key,source/'secrets/mfa-key')
        except ArchiveError: pass
        else: raise AssertionError('Occupied restore destination accepted')
        up(target,capture=True)
        services=compose(target,'ps','--services','--filter','status=running',capture=True).stdout.split()
        assert not {'network-worker','billing-worker','reminder-worker'} & set(services),services
        print('PASS: all application/auth tables, sequences, binary photos and ledger match after SQL restore; web-only startup holds every worker.',flush=True)
        verify_recovery(target)

        verify_restored_access(target,identity)
        assert fingerprint(source)==before
        print('PASS: old sessions invalidated, restored password/TOTP/CSRF/authorized photo download work; target OTP replay refused; source unchanged.',flush=True)

        package=root/'exported-package';custody=root/'exported-keys'
        metadata=export_package(archive,keyfile,source/'secrets/mfa-key',package,custody)
        assert str(source) not in json.dumps(metadata) and identity.totp_secret not in json.dumps(metadata)
        received=root/'received-copy';received_keys=root/'received-key-custody'
        shutil.copytree(package,received);shutil.copytree(custody,received_keys)
        portable=fresh('portable-restored')
        # Mismatched original MFA custody is refused before the fresh target changes.
        mfa_copy=(received_keys/'mfa.key').read_bytes()
        (received_keys/'mfa.key').write_bytes(Fernet.generate_key())
        try:restore_package(received,received_keys,portable)
        except ArchiveError:pass
        else:raise AssertionError('Mismatched package MFA custody accepted')
        assert not manifest(portable)['initialized']
        (received_keys/'mfa.key').write_bytes(mfa_copy)
        original_session=hashlib.sha256((source/'secrets/session-key').read_bytes()).hexdigest()
        # Only the source created by fresh() in THIS temporary drill is destroyed.
        assert source in stacks and source.resolve().parent==root.resolve()
        compose(source,'down','--volumes','--remove-orphans',capture=True)
        stacks.remove(source);shutil.rmtree(source)
        archive.unlink();keyfile.unlink();shutil.rmtree(package);shutil.rmtree(custody)
        del key,first,mfa_copy
        assert not source.exists() and not archive.exists() and not keyfile.exists()
        assert verify_package(received,received_keys)==metadata
        restore_package(received,received_keys,portable)
        assert fingerprint(portable)==before
        assert hashlib.sha256((portable/'secrets/session-key').read_bytes()).hexdigest()!=original_session
        assert manifest(portable)['recovery_pending']
        up(portable,capture=True);verify_recovery(portable)
        verify_restored_access(portable,identity)
        preflight_recovery(portable,received,received_keys,fingerprint,shell)
        print('PASS: transported package and separate keys restore all tables/sequences/ledger/MFA/photo after original source directory/DB volume/archive/keys are gone; HTTPS auth works and workers remain held.',flush=True)
    finally:
        for directory in reversed(stacks):compose(directory,'down','--volumes','--remove-orphans',capture=True)


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='tagihnet-staging-recovery-check-') as directory:drill(Path(directory))
    print('PASS: all disposable recovery stacks cleaned; persistent user staging/demo preserved.')

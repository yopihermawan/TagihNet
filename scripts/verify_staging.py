"""Real containers, TLS and login on an isolated disposable PostgreSQL staging stack."""
import copy
import http.cookiejar
import json
import re
import secrets
import socket
import ssl
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, HTTPCookieProcessor, Request, build_opener

import pyotp
from staging_lab import ROOT, compose, docker, manifest, prepare, up, wait_ready


@dataclass(repr=False)
class TestIdentity:
    password: str
    totp_secret: str
    cookies: object


def verify(directory):
    # A restored manifest without its original DB volume must never silently seed a replacement.
    state=manifest(directory)
    state["initialized"]=True
    (directory/'manifest.json').write_text(json.dumps(state))
    try:
        up(directory,capture=True)
    except RuntimeError: pass
    else: raise AssertionError('Missing initialized volume was replaced')
    state["initialized"]=False
    (directory/'manifest.json').write_text(json.dumps(state))
    up(directory,capture=True)
    data=manifest(directory)
    base=f"https://localhost:{data['port']}"
    ca=directory/"secrets/ca.crt"
    context=ssl.create_default_context(cafile=str(ca))
    jar=http.cookiejar.CookieJar()
    client=build_opener(HTTPSHandler(context=context),HTTPCookieProcessor(jar))

    def request(path,form=None,headers=None):
        fields={"Origin":base,"Referer":base+"/"}
        fields.update(headers or {})
        req=Request(base+path,data=urlencode(form).encode() if form is not None else None,headers=fields)
        try:
            with client.open(req,timeout=10) as response: return response.status,response.headers,response.read(),response.url
        except HTTPError as response:
            return response.code,response.headers,response.read(),response.url

    try:
        build_opener(HTTPSHandler(context=ssl.create_default_context())).open(base+"/health/",timeout=5)
        raise AssertionError("Untrusted local CA was accepted")
    except URLError: pass
    status,headers,body,_=request('/login/')
    assert status==200 and b"Traceback" not in body and b"seed_demo" not in body
    assert 'max-age=3600' in headers['Strict-Transport-Security'] and 'no-store' in headers['Cache-Control']
    assert headers['X-Content-Type-Options']=='nosniff' and "default-src 'self'" in headers['Content-Security-Policy']
    css=re.search(rb'href="(/static/app\.[^"]+\.css)"',body).group(1).decode()
    status,headers,body,_=request(css)
    assert status==200 and 'immutable' in headers['Cache-Control']
    assert request('/health/',headers={'Host':'unapproved.example.invalid'})[0]==400
    assert request('/health/',headers={'X-Forwarded-Proto':'http','X-Forwarded-Host':'unapproved.example.invalid'})[0]==200
    for path in ('/static/data/.mfa_key','/static/.env','/static/core/models.py'):
        assert request(path)[0]==404
    print('PASS: verified HTTPS, unknown CA/host rejected, secure headers, hashed static and private-file isolation.',flush=True)

    bootstrap_args=('run','--rm','--no-deps','migrate','python','manage.py','bootstrap_isp',
        '--isp-name','ISP Staging Fiktif','--username','staging-owner','--email','staging-owner@example.invalid',
        '--password-file','/run/tagihnet/bootstrap-password')
    compose(directory,*bootstrap_args,capture=True)
    assert compose(directory,*bootstrap_args,capture=True,check=False).returncode!=0
    password=(directory/'secrets/bootstrap-password').read_text()
    def csrf(): return next(cookie.value for cookie in jar if cookie.name=='csrftoken')
    status,_,body,url=request('/login/',{'username':'staging-owner','password':password,'csrfmiddlewaretoken':csrf()})
    assert status==200 and url.endswith('/mfa/setup/')
    secret=re.search(rb'class="provisioning-secret">([^<]+)',body).group(1).decode().strip()
    status,_,body,_=request('/mfa/setup/',{'code':pyotp.TOTP(secret).now(),'csrfmiddlewaretoken':csrf()})
    assert status==200 and b'Simpan kode pemulihan' in body
    recovery_codes=[value.decode() for value in re.findall(rb'<code>([A-F0-9-]{35})</code>',body)]
    assert len(recovery_codes)==10
    assert request('/settings/access/')[0]==200
    assert request('/logout/',{})[0]==403
    session=next(cookie for cookie in jar if cookie.name=='sessionid')
    assert session.secure and session.has_nonstandard_attr('HttpOnly') and session.get_nonstandard_attr('SameSite')=='Lax'
    print('PASS: non-superuser owner bootstrap, repeat refused, real HTTPS/password/MFA/session and CSRF enforcement.',flush=True)

    database_probe='''
import os,socket
from pathlib import Path
import psycopg
from django.conf import settings
from django.db import connection
assert not settings.DEBUG and settings.SIMULATION
with connection.cursor() as c:
    c.execute("SELECT current_user,ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()")
    assert c.fetchone()==('tagihnet_runtime',True)
    c.execute("SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user")
    assert c.fetchone()==(False,False,False)
    c.execute("SELECT has_schema_privilege(current_user,'public','CREATE')")
    assert not c.fetchone()[0]
try:
    with connection.cursor() as c: c.execute('CREATE TABLE forbidden_runtime_ddl (id int)')
except psycopg.errors.InsufficientPrivilege: pass
except Exception as error:
    from django.db.utils import ProgrammingError
    assert isinstance(error,ProgrammingError) and getattr(error.__cause__,'sqlstate',None)=='42501'
else: raise AssertionError('Runtime DDL was accepted')
db=settings.DATABASES['default']
params=dict(dbname=db['NAME'],user=db['USER'],password=db['PASSWORD'],host=db['HOST'],port=db['PORT'],sslmode='verify-full',sslrootcert=db['OPTIONS']['sslrootcert'],connect_timeout=3)
for change in (dict(sslmode='disable'),dict(sslrootcert='/etc/ssl/certs/ca-certificates.crt'),dict(host='wrong.example.invalid',hostaddr=socket.gethostbyname('db'))):
    try: psycopg.connect(**{**params,**change}).close()
    except psycopg.OperationalError: pass
    else: raise AssertionError('Invalid database TLS was accepted')
assert not Path('/run/tagihnet/bootstrap-password').exists()
assert not Path('/run/tagihnet/migration-password').exists()
assert not Path('/run/tagihnet/ca.key').exists()
assert os.getuid()==10001
print('PASS: runtime SQL permissions and TLS verification, no migration/CA private credentials mounted.')
'''
    result=compose(directory,'exec','-T','web','python','manage.py','shell','-c',database_probe,capture=True)
    assert 'PASS:' in result.stdout
    print('PASS: PostgreSQL verify-full, runtime DDL denied; plaintext, wrong CA and wrong hostname rejected.',flush=True)
    ids={}
    for service in ('web','network-worker','billing-worker','reminder-worker'):
        container=compose(directory,'ps','--quiet',service,capture=True).stdout.strip()
        info=json.loads(docker('inspect',container,capture=True).stdout)[0]
        assert info['Config']['Labels']['com.docker.compose.project']==data['project']
        assert info['Config']['User']=='10001:10001' and info['HostConfig']['ReadonlyRootfs']
        assert 'ALL' in info['HostConfig']['CapDrop'] and info['State']['Running']
        assert not any(mount.get('Source','').endswith('/data') for mount in info['Mounts'])
        ids[service]=container
    db_id=compose(directory,'ps','--quiet','db',capture=True).stdout.strip()
    db_info=json.loads(docker('inspect',db_id,capture=True).stdout)[0]
    assert not db_info['NetworkSettings']['Ports'].get('5432/tcp')
    info=json.loads(docker('inspect',ids['web'],capture=True).stdout)[0]
    assert info['NetworkSettings']['Ports']['8443/tcp'][0]['HostIp']=='127.0.0.1'
    print('PASS: non-root/read-only/capability-restricted app containers, private database and loopback-only HTTPS.',flush=True)
    # Docker restart policies activate after a successful ten-second run.
    time.sleep(11)
    # Docker API kill is an administrative stop, which suppresses restart policy.
    # Crash the actual worker process instead, as an unexpected runtime failure.
    compose(directory,'exec','-T','billing-worker','python','-c',
        "import os,signal; from pathlib import Path; "
        "pids=[int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit() "
        "and (p/'cmdline').exists() and (p/'cmdline').read_bytes().split(bytes([0]))[1:3]==[b'manage.py',b'run_billing_worker']]; "
        "assert len(pids)==1; os.kill(pids[0],signal.SIGKILL)",capture=True,check=False)
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        info=json.loads(docker('inspect',ids['billing-worker'],capture=True).stdout)[0]
        if info['State']['Running'] and info['RestartCount']>=1: break
        time.sleep(.25)
    else: raise AssertionError('Worker did not restart')
    compose(directory,'restart','web',capture=True)
    wait_ready(directory)
    assert request('/settings/access/')[0]==200
    report=compose(directory,'run','--rm','--no-deps','migrate','python','manage.py','check','--deploy',capture=True)
    assert 'security.W005' in report.stderr and 'security.W021' in report.stderr
    assert set(re.findall(r'security\.W\d+',report.stderr))=={'security.W005','security.W021'}
    status,_,body,url=request('/account/profile/')
    assert status==200 and url==base+'/account/profile/'
    profile_token=re.search(rb'name="token" value="([^"]+)"',body).group(1).decode()
    profile_fields={'token':profile_token,'first_name':'Nama Staging Fiktif','last_name':'Profil'}
    assert request('/account/profile/',profile_fields)[0]==403
    status,_,body,url=request('/account/profile/',{**profile_fields,'csrfmiddlewaretoken':csrf()})
    assert status==200 and url==base+'/account/profile/' and b'value="Nama Staging Fiktif"' in body
    assert request('/account/profile/',{**profile_fields,'csrfmiddlewaretoken':csrf()})[0]==409
    assert request('/settings/access/')[0]==200
    print('PASS: own profile over verified HTTPS requires CSRF, preserves login/access, persists display name and rejects stale form replay.',flush=True)
    old_cookies=http.cookiejar.CookieJar()
    for cookie in jar:old_cookies.set_cookie(copy.copy(cookie))
    old_client=build_opener(HTTPSHandler(context=context),HTTPCookieProcessor(old_cookies))
    assert request('/account/password/')[0]==200
    changed_password=secrets.token_urlsafe(48)
    fields={'old_password':password,'new_password1':changed_password,'new_password2':changed_password,'code':recovery_codes[1]}
    assert request('/account/password/',fields)[0]==403  # No CSRF, no factor consumption.
    status,_,_,url=request('/account/password/',{**fields,'csrfmiddlewaretoken':csrf()})
    assert status==200 and url==base+'/login/'
    with old_client.open(base+'/settings/access/',timeout=10) as response:assert response.url.startswith(base+'/login/')
    assert request('/login/',{'username':'staging-owner','password':password,'csrfmiddlewaretoken':csrf()})[3]==base+'/login/'
    password=changed_password
    assert request('/login/',{'username':'staging-owner','password':password,'csrfmiddlewaretoken':csrf()})[3]==base+'/mfa/verify/'
    assert request('/mfa/verify/',{'code':recovery_codes[0],'csrfmiddlewaretoken':csrf()})[3]==base+'/'
    assert request('/settings/access/')[0]==200
    print('PASS: own password change over verified HTTPS requires current password/MFA/CSRF, revokes old session, rejects old password and still requires MFA after login.',flush=True)
    status,_,body,_=request('/plans/new/')
    assert status==200
    version=re.search(rb'name="version" value="([^"]+)"',body).group(1).decode()
    status,_,body,_=request('/plans/new/',{'name':'Paket Katalog Fiktif','speed_mbps':'20','price':'175000',
        'version':version,'csrfmiddlewaretoken':csrf()})
    assert status==200
    token=re.search(rb'name="token" value="([^"]+)"',body).group(1).decode()
    status,_,body,_=request('/plans/confirm/',{'token':token,'reviewed':'on','csrfmiddlewaretoken':csrf()})
    assert status==200
    archive_path=re.search(rb'href="(/plans/\d+/archive/)"',body).group(1).decode()
    status,_,body,_=request(archive_path)
    assert status==200
    token=re.search(rb'name="token" value="([^"]+)"',body).group(1).decode()
    assert request('/plans/availability/confirm/',{'token':token,'csrfmiddlewaretoken':csrf()})[0]==400
    status,_,body,url=request('/plans/availability/confirm/',{'token':token,'reviewed':'on','csrfmiddlewaretoken':csrf()})
    assert status==200 and 'availability=archived' in url and b'Diarsipkan' in body
    assert request('/plans/availability/confirm/',{'token':token,'reviewed':'on','csrfmiddlewaretoken':csrf()})[0]==400
    reactivate_path=re.search(rb'href="(/plans/\d+/reactivate/)"',body).group(1).decode()
    status,_,body,_=request(reactivate_path)
    assert status==200
    token=re.search(rb'name="token" value="([^"]+)"',body).group(1).decode()
    status,_,_,url=request('/plans/availability/confirm/',{'token':token,'reviewed':'on','csrfmiddlewaretoken':csrf()})
    assert status==200 and 'availability=active' in url
    print('PASS: plan creation/archive/reactivation through real HTTPS reviews/MFA, checkbox required and stale archive replay rejected.',flush=True)
    status,_,body,_=request('/settings/billing/')
    assert status==200
    original_form=re.search(rb'name="token" value="([^"]+)"',body).group(1).decode()
    fields={'token':original_form,'name':'ISP Staging Ditinjau','address':'Alamat Staging Fiktif','due_day':'31',
        'tax_bp':'1100','invoice_footer':'Footer staging ditinjau.'}
    assert request('/settings/billing/',fields)[0]==403
    status,_,body,_=request('/settings/billing/',{**fields,'csrfmiddlewaretoken':csrf()})
    assert status==200 and b'Tinjau setelan billing' in body
    token=re.search(rb'name="token" value="([^"]+)"',body).group(1).decode()
    assert request('/settings/billing/confirm/',{'token':token,'csrfmiddlewaretoken':csrf()})[0]==400
    status,_,body,url=request('/settings/billing/confirm/',{'token':token,'reviewed':'on','csrfmiddlewaretoken':csrf()})
    assert status==200 and url==base+'/settings/billing/' and b'value="ISP Staging Ditinjau"' in body
    assert request('/settings/billing/',{**fields,'csrfmiddlewaretoken':csrf()})[0]==409
    assert request('/settings/billing/confirm/',{'token':token,'reviewed':'on','csrfmiddlewaretoken':csrf()})[0]==409
    print('PASS: billing settings real HTTPS/CSRF/review/checkbox/MFA save and stale form/review409; no direct save from edit POST.',flush=True)
    status,_,body,_=request('/operations/installations/new/')
    assert status==200
    key=re.search(rb'name="request_key" value="([^"]+)"',body).group(1).decode()
    plan_id=archive_path.split('/')[2]
    status,_,body,url=request('/operations/installations/new/',{'request_key':key,'name':'PSB Koordinat Fiktif',
        'email':'','phone':'081299987654','username':'location-staging','address':'Alamat Lokasi Fiktif','area':'Uji Lokasi',
        'plan':plan_id,'assigned_to':'','csrfmiddlewaretoken':csrf()})
    assert status==200 and re.fullmatch(re.escape(base)+r'/operations/installations/\d+/',url)
    location_path=url.removeprefix(base)+'location/'
    status,_,body,_=request(location_path)
    assert status==200
    token=re.search(rb'name="token" value="([^"]+)"',body).group(1).decode()
    assert request(location_path,{'token':token,'latitude':'-6.917464','longitude':'107.619123'})[0]==403
    assert request(location_path,{'token':token,'latitude':'-6.917464','csrfmiddlewaretoken':csrf()})[0]==400
    status,_,body,_=request(location_path,{'token':token,'latitude':'-6.917464','longitude':'107.619123','csrfmiddlewaretoken':csrf()})
    assert status==200 and b'-6.917464' in body and b'mlat=-6.917464' in body
    assert request(location_path,{'token':token,'latitude':'1','longitude':'2','csrfmiddlewaretoken':csrf()})[0]==409
    print('PASS: PSB coordinates over verified HTTPS require CSRF, reject half-pair/stale409 and render a fixed external map link without embedded map requests.',flush=True)
    logs=compose(directory,'logs','--no-color',capture=True).stdout
    for name in ('session-key','mfa-key','runtime-password','migration-password','bootstrap-password'):
        assert (directory/'secrets'/name).read_text() not in logs
    assert secret not in logs and password not in logs
    assert all(value not in logs for value in recovery_codes)
    print('PASS: killed worker restarts; HTTPS/session/MFA keys survive web restart; deploy checks have only two documented HSTS-scope warnings; no factors in logs.',flush=True)
    return TestIdentity(password,secret,jar)


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='tagihnet-stage-check-') as temporary:
        directory=Path(temporary)/'stack'
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0))
            port=listener.getsockname()[1]
        prepare(directory,port)
        try: verify(directory)
        finally:
            compose(directory,'down','--volumes','--remove-orphans',capture=True)
    print('PASS: disposable staging removed; existing demo/lab/staging data preserved.')

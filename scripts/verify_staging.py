"""Real containers, TLS and login on an isolated disposable PostgreSQL staging stack."""
import http.cookiejar
import json
import re
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
    status,_,body,_=request('/mfa/setup/',{'code':pyotp.TOTP(secret).at(time.time()-30),'csrfmiddlewaretoken':csrf()})
    assert status==200 and b'Simpan kode pemulihan' in body
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
    logs=compose(directory,'logs','--no-color',capture=True).stdout
    for name in ('session-key','mfa-key','runtime-password','migration-password','bootstrap-password'):
        assert (directory/'secrets'/name).read_text() not in logs
    assert secret not in logs and password not in logs
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

"""Local business state machines. Called only inside the signed action transaction."""
import base64
import secrets
from datetime import date, timedelta
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Max, Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from . import models as m
from .cash import MANUAL, close_snapshot, correct, post
from .locations import device_revision
from .locations import pair
from .operations import digest, event
from .permissions import capabilities


def save(item, values):
    for field,value in values.items():setattr(item,field,value)
    item.version+=1
    item.save()
    return item


def create(model, member, values, key):
    return model.objects.create(tenant=member.tenant,request_key=key,**values)


def payroll_data(tenant):
    employees=list(m.Employee.objects.filter(tenant=tenant,active=True).select_related('salary').order_by('pk')[:201])
    if not employees or len(employees)>200:raise ValidationError('Payroll memerlukan 1–200 anggota tim aktif.')
    rows=[]
    for person in employees:
        if not hasattr(person,'salary'):raise ValidationError('Atur komponen gaji seluruh anggota aktif sebelum menyiapkan payroll.')
        salary=person.salary;gross=salary.base+salary.allowance;net=gross-salary.deduction
        if salary.tenant_id!=tenant.pk or not 1<=net<=2_000_000_000:raise ValidationError('Komponen gaji tidak valid.')
        rows.append({'employee_id':person.pk,'member_id':person.member_id,'name':person.name,
            'department':person.department,'gross':gross,'deduction':salary.deduction,'net':net,
            'employee_version':person.version,'terms_version':salary.version})
    if sum(row['net'] for row in rows)>2_000_000_000:raise ValidationError('Total payroll melebihi Rp2.000.000.000.')
    return rows


def payroll_current(run):
    current=payroll_data(run.tenant)
    saved=list(run.lines.order_by('employee_id').values('employee_id','member_id','name','department','gross','deduction','net','employee_version','terms_version'))
    if current!=saved:raise ValidationError('Tim atau gaji berubah. Batalkan payroll ini dan siapkan pratinjau baru.')
    return current


def extra_dependencies(member, action, item, values):
    if action=='salary':return list(m.SalaryTerms.objects.filter(tenant=member.tenant,employee=item).values())
    if action in {'payroll_create','payroll_approve','payroll_pay'}:return payroll_data(member.tenant)
    if action=='cash_close':return close_snapshot(member.tenant,date.fromisoformat(values['period']+'-01'))
    if action=='cash_correct':return list(m.CashCorrection.objects.filter(movement=item).values('pk'))
    if action in {'endpoint_set','endpoint_remove'}:return list(m.CustomerEndpoint.objects.filter(tenant=member.tenant,customer=item).values())
    if action in {'voucher_activate','voucher_revoke'}:return {'refunded':item.batch.refunded}
    return None


def voucher_cipher():
    master=base64.urlsafe_b64decode(settings.MFA_ENCRYPTION_KEY.encode())
    derived=HKDF(algorithm=hashes.SHA256(),length=32,salt=b'TagihNet',info=b'hotspot-vouchers-v1').derive(master)
    return Fernet(base64.urlsafe_b64encode(derived))


def voucher_status(voucher):
    now=timezone.now()
    if voucher.revoked:return 'Dicabut'
    if voucher.expires_at and voucher.expires_at<=now:return 'Kedaluwarsa'
    if voucher.activated_at:return 'Aktif dalam simulasi'
    if voucher.activation_deadline<=now:return 'Batas aktivasi lewat'
    return 'Belum diaktifkan'


@sensitive_variables()
def apply(member, action, item, v, key):
    tenant=member.tenant
    if action.startswith('fund_'):
        if action=='fund_create':return create(m.Fund,member,v,key)
        return save(item,v)
    if action in {'cash_post','cash_transfer'}:
        fund=m.Fund.objects.get(tenant=tenant,pk=v['fund'],active=True)
        when=date.fromisoformat(v['business_date'])
        if action=='cash_post':return post(member,fund,v['kind'],v['amount'],when,v['note'],key)
        destination=m.Fund.objects.get(tenant=tenant,pk=v['destination'],active=True)
        if destination==fund:raise ValidationError('Dana sumber dan tujuan harus berbeda.')
        outgoing=post(member,fund,'transfer_out',v['amount'],when,v['note'],key)
        post(member,destination,'transfer_in',v['amount'],when,v['note'],key,leg=2)
        return outgoing
    if action=='cash_correct':
        if item.kind not in MANUAL|{'transfer_in','transfer_out'}:
            raise ValidationError('Koreksi pencatatan ini melalui modul payroll, komisi atau voucher asalnya.')
        if item.kind.startswith('transfer_'):
            legs=list(m.CashMovement.objects.filter(tenant=tenant,request_key=item.request_key))
            if len(legs)!=2 or {leg.kind for leg in legs}!={'transfer_in','transfer_out'}:raise ValidationError('Pasangan transfer tidak lengkap.')
            for leg in legs:correct(member,leg,v['reason'])
        else:correct(member,item,v['reason'])
        return item
    if action=='cash_close':
        period=date.fromisoformat(v['period']+'-01')
        latest=m.CashClose.objects.filter(tenant=tenant).order_by('-period').first()
        if latest and period<=latest.period:raise ValidationError('Bulan ini atau bulan sesudahnya sudah ditutup.')
        return create(m.CashClose,member,{'period':period,'snapshot':close_snapshot(tenant,period),'actor':member.user},key)
    if action.startswith('employee_'):
        values={**v,'member_id':v.pop('member',None)}
        values.pop('member',None)
        if action=='employee_create':return create(m.Employee,member,values,key)
        return save(item,values)
    if action=='salary':
        terms,_=m.SalaryTerms.objects.get_or_create(tenant=tenant,employee=item,defaults=v)
        return save(terms,v)
    if action=='payroll_create':
        period=date.fromisoformat(v['period']+'-01')
        if period>timezone.localdate().replace(day=1):raise ValidationError('Payroll hanya untuk bulan berjalan atau sebelumnya.')
        rows=payroll_data(tenant)
        run=create(m.PayrollRun,member,{'period':period,'fingerprint':digest(rows),'created_by':member.user,'total':sum(row['net'] for row in rows)},key)
        m.PayrollLine.objects.bulk_create([m.PayrollLine(run=run,**row) for row in rows])
        return run
    if action=='payroll_approve':
        if item.status not in {'preview','approved'}:raise ValidationError('Payroll ini tidak dapat disetujui.')
        payroll_current(item)
        return save(item,{'status':'approved','authorized_by':member.user,'authorization_version':member.session_version,'mfa_revision':device_revision(member.user)})
    if action=='payroll_pay':
        if (item.status!='approved' or item.authorized_by_id!=member.user_id or item.authorization_version!=member.session_version
                or str(item.mfa_revision)!=device_revision(member.user)):
            raise ValidationError('Setujui ulang payroll dengan akun dan versi akses/MFA saat ini sebelum mencatat pembayaran.')
        payroll_current(item)
        movement=post(member,m.Fund.objects.get(tenant=tenant,pk=v['fund']),'payroll',item.total,timezone.localdate(),
            f'Payroll {item.period:%m/%Y}',key,source=f'payroll:{item.pk}')
        return save(item,{'status':'paid','movement':movement})
    if action=='payroll_cancel':
        if item.status not in {'preview','approved','stale'}:raise ValidationError('Payroll yang dibayar harus direversal, bukan dibatalkan.')
        return save(item,{'status':'cancelled'})
    if action=='payroll_reverse':
        if item.status!='paid':raise ValidationError('Hanya payroll dibayar yang dapat direversal.')
        correct(member,item.movement,v['reason']);return save(item,{'status':'reversed'})
    if action in {'lead_create','lead_edit'}:
        if item and item.status=='converted':raise ValidationError('Prospek sudah menjadi PSB. Kelola perubahan melalui alur PSB.')
        values={**v,'plan_id':v['plan']};values.pop('plan')
        assigned=values.pop('assigned_to',None)
        values['assigned_to_id']=assigned if 'manage_sales' in capabilities(member) else member.pk
        if action=='lead_create':return create(m.Lead,member,{**values,'created_by':member.user},key)
        return save(item,values)
    if action=='lead_convert':
        if item.status!='qualified' or item.installation_id:raise ValidationError('Prospek harus siap diajukan dan belum memiliki PSB.')
        if not item.plan.is_active:raise ValidationError('Paket sudah diarsipkan. Pilih paket aktif sebelum mengajukan PSB.')
        if m.Customer.objects.filter(tenant=tenant).filter(Q(phone=item.phone)|Q(username__iexact=item.username)).exists():
            raise ValidationError('Telepon atau username sudah dipakai pelanggan.')
        if m.Installation.objects.filter(tenant=tenant).exclude(status='cancelled').filter(Q(phone=item.phone)|Q(username__iexact=item.username)).exists():
            raise ValidationError('Telepon atau username sudah diajukan pada PSB lain.')
        values={field:getattr(item,field) for field in ('name','phone','email','username','address','area','plan_id')}
        order=m.Installation(tenant=tenant,created_by=member.user,request_key=key,fingerprint=digest(values),**values)
        order.full_clean();order.save();event(member,order,'created','PSB diajukan dari prospek sales; belum ada aktivasi atau penagihan.')
        return save(item,{'installation':order,'status':'converted'})
    if action=='commission_pay':
        if item.status!='pending':raise ValidationError('Komisi sudah dicatat dibayar atau direversal.')
        if item.lead.installation.status!='activated':raise ValidationError('Komisi hanya tersedia setelah aktivasi PSB.')
        movement=post(member,m.Fund.objects.get(tenant=tenant,pk=v['fund']),'commission',item.amount,timezone.localdate(),
            f'Komisi prospek #{item.lead_id}',key,source=f'commission:{item.pk}')
        return save(item,{'movement':movement,'status':'paid'})
    if action=='commission_reverse':
        if item.status!='paid':raise ValidationError('Hanya komisi dibayar yang dapat direversal.')
        correct(member,item.movement,v['reason']);return save(item,{'status':'reversed'})
    if action.startswith('campaign_'):
        values={**v,'plan_id':v['plan'],'starts':date.fromisoformat(v['starts']),'ends':date.fromisoformat(v['ends'])};values.pop('plan')
        if action=='campaign_create':return create(m.Campaign,member,values,key)
        return save(item,values)
    if action.startswith('hotspot_'):
        if action=='hotspot_create':return create(m.HotspotProfile,member,v,key)
        return save(item,v)
    if action=='voucher_sell':
        profile=m.HotspotProfile.objects.get(tenant=tenant,pk=v['profile'],active=True)
        snapshot={name:getattr(profile,name) for name in ('name','speed_mbps','duration_minutes','activate_within_days','price','version')}
        total=profile.price*v['quantity']
        movement=post(member,m.Fund.objects.get(tenant=tenant,pk=v['fund']),'voucher',total,timezone.localdate(),
            f'Voucher simulasi: {profile.name}',key,source=f'voucher:{key}')
        batch=create(m.VoucherBatch,member,{'profile':profile,'snapshot':snapshot,'quantity':v['quantity'],'total':total,
            'movement':movement,'actor':member.user,'fingerprint':digest(snapshot)},key)
        cipher=voucher_cipher();deadline=timezone.now()+timedelta(days=profile.activate_within_days)
        m.Voucher.objects.bulk_create([m.Voucher(tenant=tenant,batch=batch,username='TN-'+secrets.token_hex(10),
            encrypted_password=cipher.encrypt(secrets.token_urlsafe(12).encode()).decode(),activation_deadline=deadline) for _ in range(v['quantity'])])
        return batch
    if action=='voucher_activate':
        if item.revoked or item.batch.refunded:raise ValidationError('Voucher sudah dicabut/dikoreksi.')
        if item.activated_at:raise ValidationError('Voucher sudah diaktifkan; masa berlaku tidak diperpanjang.')
        now=timezone.now()
        if item.activation_deadline<=now:raise ValidationError('Batas aktivasi voucher sudah lewat.')
        return save(item,{'activated_at':now,'expires_at':now+timedelta(minutes=item.batch.snapshot['duration_minutes'])})
    if action=='voucher_revoke':return save(item,{'revoked':True})
    if action=='voucher_refund':
        if item.refunded:raise ValidationError('Penjualan ini sudah dikoreksi.')
        correct(member,item.movement,v['reason']);item.refunded=True;item.save(update_fields=['refunded'])
        from django.db.models import F
        item.vouchers.update(revoked=True,version=F('version')+1)
        return item
    if action.startswith('node_'):
        parent=m.NetworkNode.objects.get(tenant=tenant,pk=v['parent'],kind='odc',active=True) if v['parent'] else None
        if (v['kind']=='odp')!=(parent is not None):raise ValidationError('ODP wajib memiliki ODC induk; ODC tidak memiliki induk.')
        if item:
            if item.kind!=v['kind']:raise ValidationError('Jenis node tidak dapat diubah. Buat node baru untuk pemindahan.')
            if not v['active'] and (item.children.filter(active=True).exists() or item.ports.exists()):raise ValidationError('Pindahkan port pelanggan dan nonaktifkan ODP anak sebelum menonaktifkan node.')
            used=item.ports.aggregate(maximum=Max('port'))['maximum'] or 0
            if v['capacity']<used or (v['kind']=='odc' and v['capacity']<item.children.filter(active=True).count()):raise ValidationError('Kapasitas lebih kecil dari port atau ODP yang dipakai.')
        if parent and m.NetworkNode.objects.filter(parent=parent,active=True).exclude(pk=item.pk if item else None).count()>=parent.capacity and v['active']:
            raise ValidationError('Kapasitas ODC induk sudah penuh.')
        values={**v,'parent':parent};values['latitude'],values['longitude']=pair(v['latitude'],v['longitude'])
        if action=='node_create':return create(m.NetworkNode,member,values,key)
        return save(item,values)
    if action=='endpoint_set':
        node=m.NetworkNode.objects.get(tenant=tenant,pk=v['node'],kind='odp',active=True,parent__active=True)
        if not 1<=v['port']<=node.capacity:raise ValidationError('Nomor port melebihi kapasitas ODP.')
        if m.CustomerEndpoint.objects.filter(node=node,port=v['port']).exclude(customer=item).exists():raise ValidationError('Port sudah dipakai pelanggan lain.')
        endpoint,_=m.CustomerEndpoint.objects.get_or_create(tenant=tenant,customer=item,defaults={'node':node,'port':v['port']})
        save(endpoint,{'node':node,'port':v['port']});return item
    if action=='endpoint_remove':
        m.CustomerEndpoint.objects.filter(tenant=tenant,customer=item).delete();return item
    raise ValidationError('Tindakan tidak tersedia.')


def accrue_commission(order):
    """Immutable amount and recipient at activation; no external payout."""
    lead=m.Lead.objects.filter(tenant=order.tenant,installation=order).first()
    if lead and lead.assigned_to_id and lead.commission_amount>0:
        m.Commission.objects.get_or_create(tenant=order.tenant,lead=lead,
            defaults={'member_id':lead.assigned_to_id,'amount':lead.commission_amount})


def review_summary(member, action, item, values):
    """Show amounts and immutable source identity, including actions with no editable fields."""
    from .business_views import money
    rows=[]
    if item:
        rows.append(('Objek ditinjau',getattr(item,'name',getattr(item,'title',f'#{item.pk}'))))
    if action.startswith('payroll_') and item:
        rows += [('Periode payroll',item.period.strftime('%m/%Y')),('Status saat ini',item.get_status_display()),('Total payroll',money(item.total))]
    if action=='salary':rows.append(('Gaji bersih setelah perubahan',money(values['base']+values['allowance']-values['deduction'])))
    if action=='cash_correct':
        rows += [('Dana transaksi asli',item.fund.name),('Jenis transaksi asli',item.get_kind_display()),('Nominal asli',money(item.amount)),('Tanggal transaksi asli',item.business_date)]
        if item.kind.startswith('transfer_'):rows.append(('Dampak','Kedua sisi transfer dikoreksi bersama pada tanggal hari ini.'))
    if action=='cash_close':
        for account in close_snapshot(member.tenant,date.fromisoformat(values['period']+'-01')):
            rows.append(('Snapshot '+account['name'],f"Awal {money(account['opening'])}; masuk {money(account['in'])}; keluar {money(account['out'])}; akhir {money(account['closing'])}"))
    if action.startswith('commission_'):
        rows += [('Prospek komisi',item.lead.name),('Penerima komisi',item.member.user.username),('Nominal komisi',money(item.amount)),('Status saat ini',item.get_status_display())]
    if action=='voucher_sell':
        profile=m.HotspotProfile.objects.get(tenant=member.tenant,pk=values['profile'])
        rows += [('Harga per voucher',money(profile.price)),('Total penerimaan',money(profile.price*values['quantity'])),('Durasi sejak aktivasi',f'{profile.duration_minutes} menit'),('Batas aktivasi sejak penjualan',f'{profile.activate_within_days} hari')]
    if action in {'voucher_activate','voucher_revoke'}:
        rows += [('Username voucher',item.username),('Status saat ini',voucher_status(item)),('Durasi historis',f'{item.batch.snapshot["duration_minutes"]} menit')]
    if action=='voucher_refund':rows += [('Total penjualan yang dikoreksi',money(item.total)),('Jumlah voucher yang dicabut',item.quantity)]
    if action=='lead_convert':
        rows += [('Nama prospek',item.name),('Telepon',item.phone),('Username layanan',item.username),('Paket',item.plan.name),('Dampak','Membuat satu PSB baru; belum membuat pelanggan, faktur, atau komisi.')]
    if action.startswith('node_') and item:
        rows += [('ODP anak aktif',item.children.filter(active=True).count()),('Port pelanggan terpakai',item.ports.count())]
    if action=='endpoint_remove':
        endpoint=m.CustomerEndpoint.objects.filter(tenant=member.tenant,customer=item).select_related('node').first()
        rows.append(('Pemetaan yang dilepas',f'{endpoint.node.name} / port {endpoint.port}' if endpoint else 'Belum dipetakan'))
    return rows

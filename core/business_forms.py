"""A closed action registry: URL values never select arbitrary models or fields."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import unicodedata
import uuid
from django import forms
from django.core.exceptions import ValidationError
from . import models as m
from .locations import pair
from .permissions import capabilities
from .validation import normalize_phone


@dataclass(frozen=True)
class Action:
    title:str
    module:str
    cap:str
    model:object=None


ACTIONS={
    'fund_create':Action('Tambah dana','cash','manage_cash'),
    'fund_edit':Action('Ubah dana','cash','manage_cash',m.Fund),
    'cash_post':Action('Catat kas','cash','manage_cash'),
    'cash_transfer':Action('Transfer antar dana','cash','manage_cash'),
    'cash_correct':Action('Koreksi pencatatan kas','cash','manage_cash',m.CashMovement),
    'cash_close':Action('Tutup buku kas bulanan','cash','manage_cash'),
    'employee_create':Action('Tambah anggota tim','team','manage_team'),
    'employee_edit':Action('Ubah anggota tim','team','manage_team',m.Employee),
    'salary':Action('Atur komponen gaji','payroll','manage_payroll',m.Employee),
    'payroll_create':Action('Siapkan payroll','payroll','manage_payroll'),
    'payroll_approve':Action('Setujui payroll','payroll','manage_payroll',m.PayrollRun),
    'payroll_pay':Action('Catat pembayaran payroll','payroll','manage_payroll',m.PayrollRun),
    'payroll_cancel':Action('Batalkan payroll','payroll','manage_payroll',m.PayrollRun),
    'payroll_reverse':Action('Reversal payroll','payroll','manage_payroll',m.PayrollRun),
    'lead_create':Action('Tambah prospek','sales','sales'),
    'lead_edit':Action('Ubah prospek','sales','sales',m.Lead),
    'lead_convert':Action('Ajukan prospek menjadi PSB','sales','sales',m.Lead),
    'commission_pay':Action('Catat pembayaran komisi','sales','manage_sales',m.Commission),
    'commission_reverse':Action('Reversal komisi','sales','manage_sales',m.Commission),
    'campaign_create':Action('Buat kampanye','sales','manage_sales'),
    'campaign_edit':Action('Ubah/publikasikan kampanye','sales','manage_sales',m.Campaign),
    'hotspot_create':Action('Tambah profil hotspot simulasi','hotspot','manage_hotspot'),
    'hotspot_edit':Action('Ubah profil hotspot simulasi','hotspot','manage_hotspot',m.HotspotProfile),
    'voucher_sell':Action('Catat penjualan voucher simulasi','hotspot','manage_hotspot'),
    'voucher_activate':Action('Simulasikan aktivasi voucher','hotspot','manage_hotspot',m.Voucher),
    'voucher_revoke':Action('Cabut voucher simulasi','hotspot','manage_hotspot',m.Voucher),
    'voucher_refund':Action('Koreksi penjualan dan cabut seluruh voucher','hotspot','manage_hotspot',m.VoucherBatch),
    'node_create':Action('Tambah ODC/ODP','topology','manage_topology'),
    'node_edit':Action('Ubah ODC/ODP','topology','manage_topology',m.NetworkNode),
    'endpoint_set':Action('Petakan port pelanggan','topology','manage_topology',m.Customer),
    'endpoint_remove':Action('Lepas pemetaan port pelanggan','topology','manage_topology',m.Customer),
}


def serialize(values):
    result={}
    for name,value in values.items():
        if name=='token':continue
        if hasattr(value,'pk'):value=value.pk
        elif isinstance(value,date):value=value.isoformat()[:7] if name=='period' else value.isoformat()
        elif isinstance(value,(Decimal,uuid.UUID)):value=str(value)
        result[name]=value
    return result


class LocalForm(forms.Form):
    def clean(self):
        values=super().clean()
        for name,value in list(values.items()):
            if isinstance(value,str) and name!='token':
                value=unicodedata.normalize('NFC',value).strip()
                if any(unicodedata.category(char) in {'Cc','Cf','Cs'} and char not in '\n\r\t' for char in value):
                    self.add_error(name,'Karakter kontrol tidak diizinkan.')
                try:self.fields[name].run_validators(value)
                except ValidationError as error:self.add_error(name,error)
                values[name]=value
        if 'phone' in values and values['phone']:
            try:values['phone']=normalize_phone(values['phone'])
            except ValidationError as e:self.add_error('phone',e)
        if 'contact' in values:
            try:values['contact']=normalize_phone(values['contact'])
            except ValidationError as e:self.add_error('contact',e)
        if 'reason' in values and len(values['reason'])<10:self.add_error('reason','Alasan wajib sedikitnya 10 karakter.')
        for name in ('period','business_date','starts','ends'):
            if values.get(name) and values[name]<date(2000,1,1):self.add_error(name,'Gunakan tanggal sejak tahun 2000.')
        if 'latitude' in values and 'longitude' in values:
            try:values['latitude'],values['longitude']=pair(values['latitude'],values['longitude'])
            except ValidationError as e:self.add_error(None,e)
        if values.get('starts') and values.get('ends') and values['ends']<values['starts']:
            self.add_error('ends','Tanggal selesai harus sama atau sesudah tanggal mulai.')
        if {'base','allowance','deduction'}<=values.keys():
            net=values['base']+values['allowance']-values['deduction']
            if not 1<=net<=2_000_000_000:self.add_error(None,'Gaji bersih harus Rp1–2.000.000.000.')
        return values


def make_form(member, action, item=None, *, data=None, token=None):
    spec=ACTIONS[action]
    fields={};initial={}
    def text(name,label,limit,required=True,area=False):
        fields[name]=forms.CharField(label=label,max_length=limit,required=required,
            widget=forms.Textarea(attrs={'rows':3}) if area else forms.TextInput())
    def number(name,label,minimum=1,maximum=2_000_000_000):
        fields[name]=forms.IntegerField(label=label,min_value=minimum,max_value=maximum)
    def choice(name,label,query,required=True):
        if query.model is m.Membership:query=query.select_related('user')
        fields[name]=forms.ModelChoiceField(label=label,queryset=query,required=required)
        if query.model is m.Membership:fields[name].label_from_instance=lambda v:f'{v.user.username} · {v.get_role_display()}'
    def day(name,label,month=False):
        fields[name]=forms.DateField(label=label,input_formats=['%Y-%m'] if month else ['%Y-%m-%d'],
            widget=forms.DateInput(format='%Y-%m' if month else '%Y-%m-%d',attrs={'type':'month' if month else 'date','min':'2000-01' if month else '2000-01-01'}))
    def boolean(name,label):fields[name]=forms.BooleanField(label=label,required=False)
    funds=m.Fund.objects.filter(tenant=member.tenant,active=True)
    plans=m.Plan.objects.filter(tenant=member.tenant,is_active=True)
    if action.startswith('fund_'):
        text('name','Nama dana',80);fields['kind']=forms.ChoiceField(label='Jenis dana',choices=m.Fund._meta.get_field('kind').choices)
        boolean('active','Dana aktif');initial['active']=True
    elif action in {'cash_post','cash_transfer'}:
        choice('fund','Dana sumber',funds)
        if action=='cash_transfer':choice('destination','Dana tujuan',funds)
        else:fields['kind']=forms.ChoiceField(label='Jenis pencatatan',choices=m.CashMovement.KINDS[:3])
        number('amount','Nominal (rupiah)');day('business_date','Tanggal transaksi');text('note','Keterangan',250)
        from django.utils import timezone
        initial['business_date']=timezone.localdate()
    elif action=='cash_close' or action=='payroll_create':day('period','Bulan',True)
    elif action.startswith('employee_'):
        text('name','Nama anggota tim',100);text('department','Bagian',80,False);text('phone','Telepon',32,False)
        fields['email']=forms.EmailField(label='Email',required=False)
        choice('member','Akun untuk slip gaji sendiri (opsional)',m.Membership.objects.filter(tenant=member.tenant,user__is_active=True),False)
        boolean('active','Anggota aktif');initial['active']=True
    elif action=='salary':
        number('base','Gaji pokok (rupiah)',0);number('allowance','Tunjangan (rupiah)',0);number('deduction','Potongan (rupiah)',0)
        terms=m.SalaryTerms.objects.filter(employee=item,tenant=member.tenant).first()
        initial={name:getattr(terms,name,0) for name in ('base','allowance','deduction')}
    elif action in {'payroll_pay','commission_pay'}:choice('fund','Dana pembayaran',funds)
    elif action in {'cash_correct','payroll_cancel','payroll_reverse','commission_reverse','voucher_refund','voucher_revoke','endpoint_remove'}:
        text('reason','Alasan (10–250 karakter)',250)
        fields['reason'].min_length=10
    elif action in {'lead_create','lead_edit'}:
        text('name','Nama prospek',100);text('phone','Telepon',20);fields['email']=forms.EmailField(label='Email',required=False)
        fields['username']=forms.SlugField(label='Username layanan',max_length=64)
        text('address','Alamat',250,area=True);text('area','Wilayah',80)
        choice('plan','Paket aktif',plans)
        fields['status']=forms.ChoiceField(label='Tahap prospek',choices=m.Lead._meta.get_field('status').choices[:2]+[('lost','Tidak berlanjut')])
        text('note','Catatan',1000,False,True)
        if 'manage_sales' in capabilities(member):
            choice('assigned_to','Sales penanggung jawab',m.Membership.objects.filter(tenant=member.tenant,role='sales',user__is_active=True),False)
            number('commission_amount','Komisi setelah aktivasi (rupiah)',0);initial['commission_amount']=0
    elif action.startswith('campaign_'):
        text('title','Judul kampanye',100);text('description','Deskripsi (teks biasa)',2000,area=True)
        text('contact','Nomor telepon kontak publik',20);choice('plan','Paket katalog aktif',plans)
        day('starts','Tanggal mulai');day('ends','Tanggal selesai');boolean('published','Publikasikan halaman promo')
    elif action.startswith('hotspot_'):
        text('name','Nama profil',80);number('speed_mbps','Kecepatan informasi (Mbps)',1,100000)
        number('duration_minutes','Durasi sejak aktivasi (menit)',1,525600)
        number('activate_within_days','Batas aktivasi sejak penjualan (hari)',1,365)
        number('price','Harga per voucher (rupiah)',1,20_000_000)
        boolean('active','Profil aktif');initial['active']=True
    elif action=='voucher_sell':
        choice('profile','Profil hotspot aktif',m.HotspotProfile.objects.filter(tenant=member.tenant,active=True))
        number('quantity','Jumlah voucher',1,100);choice('fund','Dana penerimaan',funds)
    elif action.startswith('node_'):
        text('name','Nama node',80);fields['kind']=forms.ChoiceField(label='Jenis node',choices=m.NetworkNode._meta.get_field('kind').choices)
        choice('parent','ODC induk (untuk ODP)',m.NetworkNode.objects.filter(tenant=member.tenant,kind='odc',active=True).exclude(pk=item.pk if item else None),False)
        number('capacity','Kapasitas port',1,1024);text('address','Alamat',250,False,True)
        fields['latitude']=forms.DecimalField(label='Lintang (opsional)',max_digits=9,decimal_places=6,min_value=-90,max_value=90,required=False)
        fields['longitude']=forms.DecimalField(label='Bujur (opsional)',max_digits=9,decimal_places=6,min_value=-180,max_value=180,required=False)
        boolean('active','Node aktif');initial['active']=True
    elif action=='endpoint_set':
        choice('node','ODP aktif',m.NetworkNode.objects.filter(tenant=member.tenant,kind='odp',active=True,parent__active=True))
        number('port','Nomor port',1,1024)
        endpoint=m.CustomerEndpoint.objects.filter(tenant=member.tenant,customer=item).first()
        if endpoint:initial={'node':endpoint.node_id,'port':endpoint.port}
    if item and action.endswith('_edit'):
        initial.update({name:getattr(item,name+'_id',getattr(item,name,None)) for name in fields})
    if token:
        fields['token']=forms.CharField(max_length=32768,widget=forms.HiddenInput);initial['token']=token
    form=LocalForm(data=data,initial=initial)
    form.fields=fields
    return form

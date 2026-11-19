"""Server-rendered local operations; no external device or payment calls."""
import time
from datetime import date
from html import escape
from io import BytesIO
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from . import business as domain
from . import models as m
from .business_forms import ACTIONS, make_form, serialize
from .business_services import voucher_cipher, voucher_status
from .billing import audit
from .cash import cash_summary
from .locations import display
from .operations import member_for
from .permissions import capabilities
from .security_views import recent_factor
from .views import paginate

MODULES={'cash':('Kas & dana','cash'),'team':('Tim','team'),'payroll':('Payroll','payroll'),
    'sales':('Prospek, komisi & kampanye','sales'),'hotspot':('Hotspot simulasi','hotspot'),
    'topology':('Topologi ODC/ODP','topology')}


def url(action,pk=None):return reverse('business_action_object',args=[action,pk]) if pk else reverse('business_action',args=[action])
def back(module):return reverse('business_'+module)
def detail(module,kind,pk):return reverse('business_detail',args=[module,kind,pk])


def action_links(member, names, pk=None):
    rights=capabilities(member)
    return [{'label':ACTIONS[name].title,'url':url(name,pk)} for name in names if ACTIONS[name].cap in rights]


def section(title,columns,rows,actions=None):return {'title':title,'columns':columns,'rows':rows,'actions':actions or []}
def row(values,links=None):return {'values':values,'links':links or []}
def money(value):return 'Rp'+f'{value:,}'.replace(',','.')


def scoped_sales(member,model):
    query=model.objects.filter(tenant=member.tenant)
    if 'manage_sales' not in capabilities(member):
        query=query.filter(assigned_to=member) if model is m.Lead else query.filter(member=member)
    return query


@login_required
@require_GET
def module_page(request,module):
    if module!='cash':return render_module(request,module)
    from .billing_reports import report_snapshot
    with report_snapshot():return render_module(request,module)


def render_module(request,module):
    member=member_for(request.user)
    if module not in MODULES or MODULES[module][1] not in capabilities(member):raise PermissionDenied
    tenant=member.tenant;sections=[];rights=capabilities(member);description='Semua perubahan memerlukan tinjauan dan MFA terbaru.';dates=None
    if module=='cash':
        description='Arus kas lokal dalam rupiah. Modal, transfer dan penerimaan bukan laporan pendapatan akrual. Pembayaran lama tetap belum dipetakan.'
        from .report_views import ReportDates
        today=timezone.localdate();values=request.GET.copy()
        if 'start' not in values and 'end' not in values:values.update(start=today.replace(day=1).isoformat(),end=today.isoformat())
        dates=ReportDates(values,today=today)
        if not dates.is_valid():
            return render(request,'business/list.html',{'title':MODULES[module][0],'description':description,'dates':dates,'module':module},status=400)
        summary=cash_summary(tenant,dates.cleaned_data['end'],dates.cleaned_data['start'])
        sections.append(section('Arus kas per dana',['Dana','Saldo awal','Masuk','Keluar','Saldo akhir'],[row([s['name'],money(s['opening']),money(s['in']),money(s['out']),money(s['closing'])],action_links(member,['fund_edit'],s['id']) if s['id'] else []) for s in summary],action_links(member,['fund_create','cash_post','cash_transfer','cash_close'])))
        page=paginate(request,m.CashMovement.objects.filter(tenant=tenant,business_date__range=(dates.cleaned_data['start'],dates.cleaned_data['end'])).select_related('fund','correction'))
        sections.append(section('Pencatatan kas',['Tanggal','Dana','Jenis','Nominal','Keterangan','Koreksi'],[row([x.business_date,x.fund.name,x.get_kind_display(),money(x.amount),x.note,'Sudah dikoreksi' if hasattr(x,'correction') else '—'],action_links(member,['cash_correct'],x.pk) if not hasattr(x,'correction') else []) for x in page]))
        sections.append(section('Tutup buku tersimpan',['Bulan','Waktu'],[row([x.period.strftime('%m/%Y'),timezone.localtime(x.created_at)], [{'label':'Rincian snapshot','url':detail(module,'close',x.pk)}]) for x in m.CashClose.objects.filter(tenant=tenant)[:24]]))
    elif module=='team':
        description='Data anggota tim terpisah dari komponen gaji. Menonaktifkan anggota tim tidak menonaktifkan akun login.'
        page=paginate(request,m.Employee.objects.filter(tenant=tenant).select_related('member__user'))
        sections.append(section('Anggota tim',['Nama','Bagian','Telepon','Akun','Status'],[row([x.name,x.department,x.phone,x.member.user.username if x.member_id else '—','Aktif' if x.active else 'Nonaktif'],action_links(member,['employee_edit'],x.pk)) for x in page],action_links(member,['employee_create'])))
    elif module=='payroll':
        description='Gaji bersih = pokok + tunjangan − potongan. Angka disepakati secara manual; perhitungan PPh/BPJS dan transfer bank belum tersedia. Pratinjau menyimpan rincian historis.'
        page=paginate(request,m.PayrollRun.objects.filter(tenant=tenant))
        sections.append(section('Payroll bulanan',['Periode','Status','Total'],[row([x.period.strftime('%m/%Y'),x.get_status_display(),money(x.total)],[{'label':'Rincian dan tindakan','url':detail(module,'payroll',x.pk)}]) for x in page],action_links(member,['payroll_create'])))
        if 'manage_payroll' in rights:
            sections.append(section('Komponen gaji anggota aktif',['Nama','Pokok','Tunjangan','Potongan'],[row([x.name,money(x.salary.base) if hasattr(x,'salary') else 'Belum diatur',money(x.salary.allowance) if hasattr(x,'salary') else '—',money(x.salary.deduction) if hasattr(x,'salary') else '—'],action_links(member,['salary'],x.pk)) for x in m.Employee.objects.filter(tenant=tenant,active=True).select_related('salary')[:200]]))
    elif module=='sales':
        description='Sales melihat prospek yang ditugaskan kepadanya dan komisinya sendiri. Komisi baru timbul setelah PSB diaktivasi; tidak ada pencairan otomatis.'
        page=paginate(request,scoped_sales(member,m.Lead).select_related('plan','assigned_to__user','installation'))
        sections.append(section('Prospek',['Nama','Telepon','Paket','Tahap','Sales','PSB'],[row([x.name,x.phone,x.plan.name,x.get_status_display(),x.assigned_to.user.username if x.assigned_to_id else 'Belum ditugaskan',x.installation.get_status_display() if x.installation_id else 'Belum diajukan'],[{'label':'Rincian','url':detail(module,'lead',x.pk)}]) for x in page],action_links(member,['lead_create'])))
        sections.append(section('Komisi',['Prospek','Sales','Nominal','Status'],[row([x.lead.name,x.member.user.username,money(x.amount),x.get_status_display()],action_links(member,['commission_pay'] if x.status=='pending' else ['commission_reverse'] if x.status=='paid' else [],x.pk)) for x in scoped_sales(member,m.Commission).select_related('lead','member__user')[:100]]))
        if 'manage_sales' in rights:
            sections.append(section('Kampanye publik',['Judul','Mulai','Selesai','Publikasi'],[row([x.title,x.starts,x.ends,'Ya' if x.published else 'Tidak'],action_links(member,['campaign_edit'],x.pk)+[{'label':'Halaman publik','url':reverse('public_campaign',args=[x.public_key])}]) for x in m.Campaign.objects.filter(tenant=tenant)[:100]],action_links(member,['campaign_create'])))
    elif module=='hotspot':
        description='Voucher hanya untuk simulasi lokal. Aktivasi di sini tidak menyediakan akses internet, RADIUS, atau sesi perangkat nyata. Kata sandi hanya tersedia dalam cetak privat setelah MFA terbaru.'
        page=paginate(request,m.VoucherBatch.objects.filter(tenant=tenant).select_related('profile'))
        sections.append(section('Penjualan voucher',['Batch','Profil historis','Jumlah','Total','Status'],[row([f'#{x.pk}',x.snapshot['name'],x.quantity,money(x.total),'Dikoreksi' if x.refunded else 'Tercatat'],[{'label':'Rincian voucher','url':detail(module,'batch',x.pk)}]) for x in page],action_links(member,['voucher_sell'])))
        sections.append(section('Profil hotspot',['Nama','Kecepatan informasi','Durasi','Harga','Status'],[row([x.name,f'{x.speed_mbps} Mbps',f'{x.duration_minutes} menit',money(x.price),'Aktif' if x.active else 'Nonaktif'],action_links(member,['hotspot_edit'],x.pk)) for x in m.HotspotProfile.objects.filter(tenant=tenant)[:100]],action_links(member,['hotspot_create'])))
    else:
        description='Inventaris administratif ODC → ODP → port pelanggan. Pemetaan tidak mengubah OLT/router dan tidak membuktikan kondisi sambungan lapangan.'
        page=paginate(request,m.NetworkNode.objects.filter(tenant=tenant).select_related('parent'))
        sections.append(section('ODC dan ODP',['Nama','Jenis','ODC induk','Kapasitas','Status'],[row([x.name,x.get_kind_display(),x.parent.name if x.parent_id else '—',x.capacity,'Aktif' if x.active else 'Nonaktif'],[{'label':'Port dan lokasi','url':detail(module,'node',x.pk)}]+action_links(member,['node_edit'],x.pk)) for x in page],action_links(member,['node_create'])))
        if 'manage_topology' in rights:
            search=request.GET.get('q','').strip()[:80]
            customers=m.Customer.objects.filter(tenant=tenant).select_related('endpoint__node').order_by('name','pk')
            if search:customers=customers.filter(name__icontains=search)
            sections.append(section('Pemetaan pelanggan (maksimal 50 hasil; cari nama di atas)',['Pelanggan','ODP / port'],[row([x.name,f'{x.endpoint.node.name} / {x.endpoint.port}' if hasattr(x,'endpoint') else 'Belum dipetakan'],action_links(member,['endpoint_set']+(['endpoint_remove'] if hasattr(x,'endpoint') else []),x.pk)) for x in customers[:50]]))
    return render(request,'business/list.html',{'title':MODULES[module][0],'description':description,'sections':sections,'page':page,'module':module,'dates':dates})


@login_required
@sensitive_post_parameters('token')
@require_http_methods(['GET','POST'])
def action_form(request,action,pk=None):
    member=member_for(request.user);spec=domain.authorized(member,action)
    try:token,item=domain.form_token(request.user,action,pk)
    except ObjectDoesNotExist:raise Http404 from None
    if request.method=='POST':token=request.POST.get('token','')[:domain.MAX_TOKEN+1]
    form=make_form(member,action,item,data=request.POST if request.method=='POST' else None,token=token)
    if request.method=='POST' and form.is_valid():
        try:review_token,proposal=domain.review(request.user,action,pk,form.cleaned_data['token'],serialize(form.cleaned_data))
        except ValidationError as e:form.add_error(None,e)
        else:return review_response(request,member,spec,proposal,review_token)
    return render(request,'core/form.html',{'form':form,'title':spec.title,'back':back(spec.module),
        'description':'Periksa rincian sebelum menyimpan. Langkah berikutnya menampilkan tinjauan; diperlukan MFA dalam 5 menit terakhir.',
        'submit_label':'Tinjau perubahan'},status=400 if request.method=='POST' else 200)


class ConfirmForm(forms.Form):
    token=forms.CharField(max_length=32768,widget=forms.HiddenInput)
    reviewed=forms.BooleanField(label='Saya telah memeriksa rincian dan menyetujui pencatatan lokal ini.')


def review_response(request,member,spec,proposal,token,form=None,status=200):
    item=domain.object_for(member,proposal['action'],proposal['object'])
    fields=make_form(member,proposal['action'],item,data=proposal['after']);fields.is_valid()
    rows=[]
    for name,value in fields.cleaned_data.items():
        if isinstance(value,bool):value='Ya' if value else 'Tidak'
        elif isinstance(value,m.Membership):value=value.user.username
        rows.append((fields.fields[name].label,value if value is not None else '—'))
    from .business_services import payroll_data, review_summary
    rows=review_summary(member,proposal['action'],item,proposal['after'])+rows
    payroll=payroll_data(member.tenant) if proposal['action'] in {'payroll_create','payroll_approve','payroll_pay'} else None
    close=None
    if proposal['action']=='cash_close':
        from .cash import close_snapshot
        close=close_snapshot(member.tenant,date.fromisoformat(proposal['after']['period']+'-01'))
    return render(request,'business/review.html',{'title':spec.title,'rows':rows,'payroll':payroll,
        'form':form if form is not None else ConfirmForm(initial={'token':token}),'back':back(spec.module),
        'confirm_url':reverse('business_confirm'),'close':close},status=status)


@login_required
@sensitive_post_parameters('token')
@require_POST
def action_confirm(request):
    token=request.POST.get('token','')[:domain.MAX_TOKEN+1];member=member_for(request.user)
    try:proposal=domain.read(request.user,token,'review')
    except ValidationError:
        messages.error(request,'Tinjauan kedaluwarsa/tidak valid. Buka kembali modul.');return redirect('account_security')
    spec=ACTIONS[proposal['action']];form=ConfirmForm(request.POST)
    if form.is_valid():
        if not recent_factor(request,return_to=url(proposal['action'],proposal['object'])):return redirect('account_security')
        try:result,created=domain.confirm(request.user,token,request.session['mfa_verified_at'],request.session['access_version'])
        except ValidationError as e:form.add_error(None,e)
        except (IntegrityError,OperationalError):form.add_error(None,'Data belum disimpan. Periksa keunikan data dan tinjau ulang sebelum mencoba lagi.')
        else:
            messages.success(request,'Pencatatan lokal tersimpan.' if created else 'Permintaan ini sudah tersimpan; tidak dicatat lagi.')
            if proposal['action']=='payroll_create':return redirect(detail('payroll','payroll',result))
            if proposal['action']=='voucher_sell':return redirect(detail('hotspot','batch',result))
            return redirect(back(spec.module))
    try:return review_response(request,member,spec,proposal,token,form,400)
    except (ObjectDoesNotExist,ValidationError):
        messages.error(request,'Data atau pilihan berubah. Muat ulang modul dan buat tinjauan baru.');return redirect(back(spec.module))


@login_required
@require_GET
def detail_page(request,module,kind,pk):
    member=member_for(request.user);rights=capabilities(member)
    if module not in MODULES or MODULES[module][1] not in rights:raise PermissionDenied
    sections=[];links=[];location=None;print_url=None
    if (module,kind)==('payroll','payroll'):
        item=get_object_or_404(m.PayrollRun,tenant=member.tenant,pk=pk)
        title=f'Payroll {item.period:%m/%Y} · {item.get_status_display()}'
        sections.append(section('Rincian historis',['Nama','Bagian','Bruto','Potongan','Bersih'],[row([x.name,x.department,money(x.gross),money(x.deduction),money(x.net)]) for x in item.lines.order_by('employee_id')]))
        names=['payroll_approve','payroll_cancel'] if item.status in {'preview','approved','stale'} else ['payroll_reverse'] if item.status=='paid' else []
        if item.status=='approved':names+=['payroll_pay']
        links=action_links(member,names,item.pk)
    elif (module,kind)==('hotspot','batch'):
        item=get_object_or_404(m.VoucherBatch,tenant=member.tenant,pk=pk)
        title=f'Batch #{item.pk} · {item.snapshot["name"]}'
        sections.append(section('Voucher simulasi — tanpa akses internet nyata',['Username','Status','Batas aktivasi','Aktif sampai'],[row([x.username,voucher_status(x),timezone.localtime(x.activation_deadline),timezone.localtime(x.expires_at) if x.expires_at else '—'],action_links(member,['voucher_activate'] if voucher_status(x)=='Belum diaktifkan' else [],x.pk)+action_links(member,['voucher_revoke'] if not x.revoked else [],x.pk)) for x in item.vouchers.all()]))
        if not item.refunded:
            links=action_links(member,['voucher_refund'],item.pk)
            if 'manage_hotspot' in rights:print_url=reverse('voucher_print',args=[item.pk])
    elif (module,kind)==('cash','close'):
        item=get_object_or_404(m.CashClose,tenant=member.tenant,pk=pk);title=f'Tutup buku {item.period:%m/%Y}'
        sections.append(section('Snapshot arus kas tersimpan',['Dana','Saldo awal','Masuk','Keluar','Saldo akhir'],[row([x['name'],money(x['opening']),money(x['in']),money(x['out']),money(x['closing'])]) for x in item.snapshot]))
    elif (module,kind)==('sales','lead'):
        item=get_object_or_404(scoped_sales(member,m.Lead).select_related('installation','plan','assigned_to__user'),pk=pk)
        title=item.name
        sections.append(section('Rincian prospek',['Telepon','Alamat','Wilayah','Paket','Tahap','Catatan','PSB'],[row([item.phone,item.address,item.area,item.plan.name,item.get_status_display(),item.note,item.installation.get_status_display() if item.installation_id else 'Belum diajukan'])]))
        names=['lead_edit'] if item.status!='converted' else []
        if item.status=='qualified':names+=['lead_convert']
        links=action_links(member,names,item.pk)
    elif (module,kind)==('topology','node'):
        item=get_object_or_404(m.NetworkNode.objects.select_related('parent'),tenant=member.tenant,pk=pk)
        title=str(item);location=display(item)
        sections.append(section('ODP anak',['Nama','Kapasitas','Status'],[row([x.name,x.capacity,'Aktif' if x.active else 'Nonaktif'],[{'label':'Port dan lokasi','url':detail(module,'node',x.pk)}]) for x in item.children.filter(tenant=member.tenant)]))
        sections.append(section('Port terpakai',['Port','Kode pelanggan','Nama pelanggan'],[row([x.port,x.customer.code,x.customer.name]) for x in item.ports.filter(tenant=member.tenant).select_related('customer').order_by('port')]))
        links=action_links(member,['node_edit'],item.pk)
    else:raise Http404
    return render(request,'business/detail.html',{'title':title,'sections':sections,'actions':links,'back':back(module),'location':location,'print_url':print_url})


@login_required
@require_GET
def own_payslips(request):
    member=member_for(request.user)
    page=paginate(request,m.PayrollLine.objects.filter(member=member,run__tenant=member.tenant,run__status__in=['paid','reversed']).select_related('run').order_by('-run__period','-pk'))
    sections=[section('Slip gaji historis milik akun ini',['Periode','Nama','Bagian','Bruto','Potongan','Bersih','Status'],[row([x.run.period.strftime('%m/%Y'),x.name,x.department,money(x.gross),money(x.deduction),money(x.net),x.run.get_status_display()]) for x in page])]
    return render(request,'business/list.html',{'title':'Slip gaji saya','description':'Rincian tersimpan saat payroll disiapkan. Tidak ada transfer bank otomatis.','sections':sections,'page':page})


@require_GET
def public_campaign(request,key):
    today=timezone.localdate()
    campaign=get_object_or_404(m.Campaign.objects.select_related('plan'),public_key=key,published=True,
        starts__lte=today,ends__gte=today,plan__is_active=True)
    response=render(request,'business/campaign.html',{'campaign':campaign})
    response['Cache-Control']='no-store'
    return response


@login_required
@sensitive_variables()
@require_POST
def voucher_print(request,pk):
    member=member_for(request.user)
    if 'manage_hotspot' not in capabilities(member):raise PermissionDenied
    batch=get_object_or_404(m.VoucherBatch,tenant=member.tenant,pk=pk,refunded=False)
    if not recent_factor(request,return_to=detail('hotspot','batch',pk)):return redirect('account_security')
    with transaction.atomic():
        m.Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
        current=member_for(request.user)
        if ('manage_hotspot' not in capabilities(current) or current.session_version!=request.session.get('access_version')
                or domain.device_revision(request.user)!=request.session.get('mfa_revision')):raise PermissionDenied
        batch=m.VoucherBatch.objects.get(tenant=current.tenant,pk=pk)
        if batch.refunded:raise Http404
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        buffer=BytesIO();styles=getSampleStyleSheet();flow=[];cipher=voucher_cipher()
        for voucher in batch.vouchers.all():
            password=cipher.decrypt(voucher.encrypted_password.encode()).decode()
            lines=['TagihNet — VOUCHER SIMULASI',batch.snapshot['name'],f'Username: {voucher.username}',f'Password: {password}',
                f'Durasi: {batch.snapshot["duration_minutes"]} menit sejak aktivasi',
                f'Batas aktivasi: {timezone.localtime(voucher.activation_deadline):%d/%m/%Y %H:%M WIB}',
                f'Status saat cetak: {voucher_status(voucher)}','Tidak menyediakan akses internet nyata.']
            flow.extend([Paragraph(escape(line),styles['Normal']) for line in lines]);flow.append(Spacer(1,24))
        SimpleDocTemplate(buffer,pagesize=A4,title='Voucher simulasi TagihNet').build(flow)
        audit(current.tenant,request.user,'hotspot.vouchers_printed',batch.pk,'PDF privat voucher simulasi dibuat; kata sandi tidak dicatat dalam audit.')
    response=HttpResponse(buffer.getvalue(),content_type='application/pdf')
    response['Content-Disposition']=f'attachment; filename="TagihNet-voucher-{batch.pk}.pdf"'
    response['Cache-Control']='private, no-store';return response

"""Opt-in local operations. No seed data or external transfers."""
import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

class TenantRecord(models.Model):
    tenant=models.ForeignKey('core.Tenant',on_delete=models.PROTECT)
    class Meta:abstract=True

class Fund(TenantRecord):
    name=models.CharField(max_length=80)
    kind=models.CharField(max_length=12,choices=[('cash','Kas'),('bank','Bank'),('wallet','Dompet')])
    active=models.BooleanField(default=True)
    version=models.PositiveIntegerField(default=1)
    request_key=models.UUIDField(default=uuid.uuid4)
    class Meta:
        ordering=['name','pk']
        constraints=[models.UniqueConstraint(fields=['tenant','name'],name='fund_name_tenant'),models.UniqueConstraint(fields=['tenant','request_key'],name='fund_request_tenant')]
    def __str__(self):return self.name

class PaymentFunding(TenantRecord):
    payment=models.OneToOneField('core.Payment',on_delete=models.PROTECT,related_name='funding')
    fund=models.ForeignKey(Fund,on_delete=models.PROTECT)

class CashMovement(TenantRecord):
    KINDS=[('funding','Modal/saldo awal'),('income','Penerimaan lain'),('expense','Pengeluaran'),('transfer_in','Transfer masuk'),('transfer_out','Transfer keluar'),('payroll','Payroll'),('commission','Komisi'),('voucher','Penjualan voucher')]
    fund=models.ForeignKey(Fund,on_delete=models.PROTECT)
    kind=models.CharField(max_length=16,choices=KINDS)
    amount=models.PositiveBigIntegerField()
    business_date=models.DateField()
    note=models.CharField(max_length=250)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    request_key=models.UUIDField()
    leg=models.PositiveSmallIntegerField(default=1)
    fingerprint=models.CharField(max_length=64)
    source_key=models.CharField(max_length=100,blank=True)
    created_at=models.DateTimeField(default=timezone.now)
    class Meta:
        ordering=['-business_date','-pk']
        constraints=[models.CheckConstraint(condition=Q(amount__gt=0,amount__lte=2000000000),name='cash_positive_bounded'),models.UniqueConstraint(fields=['tenant','request_key','leg'],name='cash_request_leg'),models.UniqueConstraint(fields=['tenant','source_key'],condition=~Q(source_key=''),name='cash_source_once')]

class CashCorrection(TenantRecord):
    movement=models.OneToOneField(CashMovement,on_delete=models.PROTECT,related_name='correction')
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    reason=models.CharField(max_length=250)
    created_at=models.DateTimeField(default=timezone.now)

class CashClose(TenantRecord):
    period=models.DateField()
    snapshot=models.JSONField()
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    request_key=models.UUIDField()
    created_at=models.DateTimeField(default=timezone.now)
    class Meta:
        ordering=['-period']
        constraints=[models.UniqueConstraint(fields=['tenant','period'],name='cash_close_period'),models.UniqueConstraint(fields=['tenant','request_key'],name='cash_close_request')]

class Employee(TenantRecord):
    name=models.CharField(max_length=100)
    department=models.CharField(max_length=80,blank=True)
    phone=models.CharField(max_length=32,blank=True)
    email=models.EmailField(blank=True)
    member=models.OneToOneField('core.Membership',on_delete=models.PROTECT,null=True,blank=True)
    active=models.BooleanField(default=True)
    version=models.PositiveIntegerField(default=1)
    request_key=models.UUIDField(default=uuid.uuid4)
    class Meta:
        ordering=['name','pk']
        constraints=[models.UniqueConstraint(fields=['tenant','request_key'],name='employee_request_tenant')]
    def __str__(self):return self.name

class SalaryTerms(TenantRecord):
    employee=models.OneToOneField(Employee,on_delete=models.PROTECT,related_name='salary')
    base=models.PositiveBigIntegerField(default=0)
    allowance=models.PositiveBigIntegerField(default=0)
    deduction=models.PositiveBigIntegerField(default=0)
    version=models.PositiveIntegerField(default=1)
    class Meta:
        constraints=[models.CheckConstraint(condition=Q(base__lte=2000000000,allowance__lte=2000000000,deduction__lte=2000000000)
            & Q(deduction__lt=models.F('base')+models.F('allowance'))
            & Q(deduction__gte=models.F('base')+models.F('allowance')-2000000000),name='salary_net_bounded')]

class PayrollRun(TenantRecord):
    period=models.DateField()
    status=models.CharField(max_length=12,default='preview',choices=[('preview','Pratinjau'),('approved','Disetujui'),('paid','Dicatat dibayar'),('cancelled','Dibatalkan'),('stale','Data berubah'),('reversed','Direversal')])
    request_key=models.UUIDField()
    fingerprint=models.CharField(max_length=64)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    authorized_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,related_name='+')
    authorization_version=models.PositiveIntegerField(null=True)
    mfa_revision=models.UUIDField(null=True)
    movement=models.OneToOneField(CashMovement,on_delete=models.PROTECT,null=True)
    total=models.PositiveBigIntegerField(default=0)
    version=models.PositiveIntegerField(default=1)
    created_at=models.DateTimeField(default=timezone.now)
    class Meta:
        ordering=['-period','-pk']
        constraints=[models.UniqueConstraint(fields=['tenant','request_key'],name='payroll_request'),models.UniqueConstraint(fields=['tenant','period'],condition=~Q(status__in=['cancelled','reversed']),name='payroll_open_period')]

class PayrollLine(models.Model):
    run=models.ForeignKey(PayrollRun,on_delete=models.PROTECT,related_name='lines')
    employee=models.ForeignKey(Employee,on_delete=models.PROTECT)
    member=models.ForeignKey('core.Membership',on_delete=models.PROTECT,null=True)
    name=models.CharField(max_length=100)
    department=models.CharField(max_length=80,blank=True)
    gross=models.PositiveBigIntegerField()
    deduction=models.PositiveBigIntegerField()
    net=models.PositiveBigIntegerField()
    employee_version=models.PositiveIntegerField()
    terms_version=models.PositiveIntegerField()
    class Meta:constraints=[models.UniqueConstraint(fields=['run','employee'],name='payroll_employee_once'),
        models.CheckConstraint(condition=Q(net__gt=0,net__lte=2000000000)&Q(net=models.F('gross')-models.F('deduction')),name='payroll_line_net_valid')]

class Campaign(TenantRecord):
    title=models.CharField(max_length=100)
    description=models.TextField(max_length=2000)
    contact=models.CharField(max_length=100)
    plan=models.ForeignKey('core.Plan',on_delete=models.PROTECT)
    starts=models.DateField()
    ends=models.DateField()
    public_key=models.UUIDField(default=uuid.uuid4,unique=True)
    published=models.BooleanField(default=False)
    version=models.PositiveIntegerField(default=1)
    request_key=models.UUIDField(default=uuid.uuid4)
    class Meta:
        ordering=['-pk']
        constraints=[models.UniqueConstraint(fields=['tenant','request_key'],name='campaign_request'),
            models.CheckConstraint(condition=Q(ends__gte=models.F('starts')),name='campaign_date_order')]

class Lead(TenantRecord):
    name=models.CharField(max_length=100)
    phone=models.CharField(max_length=20)
    email=models.EmailField(blank=True)
    username=models.SlugField(max_length=64)
    address=models.CharField(max_length=250)
    area=models.CharField(max_length=80)
    plan=models.ForeignKey('core.Plan',on_delete=models.PROTECT)
    assigned_to=models.ForeignKey('core.Membership',on_delete=models.PROTECT,null=True,related_name='+')
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True)
    campaign=models.ForeignKey(Campaign,on_delete=models.PROTECT,null=True)
    status=models.CharField(max_length=12,default='new',choices=[('new','Baru'),('qualified','Siap diajukan'),('converted','Menjadi PSB'),('lost','Tidak berlanjut')])
    note=models.CharField(max_length=1000,blank=True)
    commission_amount=models.PositiveBigIntegerField(default=0)
    installation=models.OneToOneField('core.Installation',on_delete=models.PROTECT,null=True,related_name='sales_lead')
    request_key=models.UUIDField(default=uuid.uuid4)
    version=models.PositiveIntegerField(default=1)
    created_at=models.DateTimeField(default=timezone.now)
    class Meta:
        ordering=['-created_at','-pk']
        constraints=[models.UniqueConstraint(fields=['tenant','request_key'],name='lead_request'),
            models.CheckConstraint(condition=Q(commission_amount__lte=2000000000),name='lead_commission_bound')]

class Commission(TenantRecord):
    lead=models.OneToOneField(Lead,on_delete=models.PROTECT)
    member=models.ForeignKey('core.Membership',on_delete=models.PROTECT)
    amount=models.PositiveBigIntegerField()
    status=models.CharField(max_length=12,default='pending',choices=[('pending','Belum dibayar'),('paid','Dicatat dibayar'),('reversed','Direversal')])
    movement=models.OneToOneField(CashMovement,on_delete=models.PROTECT,null=True)
    version=models.PositiveIntegerField(default=1)
    created_at=models.DateTimeField(default=timezone.now)
    class Meta:constraints=[models.CheckConstraint(condition=Q(amount__gt=0,amount__lte=2000000000),name='commission_amount_bound')]

class HotspotProfile(TenantRecord):
    name=models.CharField(max_length=80)
    speed_mbps=models.PositiveIntegerField()
    duration_minutes=models.PositiveIntegerField()
    activate_within_days=models.PositiveIntegerField()
    price=models.PositiveBigIntegerField()
    active=models.BooleanField(default=True)
    version=models.PositiveIntegerField(default=1)
    request_key=models.UUIDField(default=uuid.uuid4)
    class Meta:
        ordering=['name']
        constraints=[models.UniqueConstraint(fields=['tenant','name'],name='hotspot_profile_name'),models.UniqueConstraint(fields=['tenant','request_key'],name='hotspot_profile_request'),
            models.CheckConstraint(condition=Q(speed_mbps__gte=1,speed_mbps__lte=100000,duration_minutes__gte=1,duration_minutes__lte=525600,
                activate_within_days__gte=1,activate_within_days__lte=365,price__gte=1,price__lte=20000000),name='hotspot_parameters_bound')]
    def __str__(self):return self.name

class VoucherBatch(TenantRecord):
    profile=models.ForeignKey(HotspotProfile,on_delete=models.PROTECT)
    snapshot=models.JSONField()
    quantity=models.PositiveSmallIntegerField()
    total=models.PositiveBigIntegerField()
    movement=models.OneToOneField(CashMovement,on_delete=models.PROTECT)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    request_key=models.UUIDField()
    fingerprint=models.CharField(max_length=64)
    refunded=models.BooleanField(default=False)
    created_at=models.DateTimeField(default=timezone.now)
    class Meta:
        ordering=['-pk']
        constraints=[models.UniqueConstraint(fields=['tenant','request_key'],name='voucher_batch_request'),models.CheckConstraint(condition=Q(quantity__gte=1,quantity__lte=100),name='voucher_batch_bound')]

class Voucher(TenantRecord):
    batch=models.ForeignKey(VoucherBatch,on_delete=models.PROTECT,related_name='vouchers')
    username=models.CharField(max_length=32,unique=True)
    encrypted_password=models.TextField()
    activation_deadline=models.DateTimeField()
    activated_at=models.DateTimeField(null=True)
    expires_at=models.DateTimeField(null=True)
    revoked=models.BooleanField(default=False)
    version=models.PositiveIntegerField(default=1)
    class Meta:
        ordering=['pk']
        constraints=[models.CheckConstraint(condition=Q(activated_at__isnull=True,expires_at__isnull=True)
            |(Q(activated_at__isnull=False,expires_at__isnull=False)&Q(expires_at__gt=models.F('activated_at'))),name='voucher_activation_pair')]

class NetworkNode(TenantRecord):
    name=models.CharField(max_length=80)
    kind=models.CharField(max_length=3,choices=[('odc','ODC'),('odp','ODP')])
    parent=models.ForeignKey('self',on_delete=models.PROTECT,null=True,related_name='children')
    capacity=models.PositiveIntegerField()
    address=models.CharField(max_length=250,blank=True)
    latitude=models.DecimalField(max_digits=9,decimal_places=6,null=True,blank=True)
    longitude=models.DecimalField(max_digits=9,decimal_places=6,null=True,blank=True)
    active=models.BooleanField(default=True)
    version=models.PositiveIntegerField(default=1)
    request_key=models.UUIDField(default=uuid.uuid4)
    class Meta:
        ordering=['kind','name']
        constraints=[models.UniqueConstraint(fields=['tenant','name'],name='node_name_tenant'),models.UniqueConstraint(fields=['tenant','request_key'],name='node_request_tenant'),models.CheckConstraint(condition=Q(capacity__gte=1,capacity__lte=1024),name='node_capacity_bound'),models.CheckConstraint(condition=Q(kind='odc',parent__isnull=True)|Q(kind='odp',parent__isnull=False),name='node_parent_kind'),models.CheckConstraint(condition=Q(latitude__isnull=True,longitude__isnull=True)|Q(latitude__isnull=False,longitude__isnull=False),name='node_coordinate_pair'),models.CheckConstraint(condition=Q(latitude__isnull=True)|Q(latitude__gte=-90,latitude__lte=90),name='node_latitude_range'),models.CheckConstraint(condition=Q(longitude__isnull=True)|Q(longitude__gte=-180,longitude__lte=180),name='node_longitude_range')]
    def __str__(self):return f'{self.get_kind_display()} · {self.name}'

class CustomerEndpoint(TenantRecord):
    customer=models.OneToOneField('core.Customer',on_delete=models.PROTECT,related_name='endpoint')
    node=models.ForeignKey(NetworkNode,on_delete=models.PROTECT,related_name='ports')
    port=models.PositiveIntegerField()
    version=models.PositiveIntegerField(default=1)
    class Meta:constraints=[models.UniqueConstraint(fields=['node','port'],name='node_port_once'),models.CheckConstraint(condition=Q(port__gte=1,port__lte=1024),name='node_port_bound')]

class BusinessAction(TenantRecord):
    """One committed result per signed request; replay never repeats an effect."""
    request_key=models.UUIDField()
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    action=models.CharField(max_length=40)
    fingerprint=models.CharField(max_length=64)
    result_id=models.PositiveBigIntegerField()
    created_at=models.DateTimeField(default=timezone.now)
    class Meta:constraints=[models.UniqueConstraint(fields=['tenant','request_key'],name='business_action_once')]

"""Signed, scoped, reviewed local actions. A tenant lock serializes financial decisions."""
import time
import uuid
from django.core import signing
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.forms.models import model_to_dict
from django.views.decorators.debug import sensitive_variables
from .billing import audit, retry_locked
from .models import BusinessAction, Membership, Tenant
from .locations import device_revision
from .operations import digest, member_for
from .permissions import capabilities

SALT='tagihnet.business.v1'
MAX_TOKEN=32768


def definition(action):
    from .business_forms import ACTIONS
    try:return ACTIONS[action]
    except KeyError:raise PermissionDenied('Tindakan tidak tersedia.') from None


def authorized(member, action):
    spec=definition(action)
    if not settings.SIMULATION:raise PermissionDenied('Modul operasional ini hanya tersedia dalam simulasi lokal.')
    if spec.cap not in capabilities(member):raise PermissionDenied('Izin modul tidak tersedia.')
    return spec


def object_for(member, action, pk):
    spec=authorized(member,action)
    if spec.model is None:
        if pk is not None:raise PermissionDenied
        return None
    if type(pk) is not int or pk<1:raise PermissionDenied
    query=spec.model.objects.filter(tenant=member.tenant)
    if spec.module=='sales' and 'manage_sales' not in capabilities(member):
        query=query.filter(assigned_to=member) if spec.model._meta.model_name=='lead' else query.filter(member=member)
    return query.get(pk=pk)


def row_hash(item):
    return digest({field.attname:getattr(item,field.attname) for field in item._meta.concrete_fields}) if item else None


def identity(actor, action, pk, phase, key):
    member=member_for(actor);authorized(member,action)
    return {'v':1,'actor':actor.pk,'tenant':member.tenant_id,'access':member.session_version,
        'mfa':device_revision(actor),'action':action,'object':pk,'phase':phase,'key':str(key)}


def form_token(actor, action, pk=None):
    member=member_for(actor);item=object_for(member,action,pk)
    value={**identity(actor,action,pk,'form',uuid.uuid4()),'before':row_hash(item)}
    return signing.dumps(value,salt=SALT),item


@sensitive_variables()
def read(actor, token, phase):
    try:
        if not isinstance(token,str) or len(token)>MAX_TOKEN:raise ValueError
        value=signing.loads(token,salt=SALT,max_age=900)
        fields={'v','actor','tenant','access','mfa','action','object','phase','key','before'}
        if phase=='review':fields|={'after','dependencies'}
        if not isinstance(value,dict) or set(value)!=fields or value['phase']!=phase:raise ValueError
        if type(value['v']) is not int or value['v']!=1:raise ValueError
        if any(type(value[k]) is not int or value[k]<1 for k in ('actor','tenant','access')):raise ValueError
        if value['object'] is not None and (type(value['object']) is not int or value['object']<1):raise ValueError
        if value['before'] is not None and (not isinstance(value['before'],str) or len(value['before'])!=64):raise ValueError
        if str(uuid.UUID(value['key']))!=value['key']:raise ValueError
        expected=identity(actor,value['action'],value['object'],phase,value['key'])
        if any(value[k]!=expected[k] for k in expected):raise PermissionDenied('Tinjauan bukan milik akun dan versi akses/MFA ini.')
        if phase=='review' and (not isinstance(value['after'],dict) or not isinstance(value['dependencies'],str)):raise ValueError
    except (signing.BadSignature,ValueError,TypeError,KeyError,AttributeError):
        raise ValidationError('Form atau tinjauan kedaluwarsa/tidak valid. Muat ulang modul.') from None
    return value


def cleaned(member, action, item, data):
    from .business_forms import make_form, serialize
    form=make_form(member,action,item,data=data)
    if not form.is_valid():raise ValidationError([message for messages in form.errors.values() for message in messages])
    return serialize(form.cleaned_data)


def dependencies(member, action, item, values):
    from .business_forms import make_form
    from .business_services import extra_dependencies
    form=make_form(member,action,item,data=values)
    if not form.is_valid():raise ValidationError('Pilihan berubah atau tidak tersedia. Muat ulang formulir.')
    refs={name:row_hash(v) for name,v in form.cleaned_data.items() if hasattr(v,'_meta')}
    return digest([refs,extra_dependencies(member,action,item,values)])


@sensitive_variables()
def review(actor, action, pk, token, values):
    member=member_for(actor);item=object_for(member,action,pk)
    proposal=read(actor,token,'form')
    if proposal['action']!=action or proposal['object']!=pk:raise PermissionDenied
    if proposal['before']!=row_hash(item):raise ValidationError('Data berubah sejak formulir dibuka. Muat ulang.')
    after=cleaned(member,action,item,values)
    proposal.update(phase='review',after=after,dependencies=dependencies(member,action,item,after))
    return signing.dumps(proposal,salt=SALT),proposal


@retry_locked
@transaction.atomic
@sensitive_variables()
def confirm(actor, token, verified_at, access_version):
    initial=member_for(actor)
    Tenant.objects.select_for_update(no_key=True).get(pk=initial.tenant_id)
    Membership.objects.select_for_update(no_key=True).get(pk=initial.pk)
    member=member_for(actor)
    if (type(access_version) is not int or access_version!=member.session_version
            or type(verified_at) not in (int,float) or not 0<=time.time()-verified_at<=300):
        raise PermissionDenied('MFA terbaru dan versi akses saat ini diperlukan.')
    value=read(actor,token,'review');spec=authorized(member,value['action'])
    signature=digest(value)
    previous=BusinessAction.objects.filter(tenant=member.tenant,request_key=value['key']).first()
    if previous:
        if previous.actor_id!=actor.pk or previous.action!=value['action'] or previous.fingerprint!=signature:
            raise ValidationError('Kunci permintaan sudah digunakan untuk tindakan berbeda.')
        return previous.result_id,False
    item=object_for(member,value['action'],value['object'])
    if row_hash(item)!=value['before'] or dependencies(member,value['action'],item,value['after'])!=value['dependencies']:
        raise ValidationError('Data atau pilihan berubah sejak tinjauan. Muat ulang dan tinjau lagi.')
    values=cleaned(member,value['action'],item,value['after'])
    from .business_services import apply
    result=apply(member,value['action'],item,values,uuid.UUID(value['key']))
    BusinessAction.objects.create(tenant=member.tenant,request_key=value['key'],actor=actor,
        action=value['action'],fingerprint=signature,result_id=result.pk)
    detail=values.get('reason') or 'Perubahan lokal ditinjau dan dicatat; tanpa transfer atau perintah perangkat nyata.'
    audit(member.tenant,actor,'business.'+value['action'],result.pk,detail)
    return result.pk,True

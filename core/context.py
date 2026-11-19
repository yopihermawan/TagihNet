from django.conf import settings
from django.core.exceptions import PermissionDenied
from .permissions import membership, capabilities
from .navigation import navigation

def shell(request):
    context = {"simulation": settings.SIMULATION, "staging":getattr(settings,"STAGING",False)}
    if request.user.is_authenticated:
        try:
            member = membership(request.user)
            rights=capabilities(member)
            match=getattr(request,'resolver_match',None)
            action=(match.kwargs.get('action','') if match else '')
            context.update(member=member, tenant=member.tenant, capabilities=rights,
                navigation_groups=navigation(member,rights,request.path,action))
        except PermissionDenied:
            pass
    return context

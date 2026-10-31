from django.conf import settings
from django.core.exceptions import PermissionDenied
from .permissions import membership, capabilities

def shell(request):
    context = {"simulation": settings.SIMULATION}
    if request.user.is_authenticated:
        try:
            member = membership(request.user)
            context.update(member=member, tenant=member.tenant, capabilities=capabilities(member))
        except PermissionDenied:
            pass
    return context

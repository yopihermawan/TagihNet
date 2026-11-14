import hashlib
import uuid
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods
from .models import Installation, WorkPhoto
from .operations import member_for
from .work_photos import target_query, upload_photo, visible_photos


class PhotoForm(forms.Form):
    caption = forms.CharField(label="Keterangan foto", min_length=3, max_length=200)
    photo = forms.FileField(label="Foto JPG/PNG", widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png"}),
        help_text="Maksimal 2 MiB, 4096 piksel per sisi dan 12 megapiksel. Metadata dihapus dengan pemrosesan ulang.")
    internal = forms.BooleanField(required=False, label="Internal · tidak ditampilkan kepada pelanggan tiket")
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    request_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, member, installation=False, **kwargs):
        super().__init__(*args, **kwargs)
        if member.role == "customer" or installation:
            self.fields.pop("internal")


def scoped_target(member, pk, installation=False):
    try:
        return get_object_or_404(target_query(member, installation), pk=pk)
    except PermissionDenied:
        raise Http404("Pekerjaan tidak tersedia pada cakupan akun ini.") from None


@login_required
@sensitive_post_parameters("caption")
@require_http_methods(["GET", "POST"])
def photo_list(request, pk, installation=False):
    member = member_for(request.user)
    target = scoped_target(member, pk, installation)
    initial = {"internal": True, "version": target.version, "request_key": uuid.uuid4()}
    form = PhotoForm(request.POST if request.method == "POST" else None, request.FILES if request.method == "POST" else None,
        member=member, installation=installation, initial=initial)
    if request.method == "POST":
        if member.role == "customer" and request.POST.get("internal"):
            raise PermissionDenied
        if form.is_valid():
            data = form.cleaned_data
            try:
                upload_photo(request.user, pk, data["photo"], data["caption"],
                    True if installation else False if member.role == "customer" else data["internal"],
                    data["version"], data["request_key"], request.session["access_version"], installation)
            except (ValidationError, IntegrityError, OperationalError) as exc:
                form.add_error(None, exc if isinstance(exc, ValidationError) else "Foto belum tersimpan. Muat ulang dan coba lagi.")
            except target.__class__.DoesNotExist:
                raise Http404("Akses pekerjaan telah berubah.") from None
            else:
                messages.success(request, "Foto tersimpan sebagai bukti pekerjaan. Akses unduhan mengikuti izin terkini.")
                return redirect("installation_photos" if installation else "ticket_photos", pk=pk)
    return render(request, "operations/photos.html", {"target": target, "photos": visible_photos(member, target),
        "form": form, "installation": installation,
        "back": reverse("installation_detail" if installation else "ticket_detail", args=[pk])},
        status=400 if request.method == "POST" else 200)


@login_required
@require_GET
def photo_download(request, photo_id):
    member = member_for(request.user)
    photo = get_object_or_404(WorkPhoto.objects.defer("content"), pk=photo_id, tenant=member.tenant)
    scoped_target(member, photo.installation_id or photo.ticket_id, photo.installation_id is not None)
    if member.role == "customer" and photo.internal:
        raise Http404
    extensions = {"image/jpeg": "jpg", "image/png": "png"}
    if photo.media_type not in extensions:
        raise Http404
    content = bytes(photo.content)
    if len(content) != photo.byte_size or hashlib.sha256(content).hexdigest() != photo.checksum:
        raise Http404("Foto tidak dapat diverifikasi.")
    response = HttpResponse(content, content_type=photo.media_type)
    response["Content-Disposition"] = f'attachment; filename="tagihnet-photo-{photo.pk}.{extensions[photo.media_type]}"'
    response["Content-Length"] = str(len(content))
    response["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; sandbox"
    response["Cache-Control"] = "no-store, private"
    return response

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from .models import MonitoringDevice
from .monitoring import collect_poll, manager, overview, save_device, snapshots
from .permissions import require
from .security_views import recent_factor
from .views import paginate


class DeviceForm(forms.ModelForm):
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reviewed = forms.BooleanField(label="Saya memahami NAS ini hanya simulasi tanpa koneksi ke perangkat nyata.")

    class Meta:
        model = MonitoringDevice
        fields = ["name", "location", "enabled", "scenario"]
        labels = {"name": "Nama NAS simulasi", "location": "Lokasi (opsional)",
            "enabled": "Aktifkan pengambilan snapshot simulasi", "scenario": "Skenario data simulasi"}


class PollForm(forms.Form):
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


@require("network")
@require_GET
def monitoring_list(request):
    devices = MonitoringDevice.objects.filter(tenant=request.member.tenant)
    search = request.GET.get("q", "").strip()[:80]
    if search:
        devices = devices.filter(name__icontains=search)
    if request.GET.get("enabled") in {"yes", "no"}:
        devices = devices.filter(enabled=request.GET["enabled"] == "yes")
    page = paginate(request, devices)
    return render(request, "monitoring/list.html", {"page": page, "rows": snapshots(page.object_list),
        "summary": overview(request.member.tenant)})


def detail_response(request, device, form=None, status=200):
    row = snapshots(MonitoringDevice.objects.filter(pk=device.pk, tenant=device.tenant))[0]
    return render(request, "monitoring/detail.html", {"row": row,
        "poll_form": form or PollForm(initial={"version": device.version}),
        "sessions": row["snapshot"].sessions if row["snapshot"] else [],
        "page": paginate(request, device.polls.filter(tenant=device.tenant))}, status=status)


@require("network")
@require_GET
def monitoring_detail(request, pk):
    device = get_object_or_404(MonitoringDevice, pk=pk, tenant=request.member.tenant)
    return detail_response(request, device)


@require("manage_monitoring")
@require_http_methods(["GET", "POST"])
def monitoring_form(request, pk=None):
    member = manager(request.user)
    device = get_object_or_404(MonitoringDevice, pk=pk, tenant=member.tenant) if pk else MonitoringDevice(tenant=member.tenant)
    form = DeviceForm(request.POST or None, instance=device, initial={"version": device.version})
    if request.method == "POST":
        if not recent_factor(request, return_to=request.path):
            return redirect("account_security")
        if form.is_valid():
            try:
                saved = save_device(request.user, {field: form.cleaned_data[field] for field in DeviceForm.Meta.fields},
                    form.cleaned_data["version"], request.session["mfa_verified_at"], request.session["access_version"], pk)
            except ValidationError as exc:
                form.add_error(None, exc)
            except IntegrityError:
                form.add_error(None, "Nama NAS sudah digunakan dalam ISP ini.")
            except OperationalError:
                form.add_error(None, "Database sedang sibuk. Muat ulang formulir sebelum mencoba lagi.")
            else:
                return redirect("monitoring_detail", pk=saved.pk)
    return render(request, "core/form.html", {"form": form, "title": "Ubah NAS simulasi" if pk else "Tambah NAS simulasi",
        "back": reverse("monitoring_list"), "description": "Tanpa alamat router, secret atau koneksi jaringan. "
            "Pengambilan maksimal sekali per menit dan data dianggap segar selama 120 detik. "
            "Menyimpan konfigurasi memerlukan MFA terbaru dan membuat snapshot lama hanya sebagai histori."})


@require("network")
@require_POST
def monitoring_collect(request, pk):
    device = get_object_or_404(MonitoringDevice, pk=pk, tenant=request.member.tenant)
    form = PollForm(request.POST)
    if form.is_valid():
        try:
            poll = collect_poll(pk, request.user, form.cleaned_data["version"])
        except MonitoringDevice.DoesNotExist:
            return redirect("monitoring_list")
        except ValidationError as exc:
            form.add_error(None, exc)
        except OperationalError:
            form.add_error(None, "Database sibuk. Ulangi setelah memuat ulang status.")
        else:
            if poll:
                messages.info(request, poll.reason)
            else:
                messages.info(request, "Belum waktunya mengambil snapshot baru. Muat ulang status nanti.")
            return redirect("monitoring_detail", pk=pk)
    device.refresh_from_db()
    return detail_response(request, device, form, status=400)

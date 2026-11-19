from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from .locations import MAX_TOKEN, LocationConflict, form_token, pair, save_location, target
from .models import Customer, Installation
from .operations import member_for


class CoordinateField(forms.DecimalField):
    def to_python(self, value):
        if isinstance(value,str) and len(value)>24:
            raise ValidationError("Koordinat terlalu panjang.")
        return super().to_python(value)


class LocationForm(forms.Form):
    latitude = CoordinateField(label="Lintang (latitude)",max_digits=9,decimal_places=6,min_value=-90,max_value=90,required=False)
    longitude = CoordinateField(label="Bujur (longitude)",max_digits=9,decimal_places=6,min_value=-180,max_value=180,required=False)
    token = forms.CharField(max_length=MAX_TOKEN,widget=forms.HiddenInput)

    def clean(self):
        values=super().clean()
        if not self.errors:
            values["latitude"],values["longitude"]=pair(values["latitude"],values["longitude"])
        return values


@login_required
@sensitive_variables()
@sensitive_post_parameters("token","latitude","longitude")
@require_http_methods(["GET","POST"])
def location_edit(request,pk,kind):
    member=member_for(request.user)
    try:
        item=target(member,kind,pk)
        initial={"latitude":item.latitude,"longitude":item.longitude}
        if request.method=="GET":
            initial["token"],_=form_token(request.user,kind,pk)
        form=LocationForm(request.POST if request.method=="POST" else None,initial=initial)
        status=200
        if request.method=="POST":
            status=400
            if form.is_valid():
                try:
                    changed=save_location(request.user,kind,pk,form.cleaned_data["token"],form.cleaned_data["latitude"],
                        form.cleaned_data["longitude"],request.session.get("access_version"),request.correlation_id)
                except LocationConflict as error:
                    form.add_error(None,error);status=409
                except ValidationError as error:
                    form.add_error(None,error)
                except OperationalError:
                    form.add_error(None,"Lokasi belum disimpan. Muat ulang dan coba lagi.");status=503
                else:
                    messages.success(request,"Koordinat lokasi disimpan." if changed else "Koordinat lokasi tidak berubah.")
                    return redirect(reverse("installation_detail",args=[pk]) if kind=="installation"
                        else reverse("customer_detail",args=[pk])+"?tab=location")
    except (Customer.DoesNotExist,Installation.DoesNotExist):
        raise Http404("Lokasi tidak tersedia pada cakupan akses akun ini.") from None
    back=reverse("installation_detail",args=[pk]) if kind=="installation" else reverse("customer_detail",args=[pk])+"?tab=location"
    return render(request,"locations/edit.html",{"form":form,"item":item,"kind":kind,"back":back},status=status)

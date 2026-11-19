"""Issuer identity captured only for newly issued documents; legacy records stay untouched."""
from django.db import models


class InvoiceIssuerSnapshot(models.Model):
    invoice = models.OneToOneField('core.Invoice', on_delete=models.PROTECT, related_name='issuer_snapshot')
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=250, blank=True)
    captured_at = models.DateTimeField()


class PaymentIssuerSnapshot(models.Model):
    payment = models.OneToOneField('core.Payment', on_delete=models.PROTECT, related_name='issuer_snapshot')
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=250, blank=True)
    captured_at = models.DateTimeField()

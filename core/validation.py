from django.core.exceptions import ValidationError

def normalize_phone(value):
    phone = value.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+62"):
        phone = "0" + phone[3:]
    elif phone.startswith("62"):
        phone = "0" + phone[2:]
    if not phone.isdigit() or not 10 <= len(phone) <= 15:
        raise ValidationError("Gunakan nomor telepon Indonesia, 10–15 digit.")
    return phone

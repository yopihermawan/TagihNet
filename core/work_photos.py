"""Private normalized work evidence in the database, covered by database backups."""
import hashlib
import io
import uuid
import warnings
from datetime import timedelta
from PIL import Image, ImageOps, PngImagePlugin, UnidentifiedImageError
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from .billing import audit, retry_locked
from .models import Installation, Tenant, Ticket, WorkPhoto
from .operations import check_version, event, member_for, visible_installations, visible_tickets
from .uploads import MAX_FILE_BYTES

MAX_PIXELS = 12000000
MAX_TARGET_PHOTOS = 20
MAX_TENANT_BYTES = 100*1024*1024
MAX_DAILY_UPLOADS = 50
# Bound compressed PNG metadata before pixel decoding, across app image readers.
PngImagePlugin.MAX_TEXT_CHUNK = 64*1024
PngImagePlugin.MAX_TEXT_MEMORY = 256*1024


def target_query(member, installation=False):
    return visible_installations(member) if installation else visible_tickets(member)


def visible_photos(member, target):
    values = {"installation": target} if isinstance(target, Installation) else {"ticket": target}
    query = WorkPhoto.objects.filter(tenant=member.tenant, **values).defer("content").select_related("created_by")
    return query.filter(internal=False) if member.role == "customer" else query


def normalize_photo(upload):
    extension = getattr(upload, "name", "").rsplit(".", 1)[-1].lower()
    if extension not in {"jpg", "jpeg", "png"}:
        raise ValidationError("Ekstensi foto wajib .jpg, .jpeg atau .png.")
    source = upload.read(MAX_FILE_BYTES+1)
    if not source or len(source) > MAX_FILE_BYTES:
        raise ValidationError("Foto wajib berukuran 1 byte–2 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(source)) as check:
                image_format = check.format
                if image_format not in {"JPEG", "PNG"} or getattr(check, "n_frames", 1) != 1:
                    raise ValidationError("Gunakan satu gambar JPG/PNG, tanpa animasi.")
                if (image_format == "PNG") != (extension == "png"):
                    raise ValidationError("Ekstensi file harus sesuai isi foto.")
                if not (1 <= check.width <= 4096 and 1 <= check.height <= 4096 and check.width*check.height <= MAX_PIXELS):
                    raise ValidationError("Maksimal 4096 piksel per sisi dan 12 megapiksel.")
                check.verify()
            with Image.open(io.BytesIO(source)) as decoded:
                decoded.load()
                oriented = ImageOps.exif_transpose(decoded)
                mode = "RGBA" if image_format == "PNG" and ("A" in oriented.getbands() or "transparency" in oriented.info) else "RGB"
                clean = Image.new(mode, oriented.size)
                clean.paste(oriented.convert(mode))
                output = io.BytesIO()
                clean.save(output, format=image_format, **({"quality": 85} if image_format == "JPEG" else {}))
                data = output.getvalue()
                width, height = clean.size
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValidationError("Isi foto rusak atau tidak didukung. Gunakan JPG/PNG yang valid.") from None
    if len(data) > MAX_FILE_BYTES:
        raise ValidationError("Foto hasil pemrosesan melebihi 2 MiB. Perkecil gambar sebelum mengunggah.")
    return {"content": data, "byte_size": len(data), "width": width, "height": height,
        "media_type": "image/jpeg" if image_format == "JPEG" else "image/png",
        "source_sha256": hashlib.sha256(source).hexdigest(), "checksum": hashlib.sha256(data).hexdigest()}


def upload_photo(actor, target_id, upload, caption, internal, version, request_key, access_version, installation=False):
    member = member_for(actor)
    target_query(member, installation).get(pk=target_id)
    if type(internal) is not bool or (member.role == "customer" and internal):
        raise PermissionDenied
    if installation and not internal:
        raise ValidationError("Foto PSB hanya tersedia untuk tim internal.")
    if not isinstance(caption, str) or not 3 <= len(caption.strip()) <= 200:
        raise ValidationError("Keterangan foto wajib 3–200 karakter.")
    try:
        request_key = uuid.UUID(str(request_key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Kunci permintaan foto tidak valid.") from None
    # Reject an exhausted quota before spending time on the image decoder.
    if WorkPhoto.objects.filter(tenant=member.tenant, created_by=actor,
            created_at__gte=timezone.now()-timedelta(days=1)).count() >= MAX_DAILY_UPLOADS and not WorkPhoto.objects.filter(
                tenant=member.tenant, created_by=actor, request_key=request_key).exists():
        raise ValidationError("Batas unggahan harian tercapai. Hubungi tim dukungan.")
    prepared = normalize_photo(upload)
    return save_photo(actor, target_id, prepared, caption.strip(), internal, version, request_key, access_version, installation)


@retry_locked
@transaction.atomic
def save_photo(actor, target_id, prepared, caption, internal, version, request_key, access_version, installation):
    member = member_for(actor)
    Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    member = member_for(actor)
    if member.session_version != access_version:
        raise PermissionDenied("Akses berubah. Muat ulang formulir foto.")
    target = target_query(member, installation).select_for_update().get(pk=target_id)
    previous = WorkPhoto.objects.filter(tenant=member.tenant, created_by=actor, request_key=request_key).defer("content").first()
    if previous:
        if (previous.ticket_id != (None if installation else target.pk)
                or previous.installation_id != (target.pk if installation else None)
                or previous.caption != caption or previous.internal != internal or previous.source_sha256 != prepared["source_sha256"]):
            raise ValidationError("Kunci permintaan telah digunakan untuk foto berbeda.")
        return previous
    check_version(target, version)
    target_values = {"installation": target} if installation else {"ticket": target}
    if WorkPhoto.objects.filter(tenant=member.tenant, **target_values).count() >= MAX_TARGET_PHOTOS:
        raise ValidationError("Batas lampiran pekerjaan tercapai. Hubungi tim dukungan.")
    stored = WorkPhoto.objects.filter(tenant=member.tenant).aggregate(total=Sum("byte_size"))["total"] or 0
    if stored+prepared["byte_size"] > MAX_TENANT_BYTES:
        raise ValidationError("Batas penyimpanan foto ISP tercapai. Hubungi pengelola.")
    if WorkPhoto.objects.filter(tenant=member.tenant, created_by=actor,
            created_at__gte=timezone.now()-timedelta(days=1)).count() >= MAX_DAILY_UPLOADS:
        raise ValidationError("Batas unggahan harian tercapai.")
    photo = WorkPhoto(tenant=member.tenant, created_by=actor, request_key=request_key,
        caption=caption, internal=internal, **target_values, **prepared)
    photo.full_clean()
    photo.save()
    event(member, target, "photo_added", "Foto ditambahkan: "+caption, internal=internal)
    audit(member.tenant, actor, "photo.created", photo.pk,
        f"{photo.media_type}; {photo.width}x{photo.height}; {photo.byte_size} byte; internal={photo.internal}.")
    return photo

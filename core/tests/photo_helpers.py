import io
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile


def photo_bytes(image_format="PNG", **options):
    buffer = io.BytesIO()
    Image.new("RGBA" if image_format == "PNG" else "RGB", (16, 12), "#15776a").save(buffer, format=image_format, **options)
    return buffer.getvalue()


def photo_upload(image_format="PNG", **options):
    return SimpleUploadedFile("evidence.png" if image_format == "PNG" else "evidence.jpg", photo_bytes(image_format, **options),
        content_type="image/png" if image_format == "PNG" else "image/jpeg")

"""Secret files support container mounts without copying values into environment or argv."""
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured


def read_secret(environ, name):
    value, filename = environ.get(name, ""), environ.get(name+"_FILE", "")
    if value and filename:
        raise ImproperlyConfigured(f"Pilih satu sumber {name}: variabel atau file.")
    if not filename:
        return value
    try:
        with Path(filename).open("rb") as stream:
            content = stream.read(4097)
        if len(content)>4096:
            raise ValueError
        value = content.decode("utf-8").rstrip("\r\n")
        if not value:
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        raise ImproperlyConfigured(f"File {name} tidak dapat dibaca atau tidak valid.") from None
    return value

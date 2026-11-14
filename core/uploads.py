"""One bounded in-memory file per HTTP request; no public or temporary file storage."""
from io import BytesIO
from django.core.exceptions import RequestDataTooBig, TooManyFilesSent
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.files.uploadhandler import FileUploadHandler

MAX_FILE_BYTES = 2*1024*1024


class BoundedMemoryUploadHandler(FileUploadHandler):
    def __init__(self, request=None):
        super().__init__(request)
        self.received, self.files = 0, 0

    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        if content_length is not None and content_length > MAX_FILE_BYTES+262144:
            raise RequestDataTooBig("Batas ukuran unggahan terlampaui.")

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.files += 1
        if self.files > 1:
            if hasattr(self, "buffer"):
                self.buffer.close()
            raise TooManyFilesSent("Unggah satu file per permintaan.")
        self.buffer = BytesIO()
        self.file = self.buffer  # Also expose the buffer to Django's cleanup hooks.

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > MAX_FILE_BYTES:
            self.buffer.close()
            raise RequestDataTooBig("File maksimal 2 MiB.")
        self.buffer.write(raw_data)
        return None

    def file_complete(self, file_size):
        self.buffer.seek(0)
        return InMemoryUploadedFile(self.buffer, self.field_name, self.file_name, self.content_type,
            file_size, self.charset, self.content_type_extra)

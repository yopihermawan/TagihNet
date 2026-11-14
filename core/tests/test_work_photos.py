import hashlib
import io
import struct
import uuid
import zlib
from unittest.mock import patch
from PIL import Image, PngImagePlugin
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, RequestDataTooBig, TooManyFilesSent, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from core.models import AuditEvent, Customer, Invoice, Membership, NetworkJob, Payment, Ticket, WorkEvent, WorkPhoto
from core.operations import create_installation, create_ticket, manage_ticket
from core.uploads import BoundedMemoryUploadHandler, MAX_FILE_BYTES
from core.work_photos import normalize_photo, upload_photo, visible_photos
from .auth_helpers import verified_login
from .photo_helpers import photo_bytes, photo_upload
from .test_operations_workflows import prospect_data, technician
from .test_workflow import fixtures


@override_settings(SIMULATION=True)
class WorkPhotoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.tech = technician(cls.tenants[0])
        cls.other_tech = technician(cls.tenants[0], 'photo-other-tech')
        cls.ticket = create_ticket(cls.owners[0], cls.customers[0].pk, 'Gangguan dengan foto', 'Lokasi pemeriksaan fiktif',
            'connection', assigned_id=cls.tech.pk)
        cls.foreign_ticket = create_ticket(cls.owners[1], cls.customers[1].pk, 'Gangguan tenant lain', 'Privat tenant lain', 'connection')
        cls.order = create_installation(cls.owners[0], {**prospect_data(cls.tenants[0]), 'assigned_id': cls.tech.pk})

    def setUp(self):
        verified_login(self.client, self.owners[0])
        self.url = f'/operations/tickets/{self.ticket.pk}/photos/'

    def save(self, actor=None, target=None, internal=True, **changes):
        target = target or self.ticket
        values = dict(upload=photo_upload(), caption='Foto jalur fiktif', internal=internal, version=target.version,
            request_key=uuid.uuid4(), access_version=1, installation=False)
        values.update(changes)
        return upload_photo(actor or self.owners[0], target.pk, **values)

    def post(self, **changes):
        return self.client.post(self.url, {'photo': photo_upload(), 'caption': 'Foto browser fiktif', 'internal': 'on',
            'version': self.ticket.version, 'request_key': uuid.uuid4(), **changes})

    def download(self, photo, client=None):
        return (client or self.client).get(f'/operations/photos/{photo.pk}/download/')

    def customer_client(self):
        user = get_user_model().objects.create_user('photo-customer')
        Membership.objects.create(user=user, tenant=self.tenants[0], role='customer', customer=self.customers[0])
        client = Client(); verified_login(client, user)
        return client, user

    def test_png_is_verified_and_stored_in_database_without_metadata(self):
        metadata = PngImagePlugin.PngInfo(); metadata.add_text('private', 'location must not survive')
        photo = self.save(upload=photo_upload(pnginfo=metadata))
        self.assertEqual(photo.media_type, 'image/png')
        self.assertEqual((photo.width, photo.height), (16, 12))
        content = bytes(WorkPhoto.objects.get(pk=photo.pk).content)
        self.assertEqual(hashlib.sha256(content).hexdigest(), photo.checksum)
        with Image.open(io.BytesIO(content)) as image:
            self.assertNotIn('private', image.info)
            self.assertEqual(image.mode, 'RGBA')
        self.assertNotIn(b'location must not survive', content)

    def test_jpeg_exif_orientation_applied_and_exif_removed(self):
        exif = Image.Exif(); exif[274] = 6; exif[270] = 'private device metadata'
        photo = self.save(upload=photo_upload('JPEG', exif=exif))
        self.assertEqual((photo.width, photo.height), (12, 16))
        with Image.open(io.BytesIO(bytes(photo.content))) as image:
            self.assertFalse(image.getexif())
        self.assertNotIn(b'private device metadata', bytes(photo.content))

    def test_invalid_content_extension_mismatch_and_active_formats_rejected(self):
        payloads = [('photo.png', b'<svg><script>bad()</script></svg>'), ('photo.jpg', b'%PDF-1.7 test'),
            ('photo.html', photo_bytes()), ('photo.png', photo_bytes('JPEG')), ('photo.png', photo_bytes()[:40])]
        for name, content in payloads:
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.save(upload=SimpleUploadedFile(name, content, content_type='image/png'))
        self.assertFalse(WorkPhoto.objects.exists())

    def test_declared_mime_is_not_trusted(self):
        photo = self.save(upload=SimpleUploadedFile('photo.png', photo_bytes(), content_type='text/html'))
        self.assertEqual(photo.media_type, 'image/png')

    def test_animated_png_is_rejected(self):
        output = io.BytesIO()
        Image.new('RGB', (8, 8), 'red').save(output, format='PNG', save_all=True,
            append_images=[Image.new('RGB', (8, 8), 'blue')])
        with self.assertRaises(ValidationError): self.save(upload=SimpleUploadedFile('a.png', output.getvalue()))

    def test_dimensions_and_pixel_bombs_rejected_before_decoding(self):
        def chunk(kind, content): return struct.pack('>I', len(content))+kind+content+struct.pack('>I', zlib.crc32(kind+content))
        for width, height in [(4097, 1), (4000, 4000)]:
            source = b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))+chunk(b'IDAT', b'')+chunk(b'IEND', b'')
            with self.subTest(width=width), self.assertRaises(ValidationError):
                normalize_photo(SimpleUploadedFile('bomb.png', source))

    def test_empty_oversized_and_excessive_metadata_rejected(self):
        for content in [b'', b'x'*(MAX_FILE_BYTES+1)]:
            with self.assertRaises(ValidationError): self.save(upload=SimpleUploadedFile('x.png', content))
        metadata = PngImagePlugin.PngInfo(); metadata.add_text('text', 'x'*70000, zip=True)
        with self.assertRaises(ValidationError): self.save(upload=photo_upload(pnginfo=metadata))

    def test_normalized_output_limit_is_enforced(self):
        image = Image.new('RGB', (16, 12))
        image.putdata([(i*31 % 256, i*17 % 256, i*47 % 256) for i in range(192)])
        output = io.BytesIO(); image.save(output, format='JPEG', quality=1)
        source = output.getvalue()
        prepared = normalize_photo(SimpleUploadedFile('a.jpg', source))
        self.assertGreater(prepared['byte_size'], len(source))
        with patch('core.work_photos.MAX_FILE_BYTES', len(source)):
            with self.assertRaises(ValidationError): normalize_photo(SimpleUploadedFile('a.jpg', source))

    def test_success_has_one_audited_photo_and_no_financial_side_effect(self):
        before = [model.objects.count() for model in [Customer, Invoice, Payment, NetworkJob]]
        photo = self.save()
        self.assertEqual(WorkPhoto.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='photo.created', target=str(photo.pk)).count(), 1)
        self.assertEqual(WorkEvent.objects.filter(ticket=self.ticket, kind='photo_added').count(), 1)
        self.assertEqual([model.objects.count() for model in [Customer, Invoice, Payment, NetworkJob]], before)

    def test_replay_same_key_does_not_duplicate_and_conflicting_file_is_rejected(self):
        key = uuid.uuid4(); photo = self.save(request_key=key)
        self.assertEqual(self.save(request_key=key).pk, photo.pk)
        with self.assertRaises(ValidationError): self.save(request_key=key, caption='Different caption')
        with self.assertRaises(ValidationError): self.save(request_key=key, upload=photo_upload('JPEG'))
        self.assertEqual(WorkPhoto.objects.count(), 1)

    def test_request_key_cannot_be_reused_for_another_target(self):
        key = uuid.uuid4(); self.save(request_key=key)
        other = create_ticket(self.owners[0], self.customers[0].pk, 'Tiket foto kedua', 'Pemeriksaan fiktif', 'connection')
        with self.assertRaises(ValidationError): self.save(target=other, request_key=key)

    def test_replay_succeeds_when_quota_is_full(self):
        key = uuid.uuid4(); photo = self.save(request_key=key)
        with patch('core.work_photos.MAX_DAILY_UPLOADS', 1), patch('core.work_photos.MAX_TARGET_PHOTOS', 1):
            self.assertEqual(self.save(request_key=key).pk, photo.pk)
            with self.assertRaises(ValidationError): self.save()

    def test_stale_target_or_access_version_cannot_store(self):
        with self.assertRaises(ValidationError): self.save(version=self.ticket.version+1)
        with self.assertRaises(PermissionDenied): self.save(access_version=2)
        self.assertFalse(WorkPhoto.objects.exists())

    def test_target_tenant_and_daily_quotas_are_enforced(self):
        self.save()
        for name, value in [('MAX_TARGET_PHOTOS', 1), ('MAX_TENANT_BYTES', 1), ('MAX_DAILY_UPLOADS', 1)]:
            with self.subTest(limit=name), patch('core.work_photos.'+name, value), self.assertRaises(ValidationError): self.save()
        self.assertEqual(WorkPhoto.objects.count(), 1)

    def test_failed_audit_rolls_back_photo_and_timeline(self):
        events = WorkEvent.objects.count()
        with patch('core.work_photos.audit', side_effect=RuntimeError('fixture')), self.assertRaises(RuntimeError): self.save()
        self.assertFalse(WorkPhoto.objects.exists())
        self.assertEqual(WorkEvent.objects.count(), events)

    def test_public_ticket_photo_available_to_own_customer_internal_hidden(self):
        public = self.save(internal=False, caption='Public photo')
        private = self.save(caption='PRIVATE INTERNAL PHOTO')
        client, _ = self.customer_client()
        response = client.get(self.url)
        self.assertContains(response, public.caption)
        self.assertNotContains(response, private.caption)
        self.assertEqual(self.download(public, client).status_code, 200)
        self.assertEqual(self.download(private, client).status_code, 404)

    def test_customer_uploads_only_public_to_own_ticket(self):
        client, user = self.customer_client()
        photo = self.save(actor=user, internal=False)
        self.assertFalse(photo.internal)
        with self.assertRaises(PermissionDenied): self.save(actor=user, internal=True)
        response = client.post(self.url, {'photo': photo_upload(), 'caption': 'Photo from customer', 'version': 1,
            'request_key': uuid.uuid4(), 'internal': 'on'})
        self.assertEqual(response.status_code, 403)

    def test_assigned_technician_loses_download_and_upload_after_reassignment(self):
        photo = self.save(actor=self.tech.user)
        client = Client(); verified_login(client, self.tech.user)
        self.assertEqual(self.download(photo, client).status_code, 200)
        manage_ticket(self.owners[0], self.ticket.pk, 'normal', 'connection', self.other_tech.pk, self.ticket.version, uuid.uuid4())
        self.assertEqual(self.download(photo, client).status_code, 404)
        with self.assertRaises(Ticket.DoesNotExist): self.save(actor=self.tech.user, request_key=photo.request_key)

    def test_other_customer_in_same_isp_cannot_read_or_upload(self):
        other = Customer.objects.create(tenant=self.tenants[0], plan=self.customers[0].plan, code='OTHER-PHOTO',
            name='Pelanggan lain', phone='081277700001', username='other-photo', address='Fiktif', area='Dago')
        user = get_user_model().objects.create_user('other-photo-customer')
        Membership.objects.create(user=user, tenant=self.tenants[0], role='customer', customer=other)
        client = Client(); verified_login(client, user)
        photo = self.save(internal=False)
        self.assertEqual(client.get(self.url).status_code, 404)
        self.assertEqual(self.download(photo, client).status_code, 404)
        with self.assertRaises(Ticket.DoesNotExist): self.save(actor=user, internal=False)

    def test_revoked_custom_ticket_permission_blocks_existing_photo(self):
        from core.access import save_role
        from core.models import AccessRole
        role = save_role(self.owners[0], 'Photo operator', {'tickets'})
        user = get_user_model().objects.create_user('photo-operator')
        Membership.objects.create(user=user, tenant=self.tenants[0], role='ops', access_role=role)
        photo = self.save(actor=user)
        client = Client(); verified_login(client, user)
        self.assertEqual(self.download(photo, client).status_code, 200)
        AccessRole.objects.filter(pk=role.pk).update(capabilities=[])
        self.assertEqual(self.download(photo, client).status_code, 404)
        with self.assertRaises(PermissionDenied): self.save(actor=user, request_key=photo.request_key)

    def test_installation_photos_are_internal_and_portal_cannot_access(self):
        photo = self.save(target=self.order, installation=True, actor=self.tech.user)
        self.assertTrue(photo.internal)
        self.assertEqual(photo.installation_id, self.order.pk)
        with self.assertRaises(ValidationError): self.save(target=self.order, installation=True, internal=False)
        client, _ = self.customer_client()
        self.assertEqual(self.download(photo, client).status_code, 404)

    def test_foreign_tenant_and_wrong_presets_cannot_list_or_download(self):
        foreign = upload_photo(self.owners[1], self.foreign_ticket.pk, photo_upload(), 'Foto tenant asing', True, 1, uuid.uuid4(), 1)
        self.assertEqual(self.download(foreign).status_code, 404)
        for role in ['finance', 'cashier', 'noc']:
            user = get_user_model().objects.create_user('photo-'+role)
            Membership.objects.create(user=user, tenant=self.tenants[0], role=role)
            client = Client(); verified_login(client, user)
            self.assertEqual(client.get(self.url).status_code, 404)
            self.assertEqual(self.download(self.save(), client).status_code, 404)

    def test_download_is_attachment_private_sandboxed_and_original_name_unused(self):
        photo = self.save(upload=SimpleUploadedFile('../../unsafe-name.png', photo_bytes(), content_type='text/html'))
        response = self.download(photo)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertIn('attachment; filename="tagihnet-photo-', response['Content-Disposition'])
        self.assertNotIn('unsafe-name', response['Content-Disposition'])
        self.assertIn('sandbox', response['Content-Security-Policy'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('no-store', response['Cache-Control'])

    def test_corrupt_blob_or_unknown_mime_cannot_be_downloaded(self):
        photo = self.save()
        WorkPhoto.objects.filter(pk=photo.pk).update(content=b'corrupt')
        self.assertEqual(self.download(photo).status_code, 404)
        WorkPhoto.objects.filter(pk=photo.pk).update(media_type='text/html')
        self.assertEqual(self.download(photo).status_code, 404)

    def test_metadata_list_defers_binary_and_get_does_not_mutate(self):
        photo = self.save()
        member = Membership.objects.get(user=self.owners[0])
        listed = visible_photos(member, self.ticket).get(pk=photo.pk)
        self.assertIn('content', listed.get_deferred_fields())
        before = (WorkPhoto.objects.count(), WorkEvent.objects.count(), AuditEvent.objects.count())
        self.client.get(self.url); self.download(photo)
        self.assertEqual((WorkPhoto.objects.count(), WorkEvent.objects.count(), AuditEvent.objects.count()), before)

    def test_post_requires_csrf_and_download_get_only(self):
        client = Client(enforce_csrf_checks=True); verified_login(client, self.owners[0])
        self.assertEqual(client.post(self.url, {'caption': 'no CSRF', 'photo': photo_upload()}).status_code, 403)
        photo = self.save()
        self.assertEqual(self.client.post(f'/operations/photos/{photo.pk}/download/').status_code, 405)
        self.assertEqual(Client().get(self.url).status_code, 302)

    def test_upload_post_redirect_and_safe_caption(self):
        self.assertRedirects(self.post(caption='<script>private()</script>'), self.url)
        response = self.client.get(self.url)
        self.assertContains(response, '&lt;script&gt;private()&lt;/script&gt;')
        self.assertNotContains(response, '<script>private()</script>')

    def test_oversized_http_upload_and_multiple_files_are_rejected(self):
        big = SimpleUploadedFile('large.png', b'x'*(MAX_FILE_BYTES+1))
        response = self.post(photo=big)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.wsgi_request.upload_handlers[0].file.closed)
        self.assertEqual(self.post(photo=[photo_upload(), photo_upload()]).status_code, 400)
        self.assertFalse(WorkPhoto.objects.exists())

    def test_upload_handler_enforces_actual_bytes_and_never_spools_to_disk(self):
        handler = BoundedMemoryUploadHandler()
        handler.new_file('photo', 'x.png', 'image/png', 1, None)
        self.assertIsNone(handler.receive_data_chunk(b'x'*MAX_FILE_BYTES, 0))
        with self.assertRaises(RequestDataTooBig): handler.receive_data_chunk(b'x', MAX_FILE_BYTES)
        with self.assertRaises(TooManyFilesSent): handler.new_file('photo', 'y.png', 'image/png', 1, None)
        with self.assertRaises(RequestDataTooBig): handler.handle_raw_input(None, {}, MAX_FILE_BYTES+262145, None)

    def test_database_target_constraint_and_model_tenant_guard(self):
        photo = self.save()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            WorkPhoto.objects.filter(pk=photo.pk).update(ticket=None)
        photo.tenant = self.tenants[1]
        with self.assertRaises(ValidationError): photo.full_clean()

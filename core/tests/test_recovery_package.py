import base64
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from django.test import SimpleTestCase
from core.backup_archive import ArchiveError,encrypt_stream,generate_key,read_key
from scripts import recovery_package as kit


class RecoveryPackageTests(SimpleTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='tagihnet-package-unit-')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.keyfile=self.root/'original-key.json';generate_key(self.keyfile);self.key=read_key(self.keyfile)
        self.mfa=self.root/'original-mfa';self.mfa.write_bytes(Fernet.generate_key());self.mfa.chmod(0o600)
        self.archive=self.root/'original.tnbk'
        encrypt_stream(io.BytesIO(b'PGDMPfixture'),self.archive,self.key,'postgresql')
        self.package=self.root/'archives/package';self.keys=self.root/'custody/keys'
        self.revision=patch.object(kit,'exporter_commit',return_value='a'*40);self.revision.start();self.addCleanup(self.revision.stop)

    def export(self):return kit.export_package(self.archive,self.keyfile,self.mfa,self.package,self.keys)

    def metadata(self,data):
        path=self.package/'manifest.json';path.write_bytes(kit.json_bytes(data))

    def refused_restore(self):
        with patch.object(kit,'restore_database') as restore,self.assertRaises((ArchiveError,OSError,ValueError)):
            kit.restore_package(self.package,self.keys,self.root/'unused-target')
        restore.assert_not_called();self.assertFalse((self.root/'unused-target').exists())

    def test_export_preserves_originals_and_separates_private_material(self):
        before=[p.read_bytes() for p in (self.archive,self.keyfile,self.mfa)]
        meta=self.export()
        self.assertEqual(kit.verify_package(self.package,self.keys),meta)
        self.assertEqual([p.read_bytes() for p in (self.archive,self.keyfile,self.mfa)],before)
        self.assertEqual({p.name for p in self.package.iterdir()},{'manifest.json','archive.tnbk'})
        self.assertEqual(read_key(self.keys/'backup-key.json'),self.key)
        self.assertEqual((self.keys/'mfa.key').read_bytes(),self.mfa.read_bytes())
        for root in (self.package,self.keys):
            self.assertEqual(root.stat().st_mode&0o777,0o700)
            for p in root.iterdir():self.assertEqual(p.stat().st_mode&0o777,0o600)
        self.assertNotIn(str(self.root),json.dumps(meta))
        self.assertNotIn(self.mfa.read_text(),json.dumps(meta))

    def test_wrong_archive_key_is_rejected_before_output_creation(self):
        wrong=self.root/'wrong';generate_key(wrong)
        with self.assertRaises(ArchiveError):kit.export_package(self.archive,wrong,self.mfa,self.package,self.keys)
        self.assertFalse(self.package.exists());self.assertFalse(self.keys.exists())

    def test_sqlite_or_wrong_postgres_signature_is_rejected(self):
        for n,(kind,content) in enumerate((('sqlite',b'SQLite fixture'),('postgresql',b'not a dump'))):
            archive=self.root/f'bad-{n}';encrypt_stream(io.BytesIO(content),archive,self.key,kind)
            with self.subTest(kind=kind),self.assertRaises(ArchiveError):kit.export_package(archive,self.keyfile,self.mfa,self.package,self.keys)
        self.assertFalse(self.package.exists())

    def test_tampered_input_is_rejected_before_output_creation(self):
        data=bytearray(self.archive.read_bytes());data[-1]^=1;self.archive.write_bytes(data)
        with self.assertRaises(ArchiveError):self.export()
        self.assertFalse(self.package.exists())

    def test_existing_destination_is_never_overwritten(self):
        self.package.mkdir(parents=True);(self.package/'keep').write_text('retained')
        with patch.object(kit,'verify_dump') as check,self.assertRaises(ArchiveError):self.export()
        check.assert_not_called();self.assertEqual((self.package/'keep').read_text(),'retained');self.assertFalse(self.keys.exists())

    def test_existing_key_destination_is_rejected_before_creating_package(self):
        self.keys.mkdir(parents=True)
        with self.assertRaises(ArchiveError):self.export()
        self.assertFalse(self.package.exists())

    def test_equal_or_nested_destinations_are_rejected(self):
        for package,keys in ((self.package,self.package),(self.package,self.package/'keys'),(self.keys/'package',self.keys)):
            with self.subTest(package=package),self.assertRaises(ArchiveError):kit.export_package(self.archive,self.keyfile,self.mfa,package,keys)
        self.assertFalse(self.package.exists());self.assertFalse(self.keys.exists())

    def test_archive_replacement_between_authentication_and_digest_is_refused(self):
        replacement = self.root / 'replacement.tnbk'
        replacement.write_bytes(self.archive.read_bytes())
        replacement.chmod(0o600)
        original_digest = kit.digest_stream

        def replace_after_digest(stream):
            result = original_digest(stream)
            os.replace(replacement, self.archive)
            return result

        with patch.object(kit, 'digest_stream', side_effect=replace_after_digest), self.assertRaises(ArchiveError):
            kit.archive_details(self.archive, self.key)

    def test_symlink_or_fifo_archive_is_refused(self):
        link=self.root/'link';link.symlink_to(self.archive)
        with self.assertRaises(OSError):kit.export_package(link,self.keyfile,self.mfa,self.package,self.keys)
        fifo=self.root/'fifo';os.mkfifo(fifo,0o600)
        with self.assertRaises(ArchiveError):kit.export_package(fifo,self.keyfile,self.mfa,self.package,self.keys)
        self.assertFalse(self.package.exists())

    def test_manifest_edit_cannot_reach_target(self):
        data=self.export();data['exporter_commit']='b'*40;self.metadata(data);self.refused_restore()

    def test_same_key_identifier_with_wrong_material_cannot_reach_target(self):
        self.export();p=self.keys/'backup-key.json';data=json.loads(p.read_text())
        data['key']=base64.b64encode(os.urandom(32)).decode();p.write_text(json.dumps(data));self.refused_restore()

    def test_swap_to_other_authenticated_archive_cannot_reach_target(self):
        self.export();other=self.root/'other.tnbk';encrypt_stream(io.BytesIO(b'PGDMPdifferent'),other,self.key,'postgresql')
        (self.package/'archive.tnbk').write_bytes(other.read_bytes());self.refused_restore()

    def test_valid_manifest_mac_does_not_replace_full_gcm_authentication(self):
        data=self.export();path=self.package/'archive.tnbk';raw=bytearray(path.read_bytes());raw[-1]^=1;path.write_bytes(raw)
        data['archive_size'],data['archive_sha256']=kit.digest_archive(path)
        data['mac']=kit.manifest_mac(data,self.key);self.metadata(data);self.refused_restore()

    def test_wrong_mfa_key_is_rejected_even_without_database_devices(self):
        self.export();(self.keys/'mfa.key').write_bytes(Fernet.generate_key());self.refused_restore()

    def test_missing_files_make_package_incomplete(self):
        self.export();path=self.package/'manifest.json';saved=path.read_bytes();path.unlink()
        self.refused_restore();path.write_bytes(saved);path.chmod(0o600)
        (self.keys/'mfa.key').unlink();self.refused_restore()

    def test_extra_key_file_inside_package_is_refused(self):
        self.export();(self.package/'unexpected-key').write_bytes(self.mfa.read_bytes());self.refused_restore()

    def test_duplicate_fields_unknown_version_and_invalid_sizes_are_refused(self):
        data=self.export();path=self.package/'manifest.json'
        path.write_text('{"version":1,"version":1}');self.refused_restore()
        for changes in ({'version':True},{'version':2},{'archive_size':True},{'archive_size':kit.MAX_ARCHIVE+1},{'package_id':'../bad'},{'created_at':'2026-11-15T00:00:00'}):
            with self.subTest(changes=changes):self.metadata({**data,**changes});self.refused_restore()

    def test_oversize_manifest_is_refused(self):
        self.export();(self.package/'manifest.json').write_bytes(b' '*(16384+1));self.refused_restore()

    def test_public_file_or_directory_permissions_are_refused(self):
        self.export()
        for path in (self.package/'manifest.json',self.package/'archive.tnbk',self.keys/'mfa.key',self.keys/'backup-key.json'):
            with self.subTest(path=path):
                path.chmod(0o644);self.refused_restore();path.chmod(0o600)
        self.keys.chmod(0o755);self.refused_restore()

    def test_symlink_member_or_key_directory_is_refused(self):
        self.export();path=self.package/'archive.tnbk';path.unlink();path.symlink_to(self.archive);self.refused_restore()
        path.unlink();path.write_bytes(self.archive.read_bytes());path.chmod(0o600)
        alias=self.root/'keys-alias';alias.symlink_to(self.keys,target_is_directory=True)
        with self.assertRaises(ArchiveError):kit.verify_package(self.package,alias)

    def test_interrupted_export_has_no_completion_manifest(self):
        with patch.object(kit,'copy_archive',side_effect=OSError('private diagnostic')),self.assertRaises(OSError):self.export()
        self.assertFalse((self.package/'manifest.json').exists())
        self.refused_restore();self.assertTrue(self.archive.exists());self.assertTrue(self.keyfile.exists())

    def test_restore_freezes_inputs_and_removes_private_temporary_copies(self):
        self.export();original=self.archive.read_bytes();mfa=self.mfa.read_bytes();seen=[]
        def restore(target,archive,key,mfa_file):
            seen.extend([archive,mfa_file]);self.assertEqual(archive.read_bytes(),original);self.assertEqual(mfa_file.read_bytes(),mfa)
            self.assertNotEqual(archive.parent,self.package);self.assertEqual(mfa_file.stat().st_mode&0o777,0o600)
            (self.keys/'mfa.key').write_bytes(Fernet.generate_key());(self.package/'archive.tnbk').write_bytes(b'changed')
            self.assertEqual(mfa_file.read_bytes(),mfa);self.assertEqual(archive.read_bytes(),original)
        with patch.object(kit,'restore_database',side_effect=restore):kit.restore_package(self.package,self.keys,self.root/'target')
        self.assertTrue(seen);self.assertTrue(all(not p.exists() for p in seen))

    def test_offline_verify_does_not_contact_database(self):
        self.export()
        with patch.object(kit,'restore_database') as restore:kit.verify_package(self.package,self.keys)
        restore.assert_not_called()

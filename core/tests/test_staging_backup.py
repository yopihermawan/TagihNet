import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from django.test import SimpleTestCase
from core.backup_archive import ArchiveError, decrypt_stream, encrypt_stream, generate_key, read_key, verify_archive
from core.backup_database import postgres_backup_command
from scripts import staging_backup as stage
from scripts.staging_lab import operation_lock, up, resume_workers


class StagingBackupTests(SimpleTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='tagihnet-staging-backup-unit-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.keyfile=self.root/'archive-key.json'
        generate_key(self.keyfile)
        self.key=read_key(self.keyfile)

    def encrypted(self,data=b'PGDMPfixture',kind='postgresql'):
        target=self.root/'archive.tnbk'
        encrypt_stream(io.BytesIO(data),target,self.key,kind)
        return target

    def target(self,**changes):
        directory=self.root/'target'
        directory.mkdir()
        state={'project':'tagihnet-stage-1234567890','directory':str(directory),'port':19443,'initialized':False,**changes}
        (directory/'manifest.json').write_text(json.dumps(state))
        return directory

    def test_dump_stream_publishes_only_after_success_and_authenticates(self):
        archive=self.root/'dump.tnbk'
        postgres_backup_command(archive,self.key,[sys.executable,'-c',"import sys; sys.stdout.buffer.write(b'PGDMPsample')"],os.environ.copy())
        self.assertEqual(verify_archive(archive,self.key)['kind'],'postgresql')
        result=io.BytesIO()
        with archive.open('rb') as source: decrypt_stream(source,result,self.key)
        self.assertEqual(result.getvalue(),b'PGDMPsample')
        self.assertEqual(archive.stat().st_mode&0o777,0o600)

    def test_dump_failure_never_publishes_partial_encrypted_archive(self):
        archive=self.root/'failed.tnbk'
        with self.assertRaises(ArchiveError):
            postgres_backup_command(archive,self.key,[sys.executable,'-c',"import sys; sys.stdout.buffer.write(b'partial'); sys.exit(2)"],os.environ.copy())
        self.assertFalse(archive.exists())
        self.assertEqual(list(self.root.glob('.tagihnet-pg-backup-*')),[])

    def test_existing_archive_is_preserved_without_starting_dump(self):
        archive=self.encrypted()
        before=archive.read_bytes()
        with patch.object(stage,'database_container') as probe,self.assertRaises(ArchiveError):
            stage.backup(self.root,archive,self.key)
        probe.assert_not_called()
        self.assertEqual(archive.read_bytes(),before)

    def test_wrong_key_or_tampering_never_contacts_docker(self):
        archive=self.encrypted()
        other=self.root/'other-key.json'
        generate_key(other)
        with patch.object(stage,'docker') as docker,self.assertRaises(ArchiveError):
            stage.restore(self.root,archive,read_key(other),self.root/'absent-mfa')
        docker.assert_not_called()
        data=bytearray(archive.read_bytes());data[-1]^=1;archive.write_bytes(data)
        with patch.object(stage,'docker') as docker,self.assertRaises(ArchiveError):
            stage.restore(self.root,archive,self.key,self.root/'absent-mfa')
        docker.assert_not_called()

    def test_non_postgres_or_wrong_magic_never_contacts_docker(self):
        archive=self.encrypted(b'SQLite fixture',kind='sqlite')
        with patch.object(stage,'docker') as docker,self.assertRaises(ArchiveError):
            stage.restore(self.root,archive,self.key,self.root/'mfa')
        docker.assert_not_called()

    def test_mfa_file_permissions_format_and_symlink(self):
        path=self.root/'mfa'
        path.write_bytes(Fernet.generate_key());path.chmod(0o600)
        self.assertEqual(stage.mfa_key(path),path.read_bytes())
        path.chmod(0o644)
        with self.assertRaises(ArchiveError):stage.mfa_key(path)
        path.chmod(0o600);path.write_bytes(b'not-a-key')
        with self.assertRaises(ArchiveError):stage.mfa_key(path)
        link=self.root/'link';link.symlink_to(path)
        with self.assertRaises(OSError):stage.mfa_key(link)

    def test_source_or_initialized_target_refused(self):
        directory=self.target(initialized=True)
        with patch.object(stage,'docker') as docker,self.assertRaises(ArchiveError):stage.fresh_target(directory)
        docker.assert_not_called()

    def test_existing_volume_is_not_a_restore_destination(self):
        directory=self.target()
        with patch.object(stage,'docker',return_value=subprocess.CompletedProcess([],0,'[]','')),self.assertRaises(ArchiveError):
            stage.fresh_target(directory)

    def test_socket_or_permission_error_is_not_absence_of_volume(self):
        directory=self.target()
        with patch.object(stage,'docker',return_value=subprocess.CompletedProcess([],1,'','permission denied')),self.assertRaises(ArchiveError):
            stage.fresh_target(directory)

    def test_confirmed_absent_volume_is_allowed(self):
        directory=self.target()
        with patch.object(stage,'docker',return_value=subprocess.CompletedProcess([],1,'','no such volume')):
            self.assertFalse(stage.fresh_target(directory)['initialized'])

    def test_up_cannot_race_a_restore_in_the_same_directory(self):
        directory=self.target()
        with operation_lock(directory),patch('scripts.staging_lab.compose') as compose,self.assertRaises(RuntimeError):
            up(directory)
        compose.assert_not_called()

    def test_failed_or_interrupted_restore_cannot_be_started(self):
        directory=self.target(restore_state='failed')
        with patch('scripts.staging_lab.compose') as compose,self.assertRaises(RuntimeError):up(directory)
        compose.assert_not_called()

    def test_restored_up_starts_web_without_workers(self):
        directory=self.target(initialized=True,restore_state='restored',recovery_pending=True)
        volume=subprocess.CompletedProcess([],0,json.dumps([{'Labels':{'com.docker.compose.project':'tagihnet-stage-1234567890'}}]),'')
        with patch('scripts.staging_lab.docker',return_value=volume),patch('scripts.staging_lab.wait_ready'),patch('scripts.staging_lab.compose') as compose:
            up(directory)
        self.assertEqual(compose.call_args.args[1:],('up','--detach','--no-build','web'))

    def test_failed_volume_identity_is_denied(self):
        directory=self.target()
        result=subprocess.CompletedProcess([],0,'container-id','')
        with patch.object(stage,'compose',return_value=result),patch.object(stage,'docker',return_value=subprocess.CompletedProcess([],0,json.dumps([{'Config':{'Labels':{},'Image':stage.PG_IMAGE},'State':{'Running':True},'Mounts':[]}]),'')),self.assertRaises(ArchiveError):
            stage.database_container(directory)

    def test_resume_workers_cannot_race_a_restore_or_up(self):
        directory=self.target(initialized=True,restore_state='restored',recovery_pending=True)
        with operation_lock(directory),patch('scripts.staging_lab.compose') as compose,self.assertRaises(RuntimeError):
            resume_workers(directory)
        compose.assert_not_called()

    def test_explicit_resume_starts_workers_and_clears_recovery_hold(self):
        directory=self.target(initialized=True,restore_state='restored',recovery_pending=True)
        volume=subprocess.CompletedProcess([],0,json.dumps([{'Labels':{'com.docker.compose.project':'tagihnet-stage-1234567890'}}]),'')
        with patch('scripts.staging_lab.docker',return_value=volume),patch('scripts.staging_lab.wait_ready'),patch('scripts.staging_lab.compose') as compose:
            resume_workers(directory)
        self.assertFalse(json.loads((directory/'manifest.json').read_text())['recovery_pending'])
        self.assertEqual(compose.call_args.args[4:],('network-worker','billing-worker','reminder-worker'))

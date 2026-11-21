"""Read-only SQLite0014→0015 reconciliation, including automatic Django permission metadata."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from core.backup_archive import atomic_output
from scripts.plan_archive_migration_check import row_digest

MODELS={'invoiceissuersnapshot','paymentissuersnapshot'}
NEW_TABLES={'core_'+name for name in MODELS}


def check(database,baseline,*,models=MODELS,migration='0015_document_issuers'):
    previous=json.loads(Path(baseline).read_text())['development'];added={}
    new_tables={'core_'+name for name in models}
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert tables==set(previous['database'])|new_tables
        for table in new_tables:
            assert table.replace('_','').isalnum()
            assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0,'Unexpected seeded records'
        for table,before in previous['database'].items():
            assert table.replace('_','').isalnum()
            columns=[r[1] for r in db.execute('PRAGMA table_info("'+table+'")')]
            assert columns==before['columns'],'Old columns changed'
            pk=next(r[1] for r in db.execute('PRAGMA table_info("'+table+'")') if r[5])
            rows=db.execute('SELECT * FROM "'+table+'" ORDER BY "'+pk+'"').fetchall()
            after={str(r[columns.index(pk)]):row_digest(r) for r in rows}
            assert all(after.get(k)==v for k,v in before['rows'].items()),'Old row changed: '+table
            new=set(after)-set(before['rows'])
            if table=='django_migrations':
                assert len(new)==1
                assert db.execute('SELECT app,name FROM django_migrations WHERE id=?',(next(iter(new)),)).fetchone()==('core',migration)
            elif table=='django_content_type':
                assert len(new)==len(models)
                actual={db.execute('SELECT app_label,model FROM django_content_type WHERE id=?',(pk,)).fetchone() for pk in new}
                assert actual=={('core',name) for name in models}
            elif table=='auth_permission':
                assert len(new)==4*len(models)
                expected={(name,f'{verb}_{name}') for name in models for verb in ('add','change','delete','view')}
                actual={db.execute('SELECT c.model,p.codename FROM auth_permission p JOIN django_content_type c ON c.id=p.content_type_id WHERE p.id=?',(pk,)).fetchone() for pk in new}
                assert actual==expected
            else:assert not new,'Unexpected rows added: '+table
            added[table]=len(new)
        sequences=dict(db.execute('SELECT name,seq FROM sqlite_sequence'))
        for name,seq in previous['sequences'].items():
            assert sequences[name]==seq+added.get(name,0),'Sequence changed: '+name
        assert set(sequences)-set(previous['sequences'])<=new_tables
        assert all(sequences[name]==0 for name in set(sequences)-set(previous['sequences']))
    issuer=migration=='0015_document_issuers'
    return {'status':'passed','scope':'schema0014_to0015_sqlite' if issuer else migration,'old_rows_and_columns_unchanged':True,
        ('new_issuer_tables_empty' if issuer else 'new_tables_empty'):len(new_tables),
        'new_content_types':len(models),'new_permissions':len(models)*4,
        'foreign_keys':'ok','old_sequences_preserved_except_expected_metadata':True,'production_ready':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,required=True);p.add_argument('--baseline',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    try:
        result=check(a.database,a.baseline)
        with atomic_output(a.output) as stream:stream.write(json.dumps(result,sort_keys=True,indent=2).encode())
        print(json.dumps(result,sort_keys=True))
    except Exception as error:p.exit(1,'Document issuer migration reconciliation refused: '+type(error).__name__+'; no row data/keys printed.\n')


if __name__=='__main__':main()

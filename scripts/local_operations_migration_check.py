"""Read-only SQLite0013→0014 reconciliation, including automatic Django permission metadata."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from core.backup_archive import atomic_output
from scripts.plan_archive_migration_check import row_digest

MODELS={'fund','paymentfunding','cashmovement','cashcorrection','cashclose','employee','salaryterms',
    'payrollrun','payrollline','campaign','lead','commission','hotspotprofile','voucherbatch','voucher',
    'networknode','customerendpoint','businessaction'}
NEW_TABLES={'core_'+name for name in MODELS}


def check(database,baseline):
    previous=json.loads(Path(baseline).read_text())['development'];added={}
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert tables==set(previous['database'])|NEW_TABLES
        for table in NEW_TABLES:assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0,'Unexpected seeded records'
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
                assert db.execute('SELECT app,name FROM django_migrations WHERE id=?',(next(iter(new)),)).fetchone()==('core','0014_local_operations')
            elif table=='django_content_type':
                assert len(new)==len(MODELS)
                actual={db.execute('SELECT app_label,model FROM django_content_type WHERE id=?',(pk,)).fetchone() for pk in new}
                assert actual=={('core',name) for name in MODELS}
            elif table=='auth_permission':
                assert len(new)==4*len(MODELS)
                expected={(name,f'{verb}_{name}') for name in MODELS for verb in ('add','change','delete','view')}
                actual={db.execute('SELECT c.model,p.codename FROM auth_permission p JOIN django_content_type c ON c.id=p.content_type_id WHERE p.id=?',(pk,)).fetchone() for pk in new}
                assert actual==expected
            else:assert not new,'Unexpected rows added: '+table
            added[table]=len(new)
        sequences=dict(db.execute('SELECT name,seq FROM sqlite_sequence'))
        for name,seq in previous['sequences'].items():
            assert sequences[name]==seq+added.get(name,0),'Sequence changed: '+name
        assert set(sequences)-set(previous['sequences'])<=NEW_TABLES
        assert all(sequences[name]==0 for name in set(sequences)-set(previous['sequences']))
    return {'status':'passed','scope':'schema0013_to0014_sqlite','old_rows_and_columns_unchanged':True,
        'new_business_tables_empty':len(NEW_TABLES),'new_content_types':len(MODELS),'new_permissions':len(MODELS)*4,
        'foreign_keys':'ok','old_sequences_preserved_except_expected_metadata':True,'production_ready':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,required=True);p.add_argument('--baseline',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    try:
        result=check(a.database,a.baseline)
        with atomic_output(a.output) as stream:stream.write(json.dumps(result,sort_keys=True,indent=2).encode())
        print(json.dumps(result,sort_keys=True))
    except Exception as error:p.exit(1,'Local operations migration reconciliation refused: '+type(error).__name__+'; no row data/keys printed.\n')


if __name__=='__main__':main()

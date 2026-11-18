"""Read-only reconciliation of retained SQLite schema0011 rows after plan availability migration0012."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from core.backup_archive import atomic_output


def row_digest(value):
    return hashlib.sha256(json.dumps(value,default=lambda item:item.hex(),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def check(database,baseline,sequences):
    import sqlite3
    old=json.loads(Path(baseline).read_text())['development']['database']
    old_sequences=json.loads(Path(sequences).read_text())
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert tables==set(old), 'Unexpected table set'
        for table,before in old.items():
            assert table.replace('_','').isalnum()
            columns=[r[1] for r in db.execute('PRAGMA table_info("'+table+'")')]
            assert set(columns)==set(before['columns'])|({'is_active'} if table=='core_plan' else set())
            pk=next(r[1] for r in db.execute('PRAGMA table_info("'+table+'")') if r[5])
            names=before['columns'];assert all(name.replace('_','').isalnum() for name in names)
            rows=db.execute('SELECT '+','.join('"'+name+'"' for name in names)+' FROM "'+table+'" ORDER BY "'+pk+'"').fetchall()
            after={str(row[names.index(pk)]):row_digest(row) for row in rows}
            if table=='django_migrations':
                assert all(after.get(key)==value for key,value in before['rows'].items())
                new=set(after)-set(before['rows']);assert len(new)==1
                assert db.execute('SELECT app,name FROM django_migrations WHERE id=?',(next(iter(new)),)).fetchone()==('core','0012_plan_availability')
            else:assert after==before['rows'], 'Old row changed: '+table
        assert db.execute('SELECT count(*) FROM core_plan WHERE is_active IS NOT 1').fetchone()[0]==0
        after_sequences=dict(db.execute('SELECT name,seq FROM sqlite_sequence'))
        expected={**old_sequences,'django_migrations':old_sequences['django_migrations']+1}
        assert after_sequences==expected, 'Unexpected sequence high-water change'
        plans=db.execute('SELECT count(*) FROM core_plan').fetchone()[0]
    return {'status':'passed','scope':'schema0011_to0012_sqlite','old_rows_unchanged':True,
        'existing_plans_active':plans,'foreign_keys':'ok','sequences_preserved_except_new_migration':True,'production_ready':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--sequences-before',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():raise ValueError('Output exists')
        result=check(args.database,args.baseline,args.sequences_before)
        with atomic_output(args.output) as stream:stream.write(json.dumps(result,sort_keys=True,indent=2).encode())
        print(json.dumps(result,sort_keys=True))
    except Exception as error:parser.exit(1,'Migration reconciliation refused: '+type(error).__name__+'; no row data/keys printed.\n')


if __name__=='__main__':main()

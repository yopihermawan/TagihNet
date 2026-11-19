"""Read-only reconciliation of retained SQLite0012 values after coordinate migration0013."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from core.backup_archive import atomic_output
from scripts.plan_archive_migration_check import row_digest


def check(database,baseline):
    previous=json.loads(Path(baseline).read_text())['development']
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert tables==set(previous['database'])
        for table,before in previous['database'].items():
            assert table.replace('_','').isalnum()
            columns=[r[1] for r in db.execute('PRAGMA table_info("'+table+'")')]
            additions={'latitude','longitude'} if table in {'core_customer','core_installation'} else set()
            assert set(columns)==set(before['columns'])|additions
            pk=next(r[1] for r in db.execute('PRAGMA table_info("'+table+'")') if r[5])
            names=before['columns'];assert all(n.replace('_','').isalnum() for n in names)
            rows=db.execute('SELECT '+','.join('"'+n+'"' for n in names)+' FROM "'+table+'" ORDER BY "'+pk+'"').fetchall()
            after={str(row[names.index(pk)]):row_digest(row) for row in rows}
            if table=='django_migrations':
                assert all(after.get(k)==v for k,v in before['rows'].items())
                new=set(after)-set(before['rows']);assert len(new)==1
                assert db.execute('SELECT app,name FROM django_migrations WHERE id=?',(next(iter(new)),)).fetchone()==('core','0013_location_coordinates')
            else:assert after==before['rows'],'Old row changed: '+table
            if additions:
                assert not db.execute('SELECT count(*) FROM "'+table+'" WHERE latitude IS NOT NULL OR longitude IS NOT NULL').fetchone()[0]
        assert dict(db.execute('SELECT name,seq FROM sqlite_sequence'))=={
            **previous['sequences'],'django_migrations':previous['sequences']['django_migrations']+1}
        customers=db.execute('SELECT count(*) FROM core_customer').fetchone()[0]
    return {'status':'passed','scope':'schema0012_to0013_sqlite','old_rows_unchanged':True,
        'existing_customers_without_invented_coordinates':customers,'foreign_keys':'ok',
        'sequences_preserved_except_new_migration':True,'production_ready':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():raise ValueError('Output exists')
        result=check(args.database,args.baseline)
        with atomic_output(args.output) as stream:stream.write(json.dumps(result,sort_keys=True,indent=2).encode())
        print(json.dumps(result,sort_keys=True))
    except Exception as error:parser.exit(1,'Location migration reconciliation refused: '+type(error).__name__+'; no row data/keys printed.\n')


if __name__=='__main__':main()

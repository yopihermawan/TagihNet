"""Hash-only reconciliation for the additive document-issuer PostgreSQL migration."""
import json
from scripts.document_issuer_migration_check import MODELS


def snapshot(directory):
    from scripts.verify_staging_backup import shell
    code='''
import hashlib,json
from django.db import connection
from psycopg import sql
result={'tables':{},'sequences':{}}
with connection.cursor() as c:
    c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    tables=[r[0] for r in c.fetchall()]
    for table in tables:
        c.execute("SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) WHERE i.indrelid=%s::regclass AND i.indisprimary",[table])
        pk=c.fetchone()[0]
        c.execute(sql.SQL('SELECT * FROM {} LIMIT 0').format(sql.Identifier(table)))
        columns=[v.name for v in c.description]
        c.execute(sql.SQL('SELECT {},row_to_json(t)::text FROM {} t ORDER BY {}').format(sql.Identifier(pk),sql.Identifier(table),sql.Identifier(pk)))
        rows={str(pk):hashlib.sha256(text.encode()).hexdigest() for pk,text in c.fetchall()}
        result['tables'][table]={'columns':columns,'rows':rows}
    c.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename")
    for name, in c.fetchall():
        c.execute(sql.SQL('SELECT last_value,is_called FROM {}').format(sql.Identifier(name)))
        result['sequences'][name]=list(c.fetchone())
print('RAW:'+json.dumps(result,sort_keys=True))
'''
    return json.loads(next(line.removeprefix('RAW:') for line in shell(directory,code).splitlines() if line.startswith('RAW:')))


def reconcile(directory,old,*,models=MODELS,migration='0015_document_issuers'):
    from scripts.verify_staging_backup import shell
    new_tables={'core_'+name for name in models}
    current=snapshot(directory)
    assert set(current['tables'])==set(old['tables'])|new_tables
    assert all(not current['tables'][name]['rows'] for name in new_tables)
    changes={}
    for table,before in old['tables'].items():
        after=current['tables'][table]
        assert before['columns']==after['columns'],'Old columns changed'
        assert all(after['rows'].get(pk)==value for pk,value in before['rows'].items()),'Old PostgreSQL row changed: '+table
        additions=set(after['rows'])-set(before['rows'])
        expected={'django_migrations':1,'django_content_type':len(models),'auth_permission':4*len(models)}.get(table,0)
        assert len(additions)==expected,'Unexpected PostgreSQL rows: '+table
        changes[table]=len(additions)
    for sequence,before in old['sequences'].items():
        extra=changes.get(sequence.removesuffix('_id_seq'),0)
        assert current['sequences'][sequence]==([before[0]+extra,True] if extra else before),'Old sequence changed: '+sequence
    new_sequences=set(current['sequences'])-set(old['sequences'])
    assert new_sequences=={table+'_id_seq' for table in new_tables}
    assert all(current['sequences'][name]==[1,False] for name in new_sequences)
    # Exact metadata identity, rather than just matching additional row counts.
    ids={table:sorted(set(current['tables'][table]['rows'])-set(old['tables'][table]['rows'])) for table in ('django_migrations','django_content_type','auth_permission')}
    code=f'''
from django.db import connection
ids={ids!r};models={sorted(models)!r};migration={migration!r}
with connection.cursor() as c:
    c.execute('SELECT app,name FROM django_migrations WHERE id=ANY(%s)',[[int(v) for v in ids['django_migrations']]])
    assert c.fetchall()==[('core',migration)]
    c.execute('SELECT app_label,model FROM django_content_type WHERE id=ANY(%s)',[[int(v) for v in ids['django_content_type']]])
    assert set(c.fetchall())=={{('core',name) for name in models}}
    c.execute('SELECT c.app_label,c.model,p.codename FROM auth_permission p JOIN django_content_type c ON c.id=p.content_type_id WHERE p.id=ANY(%s)',[[int(v) for v in ids['auth_permission']]])
    assert set(c.fetchall())=={{('core',name,verb+'_'+name) for name in models for verb in ('add','change','delete','view')}}
print('PASS: exact new metadata identities')
'''
    assert 'PASS:' in shell(directory,code)
    table_key='new_issuer_tables_empty' if migration=='0015_document_issuers' else 'new_tables_empty'
    return {'status':'passed','old_rows_and_columns_unchanged':True,table_key:len(new_tables),
        'expected_django_metadata_additions':True,'old_sequences_preserved_except_expected_metadata':True,'production_ready':False}

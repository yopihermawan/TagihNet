"""Optional process-local loop progress. Never writes business data or raises I/O failures."""
import json
import os
import tempfile
import time
from pathlib import Path

ROLES={'network','billing','reminder'}
STATES={'busy','ok','degraded','stopping'}


def heartbeat(role,state='ok'):
    if role not in ROLES or state not in STATES:
        raise ValueError('Heartbeat role/state tidak valid.')
    directory=os.getenv('TAGIHNET_HEARTBEAT_DIR')
    if not directory:return None
    temporary=None
    try:
        root=Path(directory)
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        data={'version':1,'role':role,'state':state,'updated_at':time.time(),'pid':os.getpid()}
        with tempfile.NamedTemporaryFile(mode='w',prefix='.'+role+'-',dir=root,delete=False) as stream:
            temporary=Path(stream.name)
            json.dump(data,stream)
            stream.flush()
            os.replace(temporary,root/(role+'.json'))
        return True
    except OSError:
        return False
    finally:
        if temporary:
            try:temporary.unlink(missing_ok=True)
            except OSError:pass

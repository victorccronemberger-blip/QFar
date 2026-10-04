"""Serialize a recording's protocol across local callers, not across accounts."""
from contextlib import ExitStack
from functools import wraps
import hashlib
import inspect

from . import config
from .operation_lease import operation_lease, OperationLeaseError

def _path(sid):
    return config.DATA_DIR / 'upload-session-leases' / (hashlib.sha256(sid.encode('utf8')).hexdigest()+'.lock')

def session_protocol(fn):
    signature=inspect.signature(fn)
    @wraps(fn)
    def guarded(*args,**kwargs):
        from .upload import _sidecar_filename, UploadError
        import uuid
        bound=signature.bind(*args,**kwargs)
        sid=bound.arguments.get('session_id')
        if sid is None:
            sid=str(uuid.uuid4())
            bound.arguments['session_id']=sid
        _sidecar_filename(sid)
        try:
            with operation_lease(_path(sid)):
                return fn(*bound.args,**bound.kwargs)
        except OperationLeaseError:
            raise UploadError('A sessão já possui uma operação local em andamento.',transient=True,phase='preflight') from None
    return guarded

def pending_protocol(fn):
    signature=inspect.signature(fn)
    @wraps(fn)
    def guarded(*args,**kwargs):
        from .upload import list_sidecars, UploadError
        bound=signature.bind(*args,**kwargs)
        arguments=bound.arguments
        session=arguments['session']
        owner=arguments.get('account_email') or getattr(session,'email',None)
        if owner is not None and (not isinstance(owner,str) or not owner.strip()):
            raise UploadError('Conta de retomada inválida.',phase='preflight')
        org=arguments.get('required_org_key');selected=arguments.get('session_ids')
        rows=list_sidecars()
        sids=sorted({row['session_id'] for row in rows
            if (owner is None or (isinstance(row.get('account_email'),str) and row['account_email'].strip().casefold()==owner.strip().casefold()))
            and (org is None or row.get('org_key')==org)
            and (selected is None or row.get('session_id') in selected)})
        try:
            with ExitStack() as stack:
                for sid in sids:
                    if not isinstance(sid,str):
                        raise UploadError('Identidade de retomada inválida.',phase='preflight')
                    stack.enter_context(operation_lease(_path(sid)))
                return fn(*args,**kwargs)
        except OperationLeaseError:
            raise UploadError('Uma sessão selecionada está em uso; preserve a retomada.',transient=True,phase='preflight') from None
    return guarded

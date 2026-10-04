"""Authoritative start tombstones. No TTL eviction or implicit reset.

A claim precedes Runner.start. Lost acknowledgement remains reviewable after
restart, never a permission to run again. Previews and running threads are not
reconstituted here; remote/local commits remain the journal's responsibility.
"""
import hashlib
import json
import re
from copy import deepcopy

from . import config
from .atomic_io import load_json_state, save_json, decode_json_state
from .operation_lease import operation_lease
from .media_lifecycle import media_state_lease

_ID = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_LIMIT = 8192
_REPLY_KEYS = {'ok','already_running','start_request_id','preflight_id','receipt','account_email','task_id',
               'total_sends','accounts','selected_tasks','original_summary','removed_accounts','dataset','skipped_accounts'}

class StartStoreError(ValueError):
    pass
class StartConflictError(StartStoreError):
    pass

def _path():
    # Never match the established campaign_*.json immutable history namespace.
    return config.DATA_DIR / 'start_requests.json'

def request_digest(body):
    try:
        raw=json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
        decode_json_state(raw)
        return hashlib.sha256(raw).hexdigest()
    except (TypeError,ValueError,OverflowError):
        raise StartStoreError('Registro de início inválido; preserve o estado local.') from None

def _read(value=None):
    try:
        if (config.DATA_DIR/'campaign_start_requests.json').exists():
            # An unpublished candidate used the history namespace. It cannot
            # be silently treated as no tombstones or automatically removed.
            raise ValueError
        store=load_json_state(_path(), {'version':1,'requests':{}}) if value is None else value
        if (type(store) is not dict or type(store.get('version')) is not int or store['version'] != 1
                or type(store.get('requests')) is not dict or len(store['requests']) > _LIMIT):
            raise ValueError
        for uid,row in store['requests'].items():
            if (not isinstance(uid,str) or not _ID.fullmatch(uid) or type(row) is not dict
                    or row.get('start_request_id') != uid or row.get('kind') not in {'original','dataset'}
                    or row.get('phase') not in {'claimed','admitted'}
                    or not isinstance(row.get('request_digest'),str) or not _HASH.fullmatch(row['request_digest'])
                    or not isinstance(row.get('receipt_id'),str) or not _ID.fullmatch(row['receipt_id'])
                    or type(row.get('bindings')) is not dict or type(row.get('protected_assets')) is not list):
                raise ValueError
            bindings=row['bindings']
            if (type(bindings.get('accounts')) is not list or not bindings['accounts']
                    or any(type(a) is not dict or set(a) != {'email','org_key'}
                           or any(not isinstance(v,str) or not v.strip() for v in a.values()) for a in bindings['accounts'])
                    or type(bindings.get('tasks')) is not list or not bindings['tasks']
                    or any(not isinstance(t,str) or not _ID.fullmatch(t) for t in bindings['tasks'])):
                raise ValueError
            for asset in row['protected_assets']:
                if (type(asset) is not dict or set(asset) != {'path','sha256'}
                        or not isinstance(asset['path'],str) or not asset['path']
                        or not isinstance(asset['sha256'],str) or not _HASH.fullmatch(asset['sha256'])):
                    raise ValueError
            if row['phase'] == 'admitted':
                reply=row.get('reply')
                if (type(reply) is not dict or reply.get('ok') is not True
                        or set(reply)-_REPLY_KEYS
                        or reply.get('start_request_id') != uid
                        or type(reply.get('total_sends')) is not int or reply['total_sends'] < 1):
                    raise ValueError
        return store
    except (OSError,ValueError,TypeError):
        raise StartStoreError('Registro de início ilegível; preserve os dados para revisão.') from None

def _validate_request(uid,kind,receipt_id,body,row=None):
    if not isinstance(uid,str) or not _ID.fullmatch(uid) or kind not in {'original','dataset'}:
        raise StartStoreError('Identidade de início inválida.')
    digest=request_digest(body)
    if row is not None and (row['kind'] != kind or row['receipt_id'] != receipt_id or row['request_digest'] != digest):
        raise StartConflictError('O identificador de início já pertence a outra operação.')
    return digest

def lookup(uid, *, kind=None, receipt_id=None, body=None):
    if not isinstance(uid,str) or not _ID.fullmatch(uid):
        raise StartStoreError('Identidade de início inválida.')
    with operation_lease(_path().with_suffix('.lock')):
        row=_read()['requests'].get(uid)
        if row is not None and body is not None:
            _validate_request(uid,kind,receipt_id,body,row)
        return deepcopy(row)

def claim(uid, *, kind, receipt_id, body, bindings, protected_assets=None):
    with media_state_lease(wait=True), operation_lease(_path().with_suffix('.lock')):
        store=_read();prior=store['requests'].get(uid)
        digest=_validate_request(uid,kind,receipt_id,body,prior)
        if prior is not None:
            return False,deepcopy(prior)
        if len(store['requests']) >= _LIMIT:
            raise StartStoreError('O registro de início exige revisão antes de novos pedidos.')
        row={'start_request_id':uid,'kind':kind,'receipt_id':receipt_id,'request_digest':digest,
             'phase':'claimed','bindings':deepcopy(bindings),'protected_assets':deepcopy(protected_assets or [])}
        store['requests'][uid]=row
        # Validate the prospective structure before publishing; no fallback on
        # corrupt stores, even if the requested UUID has never been seen.
        if not isinstance(receipt_id,str) or not _ID.fullmatch(receipt_id):
            raise StartStoreError('Identidade de início inválida.')
        request_digest(store)
        _read(store)
        save_json(_path(),store)
        _read()
        return True,deepcopy(row)

def acknowledge(uid, reply):
    with operation_lease(_path().with_suffix('.lock')):
        store=_read();row=store['requests'].get(uid)
        if row is None or row['phase'] != 'claimed':
            raise StartStoreError('O início não possui reserva válida.')
        request_digest(reply)
        if (type(reply) is not dict or set(reply)-_REPLY_KEYS
                or reply.get('ok') is not True or reply.get('start_request_id') != uid
                or type(reply.get('total_sends')) is not int or reply['total_sends'] < 1):
            raise StartStoreError('O início não possui confirmação válida.')
        store['requests'][uid]={**row,'phase':'admitted','reply':deepcopy(reply)}
        _read(store)
        save_json(_path(),store)

def protected_paths():
    with operation_lease(_path().with_suffix('.lock')):
        return [asset['path'] for row in _read()['requests'].values() for asset in row['protected_assets']]

def public_status(uid):
    row=lookup(uid)
    if row is None:
        return {'ok':True,'start_request_id':uid,'status':'not_found','found':False,'may_start':False}
    reply=row.get('reply')
    return {'ok':True,'start_request_id':uid,'found':True,'kind':row['kind'],
            'status':'admitted' if row['phase']=='admitted' else 'review', 'may_start':False,
            'receipt':row['receipt_id'],'preflight_id':row['receipt_id'],
            'accounts':[a['email'] for a in row['bindings']['accounts']],
            'task_ids':row['bindings']['tasks'], 'outcome_unknown':row['phase']=='claimed',
            'reply':deepcopy(reply) if reply is not None else None}

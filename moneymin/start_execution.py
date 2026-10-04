"""Conservative UUID/history correlation; admission is not delivery evidence."""
from . import config, recovery
from .atomic_io import load_json_state
from .upload_types import journal_delivery_confirmed

def execution_evidence(row, *, running_id=None, thread_running=False):
    result={'execution_state':'review','terminal':False,'log_name':None,'delivery_confirmed':False}
    uid=row['start_request_id']
    if thread_running and running_id==uid:
        return {**result,'execution_state':'running'}
    try:
        matches=[]
        for path in sorted(config.DATA_DIR.glob('campaign_*.json')):
            if path.is_symlink() or path.resolve().parent != config.DATA_DIR.resolve():
                return result
            data=load_json_state(path,None)
            if type(data) is not dict:
                return result
            if data.get('start_request_id')==uid:
                matches.append((path,data))
        if len(matches)!=1:
            return result
        path,log=matches[0]
        state=log.get('status');bindings=row['bindings']
        expected_accounts={a['email'].strip().casefold():a['org_key'] for a in bindings['accounts']}
        actual_accounts=log.get('accounts')
        if (state not in {'done','partial','stopped','error'} or type(actual_accounts) is not list
                or any(not isinstance(a,str) or not a.strip() for a in actual_accounts)
                or len(actual_accounts)!=len(expected_accounts)
                or {a.strip().casefold() for a in actual_accounts} != set(expected_accounts)
                or type(log.get('items')) is not list):
            return result
        groups={rows[0]['session_id']:rows for rows in recovery._groups(include_reconciled=True)}
        known_original=bindings.get('session_id')
        for rows in groups.values():
            if (rows[0]['session_id']==known_original
                    or any(isinstance(r.get('campaign_context'),dict)
                           and r['campaign_context'].get('history_name')==path.name for r in rows)):
                if any(r.get('account_email','').strip().casefold() not in expected_accounts
                       or r.get('org_key') != expected_accounts.get(r.get('account_email','').strip().casefold())
                       or r.get('task_id') not in bindings['tasks'] for r in rows):
                    return result
        delivery=[]
        from .campaign_evidence import current_result
        for item in log['items']:
            if type(item) is not dict or item.get('task_id') not in bindings['tasks'] or type(item.get('accounts',[])) is not list:
                return result
            if item.get('org_key') is not None and item['org_key'] not in expected_accounts.values():
                return result
            for attempt in item.get('accounts',[]):
                if (type(attempt) is not dict or not isinstance(attempt.get('email'),str)
                        or attempt['email'].strip().casefold() not in expected_accounts
                        or any(type(attempt[k]) is not bool for k in ('ok','skipped','finalized')
                               if k in attempt and not(k=='finalized' and attempt[k] is None))):
                    return result
                if attempt.get('skipped') is True:
                    continue
                sid=attempt.get('session_id')
                if known_original is not None and sid not in (None,'',known_original):
                    return result
                rows=groups.get(sid,[])
                if rows and any(r.get('account_email','').strip().casefold()!=attempt['email'].strip().casefold()
                                or r.get('org_key')!=expected_accounts[attempt['email'].strip().casefold()]
                                or r.get('task_id')!=item['task_id'] for r in rows):
                    return result
                delivery.append(current_result(item,attempt,path.name,groups)['status']=='confirmed')
        return {'execution_state':state,'terminal':True,'log_name':path.name,
                'delivery_confirmed':bool(delivery) and all(delivery)}
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        return result

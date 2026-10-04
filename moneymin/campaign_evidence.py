"""Read-only current receipts, separate from an immutable campaign attempt."""
from __future__ import annotations

from typing import Any

from . import config, recovery
from .atomic_io import load_json_state
from .upload_types import journal_delivery_confirmed, journal_evaluation_confirmed


def publication_index() -> dict[str, list[tuple[dict, dict, str]]] | None:
    """Read immutable histories once; a receipt alone is not its publication.

    Unreadable or structurally ambiguous history cannot authorize hiding a
    confirmed receipt. Neither journals nor original histories are rewritten.
    """
    paths = set()
    try:
        for root in {config.DATA_DIR.resolve(), config.MEDIA_DATA_DIR.resolve()}:
            if root.exists():
                paths.update(root.glob('campaign_*.json'))
        result: dict[str, list[tuple[dict, dict, str]]] = {}
        for path in sorted(paths):
            history = load_json_state(path, None)
            if not isinstance(history, dict) or not isinstance(history.get('items', []), list):
                return None
            for item in history.get('items', []):
                if not isinstance(item, dict) or not isinstance(item.get('accounts', []), list):
                    return None
                for attempt in item.get('accounts', []):
                    if not isinstance(attempt, dict):
                        return None
                    if any(type(attempt[key]) is not bool for key in ('ok', 'skipped', 'recovered', 'finalized')
                           if key in attempt and not (key == 'finalized' and attempt[key] is None)):
                        return None
                    sid = attempt.get('session_id')
                    if isinstance(sid, str) and sid:
                        result.setdefault(sid, []).append((item, attempt, path.name))
        return result
    except (OSError, UnicodeError, ValueError):
        return None


def publication_registered(rows: list[dict], index: dict | None) -> bool:
    """Only one complete owner/task/clip/SID reference can hide the receipt."""
    if not rows or index is None:
        return False
    sid = rows[0].get('session_id')
    references = index.get(sid, [])
    context = rows[0].get('campaign_context')
    if isinstance(context, dict) and context.get('history_name') is not None:
        references = [entry for entry in references if entry[2] == context['history_name']]
    if len(references) != 1:
        return False
    item, attempt, name = references[0]
    return current_result(item, attempt, name, {sid: rows})['status'] == 'confirmed'


def current_groups() -> dict[str, list[dict[str, Any]]] | None:
    """Keep reconciled receipts: disappearing from the queue is not evidence."""
    try:
        return {rows[0]['session_id']: rows for rows in recovery._groups(include_reconciled=True)}
    except (OSError, ValueError):
        return None


def current_result(item: dict[str, Any], attempt: dict[str, Any], history_name: str | None,
                   groups: dict[str, list[dict[str, Any]]] | None) -> dict[str, Any]:
    """A preview, reset dedup index or a filtered subset never proves delivery."""
    def result(status, detail, *, source='none', reconciled=None, found=0, expected=None):
        return {'status': status, 'detail': detail, 'evidence_source': source,
                'reconciled': reconciled, 'chunks_found': found, 'chunks_expected': expected}

    if groups is None:
        return result('review', 'Não foi possível validar os registros atuais. A tentativa original foi preservada.')
    sid = attempt.get('session_id')
    rows = groups.get(sid, []) if isinstance(sid, str) else []
    if not rows:
        return result('unknown', 'Nenhum recibo atual vinculado a esta tentativa; consulte a confirmação original abaixo.')
    expected = rows[0].get('expected_chunk_count', 1)
    review = result('review', 'O recibo atual tem partes, identidade ou confirmação que precisam de revisão.',
                    source='journal', found=len(rows), expected=expected if type(expected) is int else None)
    email, org, task = attempt.get('email'), attempt.get('org_key'), item.get('task_id')
    registry = item.get('registry_key') or (
        f"minute|{task}|{item['task_name']}" if task and item.get('task_name') else item.get('task_scenario'))
    clip = item.get('clip_uid')
    if (not all(isinstance(value, str) and value for value in (email, org, task, registry, clip))
            or not recovery._complete_chunk_group(rows)):
        return review
    context = rows[0].get('campaign_context')
    if (not isinstance(context, dict) or context.get('registry_key') != registry
            or context.get('clip_uid') != clip or context.get('task_id') != task
            or (context.get('history_name') is not None and context['history_name'] != history_name)):
        return review
    if not all(row.get('account_email') == email and row.get('org_key') == org
               and row.get('session_id') == sid and row.get('task_id') == task
               and row.get('campaign_context') == context for row in rows):
        return review
    if any(not isinstance(row.get('upload_id'), str) or not row['upload_id'].strip() for row in rows):
        return review
    if any(row.get('phase') == 'evaluation_review' or row.get('state') in {'failed', 'quarantine'} for row in rows):
        return review
    if all(journal_delivery_confirmed(row) for row in rows):
        reconciled = all(row.get('campaign_reconciled') is True for row in rows)
        return result('confirmed', 'Finalização confirmada no recibo atual; a tentativa original permanece registrada.'
                      + (' O recibo registra reconciliação local.' if reconciled else ' Falta reconciliar o índice local.'),
                      source='journal', reconciled=reconciled, found=len(rows), expected=expected)
    if any(row.get('finalized') is True and not journal_evaluation_confirmed(row) for row in rows):
        return review
    return result('pending', 'O recibo atual ainda não confirma a finalização de todas as partes.',
                  source='journal', reconciled=False, found=len(rows), expected=expected)

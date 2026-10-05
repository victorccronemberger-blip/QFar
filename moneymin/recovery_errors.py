"""Safe, stable diagnostics for authoritative recovery state.

Only fixed descriptions and a hash of a basename may cross the local API.
Never serialize an exception, journal payload, account or absolute path.
"""
from hashlib import sha256
from pathlib import Path


_MESSAGES = {
    "store_unreadable": "Não foi possível abrir a pasta dos registros de envio.",
    "migration_marker": "O índice de migração sidecar_migration.json está inválido ou ilegível.",
    "migration_source": "Um registro antigo da biblioteca está inválido ou ilegível.",
    "journal_conflict": "Há registros diferentes para a mesma sessão na biblioteca e nesta instalação.",
    "archive_conflict": "Há arquivos de retomada diferentes na biblioteca e nesta instalação.",
    "journal_unreadable": "Um registro de envio está inválido ou ilegível.",
    "journal_identity": "Um registro de envio não corresponde à conta, sessão ou parte esperada.",
    "session_conflict": "Uma sessão de envio está associada a contas ou organizações diferentes.",
    "reset_history": "O histórico sent_reset_history.json está inválido ou ilegível.",
    "busy": "Os registros estão em uso por outra operação. Aguarde e clique em Tentar novamente.",
    "inspection_failed": "Não foi possível concluir a leitura dos registros de recuperação.",
}


class RecoveryReadError(ValueError):
    def __init__(self, code: str, record: Path | None = None):
        self.code = code if code in _MESSAGES else "inspection_failed"
        self.record_ref = sha256(record.name.encode("utf-8")).hexdigest()[:12] if record else None
        super().__init__(_MESSAGES[self.code] + " Os registros foram preservados; revise antes de continuar.")


def diagnostic(exc: Exception) -> dict:
    code = exc.code if isinstance(exc, RecoveryReadError) else "inspection_failed"
    result = {"code": code, "message": _MESSAGES[code]}
    if isinstance(exc, RecoveryReadError) and exc.record_ref:
        result["record_ref"] = exc.record_ref
    return result


def error_response(exc: Exception) -> dict:
    detail = diagnostic(exc)
    reference = f" · referência {detail['record_ref']}" if detail.get("record_ref") else ""
    return {"error_code": "recovery_read_failed", "recovery_error": detail,
            "error": f"{detail['message']} Código: {detail['code']}{reference}. "
                     "Preserve os registros; copie o diagnóstico para revisão."}

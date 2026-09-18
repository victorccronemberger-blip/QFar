"""Diagnósticos de conta seguros para a UI, sem propagar respostas com segredos."""
from __future__ import annotations

import re


def account_issue(email: str, error: Exception, *, stage: str = "Validação do acesso") -> dict:
    raw = str(error).casefold()
    code = "unknown"
    reason = "Não foi possível concluir a verificação desta conta."
    action = "Não remova a conta por este diagnóstico. Verifique novamente; se persistir, copie o diagnóstico para o suporte."
    explicit = getattr(error, "account_issue_code", None)
    if explicit == "restricted":
        code, reason = "restricted", "A plataforma informou uma restrição na conta ou organização."
        action = "Consulte o estado no Minute e contate o suporte da plataforma. Trocar a senha não remove a restrição."
    elif explicit == "version":
        code, reason = "version", "A versão do aplicativo precisa ser atualizada."
        action = "Atualize o QMoney. Este diagnóstico não indica problema com a conta."
    elif explicit == "invalid_response" or any(x in raw for x in ("não-json", "resposta inválida", "perfil incompleto")):
        code, reason = "invalid_response", "O serviço devolveu uma resposta incompleta ou inválida."
        action = "Aguarde e verifique novamente. Não remova a conta nem altere a senha por esta resposta."
    elif explicit == "missing_access" or any(x in raw for x in ("nenhum acesso salvo", "sem token salvo", "token ilegível", "token vazio ou corrompido")):
        code, reason = "missing_access", "O acesso local está ausente ou não pode ser lido."
        action = "Em Contas, informe o mesmo e-mail e a senha do Minute e clique em Conectar. Não é necessário remover a conta."
    elif explicit == "organization" or "nenhuma organização" in raw:
        code, reason = "organization", "O login respondeu, mas a conta não está vinculada a uma organização."
        action = "Confira o vínculo da conta no Minute; se necessário, solicite a regularização ao suporte."
    elif explicit == "rate_limit" or re.search(r"\b429\b|too_many|too many|rate limit", raw):
        code, reason = "rate_limit", "O serviço limitou temporariamente as consultas."
        action = "Aguarde alguns minutos e verifique novamente. Não altere a senha por esse erro."
    elif explicit == "tls" or any(x in raw for x in ("certificate_verify", "certificate verify", "ssl", "tls")):
        code, reason = "tls", "Não foi possível validar a conexão segura com o serviço."
        action = "Confira data e hora do Windows e a instalação do QMoney. Não desative a validação de certificados."
    elif explicit == "timeout" or any(x in raw for x in ("timeout", "timed out", "tempo esgotado")) or re.search(r"\b408\b", raw):
        code, reason = "timeout", "O serviço não respondeu dentro do prazo."
        action = "Confira a conexão e tente verificar novamente. Isso não comprova que a senha está incorreta."
    elif explicit == "network" or any(x in raw for x in ("connection", "conexão", "resolve host", "getaddrinfo", "urlopen", "dns")):
        code, reason = "network", "Não foi possível alcançar o serviço."
        action = "Confira a conexão com a internet e verifique novamente. Não é necessário remover a conta."
    elif explicit == "service" or re.search(r"\b5\d{2}\b", raw):
        code, reason = "service", "O serviço remoto está temporariamente indisponível."
        action = "Aguarde e verifique novamente. Reautenticar a conta não corrige uma indisponibilidade do serviço."
    elif explicit == "forbidden" or re.search(r"\b403\b|forbidden", raw):
        code, reason = "forbidden", "O serviço recusou a permissão para esta operação."
        action = "Confira as permissões no Minute. Um HTTP 403 isolado não confirma conta desativada; não remova a conta por ele."
    elif explicit == "authentication" or re.search(r"\b401\b|invalid_login|invalid_password|invalid_grant|invalid_refresh_token|token_expired", raw):
        code, reason = "authentication", "O serviço não aceitou ou não conseguiu renovar o acesso salvo."
        action = "Em Contas, informe o mesmo e-mail e a senha do Minute e clique em Conectar; depois verifique novamente."
    # Só campos técnicos conhecidos: nunca devolver tokens, URLs assinadas,
    # senhas ou corpos arbitrários de exceções para a UI/área de transferência.
    http = re.search(r"\b(?:400|401|403|404|408|429|500|502|503|504)\b", raw)
    detail = type(error).__name__ + (f" · HTTP {http.group()}" if http else "")
    return dict(email=email, code=code, stage=stage, reason=reason, action=action, detail=detail,
                restriction_confirmed=explicit == "restricted",
                retryable=code in {"timeout", "network", "service", "rate_limit", "invalid_response"})


def issue_text(issue: dict) -> str:
    return f"{issue['email']}: {issue['reason']} {issue['action']}"

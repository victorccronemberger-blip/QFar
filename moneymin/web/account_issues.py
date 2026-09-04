"""Diagnósticos de conta seguros para a UI, sem propagar respostas com segredos."""
from __future__ import annotations

import re


def account_issue(email: str, error: Exception, *, stage: str = "Validação do acesso") -> dict:
    raw = str(error).casefold()
    code = "unknown"
    reason = "Não foi possível concluir a verificação desta conta."
    action = "Verifique novamente esta conta. Se persistir, copie este diagnóstico para o suporte."
    if any(x in raw for x in ("desativad", "disabled", "on_hold", "inactive")):
        code, reason = "restricted", "A plataforma informou uma restrição na conta ou organização."
        action = "Consulte o estado no Minute e contate o suporte da plataforma. Trocar a senha não remove a restrição."
    elif any(x in raw for x in ("nenhum acesso salvo", "sem token salvo", "token ilegível", "token vazio ou corrompido")):
        code, reason = "missing_access", "O acesso local está ausente ou não pode ser lido."
        action = "Em Contas, informe o mesmo e-mail e a senha do Minute e clique em Conectar. Não é necessário remover a conta."
    elif "nenhuma organização" in raw:
        code, reason = "organization", "O login respondeu, mas a conta não está vinculada a uma organização."
        action = "Confira o vínculo da conta no Minute; se necessário, solicite a regularização ao suporte."
    elif any(x in raw for x in ("429", "too_many", "too many", "rate limit")):
        code, reason = "rate_limit", "O serviço limitou temporariamente as consultas."
        action = "Aguarde alguns minutos e verifique novamente. Não altere a senha por esse erro."
    elif any(x in raw for x in ("certificate_verify", "certificate verify", "ssl", "tls")):
        code, reason = "tls", "Não foi possível validar a conexão segura com o serviço."
        action = "Confira data e hora do Windows e a instalação do QMoney. Não desative a validação de certificados."
    elif any(x in raw for x in ("timeout", "timed out", "tempo esgotado")):
        code, reason = "timeout", "O serviço não respondeu dentro do prazo."
        action = "Confira a conexão e tente verificar novamente. Isso não comprova que a senha está incorreta."
    elif any(x in raw for x in ("connection", "conexão", "resolve host", "getaddrinfo", "urlopen", "dns")):
        code, reason = "network", "Não foi possível alcançar o serviço."
        action = "Confira a conexão com a internet e verifique novamente. Não é necessário remover a conta."
    elif re.search(r"\b50[0234]\b", raw):
        code, reason = "service", "O serviço remoto está temporariamente indisponível."
        action = "Aguarde e verifique novamente. Reautenticar a conta não corrige uma indisponibilidade do serviço."
    elif "403" in raw or "forbidden" in raw:
        code, reason = "forbidden", "O serviço recusou a permissão para esta operação."
        action = "Confira as permissões e o estado desta conta no Minute. Se persistir, contate o suporte."
    elif any(x in raw for x in ("401", "invalid_login", "invalid_password", "invalid_grant", "token", "sessão inválida", "senha", "credential")):
        code, reason = "authentication", "O serviço não aceitou ou não conseguiu renovar o acesso salvo."
        action = "Em Contas, informe o mesmo e-mail e a senha do Minute e clique em Conectar; depois verifique novamente."
    # Só campos técnicos conhecidos: nunca devolver tokens, URLs assinadas,
    # senhas ou corpos arbitrários de exceções para a UI/área de transferência.
    http = re.search(r"\b(?:400|401|403|404|408|429|500|502|503|504)\b", raw)
    detail = type(error).__name__ + (f" · HTTP {http.group()}" if http else "")
    return dict(email=email, code=code, stage=stage, reason=reason, action=action, detail=detail)


def issue_text(issue: dict) -> str:
    return f"{issue['email']}: {issue['reason']} {issue['action']}"

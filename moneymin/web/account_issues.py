"""Diagnósticos de conta seguros para a UI, sem propagar respostas com segredos."""
from __future__ import annotations

import re


def account_issue(email: str, error: Exception, *, stage: str = "Validação do acesso") -> dict:
    raw = str(error).casefold()
    code = "unknown"
    reason = "Não foi possível concluir a verificação desta conta."
    action = "Não remova a conta por este diagnóstico. Verifique novamente; se persistir, copie o diagnóstico para o suporte."
    explicit = getattr(error, "account_issue_code", None)
    if isinstance(error, PermissionError):
        code, reason = "local_permission", "O Windows não permitiu atualizar o acesso local desta conta."
        action = "A conta permanece cadastrada. Continue com as contas aprovadas e verifique este acesso novamente depois."
    elif explicit == "app_check":
        code, reason = "app_check", "O serviço recusou a validação App Check desta instalação."
        action = "A integração precisa de uma configuração App Check autorizada pela plataforma. Reconectar a conta não remove essa exigência."
    elif explicit == "email_verification":
        code, reason = "email_verification", "A Crowtado exige verificação por e-mail."
        action = "Confira a integração da caixa de entrada para receber o código. Não altere a senha por este diagnóstico."
    elif explicit == "mail_authentication":
        code, reason = "mail_authentication", "A Hostinger recusou o token da caixa de e-mail desta conta."
        action = "Reconecte a caixa em Integrações → Hostinger com um token válido e consulte novamente. A senha Crowtado e o cadastro da conta foram preservados."
    elif explicit == "crowtado_account_missing":
        code, reason = "crowtado_account_missing", "A Crowtado não encontrou uma conta para este e-mail."
        action = "Confira o acesso no site da Crowtado. Uma conta conectada ao Minute não confirma cadastro na Crowtado."
    elif explicit == "restricted":
        code, reason = "restricted", "A plataforma informou uma restrição na conta ou organização."
        action = "Consulte o estado no Minute e contate o suporte da plataforma. Trocar a senha não remove a restrição."
        if "crowtado" in stage.casefold():
            reason = "A Crowtado confirmou uma restrição de acesso desta conta."
            action = "Confira a conta no site e contate o suporte da Crowtado. Trocar a senha não remove essa restrição."
    elif explicit == "version":
        code, reason = "version", "A versão do aplicativo precisa ser atualizada."
        action = "Atualize o QMoney. Este diagnóstico não indica problema com a conta."
    elif explicit == "device":
        code, reason = "device", "O serviço bloqueou este dispositivo."
        action = "Confira o estado do aparelho no Minute. Trocar a senha não remove o bloqueio de dispositivo."
    elif explicit == "uber_device":
        code, reason = "uber_device", "O serviço vinculou o acesso ao login Uber neste dispositivo."
        action = "Conclua o fluxo Uber no aplicativo Minute, se aplicável. Não remova a conta nem altere a senha por este diagnóstico."
    elif explicit == "policy":
        if "vpn" in raw:
            code, reason = "policy", "A operação foi bloqueada porque há VPN ativa neste Windows."
            action = "Desative a VPN (ou defina MINUTE_VPN_ENFORCE=0 só se souber o risco). Não remova a conta nem altere a senha."
        elif "localiza" in raw or "requires_location" in raw:
            code, reason = "policy", "O serviço exige localização do dispositivo para novos envios."
            action = "Configure coordenadas reais (MINUTE_DEVICE_LAT/LNG) apenas se forem verdadeiras. Não invente GPS."
        elif "geo_" in raw or "quota_exceeded" in raw or "autorização de gravação" in raw:
            code, reason = "policy", "A autorização geográfica ou de quota recusou novos envios."
            action = "Confira quota e elegibilidade no Minute. Não remova a conta nem altere a senha por esse diagnóstico."
        else:
            code, reason = "policy", "A operação não atende à política de integração ou gravação."
            action = "Confira as restrições informadas e a origem da gravação. Não remova a conta nem altere a senha por esse diagnóstico."
    elif explicit == "invalid_response" or any(x in raw for x in ("não-json", "resposta inválida", "perfil incompleto")):
        code, reason = "invalid_response", "O serviço devolveu uma resposta incompleta ou inválida."
        action = "Aguarde e verifique novamente. Não remova a conta nem altere a senha por esta resposta."
    elif explicit == "missing_access" or any(x in raw for x in ("nenhum acesso salvo", "sem token salvo", "token ilegível", "token vazio ou corrompido")):
        code, reason = "missing_access", "O acesso local está ausente ou não pode ser lido."
        action = "Em Contas, informe o mesmo e-mail e a senha do Minute e clique em Conectar. Não é necessário remover a conta."
        if "crowtado" in stage.casefold():
            reason = "A senha Crowtado não está disponível para verificar esta conta."
            action = "Use Conectar Crowtado e salve a senha deste serviço. O resultado do Minute permanece independente."
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
        if "app_version_too_old" in raw:
            code, reason = "version", "A versão do aplicativo precisa ser atualizada."
            action = "Atualize o QMoney. Este diagnóstico não indica problema com a conta."
        elif "uber-device" in raw:
            code, reason = "uber_device", "O serviço vinculou o acesso ao login Uber neste dispositivo."
            action = "Conclua o fluxo Uber no aplicativo Minute, se aplicável. Não remova a conta nem altere a senha por este diagnóstico."
        else:
            code, reason = "forbidden", "O serviço recusou a permissão para esta operação."
            action = "Confira as permissões no Minute. Um HTTP 403 isolado não confirma conta desativada; não remova a conta por ele."
    elif explicit == "authentication" or re.search(r"\b401\b|invalid_login|invalid_password|invalid_grant|invalid_refresh_token|token_expired", raw):
        code, reason = "authentication", "O serviço não aceitou ou não conseguiu renovar o acesso salvo."
        action = "Em Contas, informe o mesmo e-mail e a senha do Minute e clique em Conectar; depois verifique novamente."
        if "crowtado" in stage.casefold():
            action = "Use Conectar Crowtado na linha da conta e confira a senha da Crowtado. Não é necessário remover a conta do Minute."
    # Só campos técnicos conhecidos: nunca devolver tokens, URLs assinadas,
    # senhas ou corpos arbitrários de exceções para a UI/área de transferência.
    http = re.search(r"\b(?:400|401|403|404|408|429|500|502|503|504)\b", raw)
    status = getattr(error, "http_status", None)
    status = status if isinstance(status, int) and 400 <= status <= 599 else (http.group() if http else None)
    detail = type(error).__name__ + (f" · HTTP {status}" if status else "")
    if getattr(error, "profile_read", False) is True:
        detail += " · GET /api/v1/users/me"
    blocked = getattr(error, "blocked_reason", None)
    if blocked in ("user", "device", "uber-device"):
        detail += f" · X-Blocked-Reason: {blocked}"
    return dict(email=email, code=code, stage=stage, reason=reason, action=action, detail=detail,
                restriction_confirmed=explicit == "restricted",
                retryable=code in {"timeout", "network", "service", "rate_limit", "invalid_response"})


def issue_text(issue: dict) -> str:
    return f"{issue['email']}: {issue['reason']} {issue['action']}"

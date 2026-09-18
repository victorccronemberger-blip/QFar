"""Identidade para contas novas: nome, e-mail e senha sem layout único.

O lote antigo gerava sempre ``nome.sobrenome`` + 4 dígitos no mesmo domínio.
Isso forma um cluster óbvio. Aqui o local-part mistura concatenação, inicial,
underscore, hífen, ano com 2 dígitos e nomes compostos — nunca o padrão
``a.b####``.
"""
from __future__ import annotations

import random
import re
import secrets
import string
import unicodedata
from datetime import datetime
from typing import Any

NOMES_F = [
    "Ana", "Beatriz", "Camila", "Daniela", "Elisa", "Fernanda", "Gabriela",
    "Helena", "Isabela", "Juliana", "Karina", "Larissa", "Marina", "Natalia",
    "Olivia", "Patricia", "Rafaela", "Sofia", "Tatiana", "Vanessa", "Yasmin",
    "Ana Paula", "Maria Clara", "Luiza", "Amanda", "Priscila", "Bianca",
]
NOMES_M = [
    "Andre", "Bruno", "Caio", "Diego", "Eduardo", "Felipe", "Gabriel",
    "Henrique", "Igor", "Joao", "Lucas", "Marcos", "Nicolas", "Otavio",
    "Pedro", "Rafael", "Thiago", "Vitor", "Wagner", "Yuri",
    "Joao Pedro", "Luis", "Mateus", "Renato", "Alexandre", "Paulo",
]
SOBRENOMES = [
    "Almeida", "Araujo", "Barbosa", "Barros", "Campos", "Cardoso", "Carvalho",
    "Castro", "Costa", "Dias", "Duarte", "Fernandes", "Ferreira", "Freitas",
    "Gomes", "Lima", "Macedo", "Martins", "Mendes", "Moraes", "Moreira",
    "Nascimento", "Nogueira", "Oliveira", "Pereira", "Pinto", "Ribeiro",
    "Rocha", "Santana", "Santos", "Silva", "Soares", "Souza", "Teixeira",
    "Vieira",
]

_LOCAL_RE = re.compile(r"^[a-z][a-z0-9._-]{1,28}[a-z0-9]$")
_OLD_CLUSTER = re.compile(r"^[a-z]+\.[a-z]+\d{4}$")


def _ascii(value: str) -> str:
    stripped = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in stripped if unicodedata.category(ch) != "Mn")


def _tokens(value: str) -> list[str]:
    return [part for part in _ascii(value).lower().split() if part]


def _yy() -> str:
    return f"{random.randint(78, 99):02d}"


def _nn() -> str:
    return f"{random.randint(2, 89):02d}"


def _local_candidates(nome: str, sobrenome: str) -> list[str]:
    first = _tokens(nome)
    last = _tokens(sobrenome)
    if not first or not last:
        raise ValueError("nome e sobrenome são obrigatórios")
    f0, s0 = first[0], last[0]
    fi, si = f0[0], s0[0]
    joined_first = "".join(first)
    dotted_first = ".".join(first)
    out: list[str] = [
        f"{f0}.{s0}",
        f"{f0}_{s0}",
        f"{joined_first}{s0}",
        f"{fi}{s0}",
        f"{f0}.{si}",
        f"{s0}.{f0}",
        f"{f0}{_yy()}",
        f"{f0}.{s0}{_yy()}",
        f"{fi}.{s0}",
        f"{f0}-{s0}",
        f"{joined_first}{s0}{_nn()}",
        f"{s0}{fi}{_nn()}",
        f"{f0}{si}{_nn()}",
        f"{dotted_first}.{s0}",
        f"{fi}{s0}{_yy()}",
        f"{f0}.{s0}.{_yy()}",
    ]
    if len(first) > 1:
        out.extend((
            f"{fi}{first[1][0]}{s0}",
            f"{f0}.{first[1]}.{s0}",
            f"{f0}_{first[1]}_{s0}",
        ))
    unique: list[str] = []
    seen: set[str] = set()
    for item in out:
        item = re.sub(r"[._-]{2,}", ".", item).strip("._-")
        if item in seen or _OLD_CLUSTER.fullmatch(item):
            continue
        if not _LOCAL_RE.fullmatch(item):
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _birth() -> tuple[int, int]:
    """Mês e ano de nascimento — adultos 21–45 anos."""
    age = random.randint(21, 45)
    year = datetime.now().year - age
    month = random.randint(1, 12)
    return month, year


def gerar_senha() -> str:
    """Senha forte sem o prefixo fixo ``Mm…!`` do lote antigo."""
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(10))
    return (
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + body
        + secrets.choice("!@#$%")
        + str(secrets.randbelow(90) + 10)
    )


def gerar_identidade(*, domain: str, existentes: set[str] | None = None) -> dict[str, Any]:
    """Devolve nome, sobrenome, e-mail, senha, gênero e data de nascimento."""
    known = {item.lower() for item in (existentes or set())}
    host = domain.strip().lower().lstrip("@")
    if not host:
        raise ValueError("domínio vazio")
    birth_month, birth_year = _birth()
    for _ in range(80):
        if random.random() < 0.52:
            nome = random.choice(NOMES_F)
            gender = "female"
        else:
            nome = random.choice(NOMES_M)
            gender = "male"
        sobrenome = random.choice(SOBRENOMES)
        candidates = _local_candidates(nome, sobrenome)
        random.shuffle(candidates)
        for local in candidates:
            email = f"{local}@{host}"
            if email.lower() in known:
                continue
            return {
                "nome": nome,
                "sobrenome": sobrenome,
                "email": email,
                "senha": gerar_senha(),
                "gender": gender,
                "birth_month": birth_month,
                "birth_year": birth_year,
            }
    raise RuntimeError("não foi possível gerar um e-mail inédito")

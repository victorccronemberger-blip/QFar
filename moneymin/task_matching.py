"""Compatibilidade estrita entre tarefas Minute e clipes do Ego4D.

O cenário do Ego4D descreve o vídeo pai e é amplo demais para decidir sozinho.
Um clipe só é elegível quando também contém evidência temporizada da ação da
tarefa. A ausência dessa evidência é tratada como incompatibilidade (fail closed).

Higiene anti-ban (motivos publicados pelo Minute): gravar sentado, usar o
celular, suporte de peito, tripé, título da tarefa diferente da ação. Sem
prova da ação + sem esses sinais, o clipe não entra.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class TaskRule:
    primary: tuple[str, ...]
    evidence: tuple[tuple[str, ...], ...]
    min_evidence_groups: int | None = None
    unit_min_evidence_groups: int | None = None
    evidence_group_min_units: int = 1
    evidence_window: int | None = 4
    action_excluded: tuple[str, ...] = ()
    scenario_sufficient: tuple[str, ...] = ()
    supporting: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    min_span_s: float | None = None
    confidence: str = "high"


def _r(
    primary: str | tuple[str, ...],
    *evidence: tuple[str, ...],
    min_evidence_groups: int | None = None,
    unit_min_evidence_groups: int | None = None,
    evidence_group_min_units: int = 1,
    evidence_window: int | None = 4,
    action_excluded: tuple[str, ...] = (),
    scenario_sufficient: tuple[str, ...] = (),
    supporting: tuple[str, ...] = (),
    excluded: tuple[str, ...] = (),
    min_span_s: float | None = None,
) -> TaskRule:
    return TaskRule(
        primary=(primary,) if isinstance(primary, str) else primary,
        evidence=evidence,
        min_evidence_groups=min_evidence_groups,
        unit_min_evidence_groups=unit_min_evidence_groups,
        evidence_group_min_units=evidence_group_min_units,
        evidence_window=evidence_window,
        action_excluded=action_excluded,
        scenario_sufficient=scenario_sufficient,
        supporting=supporting,
        excluded=excluded,
        min_span_s=min_span_s,
    )


# Catálogo operacional: são as poucas tarefas para as quais há uma relação
# verificável entre o catálogo Minute e o conteúdo Ego4D disponível com IMU.
# Os termos abaixo são metadados humanos temporizados da ação visível; não são
# áudio, fala ou narração exigida do vídeo.
TASK_RULES: dict[str, TaskRule] = {
    "Change a tire": _r(
        "Getting car fixed",
        ("tire", "tyre", "wheel", "spare"),
        ("jack", "remove", "unscrew", "mount", "replace", "change", "lug nut"),
        ("car", "vehicle", "automobile", "trunk", "boot"),
    ),
    "Check & add engine oil": _r(
        "Getting car fixed",
        ("engine oil", "dipstick", "oil level", "oil cap"),
        ("check", "add", "pour", "fill", "top up", "remove", "insert"),
        ("car", "vehicle", "engine", "hood", "bonnet"),
    ),
    "Check tire pressure & add air": _r(
        "Getting car fixed",
        ("tire", "tyre", "wheel", "valve"),
        ("pressure", "gauge", "inflate", "air pump", "adds air", "pumps air"),
        ("car", "vehicle", "automobile"),
    ),
    "Cleaning Out Car": _r(
        "Car/scooter washing",
        ("interior", "inside", "dashboard", "seat", "floor mat", "car mat", "vacuum"),
        action_excluded=("exterior", "outside of the car", "car exterior",
                         "body of the car", "car body", "wheel", "tire", "tyre",
                         "roof", "hood", "bonnet"),
    ),
    "Car Wash & Detail": _r(
        "Car/scooter washing",
        ("wash", "scrub", "rinse", "soap", "sponge", "wipe", "clean"),
        ("car", "vehicle", "windshield", "wheel", "tire", "dashboard"),
    ),
    "Gardening": _r(
        "Gardening",
        ("plant", "water", "weed", "prun", "harvest", "garden", "soil", "flower", "seed", "pot",
         "dig", "fertiliz", "compost", "cultivat", "mow", "lawn", "grass cutter"),
    ),
    "Full Yard Maintenance": _r(
        ("Doing yardwork / shoveling snow", "Gardening"),
        ("mow", "lawnmower", "edge"),
        ("blower", "clipping", "sweep", "rake"),
        ("weed", "pulls grass", "removes grass"),
        ("water", "watering"),
        ("trim", "prun", "hedge", "shrub"),
        min_evidence_groups=3,
        unit_min_evidence_groups=1,
        evidence_group_min_units=5,
        evidence_window=None,
        min_span_s=300.0,
    ),
    "Pull weeds by hand": _r(
        "Gardening",
        ("pull", "pluck", "uproot", "remove", "dig out"),
        ("weed", "grass", "root", "unwanted plant"),
        action_excluded=("water", "spray", "sprayer", "trim", "prun",
                         "hedge", "shrub"),
    ),
    "Trim a hedge": _r(
        "Gardening",
        ("trim", "prun", "cut", "shear"),
        ("hedge", "shrub", "bush", "branch"),
        action_excluded=("weed", "water", "spray", "sprayer",
                         "plant pot", "flower pot"),
    ),
    "Patch & Paint Walls": _r(
        ("jobs related to construction", "Crafting/knitting/sewing/drawing/painting"),
        ("wall", "plaster", "drywall"),
        ("hole", "dent", "patch", "filler", "paint", "sandpaper", "primer"),
        action_excluded=("wood", "fence", "furniture", "wallpaper",
                         "wall scraping fluid"),
    ),
    "Gutter Cleaning": _r(
        ("Doing yardwork / shoveling snow", "Household cleaners", "jobs related to construction"),
        ("gutter", "downspout", "roof channel"),
        ("leaf", "leaves", "debris", "dirt", "clog"),
        ("clean", "clear", "remove", "flush", "rinse", "scoop"),
    ),
    "Hang Art & Mirrors": _r(
        ("Fixing something in the home", "jobs related to construction"),
        ("picture", "frame", "mirror", "art", "painting"),
        ("wall", "stud", "nail", "hook"),
        ("hang", "mount", "drill", "attach", "fix"),
    ),
    "Hang Curtains": _r(
        ("Fixing something in the home", "jobs related to construction"),
        ("curtain", "drape"),
        ("rod", "ring", "hook", "rail"),
        ("hang", "mount", "install", "thread", "attach"),
    ),
    "Holiday Decoration Setup": _r(
        ("Hosting a party", "Fixing something in the home"),
        ("decoration", "ornament", "christmas", "holiday", "garland", "lights"),
        ("unpack", "hang", "place", "arrange", "decorate", "set up", "setup"),
    ),
    "Replace showerhead": _r(
        "Fixing something in the home",
        ("shower", "showerhead", "aerator"),
        ("unscrew", "screw", "remove", "replace", "install", "tighten", "wrench"),
    ),
    "Tighten Cabinet & Door Hinges": _r(
        ("Carpenter", "Fixing something in the home"),
        ("hinge", "handle", "knob", "cabinet", "door"),
        ("tighten", "adjust", "screw", "screwdriver", "fix", "repair"),
    ),
    "Replace Bulbs & Batteries": _r(
        "Fixing something in the home",
        ("bulb", "battery", "batteries", "smoke detector", "remote", "lamp", "light"),
        ("replace", "remove", "install", "unscrew", "screw", "open"),
    ),
    "Furniture Assembly": _r(
        "Assembling furniture",
        ("assembl", "furniture", "shelf", "table", "chair", "cabinet", "drawer",
         "structure", "panel", "frame", "rack", "desk", "bed"),
        ("screw", "bolt", "piece", "part", "attach", "fit", "install", "assembl",
         "tighten", "join", "connect", "build", "construct"),
        # Sem scenario_sufficient: montar móvel sentado no chão é o ban
        # clássico. Exige evidência da ação; higiene recusa "sits/sitting".
    ),
    "Pet Care Routine": _r(
        ("Washing the dog / pet", "Playing with pets"),
        ("wash", "bath", "bathe", "brush", "comb", "groom", "clean", "feed",
         "food", "water", "restock", "litter"),
        ("dog", "pet", "cat", "horse", "animal", "cage", "kennel", "litter"),
    ),
    "Walk the Dog": _r(
        "Walking the dog / pet",
        ("dog", "pet", "leash"),
        ("walk", "walking", "leash", "street", "road", "path"),
        action_excluded=("stroller", "baby"),
    ),
    "Water Houseplants": _r(
        "Potting plants (indoor)",
        ("water", "watering", "pours water"),
        ("plant", "pot", "flower"),
    ),
    "Sweep the porch": _r(
        ("Cleaning / laundry", "Doing yardwork / shoveling snow"),
        ("sweep", "broom"),
        ("porch", "patio", "deck", "balcony", "veranda", "verandah", "terrace",
         "driveway", "compound", "entrance", "outside", "outdoor", "yard", "backyard"),
        action_excluded=("living room", "bedroom", "bathroom", "kitchen"),
    ),
    "Clean the Bathroom": _r(
        "Cleaning / laundry",
        ("bathroom", "toilet", "shower", "bathtub", "bath tub", "washbasin", "bathroom sink"),
        ("clean", "scrub", "wipe", "wash", "rinse"),
    ),
    "Clean Appliance": _r(
        "Cleaning / laundry",
        ("oven", "fridge", "refrigerator", "freezer", "microwave", "coffee maker",
         "washing machine", "washer", "dishwasher", "appliance", "air fryer",
         "cooker", "stove", "cooktop"),
        ("clean", "scrub", "wipe", "rinse", "descale"),
        action_excluded=("clothes", "cloths", "clothe", "laundry", "garment",
                         "shirt", "trouser", "linen", "sheet", "detergent"),
    ),
    "Loading the Laundry Machine": _r(
        "Cleaning / laundry",
        ("washer", "washing machine", "dryer"),
        ("in the washing machine", "into the washing machine", "in washer", "into washer",
         "in the dryer", "into the dryer", "loads", "loading", "transfers clothes"),
        ("clothes", "cloth", "laundry", "garment", "shirt", "trouser", "linen"),
        action_excluded=("from the washing machine", "out of the washing machine",
                         "from washer", "out of washer", "from the dryer",
                         "out of the dryer", "unloads", "unloading"),
    ),
    "Unloading the Laundry Machine": _r(
        "Cleaning / laundry",
        ("washer", "washing machine", "dryer"),
        ("from the washing machine", "out of the washing machine", "from washer",
         "out of washer", "from the dryer", "out of the dryer", "unloads", "unloading"),
        ("clothes", "cloth", "laundry", "garment", "shirt", "trouser", "linen"),
        action_excluded=("in the washing machine", "into the washing machine",
                         "in washer", "into washer", "in the dryer",
                         "into the dryer", "loads", "loading"),
    ),
    "Collecting Clothes Into a Hamper": _r(
        "Cleaning / laundry",
        ("hamper", "laundry basket", "clothes basket"),
        ("in the hamper", "into the hamper", "in the laundry basket",
         "into the laundry basket", "in the clothes basket", "into the clothes basket"),
        ("clothes", "cloth", "laundry", "garment", "shirt", "trouser", "linen"),
        action_excluded=("washing machine", "washer", "dryer",
                         "from the hamper", "out of the hamper",
                         "from the laundry basket", "out of the laundry basket",
                         "from the clothes basket", "out of the clothes basket"),
    ),
    "Hanging clothes on hangers": _r(
        "Cleaning / laundry",
        ("hanger", "hangs", "wardrobe", "closet", "clothes rack"),
        ("clothes", "cloth", "garment", "shirt", "dress", "jacket", "trouser"),
        ("hang ", "hangs ", "hanging ", "puts on hanger", "places on hanger",
         "arranges on the rod"),
    ),
    "Change Sheets & Make Bed": _r(
        "Cleaning / laundry",
        ("bed", "bedsheet", "bed sheet", "sheet", "duvet", "pillow", "blanket"),
        ("make", "change", "strip", "arrange", "cover", "fit", "put"),
        action_excluded=("bag", "couch", "sofa", "chair", "t-shirt", "tshirt"),
    ),
    "Stack firewood": _r(
        ("Farmer", "Doing yardwork / shoveling snow"),
        ("firewood", "log", "wood"),
        ("stack", "pile"),
    ),
    "Pack the Car for a Trip": _r(
        "Car - commuting, road trip",
        ("car", "vehicle", "trunk", "boot", "back seat"),
        ("bag", "luggage", "suitcase", "cooler", "box"),
        ("load", "pack", "place", "put", "arrange"),
    ),
    "Pack a Room for Moving": _r(
        ("Cleaning / laundry", "Indoor Navigation (walking)"),
        ("box", "carton", "packing"),
        ("pack", "wrap", "label", "tape", "seal", "stack"),
        ("room", "house", "item", "belonging", "furniture"),
    ),
    "Taking Out the Trash": _r(
        "Cleaning / laundry",
        ("trash bag", "garbage bag", "rubbish bag", "bin bag",
         "take out the trash", "takes out the trash", "taking out the trash",
         "take out the garbage", "takes out the garbage"),
        ("take out", "takes out", "taking out", "carries the trash",
         "carries trash", "carries the garbage", "carries garbage",
         "removes the trash", "removes trash", "removes the garbage",
         "puts the trash in the dumpster", "puts garbage in the dumpster"),
        action_excluded=("mop", "moper", "dishes", "sink", "grain", "peel",
                         "cook", "oven", "sponge", "faucet", "plate on the sink"),
    ),
    "Unpack & Set Up a Room": _r(
        "Indoor Navigation (walking)",
        ("box", "carton", "package", "cardboard"),
        ("unpack", "unbox", "open the box", "empty the box",
         "take out of the box", "takes out of the box"),
        ("room", "shelf", "table", "bed", "chair", "setup", "set up",
         "place", "arrange"),
        # Montar IKEA ≠ desembalar o cômodo (título errado = ban).
        excluded=("Assembling furniture", "Car - commuting",
                  "biology experiments"),
        action_excluded=("commuting", "biology", "experiment"),
    ),
    "Leaf Raking & Bagging": _r(
        ("Doing yardwork / shoveling snow", "Gardening"),
        ("leaf", "leaves"),
        ("rake", "raking", "rastel"),
        ("bag", "sack", "pile", "collect"),
    ),
    "Pool cleaning": _r(
        ("Swimming in a pool/ocean", "Household cleaners"),
        ("pool", "swimming pool", "skimmer"),
        ("leaf", "leaves", "debris", "dirt", "surface"),
        ("clean", "skim", "remove", "net", "empty"),
    ),
    "Pressure-Wash": _r(
        ("Doing yardwork / shoveling snow", "Household cleaners", "Car/scooter washing"),
        ("pressure washer", "power washer", "high pressure", "water jet"),
        ("patio", "driveway", "walkway", "pavement", "sidewalk", "deck", "ground", "floor"),
        ("wash", "spray", "clean", "rinse"),
    ),
    "Spread Mulch": _r(
        ("Gardening", "Farmer", "Doing yardwork / shoveling snow"),
        ("mulch", "compost", "wood chip"),
        ("spread", "shovel", "pour", "distribute", "rake"),
        ("bed", "ground", "soil", "garden"),
    ),
    "Drink Station Setup": _r(
        ("Hosting a party", "Cooking"),
        ("drink", "beverage", "bar", "bottle", "glass"),
        ("ice", "mixer", "bottle", "glass", "cup"),
        ("setup", "set up", "arrange", "place", "prepare"),
    ),
    "Party Cleanup": _r(
        ("Hosting a party", "Attending a party"),
        ("party", "gathering", "guest"),
        ("dish", "plate", "cup", "trash", "garbage", "surface", "furniture"),
        ("clean", "clear", "collect", "wipe", "remove", "return", "tidy"),
    ),
    "Party Setup & Takedown": _r(
        ("Hosting a party", "Attending a party"),
        ("party", "gathering", "birthday", "celebration"),
        ("decoration", "furniture", "food", "drink", "table", "chair"),
        ("setup", "set up", "arrange", "decorate", "clean", "takedown", "take down"),
    ),
    "Arrange Patio Furniture": _r(
        ("Doing yardwork / shoveling snow", "Gardening", "Cleaning / laundry"),
        ("patio", "balcony", "porch", "terrace", "deck", "outdoor"),
        ("furniture", "chair", "table", "bench", "sofa"),
        ("arrange", "move", "place", "wipe", "clean"),
    ),
    "Pet Feeding": _r(
        "Playing with pets",
        ("dog", "pet", "cat", "animal"),
        ("food", "feed", "kibble", "water"),
        ("bowl", "dish", "container", "feeder"),
        ("fill", "pour", "place", "give", "refresh", "change"),
    ),
    "Scoop a litter box": _r(
        ("Playing with pets", "Cleaning / laundry"),
        ("litter", "cat litter", "litter box"),
        ("scoop", "waste", "feces", "poop", "dirty litter"),
        ("clean", "remove", "dispose", "top up", "refill"),
    ),
    "Tidy the Desk": _r(
        "Working at desk",
        ("desk", "work table", "workstation"),
        ("cable", "cord", "wire", "object", "item", "paper"),
        ("tidy", "organize", "arrange", "clear", "route", "bundle"),
    ),
    "Mail & Package Sorting": _r(
        "Working at desk",
        ("mail", "letter", "package", "parcel", "envelope"),
        ("sort", "separate", "open", "route", "organize", "classify"),
    ),
    "Restock Medicine Cabinet": _r(
        ("Daily hygiene", "Cleaning / laundry"),
        ("medicine", "medication", "pill", "first aid", "bandage", "cabinet"),
        ("restock", "refill", "organize", "discard", "expire", "check", "replace"),
    ),
    "Sort Recycling": _r(
        "Cleaning / laundry",
        ("recycl", "plastic", "glass", "paper", "cardboard", "metal", "can"),
        ("bin", "container", "box", "bag"),
        ("sort", "separate", "classify", "place", "put"),
    ),
    "Shelve books": _r(
        ("Working at desk", "Reading books", "Cleaning / laundry"),
        ("book", "books"),
        ("shelf", "bookshelf", "bookcase", "rack"),
        ("organize", "sort", "arrange", "place", "shelve"),
    ),
    "Sort hardware into divided tray": _r(
        ("Carpenter", "Maker Lab", "Cleaning / laundry"),
        ("screw", "bolt", "nut", "washer", "nail", "hardware", "small part"),
        ("tray", "organizer", "bin", "container", "compartment"),
        ("sort", "separate", "organize", "classify", "place"),
    ),

    # Nomes históricos aceitos apenas para configurações antigas.
    "Furniture Assembly/ Disassembly": _r(
        "Assembling furniture",
        ("assembl", "furniture", "shelf", "table", "chair", "cabinet", "drawer",
         "structure", "panel", "frame", "rack", "desk", "bed"),
        ("screw", "bolt", "piece", "part", "attach", "fit", "install", "assembl",
         "tighten", "join", "connect", "build", "construct"),
    ),
    "Drill into workpiece": _r(
        "Carpenter",
        ("drill", "drilling"),
        ("hole", "workpiece", "wood", "board", "piece"),
    ),
    "Pet Grooming & Bath": _r(
        "Washing the dog / pet",
        ("wash", "bath", "bathe", "brush", "groom"),
        ("dog", "pet", "cat", "horse", "animal"),
    ),
}

TASK_ALIASES: dict[str, str] = {
    "Changing Light Bulbs / Smoke Detectors": "Replace Bulbs & Batteries",
    "Tighten cabinet & door hardware": "Tighten Cabinet & Door Hinges",
    "Hang curtains on a rod (rings/hooks)": "Hang Curtains",
    "Loading the Car": "Pack the Car for a Trip",
    "Shovel / Spread Mulch": "Spread Mulch",
    "Bar / Drink Station Setup": "Drink Station Setup",
    "Patio / balcony furniture arrangement": "Arrange Patio Furniture",
    "Pet Feeding & Water Refresh": "Pet Feeding",
    "Scoop a litter box (clean litter)": "Scoop a litter box",
    "Desk Tidy & Cable Management": "Tidy the Desk",
    "Restock First-Aid & Medicine": "Restock Medicine Cabinet",
    "Sort recycling into bins": "Sort Recycling",
}

STRICT_TASKS = frozenset((*TASK_RULES, *TASK_ALIASES))


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


@lru_cache(maxsize=4096)
def _norm_term(value: str) -> str:
    """Normaliza termos fixos das regras uma vez, não milhões de vezes."""
    return _norm(value)


_NORMALIZED_RULES = {_norm(k): v for k, v in TASK_RULES.items()}
_NORMALIZED_RULES.update({
    _norm(alias): TASK_RULES[canonical]
    for alias, canonical in TASK_ALIASES.items()
})


def rule_for(task_name: str) -> TaskRule | None:
    return _NORMALIZED_RULES.get(_norm(task_name))


def _has(values: set[str], pattern: str) -> bool:
    wanted = _norm(pattern)
    return any(wanted in actual for actual in values)


def score_scenarios(rule: TaskRule, scenarios: Iterable[str]) -> int | None:
    """Pontua o cenário pai; a ação específica é verificada separadamente."""
    actual = {_norm(str(s)) for s in scenarios if str(s).strip()}
    if any(_has(actual, x) for x in rule.excluded):
        return None
    primary_hits = sum(_has(actual, x) for x in rule.primary)
    if not primary_hits:
        return None
    support_hits = sum(_has(actual, x) for x in rule.supporting)
    return primary_hits * 100 + support_hits * 20 - max(0, len(actual) - 1) * 2


def scenario_is_sufficient(rule: TaskRule, scenarios: Iterable[str]) -> bool:
    """Verdadeiro quando o rótulo Ego4D já equivale exatamente à tarefa."""
    actual = {_norm(str(s)) for s in scenarios if str(s).strip()}
    return any(_has(actual, pattern) for pattern in rule.scenario_sufficient)


# Motivos de ban publicados pelo Minute (grupo de operadores). Palavra-inteira
# para não casar "headphones" com phone, nem "site" com sit.
_SIT_RE = re.compile(
    r"\b(?:sits?|sitting|seated|sit(?:ting)? down|seats on)\b", re.I)
_PHONE_RE = re.compile(
    r"\b(?:smart)?phones?\b|\biphones?\b|\bcell[\s-]?phones?\b|"
    r"\btexting\b|\bscroll(?:s|ing|ed)?\b|\bsocial media\b|"
    r"\binstagram\b|\bwhatsapp\b|\btiktok\b|"
    r"\blooks at (?:his |her |the )?phone\b|"
    r"\buses (?:his |her |the )?phone\b|"
    r"\bpicks up (?:his |her |the )?phone\b|"
    r"\bon (?:his |her |their )?phone\b|"
    r"\bwatches (?:a |the )?videos?\b|\bwatching (?:a |the )?videos?\b",
    re.I)
_CHEST_RE = re.compile(
    r"\bchest[\s-]?(?:mount|mounted|harness|strap|rig)\b", re.I)
_TRIPOD_RE = re.compile(
    r"\b(?:tripod|monopod|selfie[\s-]?stick|camera stand)\b", re.I)
_PHONE_CAM_RE = re.compile(
    r"\b(?:iphone|android|samsung galaxy|google pixel)\b", re.I)
_WATCH_RE = re.compile(
    r"\bwatch(?:es|ing)? tv\b|\bwatching television\b", re.I)
_ACTOR_RE = re.compile(r"#\s*([co])\b", re.I)


def _camera_wearer_segments(text: str) -> list[str]:
    """Separa apenas ações `#C`; `#O` nunca prova nem reprova o wearer."""
    marks = list(_ACTOR_RE.finditer(text))
    if not marks:
        cleaned = _norm(text)
        return [cleaned] if cleaned else []
    out: list[str] = []
    for idx, mark in enumerate(marks):
        if mark.group(1).casefold() != "c":
            continue
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
        cleaned = _norm(text[mark.end():end])
        if cleaned:
            out.append(cleaned)
    return out


def _clip_hygiene_text(clip: dict[str, Any]) -> str:
    action = str(clip.get("action_text") or "")
    narration = str(clip.get("narration") or "")
    # Um vídeo-pai pode listar "Talking on the phone" porque isso ocorreu em
    # outro momento. Para um corte temporizado, as ações do próprio intervalo
    # são a fonte de verdade; o cenário amplo não deve contaminar o trecho.
    segmented = bool(clip.get("needs_cut"))
    parts = [
        " ".join(_camera_wearer_segments(action)),
        " ".join(_camera_wearer_segments(narration)),
        " ".join(str(x) for x in (clip.get("action_units") or ())),
        str(clip.get("device") or ""),
        "" if segmented else " ".join(
            str(x) for x in (clip.get("scenarios") or ())),
        "" if segmented else str(clip.get("scenario") or ""),
    ]
    return " ".join(parts)


@lru_cache(maxsize=65536)
def _narration_is_dirty(text: str) -> bool:
    return hygiene_reject_reason({"action_text": text}) is not None


def hygiene_reject_reason(clip: dict[str, Any]) -> str | None:
    """Motivo de ban se o clipe for recusado; None se passou na higiene."""
    text = _clip_hygiene_text(clip)
    if _SIT_RE.search(text):
        return "sitting"
    if _PHONE_RE.search(text):
        return "phone"
    if _WATCH_RE.search(text):
        return "watching_tv"
    if _CHEST_RE.search(text):
        return "chest_mount"
    if _TRIPOD_RE.search(text):
        return "tripod"
    device = str(clip.get("device") or "")
    if device and _PHONE_CAM_RE.search(device):
        return "phone_camera"
    return None


_TERM_RE: dict[str, re.Pattern[str]] = {}


def _term_in(block: str, term: str) -> bool:
    """Evidência por palavra. 'bin' não casa 'cabinet'; 'cut' casa 'cuts/cutting'."""
    t = _norm_term(term)
    if not t:
        return False
    if " " in t:
        return t in block
    rx = _TERM_RE.get(t)
    if rx is None:
        rx = re.compile(rf"\b{re.escape(t)}")
        _TERM_RE[t] = rx
    return rx.search(block) is not None


@lru_cache(maxsize=1024)
def _evidence_group_pattern(group: tuple[str, ...]) -> re.Pattern[str]:
    """Uma busca por grupo substitui várias buscas independentes de termos."""
    alternatives: list[str] = []
    for term in group:
        normalized = _norm_term(term)
        if not normalized:
            continue
        prefix = "" if " " in normalized else r"\b"
        alternatives.append(prefix + re.escape(normalized))
    return re.compile("|".join(alternatives) if alternatives else r"(?!x)x")


def _evidence_group_present(block: str, group: tuple[str, ...]) -> bool:
    return _evidence_group_pattern(group).search(block) is not None


def span_evidence_possible(rule: TaskRule, block: str) -> bool:
    """Filtro barato e conservador antes da análise temporal completa.

    Só rejeita quando nem o texto agregado do vídeo contém o número mínimo de
    grupos exigido pelo `score_action`. Assim evita percorrer milhares de falas
    para uma regra impossível sem afrouxar nem alterar a seleção final.
    """
    required = rule.min_evidence_groups
    if required is None:
        required = len(rule.evidence)
    return sum(
        _evidence_group_present(block, group)
        for group in rule.evidence
    ) >= required


def span_search_text(events: Iterable[tuple[float, str]]) -> str:
    """Texto agregado barato para eliminar regras impossíveis antes da higiene."""
    return _norm(" ".join(str(raw_text or "") for _raw_t, raw_text in events))


# Inteligente: um bloco contínuo da tarefa OU uma fração razoável.
# 4 falas de cerca em 20 de caminhada caem; um take de 8+ falas da ação passa
# mesmo com "looks around" no meio — senão o catálogo zera.
ON_TASK_MIN_RATIO = 0.28
ON_TASK_MIN_STREAK = 8
ON_TASK_RATIO_UNITS = 10
# Cortes no vídeo-pai: só o trecho contínuo da tarefa (não o clipe oficial misto).
SPAN_MIN_RATIO = 0.75
SPAN_MAX_GAP_S = 15.0
SPAN_PAD_S = 2.0
ACTIVITY_TARGET_MAX_GAP_S = 30.0


# Relações pai/filho: uma evidência da tarefa ampla não é concorrência para a
# específica (nem vice-versa). Tarefas irmãs continuam concorrentes: aparar a
# cerca deve encerrar um trecho de arrancar ervas, por exemplo.
_TASK_CONTAINS: dict[str, frozenset[str]] = {
    "Gardening": frozenset({"Pull weeds by hand", "Trim a hedge"}),
    "Full Yard Maintenance": frozenset({
        "Gardening", "Pull weeds by hand", "Trim a hedge",
        "Leaf Raking & Bagging",
    }),
    "Car Wash & Detail": frozenset({"Cleaning Out Car"}),
    "Pet Care Routine": frozenset({"Pet Grooming & Bath", "Pet Feeding"}),
    "Party Setup & Takedown": frozenset({
        "Drink Station Setup", "Party Cleanup",
    }),
    "Unpack & Set Up a Room": frozenset({"Furniture Assembly"}),
}


def competing_span_rules(
    target_name: str,
    named_rules: Iterable[tuple[str, TaskRule]],
) -> tuple[TaskRule, ...]:
    """Regras que representam troca real de atividade para `target_name`."""
    competitors: list[TaskRule] = []
    target_children = _TASK_CONTAINS.get(target_name, frozenset())
    for other_name, other_rule in named_rules:
        if other_name == target_name:
            continue
        other_children = _TASK_CONTAINS.get(other_name, frozenset())
        if other_name in target_children or target_name in other_children:
            continue
        competitors.append(other_rule)
    return tuple(competitors)


def competing_span_names(
    target_name: str,
    named_rules: Iterable[tuple[str, TaskRule]],
) -> frozenset[str]:
    """Nomes concorrentes, usados pelo índice de ações pré-classificado."""
    target_children = _TASK_CONTAINS.get(target_name, frozenset())
    return frozenset(
        other_name for other_name, _other_rule in named_rules
        if (other_name != target_name
            and other_name not in target_children
            and target_name not in _TASK_CONTAINS.get(other_name, frozenset()))
    )


def _activity_spans(
    rule: TaskRule,
    flagged: list[tuple[float, str, str, bool, bool]],
    *,
    min_s: float,
    max_s: float,
    max_gap_s: float,
    video_duration_s: float | None,
) -> list[dict[str, Any]]:
    """Isola sessões contínuas entre evidências recorrentes da mesma tarefa."""
    target_runs: list[list[int]] = []
    current: list[int] = []
    previous_row: int | None = None
    previous_target: int | None = None
    for idx, row in enumerate(flagged):
        t, _text, _normed, on_task, boundary = row
        row_gap = (
            t - flagged[previous_row][0] if previous_row is not None else 0.0
        )
        if boundary or (previous_row is not None and row_gap > max_gap_s):
            if current:
                target_runs.append(current)
            current = []
            previous_target = None
            previous_row = idx
            continue
        if on_task:
            target_gap = (
                t - flagged[previous_target][0]
                if previous_target is not None else 0.0
            )
            if (previous_target is not None
                    and target_gap > ACTIVITY_TARGET_MAX_GAP_S):
                if current:
                    target_runs.append(current)
                current = []
            current.append(idx)
            previous_target = idx
        previous_row = idx
    if current:
        target_runs.append(current)

    spans: list[dict[str, Any]] = []
    for targets in target_runs:
        cursor = 0
        while cursor < len(targets):
            a = targets[cursor]
            best_pos: int | None = None
            pos = cursor
            while pos < len(targets):
                b = targets[pos]
                duration = flagged[b][0] - flagged[a][0]
                if duration > max_s + 1e-6:
                    break
                if duration >= min_s:
                    best_pos = pos
                pos += 1
            if best_pos is None:
                break
            b = targets[best_pos]
            start = max(0.0, flagged[a][0])
            end = flagged[b][0]
            if video_duration_s:
                end = min(end, float(video_duration_s))
            units = [
                normed for _t, _text, normed, _on, _boundary
                in flagged[a:b + 1] if normed
            ]
            text = " ".join(units)
            score = score_action(rule, text, units)
            if score is not None:
                spans.append({
                    "start": start,
                    "end": end,
                    "action_text": text,
                    "action_units": units,
                    "match_score": score,
                    "n_events": b - a + 1,
                })
            cursor = best_pos + 1
    return spans


def _evidence_hits(segment: str, rule: TaskRule) -> list[int]:
    return [sum(_term_in(segment, term) for term in group)
            for group in rule.evidence]


def _unit_on_task(segment: str, rule: TaskRule) -> bool:
    """Uma fala só conta quando traz evidência suficiente da própria ação."""
    if not rule.evidence:
        return False
    required = rule.unit_min_evidence_groups
    if required is None:
        required = rule.min_evidence_groups
    if required is None:
        required = len(rule.evidence)
    return sum(
        _evidence_group_present(segment, group)
        for group in rule.evidence
    ) >= required


def _longest_true_run(flags: list[bool]) -> int:
    best = cur = 0
    for flag in flags:
        if flag:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def score_action(rule: TaskRule, action_text: str,
                 action_units: Iterable[str] | None = None) -> int | None:
    """Exige evidência da ação; sem metadado ou sem prova, rejeita o clipe.

    Além de achar a ação, a MAIOR PARTE das falas tem de ser dela. Um trecho
    certo no meio de outra atividade é o ban de título errado.
    """
    required = rule.min_evidence_groups
    if required is None:
        required = len(rule.evidence)

    # Se o texto traz atores, ele é a fonte de verdade: action_units antigos
    # misturavam ações #O com o camera-wearer.
    segments = (_camera_wearer_segments(action_text)
                if _ACTOR_RE.search(action_text)
                else [_norm(str(x)) for x in (action_units or ()) if str(x).strip()])
    if not segments:
        segments = _camera_wearer_segments(action_text)
    segments = [segment for segment in segments
                if not re.search(r"#\s*unsure\b", segment, re.I)]
    text = " ".join(segments)
    if not text:
        return None
    if any(_term_in(text, term) for term in rule.action_excluded):
        return None
    group_units = [
        sum(any(_term_in(segment, term) for term in group)
            for segment in segments)
        for group in rule.evidence
    ]
    if sum(count >= rule.evidence_group_min_units
           for count in group_units) < required:
        return None
    window = rule.evidence_window
    blocks = [" ".join(segments)] if window is None else [
        " ".join(segments[i:i + window]) for i in range(len(segments))
    ]
    best = 0
    for block in blocks:
        group_hits = _evidence_hits(block, rule)
        if sum(hit > 0 for hit in group_hits) >= required:
            best = max(best, sum(group_hits) * 10)
    if not best:
        return None
    flags = [_unit_on_task(seg, rule) for seg in segments]
    on_task = sum(flags)
    n = len(segments)
    if n >= ON_TASK_RATIO_UNITS:
        streak = _longest_true_run(flags)
        ratio = on_task / n
        if streak >= ON_TASK_MIN_STREAK or ratio >= ON_TASK_MIN_RATIO:
            return best
        return None
    if on_task < max(1, (n + 1) // 2):
        return None
    return best


PreparedSpanEvent = tuple[float, str, str, bool]


def prepare_span_events(
    events: Iterable[tuple[float, str]],
) -> tuple[PreparedSpanEvent, ...]:
    """Converte e higieniza as narrações uma vez por vídeo Ego4D."""
    rows: list[PreparedSpanEvent] = []
    for raw_t, raw_text in events:
        try:
            t = float(raw_t)
        except (TypeError, ValueError):
            continue
        text = str(raw_text or "").strip()
        if not text:
            continue
        wearer_text = " ".join(_camera_wearer_segments(text))
        uncertain = bool(re.search(r"#\s*unsure\b", text, re.I))
        rows.append((t, text, wearer_text,
                     _narration_is_dirty(text) or uncertain))
    rows.sort(key=lambda row: row[0])
    return tuple(rows)


def label_span_events(
    prepared_events: tuple[PreparedSpanEvent, ...],
    named_rules: Iterable[tuple[str, TaskRule]],
) -> tuple[frozenset[str], ...]:
    """Classifica cada anotação uma vez para todas as tarefas possíveis."""
    rules = tuple(named_rules)
    labels: list[frozenset[str]] = []
    for _t, _text, normed, base_dirty in prepared_events:
        if base_dirty:
            labels.append(frozenset())
            continue
        labels.append(frozenset(
            name for name, rule in rules
            if (not any(_term_in(normed, term)
                        for term in rule.action_excluded)
                and _unit_on_task(normed, rule))
        ))
    return tuple(labels)


def extract_spans(
    rule: TaskRule,
    events: Iterable[tuple[float, str]],
    *,
    min_s: float = 60.0,
    max_s: float = 1800.0,
    min_ratio: float = SPAN_MIN_RATIO,
    max_gap_s: float = SPAN_MAX_GAP_S,
    pad_s: float = SPAN_PAD_S,
    video_duration_s: float | None = None,
    prepared_events: tuple[PreparedSpanEvent, ...] | None = None,
    competing_rules: Iterable[TaskRule] = (),
    activity_mode: bool = False,
    task_name: str | None = None,
    event_task_names: tuple[frozenset[str], ...] | None = None,
    competing_task_names: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Corta trechos contínuos da tarefa a partir de narrações temporizadas.

    O clipe oficial do Ego4D mistura 10–20 min de ações. As evidências da
    tarefa ancoram o trecho; ações auxiliares podem ficar entre elas, mas uma
    evidência de tarefa concorrente encerra o bloco imediatamente.
    """
    rows = prepared_events if prepared_events is not None else prepare_span_events(events)
    if not rows:
        return []
    if event_task_names is not None and len(event_task_names) != len(rows):
        raise ValueError("event_task_names deve corresponder a prepared_events")

    rivals = tuple(competing_rules)
    flagged: list[tuple[float, str, str, bool, bool]] = []
    for idx, (t, text, normed, base_dirty) in enumerate(rows):
        labels = event_task_names[idx] if event_task_names is not None else None
        if labels is not None and task_name:
            contradictory = any(
                _term_in(normed, term) for term in rule.action_excluded)
            on_task = not contradictory and task_name in labels
            competing = not on_task and bool(labels & competing_task_names)
        else:
            contradictory = any(
                _term_in(normed, term) for term in rule.action_excluded)
            on_task = (not base_dirty and not contradictory
                       and _unit_on_task(normed, rule))
            competing = (not on_task and not base_dirty and any(
                _unit_on_task(normed, rival) for rival in rivals
            ))
        dirty = base_dirty or contradictory or competing
        flagged.append((t, text, normed, on_task, dirty))
    if not any(on_task for _t, _x, _n, on_task, _d in flagged):
        return []
    if activity_mode:
        return _activity_spans(
            rule, flagged, min_s=min_s, max_s=max_s,
            max_gap_s=max_gap_s, video_duration_s=video_duration_s)

    spans: list[dict[str, Any]] = []
    i = 0
    n = len(flagged)
    while i < n:
        if flagged[i][4] or not flagged[i][3]:
            i += 1
            continue
        best_j: int | None = None
        on_seconds = 0.0
        total_seconds = 0.0
        j = i
        while j < n:
            if flagged[j][4]:
                break
            if j > i and (flagged[j][0] - flagged[j - 1][0]) > max_gap_s:
                break
            dur = flagged[j][0] - flagged[i][0]
            if dur > max_s + 1e-6:
                break
            if j > i:
                interval = max(0.0, flagged[j][0] - flagged[j - 1][0])
                total_seconds += interval
                if flagged[j - 1][3]:
                    on_seconds += interval
            ratio = on_seconds / total_seconds if total_seconds else 0.0
            if dur >= min_s and ratio >= min_ratio and flagged[j][3]:
                best_j = j
            j += 1
        if best_j is None:
            i += 1
            continue
        a, b = i, best_j
        start = max(0.0, flagged[a][0] - pad_s)
        end = flagged[b][0] + pad_s
        if video_duration_s:
            end = min(end, float(video_duration_s))
        if end - start < min_s:
            i = b + 1
            continue
        if end - start > max_s:
            end = start + max_s
        units = [normed for _t, _x, normed, _on, _d in flagged[a:b + 1]
                 if normed]
        text = " ".join(units)
        score = score_action(rule, text, units)
        if score is None:
            i += 1
            continue
        spans.append({
            "start": start,
            "end": end,
            "action_text": text,
            "action_units": units,
            "match_score": score,
            "n_events": b - a + 1,
        })
        i = b + 1
    return spans


def ranked_clips(task_name: str, clips: Iterable[dict[str, Any]],
                 description: str = "") -> list[dict[str, Any]]:
    """Retorna somente clipes comprovadamente compatíveis com a tarefa."""
    rule = rule_for(task_name)
    if rule is None:
        return []
    ranked: list[tuple[int, dict[str, Any]]] = []
    for clip in clips:
        if hygiene_reject_reason(clip):
            continue
        if (rule.min_span_s is not None
                and float(clip.get("dur_s") or 0) < rule.min_span_s):
            continue
        scenarios = clip.get("scenarios") or [clip.get("scenario", "")]
        scenario_score = score_scenarios(rule, scenarios)
        if scenario_score is None:
            continue
        action_score = score_action(
            rule, str(clip.get("action_text") or clip.get("narration") or ""),
            clip.get("action_units"))
        if action_score is None:
            continue
        item = dict(clip)
        item["match_score"] = scenario_score + action_score
        item["match_confidence"] = rule.confidence
        ranked.append((item["match_score"], item))
    ranked.sort(key=lambda pair: (-pair[0], pair[1].get("dur_s", 0),
                                  str(pair[1].get("clip_uid", ""))))
    return [item for _, item in ranked]


def rank_all_tasks(clips: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Uma passada nos clipes para todas as regras (catálogo da UI)."""
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in TASK_RULES}
    named_rules = list(TASK_RULES.items())
    for clip in clips:
        if hygiene_reject_reason(clip):
            continue
        scenarios = clip.get("scenarios") or [clip.get("scenario", "")]
        action = str(clip.get("action_text") or clip.get("narration") or "")
        units = clip.get("action_units")
        for name, rule in named_rules:
            scenario_score = score_scenarios(rule, scenarios)
            if scenario_score is None:
                continue
            action_score = score_action(rule, action, units)
            if action_score is None:
                continue
            item = dict(clip)
            item["match_score"] = scenario_score + action_score
            item["match_confidence"] = rule.confidence
            buckets[name].append(item)
    for _name, items in buckets.items():
        items.sort(key=lambda c: (
            -(c.get("match_score") or 0), c.get("dur_s") or 0,
            str(c.get("clip_uid") or "")))
    for alias, canonical in TASK_ALIASES.items():
        buckets[alias] = buckets.get(canonical, [])
    return buckets

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
from .background_work import checkpoint as _background_checkpoint


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
    required_action_pattern: str | None = None
    required_span_patterns: tuple[str, ...] = ()
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
    required_action_pattern: str | None = None,
    required_span_patterns: tuple[str, ...] = (),
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
        required_action_pattern=required_action_pattern,
        required_span_patterns=required_span_patterns,
    )


# Determiners/adjectives may separate a verb from its object. Another object
# or a location preposition may not: carrying boxes beside a sofa is not
# moving the sofa. These patterns constrain only the required action clause.
_OBJECT_MODIFIERS = (
    r"(?:(?:a|an|the|his|her|their|our|this|that|some|both|few|new|old|fresh|dirty|clean|"
    r"wooden|metal|plastic|heavy|large|small|big|red|blue|black|white|brown|"
    r"empty|full|hot|cold|cooked|frozen|leftover|remaining|delivered|beverage|gym|fitness|dining|dinner)\s+)*"
)
_FURNITURE_MOVED = (
    r"\b(?:lift\w*|carr\w*|mov\w*|relocat\w*)\s+" + _OBJECT_MODIFIERS +
    r"(?:furniture|chairs?|tables?|sofas?|couches?|cabinets?|wardrobes?|desks?|beds?|shelves?|dressers?|benches?)\b"
)
_BEVERAGE_PREPARED = (
    r"(?:\b(?:brew\w*|steep\w*|grind\w*|tamp\w*|make\w*|makes?|prepar\w*|blend\w*|"
    r"squeez\w*|mix\w*|stir\w*|pour\w*)\s+" + _OBJECT_MODIFIERS +
    r"(?:coffee|tea|espresso|beverage|cocoa|hot chocolate|juice|smoothie)\b|"
    r"\b(?:puts?|putting|plac\w*|insert\w*|load\w*)\b.{0,45}\b(?:coffee|espresso) (?:pod|capsule)\b"
    r"(?:(?!\b(?:beside|near|next)\b).){0,80}\b(?:in|into)\s+" + _OBJECT_MODIFIERS +
    r"(?:coffee(?: pod bin of the coffee)?|espresso)\s+(?:machine|maker)\b|"
    r"\b(?:press\w*|push\w*)\s+(?:(?:a|the|some)\s+)?(?:buttons?|lever)\b.{0,45}"
    r"\b(?:on|of)\s+" + _OBJECT_MODIFIERS + r"(?:coffee|espresso)\s+(?:machine|maker)\b|"
    r"\b(?:press\w*|push\w*)\s+(?:down\s+)?" + _OBJECT_MODIFIERS +
    r"(?:coffee|espresso)\s+(?:machine|maker)\s+(?:buttons?|lever)\b|"
    r"\bdip\w*\s+" + _OBJECT_MODIFIERS + r"(?:tea bags?|teabags?)\b.{0,55}"
    r"\b(?:in|into)\s+" + _OBJECT_MODIFIERS + r"(?:water|mug|cup)\b)"
)
_BEVERAGE_RESET = (
    r"\b(?:reset\w*|clean\w*|wipe\w*|rins\w*|wash\w*|discard\w*|dispos\w*|"
    r"empt\w*|tidy\w*|return\w*|remov\w*|puts? away)\s+" + _OBJECT_MODIFIERS +
    r"(?:coffee|tea|espresso|station|counter|cups?|mugs?|kettle|grounds|filter)\b"
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
        scenario_sufficient=("Car/scooter washing",),
    ),
    "Gardening": _r(
        "Gardening",
        ("plant", "water", "weed", "prun", "harvest", "garden", "soil", "flower", "seed", "pot",
         "dig", "fertiliz", "compost", "cultivat", "mow", "lawn", "grass cutter"),
        scenario_sufficient=("Gardening",),
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
        ("curtain", "drape", "blind"),
        ("rod", "ring", "hook", "rail", "bracket", "window"),
        ("hang", "mount", "install", "thread", "attach"),
    ),
    "Holiday Decoration Setup": _r(
        ("Hosting a party", "Fixing something in the home"),
        ("decoration", "ornament", "christmas", "holiday", "garland", "lights"),
        ("unpack", "hang", "place", "arrange", "decorate", "set up", "setup",
         "take down", "takes down", "taking down", "remove", "pack away", "undecorat"),
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
        ("furniture", "shelf", "table", "chair", "cabinet", "drawer", "wardrobe",
         "bookcase", "stool", "bench", "bed frame", "chair frame", "rack", "desk", "bed"),
        ("screw", "bolt", "attach", "install", "assembl", "fasten", "disassembl",
         "tighten", "join", "connect", "builds", "built", "build a", "build the", "builds a",
         "builds the", "building a", "building the", "built a", "built the",
         "constructs", "construct a", "construct the", "constructs a", "constructs the",
         "constructing a", "constructing the", "constructed a", "constructed the"),
        action_excluded=("puzzle", "jigsaw puzzle", "badminton", "racket", "shuttlecock",
                         "board game", "card game", "game pieces", "party decor", "banner",
                         "clothing", "clothes", "shirt", "towel", "laundry", "hanger"),
        scenario_sufficient=("Assembling furniture",),
        # O cenário comprova a montagem, mas a higiene continua removendo
        # qualquer janela em que a pessoa apareça sentada.
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
        scenario_sufficient=("Walking the dog / pet",),
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
        action_excluded=("bicycle", "bike", "clothes", "garment", "brush washer"),
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
        ("hanger",),
        ("clothes", "clothing", "garment", "shirt", "dress", "jacket", "trouser", "laundry", "towel", "pants"),
        ("hang ", "hangs ", "hanging ", "put", "place"),
        action_excluded=("sew", "sewing machine"),
    ),
    "Change Sheets & Make Bed": _r(
        "Cleaning / laundry",
        ("make the bed", "makes the bed", "making the bed", "makes a bed",
         "bedsheet", "bed sheet", "sheet", "duvet", "pillow", "blanket", "mattress"),
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
        ("furniture", "shelf", "table", "chair", "cabinet", "drawer", "wardrobe",
         "bookcase", "stool", "bench", "bed frame", "chair frame", "rack", "desk", "bed"),
        ("screw", "bolt", "attach", "install", "assembl", "fasten", "disassembl",
         "tighten", "join", "connect", "builds", "built", "build a", "build the", "builds a",
         "builds the", "building a", "building the", "built a", "built the",
         "constructs", "construct a", "construct the", "constructs a", "constructs the",
         "constructing a", "constructing the", "constructed a", "constructed the"),
        action_excluded=("puzzle", "jigsaw puzzle", "badminton", "racket", "shuttlecock",
                         "board game", "card game", "game pieces", "party decor", "banner",
                         "clothing", "clothes", "shirt", "towel", "laundry", "hanger"),
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
        scenario_sufficient=("Washing the dog / pet",),
    ),
}

# Current Minute tasks, matched against their published action descriptions.
# Broader new tasks get their own rule; they are not aliases for all gardening
# or all household cleaning.
TASK_RULES.update({
    "Cleaning Car": _r(
        "Car/scooter washing",
        ("wash", "scrub", "rinse", "soap", "sponge", "wipe", "clean",
         "vacuum", "dry", "trash", "garbage"),
        ("car", "vehicle", "windshield", "wheel", "tire", "dashboard", "car seat"),
        scenario_sufficient=("Car/scooter washing",)),
    "Planting or Pulling Weeds": _r(
        ("Gardening", "Farmer"),
        ("plant", "seedling", "seed", "weed", "grass", "root", "flower", "crop"),
        ("planting", "plants a", "plants the", "plants seed", "plants flower",
         "plants sapling", "transplant", "sow", "pull", "pluck", "uproot",
         "removes weeds", "removes grass", "dig"),
        action_excluded=("watering", "waters", "sprayer", "hedge", "harvest",
                         "pruning", "pruner", "shear", "cuts the plant", "cuts plants")),
    "Folding Clothes or Putting Them on Hangers": _r(
        "Cleaning / laundry",
        ("clothes", "clothing", "garment", "shirt", "dress", "jacket", "trouser",
         "laundry", "towel", "pants"),
        ("fold", "hanger"),
        action_excluded=("washing machine", "scrub", "wring", "sew", "sewing machine")),
    "Using the Laundry Machine": _r(
        "Cleaning / laundry", ("washer", "washing machine", "dryer"),
        ("clothes", "cloth", "laundry", "garment", "shirt", "trouser", "linen"),
        ("load", "unload", "put", "remove", "take", "transfer", "start", "switch")),
    "Watering Outdoor Plants": _r(
        ("Gardening", "Doing yardwork / shoveling snow", "Farmer"),
        ("water", "watering", "hose", "sprinkl"),
        ("plant", "flower", "garden", "bed", "crop", "seedling")),
    "Spreading Mulch or Fertilizer": _r(
        ("Gardening", "Farmer", "Doing yardwork / shoveling snow"),
        ("mulch", "compost", "wood chip", "fertiliz", "fertilis", "seed"),
        ("spread", "shovel", "pour", "distribute", "rake", "scatter"),
        ("bed", "ground", "soil", "garden", "field")),
    "Leaf Raking or Blowing": _r(
        ("Doing yardwork / shoveling snow", "Gardening"),
        ("leaf", "leaves", "yard debris"), ("rake", "raking", "blower", "blow")),
    "Pack or Unpack a Car for a Trip": _r(
        "Car - commuting, road trip", ("car", "vehicle", "trunk", "boot", "back seat"),
        ("bag", "luggage", "suitcase", "cooler", "box"),
        ("load", "unload", "pack", "unpack", "place", "put", "remove", "take")),
    "Shopping": _r(
        ("Grocery shopping", "Clothes, other shopping"),
        ("shop", "store", "shelf", "cart", "basket", "cashier", "checkout"),
        ("pick", "select", "put", "buy", "pay", "browse", "take"),
        scenario_sufficient=("Grocery shopping indoors", "Clothes, other shopping")),
    "Putting Groceries Away": _r(
        ("Cleaning / laundry", "Cooking"),
        ("fridge", "refrigerator", "freezer", "pantry", "cupboard"),
        ("grocer", "food", "vegetable", "fruit", "milk", "bottle", "packet"),
        ("put", "place", "store", "arrange", "unpack")),
    "Organize the Garage": _r(
        ("Cleaning / laundry", "Fixing something in the home", "Carpenter"),
        ("garage", "storage room", "storage area"),
        ("sort", "organiz", "arrange", "tidy", "rearrange", "clear clutter", "put away")),
    "Bedroom Deep Clean": _r(
        "Cleaning / laundry", ("bedroom", "bed room"),
        ("dust", "wipe", "vacuum", "mop", "scrub", "clean", "organiz"),
        action_excluded=("laundry", "washing clothes", "folding clothes")),
    "Hand Washing Clothes": _r(
        "Cleaning / laundry", ("clothes", "cloth", "shirt", "garment", "laundry"),
        ("wash", "scrub", "wring", "rinse"),
        ("hand", "basin", "bucket", "sink", "tub"),
        action_excluded=("washing machine", "washer")),
    "Hotel Laundry Operations": _r(
        ("Cleaning / laundry", "Household cleaners"), ("hotel", "laundromat"),
        ("linen", "towel", "sheet"), ("sort", "wash", "dry", "fold")),
    "Running Industrial Laundry": _r(
        "Cleaning / laundry", ("industrial washer", "industrial dryer", "laundromat"),
        ("load", "unload", "cycle", "start", "switch")),
    "Pump Gas": _r(
        ("Car - commuting", "Getting car fixed"),
        ("fuel", "gas", "petrol", "diesel"), ("pump", "nozzle", "refuel", "fill")),
    "Shoveling Snow": _r(
        "Doing yardwork / shoveling snow", ("snow", "ice"),
        ("shovel", "clear", "salt", "ice melt")),
    "Toy or Clothing Pickup": _r(
        "Cleaning / laundry", ("toy", "clothes", "cloth", "garment"),
        ("pick", "collect", "gather", "put away"),
        ("floor", "bin", "basket", "shelf", "storage")),
    # Captured live organization catalog, 2026-10-06. These descriptions
    # require the named appliance, recipient or collaborator, not an adjacent
    # activity in the same broad parent scenario.
    "Move furniture with someone (2+ people required)": _r(
        ("Moving furniture", "Assembling furniture", "Cleaning / laundry",
         "Indoor Navigation (walking)"),
        ("furniture", "chair", "table", "sofa", "couch", "cabinet", "wardrobe",
         "desk", "bed", "shelf", "dresser", "bench"),
        ("lift", "carry", "carries", "carrying", "move", "moving", "relocat"),
        action_excluded=("alone", "by himself", "by herself", "on his own", "on her own"),
        required_action_pattern=(
            r"(?s)(?=.*" + _FURNITURE_MOVED + r")(?:"
            r"\b(?:c|wearer|he|she)\s+and\s+(?:(?:a|the|another|his|her)\s+)?"
            r"(?:man|woman|person|friend|worker|helper|other person)\s+"
            r"(?:(?:are|both|help|helps)\s+)*(?:lift\w*|carr\w*|mov\w*)\b|"
            r"\b(?:lift\w*|carr\w*|mov\w*)\b.{0,80}\btogether with\s+"
            r"(?:(?:a|the|another|his|her)\s+)?"
            r"(?:man|woman|person|friend|worker|helper|other person)\b|"
            r"\b(?:help\w*|assist\w*)\s+(?:(?:a|the|another|his|her)\s+)?"
            r"(?:man|woman|person|friend|worker|helper)\s+(?:to\s+)?"
            r"(?:lift\w*|carr\w*|mov\w*)\b)")),
    "Receive a delivery at the door": _r(
        ("Cleaning / laundry", "Indoor Navigation (walking)", "Receiving a delivery"),
        ("door", "doorstep", "entrance"),
        ("package", "parcel", "delivery", "cardboard box"),
        ("receive", "accept", "delivery", "courier", "delivery person", "delivery man",
         "answer", "opens the door", "opens door"),
        ("unbox", "unpack", "open the box", "opens the box", "opening the box",
         "open the package", "opens the package", "open the parcel", "opens the parcel"),
        unit_min_evidence_groups=2, evidence_window=None,
        required_action_pattern=(
            r"\b(?:receiv\w*|accept\w*|unbox\w*|unpack\w*|open\w*)\s+" + _OBJECT_MODIFIERS +
            r"(?:delivery\s+)?(?:packages?|parcels?|cardboard boxes?|box)\b|"
            r"\b(?:answer\w*|open\w*)\s+" + _OBJECT_MODIFIERS + r"door\b"),
        required_span_patterns=(
            r"\b(?:receiv\w*|accept\w*)\s+" + _OBJECT_MODIFIERS +
            r"(?:delivery\s+)?(?:packages?|parcels?|cardboard boxes?|box)\b",)),
    "Clean and organize gym equipment": _r(
        ("Working out at a gym", "Exercise / working out", "Cleaning / laundry"),
        ("gym", "fitness", "dumbbell", "barbell", "weight plate", "weight rack",
         "exercise machine", "treadmill", "bench press"),
        ("clean", "wipe", "disinfect", "sanitiz", "sanitize", "spray"),
        ("put back", "puts back", "put the weights back", "puts the weights back",
         "back on", "back onto", "back in", "return", "rerack", "re-rack", "stow", "organiz", "arrange"),
        unit_min_evidence_groups=2, evidence_window=None,
        required_action_pattern=(
            r"\b(?:clean\w*|wipe\w*|disinfect\w*|sanitiz\w*|spray\w*)\s+(?:down\s+)?" +
            _OBJECT_MODIFIERS + r"(?:equipment|dumbbells?|barbells?|weights?|weight plates?|"
            r"exercise machine|treadmill|bench press)\b|"
            r"\b(?:puts?|plac\w*|return\w*|rerack\w*|re-rack\w*|stow\w*|organiz\w*|arrang\w*)\s+" +
            _OBJECT_MODIFIERS + r"(?:equipment|dumbbells?|barbells?|weights?|weight plates?)\b")),
    "Carry items up and down stairs": _r(
        ("Indoor Navigation (walking)", "Cleaning / laundry", "Moving furniture"),
        ("stairs", "staircase", "stairway", "upstairs", "downstairs"),
        ("box", "bag", "item", "object", "carton", "package", "furniture", "chair",
         "table", "bucket", "basket", "luggage", "suitcase"),
        ("carry", "carries", "carrying", "haul", "bring", "brings", "take", "takes"),
        required_action_pattern=(
            r"\b(?:carr\w*|haul\w*|bring\w*|takes?|taking)\b"
            r"(?:(?!\b(?:past|beside|near|next|outside)\b).){0,80}"
            r"\b(?:up|down)\s+(?:(?:a|the)\s+)?(?:stairs|staircase|stairway)\b|"
            r"\b(?:carr\w*|haul\w*|bring\w*|takes?|taking)\b"
            r"(?:(?!\b(?:past|beside|near|next|outside)\b).){0,80}\b(?:upstairs|downstairs)\b")),
    "Serve food": _r(
        ("Cooking", "Serving food", "Hosting a party", "Waiter"),
        ("food", "meal", "rice", "soup", "bread", "meat", "salad", "chicken", "fish"),
        ("serve", "bring", "brings", "carry", "carries", "hand", "give", "place", "put"),
        ("customer", "guest", "diner", "to the man", "to a man", "to the woman",
         "to a woman", "to another person", "to someone", "for the man", "for a man",
         "for the woman", "for a woman", "for someone", "for the child", "to the child",
         "for her husband", "for his wife", "people at the table", "man", "woman", "person",
         "child", "family", "husband", "wife"),
        ("table", "dining table", "dinner table"),
        action_excluded=("empty plate", "empty bowl", "empty dish", "clean plate", "clean bowl"),
        required_action_pattern=(
            r"\b(?:serv\w*|giv\w*|bring\w*|hand\w*|pass\w*|offer\w*|plac\w*|puts?)\b"
            r".{0,65}\b(?:to|for|in front of)\s+(?:(?:a|the|another|his|her)\s+)?"
            r"(?:man|woman|person|someone|customer|guest|diner|child|children|family|husband|wife|people)\b|"
            r"\bserv\w*\s+(?:(?:a|the|another|his|her)\s+)?"
            r"(?:man|woman|person|someone|customer|guest|diner|child|children|family|people)\b")),
    "Putting Groceries & Food Away": _r(
        ("Cleaning / laundry", "Cooking"),
        ("fridge", "refrigerator", "freezer", "pantry", "cupboard", "food cabinet"),
        ("grocer", "food", "vegetable", "fruit", "milk", "bottle", "packet", "ingredient",
         "rice", "flour", "pasta", "cereal", "bread", "meat", "soup"),
        ("put", "place", "store", "arrange", "unpack", "organiz", "transfer", "sort"),
        required_action_pattern=(
            r"\b(?:puts?|plac\w*|stor\w*|arrang\w*|unpack\w*|organiz\w*|transfer\w*|sort\w*)\s+" +
            _OBJECT_MODIFIERS + r"(?:grocer\w*|food\w*|vegetable\w*|fruit\w*|milk|bottles?|packets?|"
            r"ingredients?|rice|flour|pasta|cereal|bread|meat|soup)\b"
            r"(?:(?!\b(?:beside|near|next|outside)\b).){0,65}"
            r"\b(?:in|into|inside|from|out of|to)\s+" + _OBJECT_MODIFIERS +
            r"(?:fridge|refrigerator|freezer|pantry|cupboard|food cabinet)\b")),
    "Brew Coffee or Tea": _r(
        ("Cooking", "Preparing drinks", "Bartender", "Hosting a party"),
        ("coffee", "tea", "espresso", "beverage", "cocoa", "hot chocolate", "juice", "smoothie"),
        ("brew", "steep", "grind", "tamp", "boil", "blend", "squeeze", "pour", "stir", "mix",
         "prepare", "make", "makes", "pod", "capsule", "press", "push", "dip"),
        ("reset", "clean", "wipe", "rinse", "wash", "discard", "dispose", "empty", "tidy",
         "put away", "puts away", "return", "remove"),
        unit_min_evidence_groups=1, evidence_window=None,
        action_excluded=("drinks", "drinking", "sips", "sipping"),
        required_action_pattern=rf"(?:{_BEVERAGE_PREPARED}|{_BEVERAGE_RESET})",
        required_span_patterns=(_BEVERAGE_PREPARED, _BEVERAGE_RESET)),
    "Clean and Polish Shoes": _r(
        ("Cleaning / laundry", "Daily hygiene", "Shoe shining"),
        ("shoe", "boot", "footwear"),
        ("polish", "shoe cream", "shoe wax"),
        ("brush", "buff", "shine", "rub", "wipe"),
        unit_min_evidence_groups=2,
        required_action_pattern=(
            r"\b(?:polish\w*|brush\w*|buff\w*|shin\w*|rub\w*|wipe\w*)\s+" + _OBJECT_MODIFIERS +
            r"(?:shoes?|boots?|footwear)\b(?!\s+(?:rack|box|shelf|cabinet|brush|lace))|"
            r"\bappl\w*\b.{0,35}\bpolish\b.{0,35}\bto\s+" + _OBJECT_MODIFIERS +
            r"(?:shoes?|boots?|footwear)\b")),
    "Grill Food at a Barbecue": _r(
        ("Cooking", "Grilling", "Barbecue", "Hosting a party"),
        ("grill", "barbecue", "barbeque", "bbq", "braai"),
        ("food", "meat", "chicken", "sausage", "steak", "burger", "fish", "vegetable",
         "corn", "skewer", "kebab"),
        ("cook", "grill", "flip", "turn", "tongs", "place", "put", "roast", "remove"),
        required_action_pattern=(
            r"\b(?:grills|grilling|grilled|barbecues|barbecuing)\b|"
            r"\b(?:cooks?|cooking|cooked|flips?|flipping|turns?|turning|roasts?|roasting)\b"
            r".{0,80}\b(?:on|over|in|using)\s+(?:(?:a|the)\s+)?"
            r"(?:grill|barbecue|barbeque|bbq|braai)\b")),
    "Pitch and Pack Up a Tent": _r(
        ("Camping", "Outdoor recreation", "Indoor Navigation (walking)"),
        ("tent",),
        ("pitch", "stake", "peg", "assemble", "disassemble", "dismantle", "insert", "attach", "connect", "unpack",
         "pack", "fold", "roll", "collapse", "take down", "takes down", "taking down"),
        required_action_pattern=(
            r"\b(?:pitch\w*|stake\w*|assembl\w*|disassembl\w*|dismantl\w*|unpack\w*|pack\w*|"
            r"fold\w*|roll\w*|collaps\w*|take\w*\s+down)\s+"
            r"(?:(?:up|away|a|the|camping|small|large)\s+)*tent\b|"
            r"\b(?:insert\w*|attach\w*|connect\w*)\b.{0,45}\btent (?:pole|peg|stake)\b")),
    "Replace an HVAC or Furnace Filter": _r(
        ("Fixing something in the home", "jobs related to construction", "Cleaning / laundry"),
        ("hvac", "furnace", "air conditioner", "air conditioning", "air handler", "heating unit"),
        ("filter",),
        ("replace", "remove", "insert", "install", "slide", "swap"),
        required_action_pattern=(
            r"\b(?:replac\w*|remov\w*|insert\w*|install\w*|slid\w*|swap\w*)\s+" + _OBJECT_MODIFIERS +
            r"(?:(?:hvac|furnace|air conditioner|air conditioning|air handler|heating unit)\s+"
            r"(?:air\s+)?filter|filter\s+(?:in|into|from|out of|of|for)\s+" + _OBJECT_MODIFIERS +
            r"(?:hvac|furnace|air conditioner|air conditioning|air handler|heating unit))\b")),
    "Setting the Table": _r(
        ("Cooking", "Hosting a party", "Cleaning / laundry"),
        ("table", "dining table", "dinner table"),
        ("plate", "utensil", "glass", "fork", "spoon", "knife", "napkin", "tableware",
         "cutlery", "placemat", "bowl"),
        ("set", "arrange", "place", "put", "lay", "lays"),
        required_action_pattern=(
            r"\b(?:sets?|setting|arrang\w*|lays?|laying)\s+" + _OBJECT_MODIFIERS + r"table\b|"
            r"\b(?:sets?|setting|arrang\w*|plac\w*|puts?|lays?|laying)\s+" + _OBJECT_MODIFIERS +
            r"(?:plates?|utensils?|glasses?|forks?|spoons?|knives?|napkins?|tableware|cutlery|placemats?|bowls?)\b"
            r"(?:(?!\b(?:beside|near|next|outside)\b).){0,70}"
            r"\b(?:on|onto|at)\s+" + _OBJECT_MODIFIERS + r"table\b")),
    "Set Up and Pack Away a Picnic": _r(
        ("Having a picnic", "Picnic", "Hosting a party", "Cooking"),
        ("picnic",),
        ("blanket", "food", "plate", "tableware", "basket", "cooler"),
        ("lay", "spread", "set up", "setup", "unpack", "pack", "clear", "arrange", "fold"),
        required_action_pattern=(
            r"\b(?:lay\w*|spread\w*|set\w*|unpack\w*|pack\w*|clear\w*|arrang\w*|fold\w*)\s+"
            r"(?:out\s+|up\s+|away\s+)?" + _OBJECT_MODIFIERS +
            r"picnic(?: (?:blanket|basket|food|tableware|cooler|items?))?\b|"
            r"\b(?:lay\w*|spread\w*|set\w*|unpack\w*|pack\w*|clear\w*|arrang\w*|fold\w*)\b"
            r"(?:(?!\b(?:beside|near|next|outside)\b).){0,75}"
            r"\b(?:on|onto|for)\s+" + _OBJECT_MODIFIERS + r"picnic\b")),
    "Using the Dishwasher": _r(
        ("Cleaning / laundry", "Cooking", "Household cleaners"),
        ("dishwasher", "dish washer"),
        ("dish", "plate", "cup", "bowl", "glass", "cutlery", "utensil", "spoon", "fork", "rack",
         "pan", "spatula", "colander", "strainer", "tray", "tongs", "platter", "pot",
         "chopping board", "kitchenware"),
        ("load", "unload", "put", "place", "remove", "take", "takes", "pick", "arrange", "tidy",
         "move", "start", "switch", "close", "open", "press", "push", "insert"),
        required_action_pattern=(
            r"\b(?:load\w*|unload\w*)\s+(?:(?:a|the)\s+)?dish\s?washer\b|"
            r"\b(?:load\w*|unload\w*|puts?|putting|plac\w*|remov\w*|takes?|taking|"
            r"picks? up|picking up|arrang\w*|tidy\w*|mov\w*|insert\w*)\b"
            r"(?:(?!\b(?:beside|near|next)\b).){0,110}\b(?:into|in|from|out of)\s+"
            r"(?:(?:a|the|kitchen|top|bottom|upper|lower)\s+)*"
            r"(?:(?:rack|compartment)\s+of\s+(?:(?:a|the|kitchen)\s+)*)?dish\s?washer(?:['’]s)?\b|"
            r"\b(?:puts?|putting|plac\w*|arrang\w*|tidy\w*|mov\w*|insert\w*)\b"
            r"(?:(?!\b(?:beside|near|next)\b).){0,110}\b(?:on|onto)\s+"
            r"(?:(?:a|the|kitchen|top|bottom|upper|lower)\s+)*"
            r"(?:dish\s?washer(?:['’]s)?\s+(?:(?:top|bottom|upper|lower)\s+)?rack|"
            r"rack\s+of\s+(?:(?:a|the|kitchen)\s+)*dish\s?washer)\b|"
            r"\bdish\s?washer(?:['’]s)?\s+(?:(?:top|bottom|upper|lower)\s+)?rack\b"
            r".{0,110}\b(?:puts?|putting|plac\w*|arrang\w*|tidy\w*|mov\w*)\b"
            r"(?:(?!\b(?:beside|near|next)\b).){0,70}\b(?:on|onto|in|into)\s+"
            r"(?:(?:a|the|top|bottom|upper|lower)\s+)*rack\b|"
            r"\b(?:press\w*|push\w*)\s+(?:(?:a|the)\s+)?buttons?\b.{0,40}"
            r"\b(?:on|of)\s+(?:(?:a|the|kitchen)\s+)*dish\s?washer\b|"
            r"\b(?:start\w*|switch\w*|open\w*|close\w*)\s+(?:on\s+|off\s+)?"
            r"(?:(?:a|the|kitchen)\s+)*dish\s?washer\b")),
    "Bathroom Clean & Tidy": _r(
        ("Cleaning / laundry", "Household cleaners", "Daily hygiene"),
        ("bathroom", "toilet", "shower", "bathtub", "bath tub", "washbasin", "bathroom sink"),
        ("clean", "scrub", "wipe", "wash", "rinse", "organiz", "tidy", "arrange"),
        action_excluded=("bicycle", "bike", "washing clothes", "brush washer",
                         "washes his face", "washes her face", "washes face", "brushes teeth",
                         "brushes his teeth", "brushes her teeth", "taking a shower", "takes a shower"),
        required_action_pattern=(
            r"\b(?:clean\w*|scrub\w*|wipe\w*|wash\w*|rins\w*|organiz\w*|tidy\w*|arrang\w*)\s+(?:down\s+|off\s+)?" +
            _OBJECT_MODIFIERS + r"(?:bathroom|toilet|shower|bathtub|bath tub|washbasin|bathroom sink)\b|"
            r"\b(?:clean\w*|scrub\w*|wipe\w*|wash\w*|rins\w*|organiz\w*|tidy\w*|arrang\w*)\b"
            r"(?:(?!\b(?:beside|near|next|outside)\b).){0,70}"
            r"\b(?:in|inside)\s+" + _OBJECT_MODIFIERS + r"bathroom\b")),
})

TASK_ALIASES: dict[str, str] = {
    "Check & Add Engine Oil": "Check & add engine oil",
    "Change a Tire": "Change a tire",
    "Check Tire Pressure & Add Air": "Check tire pressure & add air",
    "Replace Showerhead": "Replace showerhead",
    "Stack Firewood": "Stack firewood",
    "Sweep the Porch": "Sweep the porch",
    "Pool Cleaning": "Pool cleaning",
    "Replace Bulbs or Batteries": "Replace Bulbs & Batteries",
    "Taking Out Trash": "Taking Out the Trash",
    "Tighten Cabinet or Door Hinges": "Tighten Cabinet & Door Hinges",
    "Trim Hedges and Branches": "Trim a hedge",
    "Bathroom Deep Clean": "Clean the Bathroom",
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


def canonical_task_name(task_name: str) -> str:
    normalized = _norm(task_name)
    for name in TASK_RULES:
        if _norm(name) == normalized:
            return name
    for alias, canonical in TASK_ALIASES.items():
        if _norm(alias) == normalized:
            return canonical
    return task_name.strip()


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


# These tasks identify the action/object in the timed annotation itself.
# Location-dependent and broad one-group rules still require their scenario.
_NARRATED_CROSS_SCENARIO_TASKS = frozenset({
    "Change a tire", "Check & add engine oil", "Check tire pressure & add air",
    "Clean the Bathroom", "Clean Appliance", "Change Sheets & Make Bed",
    "Folding Clothes or Putting Them on Hangers", "Hanging clothes on hangers",
    "Using the Laundry Machine", "Loading the Laundry Machine",
    "Unloading the Laundry Machine", "Hand Washing Clothes",
    "Pet Grooming & Bath", "Pet Feeding", "Scoop a litter box",
    "Pump Gas", "Shoveling Snow", "Stack firewood", "Trim a hedge",
    "Pull weeds by hand", "Planting or Pulling Weeds", "Spread Mulch",
    "Spreading Mulch or Fertilizer", "Leaf Raking or Blowing",
    "Leaf Raking & Bagging", "Replace Showerhead", "Replace showerhead",
    "Replace Bulbs & Batteries", "Tighten Cabinet & Door Hinges",
    "Hang Curtains", "Hang Art & Mirrors", "Shelve books",
    "Move furniture with someone (2+ people required)", "Receive a delivery at the door",
    "Clean and organize gym equipment", "Carry items up and down stairs", "Serve food",
    "Putting Groceries & Food Away", "Brew Coffee or Tea", "Clean and Polish Shoes",
    "Grill Food at a Barbecue", "Pitch and Pack Up a Tent", "Replace an HVAC or Furnace Filter",
    "Setting the Table", "Set Up and Pack Away a Picnic", "Using the Dishwasher",
    "Bathroom Clean & Tidy", "Holiday Decoration Setup", "Organize the Garage",
})


def score_narrated_scenarios(task_name: str, rule: TaskRule,
                             scenarios: Iterable[str]) -> int | None:
    """A parent label ranks evidence; it cannot erase a specific timed action.

    This fallback only permits extracting a verified span, never approving an
    entire video from its label. Explicit exclusions remain authoritative.
    """
    scenarios = tuple(scenarios)
    score = score_scenarios(rule, scenarios)
    if score is not None:
        return score
    actual = {_norm(str(s)) for s in scenarios if str(s).strip()}
    if any(_has(actual, term) for term in rule.excluded):
        return None
    if (canonical_task_name(task_name) in _NARRATED_CROSS_SCENARIO_TASKS
            and len(rule.evidence) >= 2):
        return 0
    return None


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
# Verbo de outra atividade. Continua na conta da prova e pode derrubar o título.
_FOREIGN_ACTIVITY_RE = re.compile(
    r"\b(?:walk(?:s|ing)?|runs?|running|plays?|playing|cook(?:s|ing)?|"
    r"eats?|eating|drinks?|drinking|reads?|reading|writes?|writing|"
    r"drives?|driving|talks?|talking|speaks?|speaking|wash(?:es|ing)?|"
    r"cleans?|cleaning|cuts?|cutting|paints?|painting|sews?|sewing|"
    r"types?|typing|swims?|swimming|cycles?|cycling)\b",
    re.I,
)


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
    _background_checkpoint()
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
    _background_checkpoint()
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
# Folga do núcleo denso que autoriza estender um clipe de dez minutos.
ACTIVITY_TARGET_MAX_GAP_S = 30.0
LONG_ACTIVITY_MIN_S = 300.0
LONG_ACTIVITY_CONTEXT_MIN_S = 600.0
# Explicit human scene labels can support five-minute windows. They do not
# require the ten-minute threshold used for expanding sparse action evidence.
SCENARIO_ACTIVITY_MIN_S = 300.0
# Duas falas que já provam a ação podem ficar até três minutos distantes.
LONG_ACTIVITY_TARGET_MAX_GAP_S = 180.0
LONG_ACTIVITY_CONTEXT_S = 300.0
SCENARIO_BOUNDARY_BUFFER_S = 60.0
# Outra atividade narrada sem pausa por um minuto encerra o take.
# Um verbo isolado no meio da prova não encerra.
FOREIGN_ACTIVITY_BURST_S = 60.0
FOREIGN_ACTIVITY_BURST_GAP_S = 20.0


# Relações pai/filho: uma evidência da tarefa ampla não é concorrência para a
# específica (nem vice-versa). Tarefas irmãs continuam concorrentes: aparar a
# cerca deve encerrar um trecho de arrancar ervas, por exemplo.
_TASK_CONTAINS: dict[str, frozenset[str]] = {
    "Gardening": frozenset({"Pull weeds by hand", "Trim a hedge",
        "Planting or Pulling Weeds", "Watering Outdoor Plants",
        "Spreading Mulch or Fertilizer", "Leaf Raking or Blowing"}),
    "Full Yard Maintenance": frozenset({
        "Gardening", "Pull weeds by hand", "Trim a hedge",
        "Leaf Raking & Bagging",
        "Planting or Pulling Weeds", "Watering Outdoor Plants",
        "Spreading Mulch or Fertilizer", "Leaf Raking or Blowing",
    }),
    "Car Wash & Detail": frozenset({"Cleaning Out Car"}),
    "Pet Care Routine": frozenset({"Pet Grooming & Bath", "Pet Feeding"}),
    "Party Setup & Takedown": frozenset({
        "Drink Station Setup", "Party Cleanup", "Setting the Table", "Serve food",
        "Holiday Decoration Setup",
    }),
    "Unpack & Set Up a Room": frozenset({"Furniture Assembly"}),
    "Furniture Assembly/ Disassembly": frozenset({"Furniture Assembly"}),
    "Cleaning Car": frozenset({"Car Wash & Detail", "Cleaning Out Car"}),
    "Planting or Pulling Weeds": frozenset({"Pull weeds by hand"}),
    "Folding Clothes or Putting Them on Hangers": frozenset({"Hanging clothes on hangers"}),
    "Using the Laundry Machine": frozenset({"Loading the Laundry Machine", "Unloading the Laundry Machine"}),
    "Spreading Mulch or Fertilizer": frozenset({"Spread Mulch"}),
    "Leaf Raking or Blowing": frozenset({"Leaf Raking & Bagging"}),
    "Pack or Unpack a Car for a Trip": frozenset({"Pack the Car for a Trip"}),
    "Putting Groceries & Food Away": frozenset({"Putting Groceries Away"}),
    "Bathroom Clean & Tidy": frozenset({"Clean the Bathroom"}),
    "Drink Station Setup": frozenset({"Brew Coffee or Tea"}),
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
    allowed_intervals: Iterable[tuple[float, float]] | None = None,
    strict_gaps: bool = False,
) -> list[dict[str, Any]]:
    """Isola sessões contínuas entre evidências recorrentes da mesma tarefa.

    A fala que já prova a ação pode ficar até três minutos da próxima. O
    silêncio nesse intervalo continua a mesma tarefa. Higiene, exclusão e
    tarefa concorrente continuam encerrando o trecho imediatamente.
    """
    if allowed_intervals is not None:
        # Recorte ANTES de procurar a maior sessão. Se o IMU termina no meio
        # de uma ação longa, descartar a sessão inteira perde o início válido.
        # Relógios relativos também mantêm a expansão do modo longo dentro
        # de cada componente, sem preencher lacunas reais dos sensores.
        intervals: list[tuple[float, float]] = []
        for raw_start, raw_end in sorted(allowed_intervals):
            start = max(0.0, float(raw_start))
            end = float(raw_end)
            if video_duration_s is not None:
                end = min(end, float(video_duration_s))
            if end <= start:
                continue
            if intervals and start <= intervals[-1][1]:
                intervals[-1] = (intervals[-1][0], max(intervals[-1][1], end))
            else:
                intervals.append((start, end))
        covered: list[dict[str, Any]] = []
        for start, end in intervals:
            if end - start < min_s:
                continue
            rows = [(t - start, text, normed, on_task, boundary)
                    for t, text, normed, on_task, boundary in flagged
                    if start <= t <= end]
            for span in _activity_spans(
                    rule, rows, min_s=min_s, max_s=max_s, max_gap_s=max_gap_s,
                    video_duration_s=end - start, allowed_intervals=None,
                    strict_gaps=strict_gaps):
                covered.append({**span, "start": span["start"] + start,
                                "end": span["end"] + start})
        return covered

    strict_cores: list[dict[str, Any]] = []
    expand_context = min_s >= LONG_ACTIVITY_CONTEXT_MIN_S
    if expand_context:
        # Primeiro encontra uma ação realmente contínua com os limites
        # conservadores. Ela será a prova semântica para qualquer extensão.
        strict_cores = _activity_spans(
            rule,
            flagged,
            min_s=60.0,
            max_s=max_s,
            max_gap_s=max_gap_s,
            video_duration_s=video_duration_s,
            allowed_intervals=None,
            strict_gaps=True,
        )

    # O núcleo que autoriza estender o clipe continua denso. O clipe normal
    # usa a mesma folga de três minutos em qualquer duração de 1 a 30 min.
    if strict_gaps:
        row_gap_limit = max_gap_s
        target_gap_limit = ACTIVITY_TARGET_MAX_GAP_S
    else:
        row_gap_limit = LONG_ACTIVITY_TARGET_MAX_GAP_S
        target_gap_limit = LONG_ACTIVITY_TARGET_MAX_GAP_S
    target_runs: list[list[int]] = []
    current: list[int] = []
    previous_row: int | None = None
    previous_target: int | None = None
    for idx, row in enumerate(flagged):
        t, _text, _normed, on_task, boundary = row
        row_gap = (
            t - flagged[previous_row][0] if previous_row is not None else 0.0
        )
        if boundary or (previous_row is not None and row_gap > row_gap_limit):
            if current:
                target_runs.append(current)
            current = []
            previous_target = None
            # A higiene ou outra tarefa não inicia o próximo take. A fala que
            # só chegou depois da folga continua sendo prova da ação.
            if boundary or not on_task:
                previous_row = idx
                continue
        if on_task:
            target_gap = (
                t - flagged[previous_target][0]
                if previous_target is not None else 0.0
            )
            if (previous_target is not None
                    and target_gap > target_gap_limit):
                if current:
                    target_runs.append(current)
                current = []
            elif (previous_target is not None and current
                    and _sustained_foreign_activity(
                        flagged, previous_target, idx)):
                # Um minuto seguido de outra atividade não entra no título,
                # mesmo quando a prova dos dois lados ainda passaria junta.
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
            chosen = _longest_proven_target(
                rule, flagged, targets, cursor, min_s, max_s)
            if chosen is None:
                cursor += 1
                continue
            pos, score, units = chosen
            a = targets[cursor]
            b = targets[pos]
            start = max(0.0, flagged[a][0])
            end = flagged[b][0]
            if video_duration_s:
                end = min(end, float(video_duration_s))
            spans.append({
                "start": start,
                "end": end,
                "action_text": " ".join(units),
                "action_units": units,
                "match_score": score,
                "n_events": b - a + 1,
            })
            cursor = pos + 1

    if expand_context:
        # Um núcleo verificado de alguns minutos pode estar dentro de uma
        # tomada longa da mesma atividade. Expanda no máximo cinco minutos de
        # cada lado e jamais atravesse uma anotação insegura/concorrente.
        for core in strict_cores:
            core_start = float(core["start"])
            core_end = float(core["end"])
            corridor_start = max(0.0, core_start - LONG_ACTIVITY_CONTEXT_S)
            corridor_end = core_end + LONG_ACTIVITY_CONTEXT_S
            if video_duration_s:
                corridor_end = min(corridor_end, float(video_duration_s))
            for t, _text, _normed, _on_task, boundary in flagged:
                if not boundary:
                    continue
                if t <= core_start:
                    corridor_start = max(corridor_start, t + 0.001)
                elif t >= core_end:
                    corridor_end = min(corridor_end, t - 0.001)
                    break
            available = corridor_end - corridor_start
            if available < min_s:
                continue
            duration = min(max_s, available)
            midpoint = (core_start + core_end) / 2.0
            start = max(corridor_start, midpoint - duration / 2.0)
            end = start + duration
            if end > corridor_end:
                end = corridor_end
                start = end - duration
            window_rows = [
                row for row in flagged
                if start <= row[0] <= end and not row[4]
            ]
            units = [row[2] for row in window_rows if row[2]]
            expanded = {
                "start": start,
                "end": end,
                "action_text": " ".join(units),
                "action_units": units,
                "match_score": core["match_score"],
                "n_events": len(window_rows),
                "expanded_from_verified_core": True,
            }
            if not any(
                    abs(float(existing["start"]) - start) < 1e-6
                    and abs(float(existing["end"]) - end) < 1e-6
                    for existing in spans):
                spans.append(expanded)
    return spans


def scenario_activity_spans(
    flagged: list[tuple[float, str, str, bool, bool]],
    *,
    min_s: float,
    max_s: float,
    video_duration_s: float,
    allowed_intervals: Iterable[tuple[float, float]] | None = None,
) -> list[dict[str, Any]]:
    """Cria janelas longas quando o cenário Ego4D equivale à tarefa.

    Um cenário suficiente é uma anotação humana do vídeo inteiro, não uma
    inferência por palavra. Ainda assim, cada evento inseguro ou concorrente
    remove um minuto dos dois lados antes de as janelas serem formadas.
    """
    duration = max(0.0, float(video_duration_s or 0.0))
    if duration < min_s:
        return []
    blocked: list[tuple[float, float]] = []
    for t, _text, _normed, _on_task, boundary in flagged:
        if boundary:
            blocked.append((
                max(0.0, t - SCENARIO_BOUNDARY_BUFFER_S),
                min(duration, t + SCENARIO_BOUNDARY_BUFFER_S),
            ))
    merged: list[tuple[float, float]] = []
    for start, end in sorted(blocked):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    sources = ([(0.0, duration)] if allowed_intervals is None
               else list(allowed_intervals))
    safe: list[tuple[float, float]] = []
    for source_start, source_end in sources:
        source_start = max(0.0, float(source_start))
        source_end = min(duration, float(source_end))
        if source_end - source_start < min_s:
            continue
        cursor = source_start
        for start, end in merged:
            if end <= source_start or start >= source_end:
                continue
            clipped_start = max(source_start, start)
            clipped_end = min(source_end, end)
            if clipped_start - cursor >= min_s:
                safe.append((cursor, clipped_start))
            cursor = max(cursor, clipped_end)
        if source_end - cursor >= min_s:
            safe.append((cursor, source_end))

    spans: list[dict[str, Any]] = []
    for safe_start, safe_end in safe:
        safe_duration = safe_end - safe_start
        # Produza a maior quantidade de vídeos independentes, sem sobrepor
        # frames. Um corredor seguro de 24 min vira dois takes de ~12 min, em
        # vez de apenas um take monolítico.
        count = max(1, int(safe_duration // min_s))
        window_duration = min(max_s, safe_duration / count)
        for index in range(count):
            start = safe_start + index * window_duration
            end = start + window_duration
            if not min_s <= end - start <= max_s + 1e-6:
                continue
            rows = [row for row in flagged if start <= row[0] <= end]
            units = [row[2] for row in rows if row[2]]
            spans.append({
                "start": start,
                "end": end,
                "action_text": " ".join(units),
                "action_units": units,
                "match_score": 100,
                "n_events": len(rows),
                "scenario_verified": True,
            })
    return spans


def _evidence_hits(segment: str, rule: TaskRule) -> list[int]:
    return [sum(_term_in(segment, term) for term in group)
            for group in rule.evidence]


@lru_cache(maxsize=131072)
def _unit_on_task(segment: str, rule: TaskRule) -> bool:
    """Uma fala só conta quando traz evidência suficiente da própria ação."""
    if not rule.evidence:
        return False
    if (rule.required_action_pattern is not None
            and not _required_action_pattern(rule.required_action_pattern).search(segment)):
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


@lru_cache(maxsize=256)
def _required_action_pattern(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


def _sustained_foreign_activity(
    flagged: list[tuple[float, str, str, bool, bool]],
    left: int,
    right: int,
) -> bool:
    """Verdadeiro quando outra atividade ocupa um minuto entre duas provas."""
    burst_start: float | None = None
    last_t: float | None = None
    for index in range(left + 1, right):
        t, _text, normed, on_task, boundary = flagged[index]
        foreign = (
            not on_task and not boundary and bool(normed)
            and _FOREIGN_ACTIVITY_RE.search(normed) is not None
        )
        if not foreign:
            burst_start = None
            last_t = None
            continue
        if (burst_start is None or last_t is None
                or t - last_t > FOREIGN_ACTIVITY_BURST_GAP_S):
            burst_start = t
        last_t = t
        if t - burst_start >= FOREIGN_ACTIVITY_BURST_S:
            return True
    return False


def _kept_clears_on_task_gate(segments: list[str], rule: TaskRule) -> bool:
    """O mesmo piso de proporção de score_action, sem recontar a evidência."""
    if not segments:
        return False
    flags = [_unit_on_task(segment, rule) for segment in segments]
    on_task = sum(flags)
    count = len(flags)
    if count >= ON_TASK_RATIO_UNITS:
        return (_longest_true_run(flags) >= ON_TASK_MIN_STREAK
                or on_task / count >= ON_TASK_MIN_RATIO)
    return on_task >= max(1, (count + 1) // 2)


def _is_activity_filler(normed: str, rule: TaskRule) -> bool:
    """Fala que não prova esta tarefa nem descreve outra atividade.

    "Olha em volta" entre duas provas não muda o título. "Caminha" ou
    "lava" continuam na conta, para um passeio não virar jardinagem.
    """
    if not normed or _unit_on_task(normed, rule):
        return False
    if any(_evidence_group_present(normed, group) for group in rule.evidence):
        return False
    return _FOREIGN_ACTIVITY_RE.search(normed) is None


def _activity_span_score(
    rule: TaskRule, rows: list[tuple[float, str, str, bool, bool]],
) -> tuple[int | None, list[str]]:
    """Prova o trecho sem deixar ruído de câmera apagar a ação."""
    full = [row[2] for row in rows if row[2]]
    if not full:
        return None, full
    full_text = " ".join(full)
    if any(_term_in(full_text, term) for term in rule.action_excluded):
        return None, full
    kept = [unit for unit in full if not _is_activity_filler(unit, rule)]
    if not kept or not _kept_clears_on_task_gate(kept, rule):
        return None, full
    return score_action(rule, " ".join(kept), kept), full


def _longest_proven_target(
    rule: TaskRule,
    flagged: list[tuple[float, str, str, bool, bool]],
    targets: list[int],
    origin: int,
    min_s: float,
    max_s: float,
) -> tuple[int, int, list[str]] | None:
    """O maior fim que ainda passa na prova. Se o trecho cheio falha, o miolo fica."""
    start = flagged[targets[origin]][0]
    hi: int | None = None
    for pos in range(len(targets) - 1, origin - 1, -1):
        duration = flagged[targets[pos]][0] - start
        if duration > max_s + 1e-6:
            continue
        hi = pos
        break
    if hi is None or flagged[targets[hi]][0] - start < min_s:
        return None
    score, units = _activity_span_score(
        rule, flagged[targets[origin]:targets[hi] + 1])
    if score is not None:
        return hi, score, units

    left = targets[origin]
    limit = targets[hi]
    for index in range(left, limit + 1):
        normed = flagged[index][2]
        if normed and any(_term_in(normed, term) for term in rule.action_excluded):
            limit = index - 1
            break
    while hi > origin and targets[hi] > limit:
        hi -= 1
    if flagged[targets[hi]][0] - start < min_s:
        return None

    on_flags: list[bool] = []
    kept_at: list[int] = []
    for index in range(left, targets[hi] + 1):
        normed = flagged[index][2]
        if not normed or _is_activity_filler(normed, rule):
            continue
        on_flags.append(_unit_on_task(normed, rule))
        kept_at.append(index)
    if not on_flags:
        return None
    prefix_on: list[int] = []
    prefix_best: list[int] = []
    streak = best = on_count = 0
    for flag in on_flags:
        if flag:
            streak += 1
            on_count += 1
            best = max(best, streak)
        else:
            streak = 0
        prefix_on.append(on_count)
        prefix_best.append(best)

    count = len(on_flags)
    for pos in range(hi, origin - 1, -1):
        end_index = targets[pos]
        if flagged[end_index][0] - start < min_s:
            break
        while count > 0 and kept_at[count - 1] > end_index:
            count -= 1
        if count == 0:
            continue
        on_count = prefix_on[count - 1]
        if count >= ON_TASK_RATIO_UNITS:
            clears = (prefix_best[count - 1] >= ON_TASK_MIN_STREAK
                      or on_count / count >= ON_TASK_MIN_RATIO)
        else:
            clears = on_count >= max(1, (count + 1) // 2)
        if not clears:
            continue
        score, units = _activity_span_score(
            rule, flagged[left:end_index + 1])
        if score is not None:
            return pos, score, units
        # Mais evidência não aparece ao encurtar o mesmo começo.
        return None
    return None


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
    if (rule.required_action_pattern is not None
            and not any(_unit_on_task(segment, rule) for segment in segments)):
        return None
    if any(not any(_required_action_pattern(pattern).search(segment) for segment in segments)
           for pattern in rule.required_span_patterns):
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
        uncertain = bool(re.search(r"#\s*unsure\b", text, re.I))
        # `#unsure` significa apenas que o anotador não reconheceu um objeto.
        # Não serve como evidência semântica, mas também não representa por si
        # só celular, pessoa sentada ou troca de tarefa.
        wearer_text = "" if uncertain else " ".join(
            _camera_wearer_segments(text))
        rows.append((t, text, wearer_text, _narration_is_dirty(text)))
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
        _background_checkpoint()
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
    allowed_intervals: Iterable[tuple[float, float]] | None = None,
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
            max_gap_s=max_gap_s, video_duration_s=video_duration_s,
            allowed_intervals=allowed_intervals)

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
            if (rule.min_span_s is not None
                    and float(clip.get("dur_s") or 0) < rule.min_span_s):
                continue
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

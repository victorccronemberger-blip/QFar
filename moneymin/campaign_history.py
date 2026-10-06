"""Campaign history namespace, excluding reserved Library metadata suffixes."""


def is_campaign_history_name(name: str) -> bool:
    return (name.startswith("campaign_") and name.endswith(".json")
            and not name.endswith((".source.json", ".metadata.json")))


def is_library_marker_name(name: str) -> bool:
    return name.casefold().endswith((".source.json", ".metadata.json",
                                    ".source.json.tmp", ".metadata.json.tmp"))

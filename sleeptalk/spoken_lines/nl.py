"""Dutch (nl). Je form, spoken register.

Written by Claude Fable 5.1 on 2026-09-17; not yet checked by a native
speaker. Review the empathy pool first: it must stay neutral.
"""

PREAMBLE_POOLS = {
    "ack": ("Oké.", "Prima.", "Begrepen.", "Goed.", "Ja.", "Duidelijk."),
    "action": (
        "Even kijken.", "Ik check het.", "Ik ga het na.",
        "Ik kijk er nu naar.", "Even opzoeken.", "Momentje, ik check het.",
    ),
    "ack_action": (
        "Oké, even kijken.", "Begrepen, ik check het.",
        "Prima, ik ga het na.", "Goed, even checken.",
    ),
    "action:task": ("Komt goed.", "Begrepen, ik ga ermee aan de slag.", "Doe ik nu.", "Ben ermee bezig."),
    "ack:empathy": ("Oh, ik snap het.", "Ah, oké.", "Hm, ik begrijp het."),
    "working": ("Nog mee bezig.", "Ik werk er nog aan."),
    "control": ("Gestopt.", "Oké, ik stop."),
    "greeting": ("Hoi, ik ben er.", "Hoi hoi.", "Hallo hallo."),
    "cron_offer": (
        "Welkom terug. Wil je dat ik het laatste van je geplande taken doorneem?",
    ),
    "reassure": (
        "Ik werk er nog aan.",
        "Momentje, ik ben nog bezig.",
        "Nog niet klaar, bijna.",
        "Bijna klaar, momentje.",
        "Het duurt even, maar ik ben ermee bezig.",
        "Ik ben je niet vergeten, nog mee bezig.",
        "Ik check het nog.",
        "Heb even geduld.",
        "Ik heb hier iets langer voor nodig.",
        "Nog een momentje, ik check het.",
    ),
}

REASSURE_TOOL_POOLS = {
    "read": ("Ik lees het nog.", "Ik ga het bestand nog door.", "Ik zit nog in dat bestand."),
    "edit": ("Ik maak de wijzigingen nog.", "De wijzigingen lopen nog.", "Ik werk nog aan de aanpassingen."),
    "write": ("Ik schrijf het nog.", "Ik schrijf het bestand nog.", "Het bestand wordt nog geschreven."),
    "bash": (
        "Dat commando loopt nog.", "Ik voer het commando nog uit.",
        "Het commando is nog bezig.", "Dat commando duurt even.",
    ),
    "search": ("Ik zoek nog.", "Ik zoek het nog op.", "Het zoeken duurt even.", "Ik haal de resultaten nog op."),
    "other": ("Ik werk er nog aan.", "Momentje, ik ben nog bezig.", "Nog mee bezig, momentje."),
}

NARRATION_PHRASES = {
    "read": "Ik lees een bestand.",
    "edit": "Ik bewerk code.",
    "write": "Ik schrijf een bestand.",
    "bash": "Ik voer een commando uit.",
    "search": "Ik zoek.",
    "task": "Ik start een subtaak.",
    "mcp": "Ik gebruik een van mijn tools.",
    "other": "Ik werk eraan.",
}

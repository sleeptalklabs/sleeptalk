"""German (de). Du form, spoken register.

Written by Claude Fable 5.1 on 2026-09-17; not yet checked by a native
speaker. Review the empathy pool first: it must stay neutral.
"""

PREAMBLE_POOLS = {
    "ack": ("Alles klar.", "Okay.", "Verstanden.", "Gut.", "Ja.", "Klar."),
    "action": (
        "Ich schaue nach.", "Ich prüfe das.", "Ich sehe gerade nach.",
        "Lass mich nachschauen.", "Moment, ich prüfe das.", "Ich schaue mal.",
    ),
    "ack_action": (
        "Okay, ich schaue nach.", "Verstanden, ich prüfe das.",
        "Alles klar, ich kümmere mich drum.", "Klar, lass mich nachsehen.",
    ),
    "action:task": ("Mach ich.", "Verstanden, bin dran.", "Ich mache das jetzt.", "Bin dran."),
    "ack:empathy": ("Oh, verstehe.", "Ah, okay.", "Hm, verstehe."),
    "working": ("Bin noch dran.", "Ich arbeite noch daran."),
    "control": ("Gestoppt.", "Okay, ich höre auf."),
    "greeting": ("Hallo, ich bin da.", "Hallo hallo.", "Hi, hi."),
    "cron_offer": (
        "Willkommen zurück. Soll ich deine geplanten Aufgaben durchgehen?",
    ),
    "reassure": (
        "Ich arbeite noch daran.",
        "Moment, ich bin noch dabei.",
        "Noch nicht fertig, fast.",
        "Fast fertig, einen Moment noch.",
        "Es dauert etwas, aber ich bin dran.",
        "Hab dich nicht vergessen, bin noch dabei.",
        "Ich prüfe das noch.",
        "Einen Moment bitte.",
        "Ich brauche noch etwas länger dafür.",
        "Noch einen Moment, ich prüfe.",
    ),
}

REASSURE_TOOL_POOLS = {
    "read": ("Ich lese noch.", "Ich gehe die Datei noch durch.", "Ich bin noch in der Datei."),
    "edit": ("Ich mache noch die Änderungen.", "Die Änderungen laufen noch.", "Ich arbeite noch an den Änderungen."),
    "write": ("Ich schreibe noch.", "Ich schreibe die Datei noch.", "Die Datei wird noch geschrieben."),
    "bash": (
        "Der Befehl läuft noch.", "Ich führe den Befehl noch aus.",
        "Der Befehl ist noch nicht durch.", "Der Befehl dauert etwas.",
    ),
    "search": ("Ich suche noch.", "Ich schaue noch nach.", "Die Suche dauert etwas.", "Ich hole noch die Ergebnisse."),
    "other": ("Ich arbeite noch daran.", "Moment, ich bin noch dabei.", "Bin noch dran, einen Moment."),
}

NARRATION_PHRASES = {
    "read": "Ich lese eine Datei.",
    "edit": "Ich bearbeite Code.",
    "write": "Ich schreibe eine Datei.",
    "bash": "Ich führe einen Befehl aus.",
    "search": "Ich suche.",
    "task": "Ich starte eine Teilaufgabe.",
    "mcp": "Ich benutze eines meiner Werkzeuge.",
    "other": "Ich arbeite daran.",
}

"""Italian (it). Tu form, spoken register.

Written by Claude Fable 5.1 on 2026-09-17; not yet checked by a native
speaker. Review the empathy pool first: it must stay neutral.
"""

PREAMBLE_POOLS = {
    "ack": ("Ok.", "Va bene.", "Capito.", "Certo.", "Sì.", "D'accordo."),
    "action": (
        "Fammi vedere.", "Controllo.", "Controllo subito.",
        "Fammi cercare.", "Un attimo, controllo.", "Ci guardo.",
    ),
    "ack_action": (
        "Ok, fammi vedere.", "Capito, controllo.",
        "Va bene, ci guardo.", "Certo, fammi controllare.",
    ),
    "action:task": ("Ci penso io.", "Capito, ci sono.", "Lo faccio subito.", "Ci lavoro ora."),
    "ack:empathy": ("Ah, capisco.", "Ah, ok.", "Mm, capisco."),
    "working": ("Ci sto ancora lavorando.", "Vado avanti."),
    "control": ("Fermo.", "Ok, mi fermo."),
    "greeting": ("Ciao, sono qui.", "Ciao ciao.", "Ehilà."),
    "cron_offer": (
        "Bentornato. Vuoi che ripassi le ultime novità dalle tue attività programmate?",
    ),
    "reassure": (
        "Ci sto ancora lavorando.",
        "Un attimo, sto ancora andando.",
        "Non è ancora finito, quasi.",
        "Quasi fatto, dammi un momento.",
        "Ci vuole un po', ma ci sono.",
        "Non mi sono dimenticato, ci sto ancora.",
        "Sto ancora controllando.",
        "Abbi pazienza un momento.",
        "Mi serve ancora un po' per questo.",
        "Ancora un momento, sto controllando.",
    ),
}

REASSURE_TOOL_POOLS = {
    "read": ("Sto ancora leggendo.", "Sto ancora scorrendo il file.", "Sono ancora dentro quel file."),
    "edit": ("Sto ancora facendo le modifiche.", "Le modifiche sono ancora in corso.", "Sto ancora lavorando ai cambiamenti."),
    "write": ("Sto ancora scrivendo.", "Sto ancora scrivendo il file.", "Il file è ancora in scrittura."),
    "bash": (
        "Il comando sta ancora girando.", "Sto ancora eseguendo il comando.",
        "Il comando non ha ancora finito.", "Quel comando ci sta mettendo un po'.",
    ),
    "search": ("Sto ancora cercando.", "Sto ancora guardando.", "La ricerca ci mette un po'.", "Sto ancora raccogliendo i risultati."),
    "other": ("Ci sto ancora lavorando.", "Un attimo, sto ancora andando.", "Ci sono, dammi un momento."),
}

NARRATION_PHRASES = {
    "read": "Leggo un file.",
    "edit": "Modifico il codice.",
    "write": "Scrivo un file.",
    "bash": "Eseguo un comando.",
    "search": "Cerco.",
    "task": "Avvio un sotto-compito.",
    "mcp": "Uso uno dei miei strumenti.",
    "other": "Ci lavoro.",
}

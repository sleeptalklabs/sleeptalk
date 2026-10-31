"""French (fr). Tutoiement, spoken register.

Written by Claude Fable 5.1 on 2026-09-17; not yet checked by a native
speaker. Review the empathy pool first: it must stay neutral.
"""

PREAMBLE_POOLS = {
    "ack": ("D'accord.", "Ok.", "Compris.", "Bien.", "Oui.", "Entendu."),
    "action": (
        "Je regarde.", "Je vérifie ça.", "Je regarde ça tout de suite.",
        "Laisse-moi chercher.", "Une seconde, je vérifie.", "Je vais voir.",
    ),
    "ack_action": (
        "D'accord, je regarde.", "Compris, je vérifie.",
        "Ok, je m'en occupe.", "Bien, laisse-moi vérifier.",
    ),
    "action:task": ("Je m'en occupe.", "Compris, j'y vais.", "Je le fais tout de suite.", "C'est parti."),
    "ack:empathy": ("Ah, je vois.", "Ah, d'accord.", "Hmm, je comprends."),
    "working": ("J'y suis encore.", "Je continue."),
    "control": ("Arrêté.", "D'accord, j'arrête."),
    "greeting": ("Salut, je suis là.", "Salut salut.", "Bonjour, bonjour."),
    "cron_offer": (
        "Content de te revoir. Je te fais le point sur tes tâches planifiées ?",
    ),
    "reassure": (
        "J'y travaille encore.",
        "Patience, ça avance.",
        "Pas encore fini, presque.",
        "Presque, encore un instant.",
        "Ça prend un peu de temps, mais j'y suis.",
        "Je ne t'ai pas oublié, j'y suis encore.",
        "Je vérifie encore.",
        "Un instant, s'il te plaît.",
        "Il me faut encore un peu de temps.",
        "Encore un instant, je vérifie.",
    ),
}

REASSURE_TOOL_POOLS = {
    "read": ("Je lis encore.", "Je parcours encore le fichier.", "Je suis encore dans ce fichier."),
    "edit": ("Je fais encore les modifications.", "Les modifications sont en cours.", "Je travaille encore sur les changements."),
    "write": ("J'écris encore.", "J'écris encore le fichier.", "Le fichier est en cours d'écriture."),
    "bash": (
        "La commande tourne encore.", "Je lance encore la commande.",
        "La commande est toujours en cours.", "Cette commande prend du temps.",
    ),
    "search": ("Je cherche encore.", "Je regarde encore.", "La recherche prend un peu de temps.", "Je récupère encore les résultats."),
    "other": ("J'y travaille encore.", "Patience, ça avance.", "J'y suis, un instant."),
}

NARRATION_PHRASES = {
    "read": "Je lis un fichier.",
    "edit": "Je modifie du code.",
    "write": "J'écris un fichier.",
    "bash": "Je lance une commande.",
    "search": "Je cherche.",
    "task": "Je lance une sous-tâche.",
    "mcp": "J'utilise un de mes outils.",
    "other": "J'y travaille.",
}

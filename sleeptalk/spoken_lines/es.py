"""Spanish (es). Neutral register, tú form, works for Spain and Latin America.

Written by Claude Fable 5.1 on 2026-09-17; not yet checked by a native
speaker. Review the empathy pool first: it must stay neutral.
"""

PREAMBLE_POOLS = {
    "ack": ("Vale.", "De acuerdo.", "Entendido.", "Claro.", "Sí.", "Bien."),
    "action": (
        "Déjame ver.", "Lo miro ahora.", "Voy a comprobarlo.",
        "Un momento, lo reviso.", "Déjame buscarlo.", "Un segundo, lo compruebo.",
    ),
    "ack_action": (
        "Vale, déjame ver.", "Entendido, lo compruebo.",
        "Claro, lo miro ahora.", "De acuerdo, un momento.",
    ),
    "action:task": ("Voy.", "Entendido, me pongo.", "Lo hago ahora.", "En ello."),
    "ack:empathy": ("Ah, entiendo.", "Vaya, ya veo.", "Mm, entiendo."),
    "working": ("Sigo en ello.", "Todavía trabajando."),
    "control": ("Parado.", "Vale, paro."),
    "greeting": ("Hola, aquí estoy.", "Hola, hola.", "Buenas."),
    "cron_offer": (
        "Bienvenido de nuevo. ¿Quieres que repase lo último de tus tareas programadas?",
    ),
    "reassure": (
        "Sigo trabajando en ello.",
        "Un momento, todavía sigo.",
        "Aún no está, casi.",
        "Casi listo, dame un momento.",
        "Está tardando un poco, pero sigo.",
        "No me he olvidado, sigo en ello.",
        "Sigo comprobándolo.",
        "Ten paciencia un momento.",
        "Dame un poco más con esto.",
        "Un momento más, sigo mirando.",
    ),
}

REASSURE_TOOL_POOLS = {
    "read": ("Sigo leyéndolo.", "Todavía repasando el archivo.", "Sigo revisando ese archivo."),
    "edit": ("Sigo con los cambios.", "Los cambios siguen en marcha.", "Todavía aplicando cambios."),
    "write": ("Sigo escribiéndolo.", "Todavía escribiendo el archivo.", "El archivo se está escribiendo."),
    "bash": (
        "Ese comando sigue en marcha.", "Sigo ejecutando el comando.",
        "El comando todavía va.", "Ese comando está tardando.",
    ),
    "search": ("Sigo buscando.", "Todavía buscándolo.", "La búsqueda tarda un poco.", "Sigo recopilando resultados."),
    "other": ("Sigo trabajando en ello.", "Un momento, todavía sigo.", "Sigo en ello, dame un momento."),
}

NARRATION_PHRASES = {
    "read": "Leyendo un archivo.",
    "edit": "Editando código.",
    "write": "Escribiendo un archivo.",
    "bash": "Ejecutando un comando.",
    "search": "Buscando.",
    "task": "Iniciando una subtarea.",
    "mcp": "Usando una de mis herramientas.",
    "other": "Trabajando en ello.",
}

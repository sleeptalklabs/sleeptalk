"""Polish (pl). Informal ty form, spoken register.

First-person past forms are gendered in Polish ("zapomniałem" is masculine).
The tables use masculine; a feminine voice needs "zapomniałam". Written by
Claude Fable 5.1 on 2026-09-17; not yet checked by a native speaker.
"""

PREAMBLE_POOLS = {
    "ack": ("Dobra.", "Okej.", "Rozumiem.", "Jasne.", "Tak.", "W porządku."),
    "action": (
        "Zaraz sprawdzę.", "Sprawdzam.", "Sprawdzam to teraz.",
        "Poszukam.", "Chwila, sprawdzam.", "Zobaczę.",
    ),
    "ack_action": (
        "Dobra, zaraz sprawdzę.", "Rozumiem, sprawdzam.",
        "Jasne, zajmę się tym.", "Okej, sprawdzę.",
    ),
    "action:task": ("Robię.", "Rozumiem, biorę się za to.", "Robię to teraz.", "Już się tym zajmuję."),
    "ack:empathy": ("Och, rozumiem.", "Aha, dobrze.", "Hmm, rozumiem."),
    "working": ("Wciąż nad tym pracuję.", "Nadal to robię."),
    "control": ("Zatrzymane.", "Dobra, przerywam."),
    "greeting": ("Cześć, jestem.", "Hej hej.", "Halo, halo."),
    "cron_offer": (
        "Witaj z powrotem. Przejrzeć wyniki twoich zaplanowanych zadań?",
    ),
    "reassure": (
        "Wciąż nad tym pracuję.",
        "Chwila, jeszcze działam.",
        "Jeszcze nie skończone, już prawie.",
        "Prawie gotowe, daj mi chwilę.",
        "Trochę to trwa, ale pracuję.",
        "Nie zapomniałem o tobie, wciąż nad tym siedzę.",
        "Wciąż to sprawdzam.",
        "Jeszcze moment.",
        "Potrzebuję na to trochę więcej czasu.",
        "Jeszcze chwila, sprawdzam.",
    ),
}

REASSURE_TOOL_POOLS = {
    "read": ("Wciąż to czytam.", "Wciąż przeglądam plik.", "Nadal siedzę w tym pliku."),
    "edit": ("Wciąż wprowadzam zmiany.", "Zmiany wciąż się wprowadzają.", "Nadal pracuję nad zmianami."),
    "write": ("Wciąż to zapisuję.", "Wciąż zapisuję plik.", "Plik wciąż jest zapisywany."),
    "bash": (
        "To polecenie wciąż działa.", "Wciąż wykonuję polecenie.",
        "Polecenie jeszcze trwa.", "To polecenie trochę trwa.",
    ),
    "search": ("Wciąż szukam.", "Wciąż to sprawdzam.", "Wyszukiwanie chwilę trwa.", "Wciąż zbieram wyniki."),
    "other": ("Wciąż nad tym pracuję.", "Chwila, jeszcze działam.", "Wciąż nad tym siedzę, daj mi chwilę."),
}

NARRATION_PHRASES = {
    "read": "Czytam plik.",
    "edit": "Edytuję kod.",
    "write": "Zapisuję plik.",
    "bash": "Uruchamiam polecenie.",
    "search": "Szukam.",
    "task": "Uruchamiam podzadanie.",
    "mcp": "Używam jednego z moich narzędzi.",
    "other": "Pracuję nad tym.",
}

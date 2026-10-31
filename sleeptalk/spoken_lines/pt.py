"""Portuguese (pt). Brazilian, spoken register; understood in Portugal.

Written by Claude Fable 5.1 on 2026-09-17; not yet checked by a native
speaker. Review the empathy pool first: it must stay neutral.
"""

PREAMBLE_POOLS = {
    "ack": ("Certo.", "Ok.", "Entendi.", "Beleza.", "Sim.", "Tá bom."),
    "action": (
        "Deixa eu ver.", "Vou conferir.", "Já vou olhar.",
        "Conferindo agora.", "Deixa eu procurar.", "Um segundo, vou verificar.",
    ),
    "ack_action": (
        "Certo, deixa eu ver.", "Entendi, conferindo.",
        "Beleza, vou olhar.", "Ok, deixa eu verificar.",
    ),
    "action:task": ("Deixa comigo.", "Entendi, já vou.", "Fazendo isso agora.", "Tô nessa."),
    "ack:empathy": ("Ah, entendo.", "Ah, tá.", "Hmm, entendo."),
    "working": ("Ainda nisso.", "Continuo trabalhando."),
    "control": ("Parei.", "Ok, parei."),
    "greeting": ("Oi, tô aqui.", "Oi, oi.", "Olá, olá."),
    "cron_offer": (
        "Bem-vindo de volta. Quer que eu revise as novidades das suas tarefas agendadas?",
    ),
    "reassure": (
        "Ainda estou trabalhando nisso.",
        "Só um momento, ainda continuo.",
        "Ainda não acabou, quase.",
        "Quase lá, me dá um momento.",
        "Está demorando um pouco, mas tô nessa.",
        "Não esqueci de você, ainda nisso.",
        "Ainda conferindo isso.",
        "Só um instante, por favor.",
        "Preciso de mais um pouco com isso.",
        "Mais um momento, ainda conferindo.",
    ),
}

REASSURE_TOOL_POOLS = {
    "read": ("Ainda lendo.", "Ainda passando pelo arquivo.", "Ainda vasculhando esse arquivo."),
    "edit": ("Ainda fazendo as edições.", "As edições ainda estão em andamento.", "Ainda trabalhando nas mudanças."),
    "write": ("Ainda escrevendo.", "Ainda escrevendo o arquivo.", "O arquivo ainda está sendo escrito."),
    "bash": (
        "Esse comando ainda está rodando.", "Ainda rodando o comando.",
        "O comando ainda vai.", "Esse comando está demorando.",
    ),
    "search": ("Ainda procurando.", "Ainda buscando isso.", "A busca está demorando um pouco.", "Ainda puxando os resultados."),
    "other": ("Ainda trabalhando nisso.", "Só um momento, ainda continuo.", "Ainda nisso, me dá um momento."),
}

NARRATION_PHRASES = {
    "read": "Lendo um arquivo.",
    "edit": "Editando código.",
    "write": "Escrevendo um arquivo.",
    "bash": "Rodando um comando.",
    "search": "Buscando.",
    "task": "Iniciando uma subtarefa.",
    "mcp": "Usando uma das minhas ferramentas.",
    "other": "Trabalhando nisso.",
}

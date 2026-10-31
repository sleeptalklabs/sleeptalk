"""Curated voices per speaking provider: one golden default plus a few picks.

These are the voices setup offers first, on every surface: the wizard, the
private key pages, and the call page. A user can always paste any other
voice ID from the provider's own library instead; ``library_url`` is where
they find one. Deepgram's model name is its voice.
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class CuratedVoice:
    provider: str
    name: str
    description: str
    voice_id: str
    default: bool = False
    # The speaking model this voice and its sample belong to, where a
    # provider has several that reuse voice names (Venice's Grok "leo" and
    # Orpheus "leo"). Empty: the provider's only model, or any of them.
    model: str = ""


# Five per provider with shipped samples, chosen 2026-09-24: the existing
# defaults first, then the most-used voices where a provider publishes usage
# (ElevenLabs' library by a year of characters: Mark #2, Brittney #3) or the
# provider's own picks for voice agents (Cartesia's stable agent voices,
# Deepgram's featured Flux voices). Groq and Together keep their short lists.
CURATED: dict[str, tuple[CuratedVoice, ...]] = {
    # On this machine: a selection from Kokoro v1.0 (Heart, its lead voice,
    # first), then Piper's one voice (the smallest download and the quickest).
    "local": (
        CuratedVoice("local", "Heart", "Kokoro: warm, natural American female.", "af_heart", default=True, model="kokoro-en"),
        CuratedVoice("local", "Bella", "Kokoro: warm American female.", "af_bella", model="kokoro-en"),
        CuratedVoice("local", "Michael", "Kokoro: calm American male.", "am_michael", model="kokoro-en"),
        CuratedVoice("local", "Emma", "Kokoro: British female.", "bf_emma", model="kokoro-en"),
        CuratedVoice("local", "George", "Kokoro: British male.", "bm_george", model="kokoro-en"),
        CuratedVoice("local", "Lessac", "Piper: clear American voice, smallest and quickest.", "lessac", model="piper-lessac-medium"),
        CuratedVoice("local", "Bria", "Kyutai Pocket TTS voice.", "pocket-bria", model="pocket-tts-en"),
        CuratedVoice("local", "Loona", "Kyutai Pocket TTS voice.", "pocket-loona", model="pocket-tts-en"),
    ),
    "cartesia": (
        CuratedVoice("cartesia", "Skylar", "Approachable American female, ideal for customer care and support.",
                     "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4", default=True),
        CuratedVoice("cartesia", "Daniel", "Clear, crisp male voice for digital assistants and system interactions.",
                     "47c38ca4-5f35-497b-b1a3-415245fb35e1"),
        CuratedVoice("cartesia", "Gemma", "Confident, emotive British female for professional assistance.",
                     "62ae83ad-4f6a-430b-af41-a9bede9286ca"),
        CuratedVoice("cartesia", "Parker", "Casual American male with supportive warmth, for friendly conversation.",
                     "30894953-bcce-41fe-892c-15ce19c843ff"),
        CuratedVoice("cartesia", "Archie", "Warm, conversational British male.", "ef191366-f52f-447a-a398-ed8c0f2943a1"),
    ),
    "elevenlabs": (
        CuratedVoice("elevenlabs", "Brittney", "Youthful and authoritative.", "kPzsL2i3teMYv0FxEYQ6", default=True),
        CuratedVoice("elevenlabs", "Mark", "A casual young adult speaking in a natural manner.", "UgBBYS2sOqTuMpoF3BR0"),
        CuratedVoice("elevenlabs", "Jessica", "Playful, bright and warm; made for conversation.", "cgSgspJ2msm6clMCkdW9"),
        CuratedVoice("elevenlabs", "Eric", "Smooth, trustworthy American tenor for assistants.", "cjVigY5qzO86Huf0OWal"),
        CuratedVoice("elevenlabs", "Sarah", "Mature, reassuring and confident.", "EXAVITQu4vr4xnSDxMaL"),
    ),
    "deepgram": (
        CuratedVoice("deepgram", "Hannah", "Flux, streamed; a warm, clear American female.", "flux-hannah-en", default=True),
        CuratedVoice("deepgram", "Kit", "Flux, streamed; a bright, friendly British male.", "flux-kit-en"),
        CuratedVoice("deepgram", "Alexis", "Flux, streamed; clear, calm and caring.", "flux-alexis-en"),
        CuratedVoice("deepgram", "Cliff", "Flux, streamed; a deep, confident American male.", "flux-cliff-en"),
        CuratedVoice("deepgram", "Sienna", "Flux, streamed; professional and warm.", "flux-sienna-en"),
    ),
    # One OpenRouter key reaches several vendors; the voice name picks the model.
    "openrouter": (
        CuratedVoice("openrouter", "Eve", "Grok Voice; calm, natural, the quickest to speak.", "eve", default=True, model="x-ai/grok-voice-tts-1.0"),
        CuratedVoice("openrouter", "Ara", "Grok Voice; bright and friendly.", "ara", model="x-ai/grok-voice-tts-1.0"),
        CuratedVoice("openrouter", "Rex", "Grok Voice; a confident, clear male voice.", "rex", model="x-ai/grok-voice-tts-1.0"),
        CuratedVoice("openrouter", "Hannah", "Deepgram Flux, free on OpenRouter; a warm, clear American female.", "flux-hannah-en", model="deepgram/flux-tts:free"),
        CuratedVoice("openrouter", "Kit", "Deepgram Flux, free on OpenRouter; a bright, friendly British male.", "flux-kit-en", model="deepgram/flux-tts:free"),
    ),
    "groq": (
        CuratedVoice("groq", "Troy", "Orpheus; a warm American male.", "troy", default=True),
        CuratedVoice("groq", "Hannah", "Orpheus; a clear American female.", "hannah"),
    ),
    "together": (
        CuratedVoice("together", "Skylar", "Cartesia Sonic; approachable American female.", "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4", default=True, model="cartesia/sonic-3"),
        CuratedVoice("together", "Daniel", "Cartesia Sonic; clear, crisp male voice.", "47c38ca4-5f35-497b-b1a3-415245fb35e1", model="cartesia/sonic-3"),
        CuratedVoice("together", "Tara", "Orpheus; expressive, a quarter of the price.", "tara", model="canopylabs/orpheus-3b-0.1-ft"),
        CuratedVoice("together", "Sky", "Kokoro; the budget pick at cents an hour.", "af_sky", model="hexgrad/Kokoro-82M"),
    ),
    # xAI's documented core Grok voices (Venice lists 26 with no ranking).
    "venice": (
        CuratedVoice("venice", "Eve", "Grok Voice; calm and natural.", "eve", default=True, model="tts-xai-v1"),
        CuratedVoice("venice", "Ara", "Grok Voice; warm and friendly.", "ara", model="tts-xai-v1"),
        CuratedVoice("venice", "Rex", "Grok Voice; a confident, clear male voice.", "rex", model="tts-xai-v1"),
        CuratedVoice("venice", "Leo", "Grok Voice; a steady, authoritative male voice.", "leo", model="tts-xai-v1"),
        CuratedVoice("venice", "Sal", "Grok Voice; balanced and even.", "sal", model="tts-xai-v1"),
    ),
    # OpenAI Realtime speaks in its own voices, saved as
    # [route].voice. Marin and Cedar are OpenAI's picks for its Realtime models.
    "openai": (
        CuratedVoice("openai", "Marin", "Newest Realtime voice; OpenAI's recommendation.", "marin", default=True),
        CuratedVoice("openai", "Cedar", "Newest Realtime voice; OpenAI's recommendation.", "cedar"),
        CuratedVoice("openai", "Alloy", "Earlier Realtime voice.", "alloy"),
        CuratedVoice("openai", "Ash", "Earlier Realtime voice.", "ash"),
        CuratedVoice("openai", "Ballad", "Earlier Realtime voice.", "ballad"),
        CuratedVoice("openai", "Coral", "Earlier Realtime voice.", "coral"),
        CuratedVoice("openai", "Echo", "Earlier Realtime voice.", "echo"),
        CuratedVoice("openai", "Sage", "Earlier Realtime voice.", "sage"),
        CuratedVoice("openai", "Shimmer", "Earlier Realtime voice.", "shimmer"),
        CuratedVoice("openai", "Verse", "Earlier Realtime voice.", "verse"),
    ),
}

LIBRARY_URLS: dict[str, str] = {
    "openai": "https://www.openai.fm/",
    "cartesia": "https://play.cartesia.ai/voices",
    "elevenlabs": "https://elevenlabs.io/app/voice-library",
    "deepgram": "https://developers.deepgram.com/docs/tts-models",
    "openrouter": "https://openrouter.ai/models?output_modalities=speech",
    "groq": "https://console.groq.com/docs/text-to-speech",
    "together": "https://docs.together.ai/docs/text-to-speech",
    "venice": "https://docs.venice.ai/api-reference/endpoint/audio/speech",
    "local": "https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md",
}


# Native voices per language, up to five, chosen 2026-09-24. The first two
# were measured by a round trip (a developer sentence spoken, then
# transcribed back); the rest follow each provider's own order (Cartesia's
# featured list, Deepgram's Aura-2 voices, ElevenLabs' library by a year of
# characters, free-tier only), balanced by gender, skipping voices made for
# ads, stories, or characters. Keys are a primary language tag, or a full
# tag where the region has its own voices.
_NATIVE: dict[tuple[str, str], tuple[tuple[str, str, str], ...]] = {
    ("cartesia", "es"): (
        ("Marta", "Spanish (Spain) female.", "de38f545-c574-44e8-9b54-a7d6fec1c6b1"),
        ("Darío", "Spanish (Spain) male.", "35b2cfc1-e6fb-4d69-a598-c1780612be4a"),
        ("Iria", "Spanish (Spain) female: thoughtful communicator.", "a7beff01-8f8b-4809-bfe6-e2166e57e0c2"),
        ("Rafael", "Spanish (Spain) male: poised advisor.", "cbb6fdf0-30dd-49f6-af7d-bbb1185c1fa5"),
        ("Celia", "Spanish (Spain) female: practical analyst.", "ad38904c-0ce9-42b1-9159-5ad5352ef089"),
    ),
    ("cartesia", "es-mx"): (
        ("Fernanda", "Mexican Spanish female.", "b4b8e2af-6139-466e-a93a-30c20d2e1fc5"),
        ("Mateo", "Mexican Spanish male.", "2fc4f1ec-bfd0-46f1-8e6d-d4279eaaf838"),
        ("Ximena", "Mexican Spanish female: calm navigator.", "3597a26f-80ef-4bd5-8101-9699bc764917"),
        ("Ramon", "Mexican Spanish male: calm coordinator.", "9b67072c-d46c-465d-87dc-f7a1c6db2bf3"),
        ("Laura", "Mexican Spanish female: trustworthy guide.", "1cc00672-e9d4-455e-b3fb-31dfb7aad231"),
    ),
    ("cartesia", "fr"): (
        ("Inès", "French female.", "7c58f4a4-a72c-42fa-a503-41b9408820f3"),
        ("Mathis", "French male.", "996ec149-0dca-4389-ad08-e2d6f906b4bf"),
        ("Zoé", "French female: informative dispatcher.", "b56a7171-86f0-42b6-b3fa-a316794aa4e0"),
        ("Henri", "French male: express host.", "d9f4af15-c402-4f50-bbda-d8823d028d6a"),
        ("Jade", "French female: steady companion.", "92579402-6868-412e-b845-3efed0be7a9e"),
    ),
    ("cartesia", "de"): (
        ("Marlene", "German female.", "9b4d08b6-0494-4301-ab92-9150f4ee2718"),
        ("Moritz", "German male.", "4ad22058-7cb6-402c-a115-196cbfc25dce"),
        ("Klara", "German female: empathetic voice.", "2578354e-4b18-4d28-832c-5943344b7085"),
        ("Clemens", "German male: precise instructor.", "57a3a9e0-a91c-4c94-a2bb-e6cbab3ae649"),
        ("Jennifer", "German female.", "ac197a78-cec7-4c50-93e5-93bdc1910b11"),
    ),
    ("cartesia", "it"): (
        ("Elena", "Italian female.", "00e9ec78-2002-41dd-8d19-6b1d3b17a461"),
        ("Alessio", "Italian male.", "c8403b5c-6465-4396-9065-a440d376528a"),
        ("Liv", "Italian female: casual friend.", "d718e944-b313-4998-b011-d1cc078d4ef3"),
        ("Fabio", "Italian male: logistics expert.", "526ab945-9cbf-4bd2-9c72-3833d55d4c68"),
        ("Sofia", "Italian female: methodical moderator.", "90c7d657-9599-4cd0-9ed2-2568359e4d1a"),
    ),
    ("cartesia", "pt"): (
        ("Helena", "Brazilian Portuguese female.", "8a6d0b8e-8cd8-4952-a41e-b7af18662135"),
        ("Felipe", "Brazilian Portuguese male.", "616c64d7-f541-436b-9b8d-e79cfbe19ef9"),
        ("Eloá", "Brazilian Portuguese female: engaging explainer.", "cb2694c3-715f-4da9-99f3-1c974fff2928"),
        ("Rafael", "Brazilian Portuguese male: dynamic speaker.", "07b6f895-78b9-4921-8e10-8a21c99c2e8a"),
        ("Alice", "Brazilian Portuguese female: informative speaker.", "9904416a-0831-44ea-b8ee-5f145e8f9bbf"),
    ),
    ("cartesia", "nl"): (
        ("Fleur", "Dutch female.", "de075c71-b2dd-4723-848d-ea9aa9cd010b"),
        ("Thijs", "Dutch male.", "95e9fdaf-cf0b-4739-b1de-3350ca50774a"),
        ("Noa", "Dutch female: reassuring responder.", "96355f3d-0179-4c9a-a8d8-11ef0779a9b8"),
        ("Stijn", "Dutch male: helpful handler.", "da743a82-ddf2-4d9b-8eb8-ff67ca0b138e"),
        ("Anneliese", "Dutch female: methodical guide.", "225ba8cf-9fc2-4371-a78c-fe38ba38898a"),
    ),
    ("cartesia", "ja"): (
        ("Naoki", "Japanese male.", "aa3ccc4a-cfd8-405c-acf5-1873a527b006"),
        ("Yuzuki", "Japanese female.", "cb21b4af-0e46-44f7-9128-51be7f898311"),
        ("Aiko", "Japanese female: calming voice.", "498e7f37-7fa3-4e2c-b8e2-8b6e9276f956"),
        ("Yuto", "Japanese male: logistical assistant.", "7ca2afba-a719-4f06-9af2-ea2b8e3cf14c"),
        ("Sakura", "Japanese female: precise host.", "579dc63a-1c19-4512-866a-1295dff18f0f"),
    ),
    ("cartesia", "ko"): (
        ("Minji", "Korean female.", "7706804e-ea85-443a-968a-b9bf363bdde8"),
        ("Jaewon", "Korean male.", "89f4372f-1f73-4b85-8e1e-5d24ed8bc826"),
        ("Yeji", "Korean female: clarity desk.", "1a602be4-3b57-4bef-a270-dfa5efed0541"),
        ("Taehyun", "Korean male: friendly host.", "e1717dc3-b87b-4720-aa7f-b6db290e0609"),
        ("Haeun", "Korean female: polished presence.", "4dd4630e-19e0-4243-bca0-676ff85119b7"),
    ),
    ("cartesia", "hi"): (
        ("Neha", "Hindi female.", "47f3bbb1-e98f-4e0c-92c5-5f0325e1e206"),
        ("Rohan", "Hindi male.", "4877b818-c7fe-4c89-b1cf-eadf8e23da72"),
        ("Siya", "Hindi female: bright conversationalist.", "4459a9a5-69d6-4680-b970-e13dc51845b6"),
        ("Kabir", "Hindi male: service integrator.", "cb9c954d-bcaa-43ed-82bf-aeb5e88a3cb5"),
        ("Anika", "Hindi female: system facilitator.", "32b0f12b-67c3-421d-8850-b46c019ced91"),
    ),
    ("cartesia", "zh"): (
        ("Jian", "Mandarin male.", "7e2a44d1-76b8-42b8-9507-fedfe3a803c8"),
        ("Hua", "Mandarin female: sunny support.", "7a5d4663-88ae-47b7-808e-8f9b9ee4127b"),
        ("Lan", "Mandarin female: instructor.", "bf32f849-7bc9-4b91-8c62-954588efcc30"),
        ("Liu", "Mandarin male: plain talker.", "653b9445-ae0c-4312-a3ce-375504cff31e"),
        ("Shuwen", "Mandarin female: precision guide.", "f9a4b3a6-b44b-469f-90e3-c8e19bd30e99"),
    ),
    ("deepgram", "es"): (
        ("Alvaro", "Spanish male.", "aura-2-alvaro-es"),
        ("Diana", "Spanish female.", "aura-2-diana-es"),
        ("Agustina", "Spanish (Spain) female: calm.", "aura-2-agustina-es"),
        ("Néstor", "Spanish (Spain) male: calm.", "aura-2-nestor-es"),
        ("Carina", "Spanish (Spain) female: professional.", "aura-2-carina-es"),
    ),
    ("deepgram", "es-mx"): (
        ("Luciano", "Mexican Spanish male.", "aura-2-luciano-es"),
        ("Olivia", "Mexican Spanish female.", "aura-2-olivia-es"),
        ("Selena", "Mexican Spanish female: approachable.", "aura-2-selena-es"),
        ("Aquila", "Mexican Spanish male: casual.", "aura-2-aquila-es"),
        ("Javier", "Mexican Spanish male: approachable.", "aura-2-javier-es"),
    ),
    ("deepgram", "fr"): (
        ("Hector", "French male.", "aura-2-hector-fr"),
        ("Agathe", "French female: charismatic.", "aura-2-agathe-fr"),
    ),
    ("deepgram", "de"): (
        ("Viktoria", "German female.", "aura-2-viktoria-de"),
        ("Julius", "German male: casual.", "aura-2-julius-de"),
        ("Aurelia", "German female: approachable.", "aura-2-aurelia-de"),
        ("Elara", "German female: calm.", "aura-2-elara-de"),
    ),
    ("deepgram", "it"): (
        ("Cesare", "Italian male.", "aura-2-cesare-it"),
        ("Melia", "Italian female.", "aura-2-melia-it"),
        ("Cinzia", "Italian female: approachable.", "aura-2-cinzia-it"),
        ("Dionisio", "Italian male: confident.", "aura-2-dionisio-it"),
        ("Demetra", "Italian female: calm.", "aura-2-demetra-it"),
    ),
    ("deepgram", "nl"): (
        ("Sander", "Dutch male.", "aura-2-sander-nl"),
        ("Rhea", "Dutch female.", "aura-2-rhea-nl"),
        ("Beatrix", "Dutch female: cheerful.", "aura-2-beatrix-nl"),
        ("Lars", "Dutch male: breathy.", "aura-2-lars-nl"),
        ("Cornelia", "Dutch female: approachable.", "aura-2-cornelia-nl"),
    ),
    ("deepgram", "ja"): (
        ("Izanami", "Japanese female.", "aura-2-izanami-ja"),
        ("Fujin", "Japanese male.", "aura-2-fujin-ja"),
        ("Ama", "Japanese female: casual.", "aura-2-ama-ja"),
        ("Ebisu", "Japanese male: calm.", "aura-2-ebisu-ja"),
        ("Uzume", "Japanese female: approachable.", "aura-2-uzume-ja"),
    ),
    ("elevenlabs", "es"): (
        ("Carolina", "Spanish (Spain) female: natural, neutral and clear.", "UOIqAnmS11Reiei1Ytkc"),
        ("David Martin", "Spanish (Spain) male: confident and balanced.", "Nh2zY9kknu6z4pZy6FhD"),
        ("Raquel", "Spanish (Spain) female: young, bright and cheerful.", "1eHrpOW5l98cxiSRjbzJ"),
        ("Carmelo", "Spanish (Spain) male: mature, mysterious and clear.", "5egO01tkUjEzu7xSSE8M"),
        ("Carolina Ruiz", "Spanish (Spain) female: nasal, natural and clear.", "h2cd3gvcqTp3m65Dysk7"),
    ),
    ("elevenlabs", "es-mx"): (
        ("Marlene", "Mexican Spanish female: melodic, pleasant and bright.", "JYyJjNPfmNJdaby8LdZs"),
        ("Enrique Mondragón", "Mexican Spanish male: elegant and dynamic.", "iDEmt5MnqUotdwCIVplo"),
        ("Ana María", "Mexican Spanish female: calm, natural and clear.", "m7yTemJqdIqrcNleANfX"),
        ("Enrique M. Nieto", "Mexican Spanish male: credible and rich.", "gbTn1bmCvNgk0QEAVyfM"),
        ("Regina", "Mexican Spanish female: contact center.", "9Godp7dNohUvXk6qp0gS"),
    ),
    ("elevenlabs", "fr"): (
        ("Lucie", "French female: support agent.", "YxrwjAKoUKULGd0g8K9Y"),
        ("Nicolas", "French male: narrator.", "aQROLel5sQbj1vuIVi6B"),
        ("Hélène", "French female: neutral, controled and old.", "imRmmzTqlLHt9Do1HufF"),
        ("Nicolas Petit", "French male: convincing and bright.", "AmMsHJaCw4BtwV3KoUXF"),
        ("Victoria", "French female: content creator.", "O31r762Gb3WFygrEOGh0"),
    ),
    ("elevenlabs", "de"): (
        ("Susi", "German female: effortless and confident.", "v3V1d2rk6528UrLKRuy8"),
        ("Otto", "German male: casual and normal.", "FTNCalFNG5bRnkkaP5Ug"),
        ("Lea", "German female: clear and feminine.", "7eVMgwCnXydb3CikjV7a"),
        ("Ben", "German male: effortless and casual.", "aTTiK3YzK3dXETpuDE2h"),
        ("Carola", "German female: sharp and clear.", "K75lPKuh15SyVhQC1LrE"),
    ),
    ("elevenlabs", "it"): (
        ("Violetta", "Italian female: ringing, bright and joyful.", "gfKKsLN1k0oYYN9n2dXX"),
        ("MarcoTrox", "Italian male: warm, balanced and polished.", "W71zT1VwIFFx3mMGH2uZ"),
        ("Giusy", "Italian female: sales and customer support.", "8KInRSd4DtD5L5gK7itu"),
        ("Marco", "Italian male: deep, rich and reflective.", "13Cuh3NuYvWOVQtLbRN8"),
        ("Linda Fiore", "Italian female: prickly, cheerful and full.", "3DPhHWXDY263XJ1d2EPN"),
    ),
    ("elevenlabs", "pt"): (
        ("Keren", "Brazilian Portuguese female: sweet, vibrant and rhythmic.", "33B4UnXyTNbgLmdEDh5P"),
        ("Matheus", "Brazilian Portuguese male: friendly and energetic.", "36rVQA1AOIPwpA3Hg1tC"),
        ("Raquel", "Brazilian Portuguese female: expressive and energetic.", "GDzHdQOi6jjf8zaXhCYD"),
        ("Will", "Brazilian Portuguese male: deep, smooth and affectionate.", "CstacWqMhJQlnfLPxRG4"),
        ("Roberta", "Brazilian Portuguese female: smooth and confident.", "RGymW84CSmfVugnA5tvA"),
    ),
    ("elevenlabs", "nl"): (
        ("Roos", "Dutch female: kind, articulate and confident.", "7qdUFMklKPaaAVMsBTBt"),
        ("Ido", "Dutch male: informal, friendly narrator.", "dLPO5AsXc3FZDbTh1IKa"),
        ("Emma", "Dutch female: calm, clear and confident.", "OlBRrVAItyi00MuGMbna"),
        ("Marcèles", "Dutch male: natural, clear and articulate.", "fzC7H9Y1bPn3gzVLtghe"),
        ("Marianne", "Dutch female: senior dutch voice.", "tfweP7lGJyLeNV9dH1Rm"),
    ),
    ("elevenlabs", "ja"): (
        ("Sakura", "Japanese female: narrational.", "RBnMinrYKeccY3vaUxlZ"),
        ("Otani", "Japanese male: inviting, clear and measured.", "3JDquces8E8bkmvbh6Bc"),
        ("Morioki", "Japanese female: calm, measured and muffled.", "8EkOjt4xTPGMclNlh1pk"),
        ("Kozy", "Japanese male: inviting, measured and guttural.", "GxxMAMfQkDlnqjpzjLHH"),
        ("Kyoko", "Japanese female: warm, clear and natural.", "4lOQ7A2l7HPuG7UIHiKA"),
    ),
    ("elevenlabs", "ko"): (
        ("Anna Kim", "Korean female: tender, calm and clear.", "uyVNoMrnUku1dZyVEXwD"),
        ("Hyuk", "Korean male: cold and clear.", "ZJCNdZEjYwkOElxugmW2"),
        ("Jiyoung", "Korean female: warm and clear.", "AW5wrnG1jVizOYY7R1Oo"),
        ("Chris", "Korean male: warm and clear.", "PDoCXqBQFGsvfO0hNkEs"),
        ("Seulki", "Korean female: inviting, calm and measured.", "ksaI0TCD9BstzEzlxj4q"),
    ),
    ("elevenlabs", "hi"): (
        ("Zara", "Hindi female: soft, professional and gentle.", "OtEfb2LVzIE45wdYe54M"),
        ("Raju", "Hindi male: clear, natural and warm.", "zT03pEAEi0VHKciJODfn"),
        ("Sia", "Hindi female: friendly, helpful and reassuring.", "KYiVPerWcenyBTIvWbfY"),
        ("Viraj", "Hindi male: warm, energetic and lively.", "iWNf11sz1GrUE4ppxTOL"),
        ("Simran", "Hindi female: young, friendly and calm.", "9w21nMuk8CWXIME31V1S"),
    ),
    ("elevenlabs", "zh"): (
        ("Coco Li", "Mandarin female: calm, young and trustworthy.", "Ca5bKgudqKJzq8YRFoAz"),
        ("James Gao", "Mandarin male: calm, friendly and warm.", "4VZIsMPtgggwNg7OXbPY"),
        ("Evan Zhao", "Mandarin male: warm, calm and trustworthy.", "MI36FIkp9wRP7cpWKPTl"),
        ("Stella Gu", "Mandarin female: cold, indifferent and calm.", "BqljjWyTnrioXPCNkCd4"),
    ),
}


# Voices that speak every language (Grok Voice), recorded in each one.
MULTILINGUAL: dict[str, tuple[str, ...]] = {
    "openrouter": ("eve", "ara", "rex", "sal", "leo"),
    "venice": ("eve", "ara", "rex", "leo", "sal"),
}
# The Grok Voice model those voices speak through, per provider.
MULTILINGUAL_MODEL: dict[str, str] = {
    "openrouter": "x-ai/grok-voice-tts-1.0",
    "venice": "tts-xai-v1",
}


def native(provider: str, language: str) -> tuple[CuratedVoice, ...]:
    """The measured native voices for this language, the starting one first."""
    provider = provider.strip().lower()
    tag = (language or "").strip().lower().replace("_", "-")
    entries = _NATIVE.get((provider, tag)) or _NATIVE.get((provider, tag.split("-", 1)[0])) or ()
    return tuple(CuratedVoice(provider, name, description, voice_id, default=index == 0)
                 for index, (name, description, voice_id) in enumerate(entries))


def native_ids(provider: str) -> set[str]:
    provider = provider.strip().lower()
    return {voice_id for (name, _tag), entries in _NATIVE.items() if name == provider for _n, _d, voice_id in entries}


def curated(provider: str) -> tuple[CuratedVoice, ...]:
    voices = CURATED.get(provider.strip().lower(), ())
    if provider.strip().lower() == "local":
        from sleeptalk.speech.local_voices import list_voices

        voices += tuple(CuratedVoice("local", **voice) for voice in list_voices())
    return voices


def default_voice_id(provider: str) -> str:
    for voice in curated(provider):
        if voice.default:
            return voice.voice_id
    return ""


def library_url(provider: str) -> str:
    return LIBRARY_URLS.get(provider.strip().lower(), "")


def find(provider: str, voice_id: str) -> CuratedVoice | None:
    for voice in curated(provider):
        if voice.voice_id == voice_id:
            return voice
    for (name, _tag) in _NATIVE:
        if name == provider.strip().lower():
            for voice in native(name, _tag):
                if voice.voice_id == voice_id:
                    return voice
    return None


def catalog_payload(providers: tuple[str, ...] | None = None, language: str = "") -> dict[str, dict]:
    """A JSON-safe catalog for the setup pages and the call page.

    With a language other than English, its native voices come first and
    the starting one is marked default.
    """
    names = providers if providers is not None else tuple(CURATED)

    english = (language or "en").strip().lower().split("-", 1)[0] in ("", "en")

    def voices(provider: str) -> list[CuratedVoice]:
        if english:
            return list(curated(provider))
        # Another language: its native voices, then voices that speak any
        # language; the English-only picks drop out.
        first = list(native(provider, language))
        speaks_all = MULTILINGUAL.get(provider, ())
        known = {v.voice_id: v for v in curated(provider)}
        rest = [replace(known.get(voice_id) or CuratedVoice(provider, voice_id.title(), "", voice_id,
                                                            model=MULTILINGUAL_MODEL.get(provider, "")), default=False)
                for voice_id in speaks_all]
        chosen = first + rest
        if chosen and not any(v.default for v in chosen):
            chosen[0] = replace(chosen[0], default=True)
        return chosen or list(curated(provider))

    return {
        provider: {
            "voices": [
                {"name": v.name, "description": v.description, "voice_id": v.voice_id, "default": v.default,
                 "model": v.model}
                for v in voices(provider)
            ],
            "library_url": library_url(provider),
        }
        for provider in names
        if curated(provider)
    }

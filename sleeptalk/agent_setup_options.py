"""Non-secret adapter choices shared by local and remote setup commands."""
from __future__ import annotations

import json

from sleeptalk.agent_auth import AgentAuthChoice, AgentAuthConfig


def add_arguments(parser) -> None:
    parser.add_argument('--agent-auth-mode', choices=('api_key',), default='')
    parser.add_argument('--agent-auth-protocol', choices=('openai-completions', 'openai-responses', 'openai-codex-responses', 'anthropic-messages', 'cursor'), default='')
    parser.add_argument('--agent-auth-url', default='', help='Exact HTTPS model-provider endpoint; no embedded credentials.')
    parser.add_argument('--agent-credential-ref', default='', help='Private credential reference, never a key or token.')
    parser.add_argument('--agent-option', action='append', default=[], metavar='NAME=VALUE', help='Non-secret adapter option, such as hermes_root. Validated by the selected adapter.')


def choices(args) -> dict:
    values = {name: getattr(args, key, '') for name, key in (
        ('mode', 'agent_auth_mode'), ('protocol', 'agent_auth_protocol'),
        ('base_url', 'agent_auth_url'), ('credential_ref', 'agent_credential_ref'))}
    result = {}
    if getattr(args, 'collect_agent_auth', False):
        if values.pop('credential_ref'):
            raise ValueError('Select either a saved reference or private model credential collection.')
        result['auth_request'] = AgentAuthChoice(**values)
    elif any(values.values()):
        result['auth'] = AgentAuthConfig(**values)
    options = {}
    for entry in getattr(args, 'agent_option', []):
        name, sep, value = entry.partition('=')
        if not sep or not name or name in options:
            raise ValueError('Adapter options require distinct NAME=VALUE choices.')
        try:
            options[name] = json.loads(value)
        except ValueError:
            options[name] = value
    if options:
        toml_value(options)
        result['options'] = options
    return result


def toml_value(value) -> str:
    if isinstance(value, dict):
        return '{ ' + ', '.join(json.dumps(key) + ' = ' + toml_value(item) for key, item in value.items()) + ' }'
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(item) for item in value) + ']'
    if value is None:
        raise ValueError('Adapter options cannot contain null values.')
    return json.dumps(value, allow_nan=False, ensure_ascii=False)


def _input_error(message: str):
    from sleeptalk.private_input import InputError

    return InputError(message)


def _speech_provider_choices() -> tuple[str, ...]:
    from sleeptalk.private_input import SPEECH_PROVIDERS

    return (*SPEECH_PROVIDERS, "local")


SPEECH_PROVIDER_CHOICES = _speech_provider_choices()


def add_speech_provider_arguments(parser) -> None:
    parser.add_argument("--tts-voice-id", default="", help="A voice the user asked for; the setup page then validates exactly this voice.")
    parser.add_argument("--speech-from-profile", default="", metavar="SLUG",
                        help="Reuse the speech provider, voice, and stored key of an existing profile (for example "
                             "`default`), so the page only asks for what the new surface needs, such as a Discord bot token.")
    parser.add_argument("--speech-provider", choices=SPEECH_PROVIDER_CHOICES, default="",
                        help="Optional. The key page always offers every provider, with its free usage and "
                             "whether a card is needed, and the user picks one there (one key listens and "
                             "speaks). Pass this only to put a provider the user already named first; the "
                             "others stay a tap away.")
    parser.add_argument("--stt-provider", choices=SPEECH_PROVIDER_CHOICES, default="",
                        help="Mixed setups only: the listening (speech-to-text) provider.")
    parser.add_argument("--tts-provider", choices=SPEECH_PROVIDER_CHOICES, default="",
                        help="Mixed setups only: the speaking (text-to-speech) provider.")


LOCAL_SPEECH_HINT = ("Local speech collects no key, so it is not set up from a key page. Write an answers file with "
                     'stt_provider = "local" and tts_provider = "local" and run `sleeptalk host ensure --answers <file>`.')


def speech_provider_choices(args) -> dict[str, str]:
    """InputSpec provider fields from the CLI flags.

    No flag at all leaves the choice to the key page, which offers every
    provider; the user's first saved key decides.
    """
    voice = {"tts_voice_id": args.tts_voice_id} if getattr(args, "tts_voice_id", "") else {}
    if "local" in (args.speech_provider, args.stt_provider, args.tts_provider):
        raise _input_error(LOCAL_SPEECH_HINT)
    if getattr(args, "speech_from_profile", ""):
        if args.speech_provider or args.stt_provider or args.tts_provider or voice:
            raise _input_error("--speech-from-profile reuses that profile's provider and voice; don't pass provider or voice flags with it.")
        return {"speech_from_profile": args.speech_from_profile}
    if args.speech_provider:
        if args.stt_provider or args.tts_provider:
            raise _input_error("--speech-provider replaces --stt-provider/--tts-provider; pass one form.")
        if voice:
            # A requested voice belongs to one provider, so that provider is fixed.
            return {"stt_provider": args.speech_provider, "tts_provider": args.speech_provider, **voice}
        return {"preferred_provider": args.speech_provider}
    if args.stt_provider and not args.tts_provider:
        raise _input_error("--stt-provider needs --tts-provider; or pass neither and the key page offers every provider.")
    if not args.tts_provider:
        if voice:
            raise _input_error("--tts-voice-id needs the provider it belongs to; pass --speech-provider with it.")
        return {}
    return {"stt_provider": args.stt_provider or "deepgram", "tts_provider": args.tts_provider, **voice}

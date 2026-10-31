"""Line edits to a profile: they never leave a file that won't load."""
from __future__ import annotations

import tomllib

from sleeptalk.profile_edit import set_string_key

# How the terminal setup rewrites a profile: quoted headers, key references inline.
REWRITTEN = (
    '["stt"]\n"provider" = "local"\n\n'
    '["credentials"]\n"refs" = { "OPENROUTER_API_KEY" = "credential:' + "a" * 32 + '" }\n\n'
    '[transport.options]\ndelivery = "local"\n'
)


def test_a_key_reference_lands_in_an_inline_refs_table(tmp_path):
    """Switching speech service on such a profile failed: "The OPENROUTER_API_KEY
    value can't be stored in the profile." A second [credentials.refs] header
    would define the table twice."""
    path = tmp_path / "p.toml"
    path.write_text(REWRITTEN)
    assert set_string_key(path, "credentials.refs", "OPENROUTER_API_KEY", "credential:" + "b" * 32)
    assert set_string_key(path, "credentials.refs", "CARTESIA_API_KEY", "credential:" + "c" * 32)
    parsed = tomllib.loads(path.read_text())
    assert parsed["credentials"]["refs"] == {"OPENROUTER_API_KEY": "credential:" + "b" * 32,
                                             "CARTESIA_API_KEY": "credential:" + "c" * 32}
    assert path.read_text().count("refs") == 1
    assert not set_string_key(path, "credentials.refs", "CARTESIA_API_KEY", "credential:" + "c" * 32)
    # Plain tables are edited as before.
    assert set_string_key(path, "transport.options", "delivery", "tailnet")
    assert tomllib.loads(path.read_text())["transport"]["options"]["delivery"] == "tailnet"

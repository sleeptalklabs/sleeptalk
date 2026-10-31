# Calls over your tailnet

If the host that runs Sleeptalk and the phone or laptop you talk from are on
the same Tailscale network, the call needs no hosted relay and no TURN.
Sleeptalk's built-in call server, the one it already uses for same-machine
browser calls, is published under the host's tailnet name with
`tailscale serve`. The browser opens `https://<host>.<tailnet>.ts.net:8443/…`,
signaling goes through Tailscale's proxy, and audio flows directly between
the two tailnet addresses. Nothing leaves the tailnet. Nothing is billed.

This is the natural choice for a VPS you reach over Tailscale, and it works
the same for a laptop and a phone at home.

## Once per tailnet

Enable HTTPS certificates under DNS in the Tailscale admin console:
<https://login.tailscale.com/admin/dns>. Browsers only allow the microphone
on HTTPS, and `tailscale serve` uses the certificate Tailscale issues for the
machine's name.

## Once per host

Tailscale must be installed and signed in. On Linux, let your user configure
serve without sudo:

```bash
sudo tailscale set --operator=$USER
```

## Sleeptalk offers it when it can

When Tailscale is connected on the machine and certificates are enabled,
setup asks once, at the point you choose a browser call: "Deliver browser
calls over your tailnet?" Yes is remembered in the profile as
`delivery = "tailnet"`; every later call uses it without a flag. Sleeptalk
asks rather than switching on its own because the host cannot tell whether
the device you will call from is on the same tailnet. If Tailscale is
present but certificates are off, setup says so with the admin link instead.

For an agent, `sleeptalk host ensure` reports a `tailnet` block:
`available` (Tailscale is ready on this host), `selected` (the profile has
chosen it), and, when available but not selected, an `ask` with the exact
question to put to you once. `host ensure --tailnet` stores the choice, on a
new profile or an existing one. Every `host start-voice` receipt then names
its `delivery`: `tailnet`, `hosted_relay`, or `local`, read from the link
itself, so an agent never mistakes a relay link for a tailnet call. A tailnet
join link contains `.ts.net`.

## Start a call

```bash
sleeptalk host start-voice --profile default --tailnet --json
sleeptalk link --tailnet --config config/profiles/default.toml
```

Or make it the profile's default so every launch, including the plain
`sleeptalk` command, uses it:

```toml
[transport.options]
delivery = "tailnet"
```

The join link is single-use and expires like every other Sleeptalk link. The
secret stays in the URL fragment. Only tailnet members can reach the name at
all, which is a second gate the hosted relay cannot offer.

## No time limit

Local and tailnet calls use nothing metered, so they have no session limit.
A call still ends after 90 seconds of silence. The hosted relay keeps its
30-minute limit because TURN relay time costs money; the call page warns
two minutes and thirty seconds before that limit and says why it ended.

## What Sleeptalk does and undoes

For each call it runs `tailscale serve --bg --https=8443 http://127.0.0.1:<port>`
against its own loopback port, and `tailscale serve --https=8443 off` when the
call ends or `sleeptalk host stop` runs. Port 8443 is used rather than 443 so
nothing else you serve on the host is touched. Sleeptalk never uses
`tailscale funnel`, which would expose the call server to the public internet.

## When it refuses

| Receipt code | Meaning | Fix |
| --- | --- | --- |
| `tailscale_missing` | No `tailscale` binary on the host | Install Tailscale and sign in |
| `tailscale_not_running` | Installed but not connected | `tailscale up` |
| `tailscale_https_disabled` | Certificates not enabled for the tailnet | Enable them in the admin console, once |
| `tailscale_operator_required` | This user may not configure serve | `sudo tailscale set --operator=$USER` |
| `tailscale_serve_failed` | Serve refused the configuration | Check `tailscale serve status` on the host |

Each of these ends the call attempt before any link is minted; the hosted
relay is not used as a silent fallback.

## Evidence

2026-09-20: a Mac on Homebrew Tailscale 1.102 published an echo call with
`sleeptalk link --tailnet --echo`; an iPhone on the same tailnet opened the
link in Safari, was prompted for the microphone, and heard itself echoed.
No STUN or TURN was configured. The serve entry was removed when the call
ended.

Same day: an Ubuntu VPS on the same tailnet, set up by the Hermes agent
running on it from a fresh wheel and told only to "start a call over the
tailnet", found `--tailnet` in the CLI help, published the call under the
VPS's tailnet name, and the iPhone joined and talked to Hermes. That is the
remote proof: the VPS shares no network with the phone except the tailnet.
After a reboot of the VPS, the agent started a new tailnet call from the
stored profile and credential without any re-entry. The hosted relay path
was proven against production the same day.

"""Trusted loopback confirmation page for remote passkey pairing."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from typing import Callable
from urllib.parse import urlsplit

from aiohttp import web

from sleeptalk.remote_crypto import encode


PAGE = b"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Pair a private setup device</title><link rel=stylesheet href=/pair.css><main aria-busy=true>
<h1>Pair a private setup device</h1><p id=status>Opening the private local invitation...</p><section id=handoff hidden>
<p>Open this one-time link on the device that will hold your passkey.</p><a id=remote target=_blank rel='noreferrer noopener'>Open secure pairing</a></section>
<section id=compare hidden><p>Compare this code with the code on the passkey device.</p><output id=fingerprint></output>
<button id=confirm type=button>Codes match, pair this device</button></section></main><script src=/pair.js></script></html>"""

SCRIPT = b"""const statusEl=document.querySelector('#status');const handoff=document.querySelector('#handoff');
const compare=document.querySelector('#compare');const remote=document.querySelector('#remote');const output=document.querySelector('#fingerprint');
let csrf='';let current='';const local=location.hash.slice(1);history.replaceState(null,'',location.pathname);
async function request(path,options={}){options.credentials='same-origin';options.cache='no-store';options.redirect='error';
options.headers={...(options.headers||{}),...(csrf?{'X-Setup-CSRF':csrf}:{})};let response=await fetch(path,options);if(!response.ok)throw new Error();return response.json()}
async function poll(){let state=await request('/status');statusEl.textContent=state.status==='awaiting_confirmation'?'Compare both codes before approving.':'Waiting for passkey registration on the other device.';
if(state.remote_url){remote.href=state.remote_url;handoff.hidden=false}if(state.fingerprint){current=state.fingerprint;output.textContent=current.match(/.{1,4}/g).join(' ');compare.hidden=false}
if(state.status==='confirmed'){statusEl.textContent='Device paired. Return to your terminal.';handoff.hidden=true;compare.hidden=true;return}setTimeout(()=>poll().catch(fail),1000)}
function fail(){statusEl.textContent='Pairing could not be verified. Return to your terminal and inspect status.'}
document.querySelector('#confirm').addEventListener('click',async()=>{try{let result=await request('/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fingerprint:current,confirmed:true})});statusEl.textContent='Device paired. Operator '+result.operator_id;handoff.hidden=true;compare.hidden=true}catch{fail()}});
(async()=>{try{let exchanged=await request('/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({invitation:local})});csrf=exchanged.csrf;document.querySelector('main').removeAttribute('aria-busy');await poll()}catch{fail()}})();"""

STYLE = b"""html{font:16px system-ui;color:#18181b;background:#fafafa}main{max-width:38rem;margin:8vh auto;padding:2rem}section{margin:2rem 0}a,button{display:inline-block;padding:.8rem 1rem;border-radius:.5rem}a{background:#18181b;color:white}button{font:inherit}output{display:block;font:700 1.4rem ui-monospace,monospace;letter-spacing:.08em;margin:1rem 0}"""


def remote_invitation_url(invitation: dict) -> str:
    from sleeptalk.remote_crypto import canonical

    origin = invitation["descriptor"]["setup_origin"]
    return origin + "/entry#" + encode(canonical(invitation))


class PairingSession:
    """One in-memory local invitation and its confirmation authority."""

    def __init__(self, store, invitation: dict, *, current_binding: Callable[[], str]):
        if invitation.get("kind") != "pairing":
            raise ValueError("Expected a pairing invitation.")
        self.store = store
        self.invitation = invitation
        self.request_id = invitation["descriptor"]["enrollment_id"]
        self.expires_at = invitation["descriptor"]["expires_at"]
        self.current_binding = current_binding
        self.local_invitation = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.cookie_name = "remote_pair_" + hashlib.sha256(self.request_id.encode()).hexdigest()[:16]
        self.remote_url = remote_invitation_url(invitation)
        self.origin = ""
        self.lock = asyncio.Lock()
        self.finished = asyncio.Event()

    def _forget(self) -> None:
        self.remote_url = ""
        self.invitation = {}
        self.local_invitation = ""

    def _active(self) -> None:
        if self.finished.is_set() or time.time() >= self.expires_at:
            raise web.HTTPGone(text="This pairing invitation ended.")

    def _authenticated(self, request: web.Request) -> None:
        self._active()
        if not secrets.compare_digest(request.cookies.get(self.cookie_name, ""), self.cookie):
            raise web.HTTPUnauthorized()
        if not secrets.compare_digest(request.headers.get("X-Setup-CSRF", ""), self.csrf):
            raise web.HTTPForbidden()

    async def exchange(self, request: web.Request) -> web.Response:
        self._active()
        body = await request.json()
        supplied = body.get("invitation") if isinstance(body, dict) else None
        if not isinstance(supplied, str) or not secrets.compare_digest(supplied, self.local_invitation):
            raise web.HTTPUnauthorized()
        response = web.json_response({"csrf": self.csrf})
        lifetime = max(1, min(600, int(self.expires_at - time.time())))
        response.set_cookie(self.cookie_name, self.cookie, httponly=True, samesite="Strict", max_age=lifetime)
        return response

    async def status(self, request: web.Request) -> web.Response:
        self._authenticated(request)
        receipt = self.store.enrollment_status(self.request_id)
        payload = receipt["payload"]
        phase = payload["status"]
        result = {"status": phase, "expires_at": self.expires_at}
        if self.remote_url and phase == "open":
            result["remote_url"] = self.remote_url
        if phase == "awaiting_confirmation":
            result["fingerprint"] = payload["fingerprint"]
        if phase == "confirmed":
            result.update(operator_id=payload["operator_id"])
        response = web.json_response(result)
        if phase in {"confirmed", "expired", "cancelled"}:
            self._forget()
            response._pairing_finished = True
        return response

    async def confirm(self, request: web.Request) -> web.Response:
        self._authenticated(request)
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"fingerprint", "confirmed"} or body.get("confirmed") is not True:
            raise web.HTTPBadRequest()
        fingerprint = body.get("fingerprint")
        if not isinstance(fingerprint, str):
            raise web.HTTPBadRequest()
        async with self.lock:
            self._active()
            status = self.store.enrollment_status(self.request_id)["payload"]
            if status.get("status") != "awaiting_confirmation" or not secrets.compare_digest(status.get("fingerprint", ""), fingerprint):
                raise web.HTTPConflict(text="The pairing code changed or is not ready.")
            result = self.store.confirm_enrollment(
                self.request_id, fingerprint, current_binding=self.current_binding(),
            )
            self._forget()
            response = web.json_response(result)
            response._pairing_finished = True
            return response

    def app(self) -> web.Application:
        @web.middleware
        async def boundary(request, handler):
            if not self.origin or request.headers.get("Host") != urlsplit(self.origin).netloc:
                raise web.HTTPForbidden()
            if request.method != "GET" and request.headers.get("Origin") != self.origin:
                raise web.HTTPForbidden()
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                response = web.Response(status=exc.status, text=exc.text, content_type="text/plain")
            except Exception:
                response = web.json_response({"status": "failed", "code": "pairing_failed"}, status=400)
            response.headers.update({
                "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            })
            if getattr(response, "_pairing_finished", False):
                try:
                    await response.prepare(request)
                    await response.write_eof()
                finally:
                    self.finished.set()
            return response

        app = web.Application(middlewares=[boundary], client_max_size=4096)
        async def asset(request):
            body, content_type = {
                "/": (PAGE, "text/html"),
                "/pair.js": (SCRIPT, "application/javascript"),
                "/pair.css": (STYLE, "text/css"),
            }[request.path]
            return web.Response(body=body, content_type=content_type)
        app.router.add_get("/", asset)
        app.router.add_get("/pair.js", asset)
        app.router.add_get("/pair.css", asset)
        app.router.add_post("/session", self.exchange)
        app.router.add_get("/status", self.status)
        app.router.add_post("/confirm", self.confirm)
        return app


async def serve_pairing(store, invitation: dict, *, current_binding: Callable[[], str], on_ready=None) -> dict:
    session = PairingSession(store, invitation, current_binding=current_binding)
    runner = web.AppRunner(session.app(), access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        session.origin = f"http://127.0.0.1:{runner.addresses[0][1]}"
        receipt = {
            "schema_version": 1, "operation": "remote_pairing", "status": "needs_input",
            "request_id": session.request_id, "expires_at": session.expires_at,
            "pairing_url": session.origin + "/#" + session.local_invitation,
            "next_action": "Open this link in your own browser on the runtime host. Do not open it in an agent browser.",
        }
        if on_ready:
            on_ready(receipt)
        timeout = max(0, session.expires_at - time.time())
        try:
            await asyncio.wait_for(session.finished.wait(), timeout=timeout)
        except TimeoutError:
            return {**receipt, "status": "expired", "pairing_url": None}
        final = store.enrollment_status(session.request_id)["payload"]
        return {"schema_version": 1, "operation": "remote_pairing", **final}
    finally:
        session._forget()
        await runner.cleanup()

"""Disposable protocol peer; no providers, accounts or persistent host state."""
import hashlib
import json
import secrets
import sys
import time

from cryptography.hazmat.primitives.asymmetric import ed25519
from fido2.webauthn import AuthenticationResponse, AttestedCredentialData
from sleeptalk import remote_crypto as c, remote_pairing as p

ORIGIN = "https://setup.example.com"
host = ed25519.Ed25519PrivateKey.generate()
enrollment = p.create_enrollment(host_signing_key=host, workspace_id=c.encode(b"w"*16), runtime_binding_digest="a"*64,
    setup_origin=ORIGIN,relay_origin=ORIGIN,display_host="Synthetic host",display_workspace="Disposable workspace",now=int(time.time()))
pending = None
proof_nonce = secrets.token_bytes(32)
operator_id = c.encode(secrets.token_bytes(16))
phase = "open"
setup = None
context = None
saved = False
cancel_context = None
cancelled = False
submissions = 0
mode = "normal"


def signed(payload):
    return {"payload":payload,"signature":c.sign(payload,host,purpose="receipt")}


def initial():
    return {"kind":"pairing","descriptor":enrollment.descriptor.model_dump(),"signature":enrollment.signature,"capability":enrollment.capability}


def handle(message):
    global pending, phase, setup, context, saved, submissions, cancel_context, cancelled, mode
    action = message["action"]
    body = message.get("body",{})
    if action == "confirm_local":
        assert phase == "awaiting_confirmation"
        phase = "confirmed"
        return {"confirmed":True}
    if action == "new_setup":
        assert phase == "confirmed"
        mode=body.get("mode",message.get("mode","normal"))
        capability=c.encode(secrets.token_bytes(32))
        d=enrollment.descriptor.model_dump()
        d.pop("enrollment_id"); d.pop("registration_challenge")
        d.update(request_id=c.encode(secrets.token_bytes(16)),operator_id=operator_id,operator_binding_revision=1,
            allowed_fields=["deepgram.api_key"],field_actions={"deepgram.api_key":"create"},
            affected_profile_ids={"deepgram.api_key":[]},expected_credential_generations={"deepgram.api_key":0},
            capability_hash=hashlib.sha256(c.decode(capability)).hexdigest())
        if mode in {"repair", "repair_scope"}:
            d["field_actions"]={"deepgram.api_key":"replace"}
            d["expected_credential_generations"]={"deepgram.api_key":1}
            d["affected_profile_ids"]={"deepgram.api_key":["voice-profile@0123456789ab", "<img src=x onerror=alert(1)>"]}
        if mode == "model_auth":
            field = "agent.credential"
            d.update(allowed_fields=[field], field_actions={field:"create"}, affected_profile_ids={field:[]},
                expected_credential_generations={field:0}, model_auth={"mode":"api_key", "protocol":"openai-completions",
                "base_url":"https://provider.example/proxy/v1", "provider":"custom-model-provider", "model":"<img src=x onerror=alert(1)>"})
        setup={"kind":"setup","descriptor":d,"signature":c.sign(d,host,purpose="invitation"),"capability":capability}
        saved=False; cancelled=False
        return setup
    pairing = message.get("request_id") == enrollment.descriptor.enrollment_id
    invitation = initial() if pairing else setup
    assert message.get("authorization") == "Bearer " + invitation["capability"]
    d=invitation["descriptor"]
    request_id=d.get("enrollment_id",d.get("request_id"))
    if action == "status":
        if pairing:
            result={"operation":"pairing_status","request_id":request_id,"client_nonce":body["client_nonce"],"status":phase}
            if phase == "open": result["publicKey"]=p.registration_options(enrollment.descriptor)
            if pending: result["fingerprint"]=pending.fingerprint
            if phase == "confirmed": result.update(operator_id=operator_id,credential_id=c.encode(AttestedCredentialData(pending.enrolled_credential).credential_id))
            return signed(result)
        if mode in {"repair","profile_error"}:
            return signed({"request_id":request_id,"client_nonce":body["client_nonce"],"revision":1,"status":"complete" if mode == "repair" else "open", "fields":{"deepgram.api_key":"saved"},"profile_ready":False, **({"profile_error":"profile_validation_failed"} if mode == "profile_error" else {})})
        return signed({"request_id":request_id,"client_nonce":body["client_nonce"],"revision":int(saved or cancelled),"status":"cancelled" if cancelled else "complete" if saved else "open","fields":{"deepgram.api_key":"saved" if saved else "empty"},"profile_ready":saved})
    if action == "registration":
        pending=p.decrypt_registration(c.canonical(body),expected=enrollment.descriptor,private_key=enrollment.private_key)
        return signed({"operation":"pairing_proof","request_id":request_id,"registration_digest":hashlib.sha256(c.canonical(body)).hexdigest(),
            "credential_digest":pending.credential_digest,"fingerprint":pending.fingerprint,"proof_nonce":c.encode(proof_nonce),
            "publicKey":p.proof_options(enrollment.descriptor,pending,proof_nonce)})
    if action == "proof":
        p.verify_possession(c.canonical(body["assertion"]),descriptor=enrollment.descriptor,pending=pending,nonce=proof_nonce)
        phase="awaiting_confirmation"
        return signed({"operation":"pairing_status","request_id":request_id,"status":phase,"fingerprint":pending.fingerprint,
            "assertion_digest":hashlib.sha256(c.canonical(body["assertion"])).hexdigest()})
    if action == "challenge":
        context=c.Context(version=1,request_id=request_id,descriptor_digest=hashlib.sha256(c.canonical(d)).hexdigest(),host_id=d["host_id"],operator_id=operator_id,
            operator_binding_revision=1,workspace_id=d["workspace_id"],runtime_binding_digest=d["runtime_binding_digest"],provider_field=body["field"],action="create",expected_credential_generation=0,
            submission_id=body["submission_id"],challenge_id=c.encode(secrets.token_bytes(16)),challenge_nonce=c.encode(secrets.token_bytes(32)),connection_generation=c.encode(secrets.token_bytes(16)),request_revision=0,request_expires_at=d["expires_at"])
        return signed({"operation":"challenge","context":context.model_dump(),"expires_at":d["expires_at"]})
    if action == "submission":
        value=c.decrypt_authorized(c.canonical(body),expected=context,private_key=enrollment.private_key,enrolled_credential=pending.enrolled_credential,rp_id="setup.example.com",origin=ORIGIN)
        assert value == "synthetic-provider-value"
        saved=mode != "retries"; submissions+=1
        digest=hashlib.sha256(c.canonical(body)).hexdigest()
        return signed({"request_id":request_id,"submission_id":context.submission_id,"digest":digest,"revision":int(saved),"field":context.provider_field,"status":"saved" if saved else "validating"})
    if action == "cancel":
        if body["phase"] == "challenge":
            cancel_context={"request_id":request_id,"challenge_id":c.encode(secrets.token_bytes(16)),"connection_generation":c.encode(secrets.token_bytes(16)),"revision":0,"nonce":c.encode(secrets.token_bytes(32)),"expires_at":d["expires_at"]}
            challenge=hashlib.sha256(b"sleeptalk/setup/cancel/v1\0"+c.canonical(cancel_context)).digest()
            server=c._server("setup.example.com",ORIGIN)
            options,_=server.authenticate_begin([AttestedCredentialData(pending.enrolled_credential)],user_verification="required",challenge=challenge)
            return signed({"operation":"cancel_challenge",**cancel_context,"client_nonce":body["client_nonce"],"publicKey":json.loads(json.dumps(dict(options)["publicKey"]))})
        challenge=hashlib.sha256(b"sleeptalk/setup/cancel/v1\0"+c.canonical(cancel_context)).digest()
        server=c._server("setup.example.com",ORIGIN); credential=AttestedCredentialData(pending.enrolled_credential)
        _,state=server.authenticate_begin([credential],user_verification="required",challenge=challenge)
        server.authenticate_complete(state,[credential],AuthenticationResponse.from_dict(body["assertion"]))
        cancelled=True
        return signed({"request_id":request_id,"status":"cancelled"})
    raise ValueError("Unknown synthetic fixture action")

print(json.dumps(initial()),flush=True)
for line in sys.stdin:
    try: result=handle(json.loads(line))
    except Exception as exc:
        import traceback
        result={"fixture_error":type(exc).__name__,"line":traceback.extract_tb(exc.__traceback__)[-1].lineno}
    print(json.dumps(result),flush=True)

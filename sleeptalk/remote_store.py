"""Durable remote authorization decisions. Secret values live only in CredentialStore."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from typing import Callable

from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption, load_der_private_key

from sleeptalk.credential_store import CredentialStore, _private_file
from sleeptalk.remote_crypto import Context, Envelope, ProtocolError, canonical, decode, encode, decrypt_authorized, parse, sign

FIELDS = {"deepgram.api_key": "deepgram", "cartesia.api_key": "cartesia", "elevenlabs.api_key": "elevenlabs", "discord.bot_token": "discord", "agent.credential": "agent"}


def field_provider(field: str, spec: dict) -> str:
    if field != 'agent.credential':
        return FIELDS[field]
    from sleeptalk.agent_auth import AgentAuthChoice, auth_scope
    try:
        choice = AgentAuthChoice.model_validate(spec.get('auth_request'))
    except ValueError:
        raise ProtocolError('Model authentication scope is missing or invalid.') from None
    return auth_scope(choice)

TERMINAL = {"complete", "cancelled", "expired"}


def identifier() -> str:
    return encode(secrets.token_bytes(16))


def digest(value: dict) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def private_bytes(key) -> str:
    return encode(key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption()))


def private_key(value: str):
    return load_der_private_key(decode(value), password=None)


class RemoteStore:
    def __init__(self, root: Path, credentials: CredentialStore, *, clock: Callable[[], float] = time.time):
        self.root, self.credentials, self.clock = Path(root), credentials, clock
        with self.transaction() as db:
            if self._get(db, "host", "identity") is None:
                self._put(db, "host", "identity", {"private_key": private_bytes(ed25519.Ed25519PrivateKey.generate())})
            self.key = private_key(self._get(db, "host", "identity")["private_key"])
        self.public_key = encode(self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
        self.host_id = hashlib.sha256(decode(self.public_key)).hexdigest()

    @contextmanager
    def transaction(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ProtocolError("Remote state must be owned by this user.")
        self.root.chmod(0o700)
        fd = _private_file(self.root / "mutation.lock")
        db = None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.close(_private_file(self.root / "remote.sqlite3"))
            db = sqlite3.connect(self.root / "remote.sqlite3", isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(kind,id))")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db is not None and db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            if db is not None:
                db.close()
            os.close(fd)

    @staticmethod
    def _get(db, kind, key):
        row = db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put(db, kind, key, value):
        db.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body", (kind, key, json.dumps(value)))

    @staticmethod
    def _all(db, kind):
        return [(key, json.loads(raw)) for key, raw in db.execute("SELECT id,body FROM records WHERE kind=?", (kind,)).fetchall()]

    def signed(self, payload: dict) -> dict:
        return {"payload": payload, "signature": sign(payload, self.key, purpose="receipt")}

    def _operator(self, db, operator_id, workspace_id, binding):
        operator = self._get(db, "operator", operator_id)
        if not operator or operator["revoked"] or operator["workspaces"].get(workspace_id) != binding:
            raise ProtocolError("Operator pairing is unavailable for this workspace.")
        return operator

    def register_confirmed_operator(self, *, operator_id: str, credential: bytes, workspace_id: str,
                                    binding: str, origin: str, enrollment_id: str):
        """Called only after the pairing ledger has verified possession and local confirmation."""
        with self.transaction() as db:
            existing = self._get(db, "operator", operator_id)
            value = {"credential": encode(credential), "workspaces": {workspace_id: binding}, "origin": origin,
                     "revision": 1, "revoked": False, "enrollment_id": enrollment_id}
            if existing and existing != value:
                raise ProtocolError("Operator binding conflicts with its enrollment.")
            self._put(db, "operator", operator_id, value)

    def create_request(self, *, operator_id: str, workspace_id: str, binding: str, spec: dict,
                       fields: dict, origin: str, display_host: str, display_workspace: str) -> dict:
        if not fields or not set(fields) <= FIELDS.keys() or len(canonical(spec)) > 16384:
            raise ProtocolError("Invalid setup scope.")
        if ('agent.credential' in fields) != bool(spec.get('auth_request')):
            raise ProtocolError('Model credential collection does not match the selected scope.')
        now = int(self.clock())
        with self.transaction() as db:
            self._reconcile(db)
            operator = self._operator(db, operator_id, workspace_id, binding)
            if operator["origin"] != origin:
                raise ProtocolError("Pairing origin changed.")
            if spec.get('request_id'):
                for old_id,old in self._all(db,'request'):
                    if old['spec'].get('request_id') == spec['request_id']:
                        if old['spec'] != spec or old['descriptor']['operator_id'] != operator_id:
                            raise ProtocolError('This caller request ID belongs to different setup choices.')
                        return {'request_id':old_id,'status':old['status'],'profile_ready':old['profile_ready'],
                            'invitation_available':False,'next_action':'Read status. Use a new request ID if the original invitation was lost.'}
            live = [r for _, r in self._all(db, "request") if r["descriptor"]["expires_at"] > now]
            if len(live) >= 3 or any(r['status'] == 'open' and r["spec"]["profile_path"] == spec["profile_path"] for r in live):
                raise ProtocolError("A setup request already owns this profile or host capacity.")
            request_id, capability = identifier(), encode(secrets.token_bytes(32))
            key = ec.generate_private_key(ec.SECP256R1())
            for field, target in fields.items():
                if set(target) != {"reference", "generation", "profiles"}:
                    raise ProtocolError("Invalid field scope.")
                if target["reference"]:
                    current = self.credentials.status(target["reference"])
                    if current.provider != field_provider(field, spec) or current.generation != target["generation"] or set(current.profiles) != set(target["profiles"]):
                        raise ProtocolError("Credential replacement scope changed.")
                elif target["generation"] != 0 or target["profiles"]:
                    raise ProtocolError("New credentials must have an empty prior scope.")
            descriptor = {"version": 1, "request_id": request_id, "operator_id": operator_id,
                "operator_binding_revision": operator["revision"], "host_id": self.host_id,
                "host_signing_public_key": self.public_key,
                "hpke_suite": "DHKEM-P256-HKDF-SHA256/HKDF-SHA256/AES-128-GCM",
                "recipient_public_key": encode(key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)),
                "workspace_id": workspace_id, "runtime_binding_digest": binding,
                "display_host": display_host[:100], "display_workspace": display_workspace[:100],
                "allowed_fields": sorted(fields), "field_actions": {f: "replace" if t["reference"] else "create" for f,t in fields.items()},
                "affected_profile_ids": {f:t["profiles"] for f,t in fields.items()},
                "expected_credential_generations": {f:t["generation"] for f,t in fields.items()},
                "issued_at": now, "expires_at": now+900, "setup_origin": origin, "relay_origin": origin,
                "capability_hash": hashlib.sha256(decode(capability)).hexdigest()}
            if spec.get('auth_request'):
                from sleeptalk.agent_auth import AgentAuthChoice, validate_model_auth_labels
                metadata = AgentAuthChoice.model_validate(spec['auth_request']).model_dump()
                try:
                    validate_model_auth_labels(spec.get('model'), spec.get('provider'))
                except ValueError as exc:
                    raise ProtocolError(str(exc)) from None
                descriptor['model_auth'] = {**metadata, 'model': spec['model'], 'provider': spec['provider']}
            state = {"descriptor": descriptor, "spec": spec, "targets": fields, "key": private_bytes(key),
                     "status": "open", "revision": 0, "generation": (self._get(db,'host','connection') or {}).get('generation'), "fields": {f:{"status":"missing"} for f in fields},
                     "challenge": None, "inflight": None, "challenge_times": [], "failures": {}, "profile_ready": False}
            comparable = {k:v for k,v in spec.items() if k != 'request_id'}
            for _,previous in sorted(self._all(db,'request'),key=lambda item:item[1]['descriptor']['issued_at'],reverse=True):
                if (previous['status'] not in {'cancelled','expired'}
                        or previous['descriptor']['operator_id'] != operator_id
                        or previous['descriptor']['operator_binding_revision'] != operator['revision']
                        or previous['descriptor']['runtime_binding_digest'] != binding
                        or {k:v for k,v in previous['spec'].items() if k != 'request_id'} != comparable):
                    continue
                for field,data in previous['fields'].items():
                    if field not in fields or data['status'] != 'saved' or fields[field]['reference'] or state['fields'][field]['status'] == 'saved':
                        continue
                    try:
                        current = self.credentials.status(data['reference'])
                    except Exception:
                        continue
                    if current.available and current.generation == data['generation']:
                        state['fields'][field] = data
            if all(data['status'] == 'saved' for data in state['fields'].values()):
                state['key'] = None
            self._put(db, "request", request_id, state)
            return {"descriptor": descriptor, "signature": sign(descriptor, self.key, purpose="invitation"), "capability": capability}

    def _request(self, db, request_id, capability=None, *, active=False):
        state = self._get(db, "request", request_id)
        if state is None:
            raise ProtocolError("Unknown remote setup request.")
        descriptor = state["descriptor"]
        if capability is not None and not secrets.compare_digest(hashlib.sha256(decode(capability)).hexdigest(), descriptor["capability_hash"]):
            raise ProtocolError("Invalid setup admission.")
        if active:
            operator = self._operator(db, descriptor["operator_id"], descriptor["workspace_id"], descriptor["runtime_binding_digest"])
            if state["status"] != "open" or descriptor["expires_at"] <= self.clock() or operator["revision"] != descriptor["operator_binding_revision"]:
                raise ProtocolError("Remote setup request is no longer active.")
        return state

    def set_connection(self, generation: str):
        if len(decode(generation)) != 16:
            raise ProtocolError("Invalid connection generation.")
        with self.transaction() as db:
            self._reconcile(db, restart=True)
            self._put(db,'host','connection',{'generation':generation})
            for request_id, state in self._all(db, "request"):
                if state["status"] == "open":
                    state["generation"], state["challenge"] = generation, None
                    self._put(db, "request", request_id, state)

    def challenge(self, request_id: str, capability: str, *, field: str, submission_id: str) -> dict:
        if len(decode(submission_id)) != 16:
            raise ProtocolError("Invalid submission identifier.")
        with self.transaction() as db:
            self._reconcile(db)
            state = self._request(db, request_id, capability, active=True)
            if field not in state["fields"] or state["fields"][field]["status"] == "saved" or state["inflight"] or not state["generation"]:
                raise ProtocolError("This field cannot accept a challenge now.")
            now = int(self.clock())
            state["challenge_times"] = [t for t in state["challenge_times"] if t > now-60]
            if len(state["challenge_times"]) >= 10:
                raise ProtocolError("Setup challenge limit reached.")
            state["challenge_times"].append(now)
            d = state["descriptor"]
            context = Context(version=1, request_id=request_id, descriptor_digest=digest(d), host_id=self.host_id,
                operator_id=d["operator_id"], operator_binding_revision=d["operator_binding_revision"],
                workspace_id=d["workspace_id"], runtime_binding_digest=d["runtime_binding_digest"],
                provider_field=field, action=d["field_actions"][field], expected_credential_generation=d["expected_credential_generations"][field],
                submission_id=submission_id, challenge_id=identifier(), challenge_nonce=encode(secrets.token_bytes(32)),
                connection_generation=state["generation"], request_revision=state["revision"], request_expires_at=d["expires_at"])
            state["challenge"] = {"context": context.model_dump(), "expires_at": min(now+60,d["expires_at"])}
            self._put(db, "request", request_id, state)
            return self.signed({"operation":"challenge", **state["challenge"]})

    def reserve(self, request_id: str, capability: str, raw: bytes) -> tuple[dict, str | None]:
        envelope = Envelope.model_validate(parse(raw))
        submission_id = envelope.context.submission_id
        operation_digest = hashlib.sha256(canonical(envelope.model_dump())).hexdigest()
        with self.transaction() as db:
            state = self._request(db, request_id, capability)
            previous = self._get(db, "submission", submission_id)
            if previous:
                if previous["request_id"] != request_id or previous["digest"] != operation_digest:
                    raise ProtocolError("Submission identifier conflicts with earlier content.")
                return previous["receipt"], None
            state = self._request(db, request_id, capability, active=True)
            challenge = state["challenge"]
            if state["inflight"] or not challenge or challenge["expires_at"] <= self.clock() or challenge["context"] != envelope.context.model_dump():
                raise ProtocolError("Submission challenge is unavailable or expired.")
            operator = self._operator(db, envelope.context.operator_id, envelope.context.workspace_id, envelope.context.runtime_binding_digest)
            from urllib.parse import urlsplit
            value = decrypt_authorized(raw, expected=Context.model_validate(challenge["context"]), private_key=private_key(state["key"]),
                enrolled_credential=decode(operator["credential"]), rp_id=urlsplit(operator["origin"]).hostname, origin=operator["origin"])
            field = envelope.context.provider_field
            state["challenge"], state["inflight"] = None, submission_id
            state["fields"][field] = {"status":"validating", "submission_id":submission_id}
            state["revision"] += 1
            receipt = self.signed({"request_id":request_id, "submission_id":submission_id, "digest":operation_digest,
                                   "revision":state["revision"], "field":field, "status":"validating"})
            submission = {"request_id":request_id, "field":field, "digest":operation_digest, "operation_id":"remote:"+submission_id,
                          "status":"validating", "receipt":receipt, "details":{}}
            self._put(db,"submission",submission_id,submission)
            self._put(db,"request",request_id,state)
            return receipt,value

    def commit(self, submission_id: str, value: str, details: dict, *, current_binding: str | Callable[[], str]) -> dict:
        with self.transaction() as db:
            self._reconcile(db)
            submission = self._get(db,"submission",submission_id)
            if not submission:
                raise ProtocolError("Unknown submission.")
            if submission["status"] == "committing":
                state = self._get(db,"request",submission["request_id"])
                self._failed(db,submission_id,submission,state,"interrupted")
                return submission["receipt"]
            if submission["status"] != "validating":
                return submission["receipt"]
            state = self._request(db,submission["request_id"],active=True)
            actual = current_binding() if callable(current_binding) else current_binding
            if state["descriptor"]["runtime_binding_digest"] != actual or state["inflight"] != submission_id:
                raise ProtocolError("The selected workspace or runtime changed.")
            submission["status"], submission["details"] = "committing", details
            self._put(db,"submission",submission_id,submission)
        # Commit the intent before entering the external credential transaction.
        with self.transaction() as db:
            submission = self._get(db,"submission",submission_id)
            state = self._request(db,submission["request_id"],active=True)
            if submission["status"] != "committing" or state["inflight"] != submission_id:
                raise ProtocolError("Submission was interrupted before commit.")
            actual = current_binding() if callable(current_binding) else current_binding
            if state['descriptor']['runtime_binding_digest'] != actual:
                raise ProtocolError('Runtime binding changed before credential commit.')
            target = state["targets"][submission["field"]]
            result = self.credentials.commit_validated(field_provider(submission["field"], state["spec"]),value,
                operation_id=submission["operation_id"],operation_digest=submission["digest"],
                reference=target["reference"],expected_generation=target["generation"],affected_profiles=target["profiles"])
            self._saved(db,submission_id,submission,state,result)
            return submission["receipt"]

    def _saved(self, db, submission_id, submission, state, result):
        submission["status"] = "saved"
        state["fields"][submission["field"]] = {"status":"saved", "submission_id":submission_id, **asdict(result), "details":submission["details"]}
        state["inflight"] = None
        if all(field["status"] == "saved" for field in state["fields"].values()):
            state["key"] = None
        state["revision"] += 1
        submission["receipt"] = self.signed({"request_id":submission["request_id"],"submission_id":submission_id,
            "digest":submission["digest"],"revision":state["revision"],"field":submission["field"],"status":"saved",**asdict(result)})
        self._put(db,"submission",submission_id,submission)
        self._put(db,"request",submission["request_id"],state)

    def fail(self, submission_id: str, code: str):
        code = {"permission_denied":"insufficient_scope", "quota_or_rate_limit":"quota_exhausted"}.get(code,code)
        if code not in {"invalid_credential","insufficient_scope","voice_unavailable","quota_exhausted","network_unavailable","validation_failed","validation_timeout","interrupted"}:
            code = "validation_failed"
        with self.transaction() as db:
            self._reconcile(db)
            submission = self._get(db,"submission",submission_id)
            if not submission or submission["status"] not in {"validating","committing"}:
                return
            state = self._get(db,"request",submission["request_id"])
            self._failed(db,submission_id,submission,state,code)

    def _failed(self, db, submission_id, submission, state, code):
        state["inflight"] = None
        state["revision"] += 1
        state["fields"][submission["field"]] = {"status":code,"submission_id":submission_id}
        state["failures"][submission["field"]] = state["failures"].get(submission["field"],0)+1
        if state["failures"][submission["field"]] >= 5:
            state["status"],state["key"],state["challenge"] = "cancelled",None,None
        submission["status"] = code
        submission["receipt"] = self.signed({"request_id":submission["request_id"],"submission_id":submission_id,
            "digest":submission["digest"],"revision":state["revision"],"field":submission["field"],"status":code})
        self._put(db,"submission",submission_id,submission)
        self._put(db,"request",submission["request_id"],state)

    def _reconcile(self, db, *, restart=False):
        for submission_id, submission in self._all(db,"submission"):
            if submission["status"] not in {"validating","committing"}:
                continue
            result = self.credentials.operation_result(submission["operation_id"])
            state = self._get(db,"request",submission["request_id"])
            if result:
                self._saved(db,submission_id,submission,state,result)
            elif restart:
                self._failed(db,submission_id,submission,state,"interrupted")
        now = int(self.clock())
        for request_id,state in self._all(db,"request"):
            if state["status"] == "open" and state["descriptor"]["expires_at"] <= now:
                state["status"],state["key"],state["challenge"] = "expired",None,None
                self._put(db,"request",request_id,state)
            if state['descriptor']['expires_at']+86400 <= now:
                for submission_id,submission in self._all(db,'submission'):
                    if submission['request_id'] == request_id:
                        db.execute('DELETE FROM records WHERE kind=? AND id=?',('submission',submission_id))
                db.execute('DELETE FROM records WHERE kind=? AND id=?',('request',request_id))
        for enrollment_id,state in self._all(db,'enrollment'):
            if state['status'] not in {'confirmed','cancelled','expired'} and state['descriptor']['expires_at'] <= now:
                state.update(status='expired',key=None,proof_nonce=None,pending=None)
                self._put(db,'enrollment',enrollment_id,state)
            if state['descriptor']['expires_at']+86400 <= now:
                db.execute('DELETE FROM records WHERE kind=? AND id=?',('enrollment',enrollment_id))

    def cancel(self, request_id: str):
        """Trusted local cancellation; remote cancellation requires a verified assertion first."""
        with self.transaction() as db:
            self._reconcile(db)
            state = self._request(db,request_id)
            if state["status"] == "open":
                state["status"],state["key"],state["challenge"] = "cancelled",None,None
                state["revision"] += 1
                self._put(db,"request",request_id,state)

    def cancel_challenge(self, request_id: str, capability: str, client_nonce: str) -> dict:
        from sleeptalk.remote_crypto import assertion_options
        if len(decode(client_nonce)) != 16:
            raise ProtocolError("Invalid cancellation nonce.")
        with self.transaction() as db:
            self._reconcile(db)
            state = self._request(db, request_id, capability, active=True)
            if not state['generation']:
                raise ProtocolError("Host is not connected.")
            now = int(self.clock())
            previous = state.get('cancel_challenge')
            if previous and previous['expires_at'] > now:
                raise ProtocolError("A cancellation challenge is already pending.")
            context = {'request_id':request_id, 'challenge_id':identifier(),
                'connection_generation':state['generation'], 'revision':state['revision'],
                'nonce':encode(secrets.token_bytes(32)), 'expires_at':min(now+60,state['descriptor']['expires_at'])}
            state['cancel_challenge'] = context
            self._put(db, 'request', request_id, state)
            operator = self._get(db, 'operator', state['descriptor']['operator_id'])
            challenge = hashlib.sha256(b'sleeptalk/setup/cancel/v1\0'+canonical(context)).digest()
            return self.signed({'operation':'cancel_challenge', 'client_nonce':client_nonce, **context,
                'publicKey':assertion_options(decode(operator['credential']),challenge,operator['origin'])})

    def cancel_authorized(self, request_id: str, capability: str, challenge_id: str, assertion: dict) -> dict:
        from sleeptalk.remote_crypto import verify_assertion
        with self.transaction() as db:
            self._reconcile(db)
            state = self._request(db, request_id, capability, active=True)
            context = state.get('cancel_challenge')
            if (not context or context['challenge_id'] != challenge_id or context['expires_at'] <= self.clock()
                    or context['connection_generation'] != state['generation'] or context['revision'] != state['revision']):
                raise ProtocolError("Cancellation challenge is unavailable or stale.")
            operator = self._get(db, 'operator', state['descriptor']['operator_id'])
            challenge = hashlib.sha256(b'sleeptalk/setup/cancel/v1\0'+canonical(context)).digest()
            verify_assertion(assertion,credential=decode(operator['credential']),challenge=challenge,origin=operator['origin'])
            state.update(status='cancelled',key=None,challenge=None,cancel_challenge=None,revision=state['revision']+1)
            self._put(db,'request',request_id,state)
            return self.signed({'request_id':request_id,'status':'cancelled','revision':state['revision']})

    def request_state(self, request_id: str) -> dict:
        """Private coordinator data. Never serialize this through a public status endpoint."""
        with self.transaction() as db:
            self._reconcile(db)
            return self._request(db, request_id)

    def finalize(self, request_id: str, document: str, *, current_binding: Callable[[], str],
                 auth_generations: dict[str, int] | None = None) -> None:
        from sleeptalk.private_input import atomic_write
        with self.transaction() as db:
            self._reconcile(db)
            state = self._request(db, request_id, active=True)
            if any(f['status'] != 'saved' for f in state['fields'].values()):
                raise ProtocolError("Provider validation is incomplete.")
            if current_binding() != state['descriptor']['runtime_binding_digest']:
                raise ProtocolError("Runtime binding changed before profile creation.")
            path = Path(state['spec']['profile_path'])
            if path.exists() and path.read_text() != document:
                raise ProtocolError("An existing profile has different settings.")
            label = state['spec']['profile']+'@'+hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
            generations = {field['reference']:field['generation'] for field in state['fields'].values()}
            expected_auth = state['spec'].get('auth')
            expected_refs = {expected_auth['credential_ref']} if expected_auth else set()
            if state['spec'].get('auth_request'):
                expected_refs = {state['fields']['agent.credential']['reference']}
            if set(auth_generations or {}) != expected_refs:
                raise ProtocolError('Model credential verification is missing before profile creation.')
            generations.update(auth_generations or {})
            with self.credentials.bind_profile_generation(generations,label):
                atomic_write(path,document.encode())
            state.update(status='complete',key=None,challenge=None,cancel_challenge=None,
                         profile_ready=True,revision=state['revision']+1,profile_error=None)
            self._put(db,'request',request_id,state)

    def profile_failed(self, request_id: str) -> None:
        with self.transaction() as db:
            state = self._request(db,request_id)
            if state['status'] == 'open':
                state['profile_error'] = 'profile_validation_failed'
                state['key'] = None
                self._put(db,'request',request_id,state)

    def complete_repair(self, request_id: str) -> None:
        with self.transaction() as db:
            self._reconcile(db)
            state = self._request(db,request_id,active=True)
            if not state['spec'].get('replace_reference') or any(f['status'] != 'saved' for f in state['fields'].values()):
                raise ProtocolError('The credential repair is incomplete.')
            state.update(status='complete',key=None,challenge=None,cancel_challenge=None,revision=state['revision']+1)
            self._put(db,'request',request_id,state)

    def revoke(self, operator_id: str):
        with self.transaction() as db:
            self._reconcile(db)
            operator = self._get(db,"operator",operator_id)
            if not operator:
                raise ProtocolError("Unknown operator.")
            operator["revoked"] = True
            operator["revision"] += 1
            self._put(db,"operator",operator_id,operator)
            for request_id,state in self._all(db,"request"):
                if state["descriptor"]["operator_id"] == operator_id and state["status"] == "open":
                    state["status"],state["key"],state["challenge"] = "cancelled",None,None
                    state["revision"] += 1
                    self._put(db,"request",request_id,state)

    def status(self, request_id: str, *, capability: str | None = None, client_nonce: str = "") -> dict:
        if client_nonce and len(decode(client_nonce)) != 16:
            raise ProtocolError("Invalid status nonce.")
        with self.transaction() as db:
            self._reconcile(db)
            state = self._request(db,request_id,capability)
            payload = {"request_id":request_id,"revision":state["revision"],"status":state["status"],
                       "fields":{f:{k:v for k,v in data.items() if k != "details"} for f,data in state["fields"].items()},
                       "client_nonce":client_nonce,"profile_ready":state["profile_ready"]}
            if state.get('profile_error'):
                payload['profile_error'] = state['profile_error']
            if state["inflight"]:
                payload["submission"] = self._get(db,"submission",state["inflight"])["receipt"]
            return self.signed(payload)

    def requests(self) -> list[dict]:
        with self.transaction() as db:
            self._reconcile(db)
            return [state for _,state in self._all(db,"request") if state["status"] == "open"]

    def create_enrollment(self, *, workspace_id: str, binding: str, origin: str,
                          display_host: str, display_workspace: str, workspace_spec: dict) -> dict:
        from sleeptalk.remote_pairing import create_enrollment
        enrollment = create_enrollment(host_signing_key=self.key,workspace_id=workspace_id,
            runtime_binding_digest=binding,setup_origin=origin,relay_origin=origin,
            display_host=display_host,display_workspace=display_workspace,now=int(self.clock()))
        descriptor = enrollment.descriptor.model_dump()
        with self.transaction() as db:
            live = [e for _,e in self._all(db,'enrollment') if e['descriptor']['expires_at'] > self.clock()]
            if len(live) >= 3:
                raise ProtocolError('Enrollment capacity reached.')
            self._put(db,'enrollment',descriptor['enrollment_id'],{'descriptor':descriptor,'key':private_bytes(enrollment.private_key),
                'status':'open','workspace_spec':workspace_spec,'pending':None,'proof_nonce':None,'registration_digest':None,'assertion_digest':None})
        return {'kind':'pairing','descriptor':descriptor,'signature':enrollment.signature,'capability':enrollment.capability}

    def _enrollment(self, db, request_id, capability=None, *, active=False):
        state = self._get(db,'enrollment',request_id)
        if not state:
            raise ProtocolError('Unknown pairing request.')
        if capability is not None and not secrets.compare_digest(hashlib.sha256(decode(capability)).hexdigest(),state['descriptor']['capability_hash']):
            raise ProtocolError('Invalid pairing admission.')
        if active and (state['status'] in {'confirmed','cancelled','expired'} or state['descriptor']['expires_at'] <= self.clock()):
            raise ProtocolError('Pairing request is no longer active.')
        return state

    def enroll_registration(self, request_id: str, capability: str, raw: bytes) -> dict:
        from sleeptalk.remote_pairing import EnrollmentDescriptor, decrypt_registration, pending_credential, proof_options
        registration_digest = hashlib.sha256(canonical(parse(raw))).hexdigest()
        with self.transaction() as db:
            state = self._enrollment(db,request_id,capability,active=True)
            descriptor = EnrollmentDescriptor.model_validate(state['descriptor'])
            if state['registration_digest']:
                if state['registration_digest'] != registration_digest:
                    raise ProtocolError('Pairing registration conflicts with the pending credential.')
                return state['proof_receipt']
            pending = decrypt_registration(raw,expected=descriptor,private_key=private_key(state['key']))
            nonce = secrets.token_bytes(32)
            state.update(status='pending',pending=encode(pending.enrolled_credential),proof_nonce=encode(nonce),
                         registration_digest=registration_digest,key=None)
            receipt = self.signed({'operation':'pairing_proof','request_id':request_id,'fingerprint':pending.fingerprint,
                'publicKey':proof_options(descriptor,pending,nonce),'registration_digest':registration_digest,
                'proof_nonce':encode(nonce),'credential_digest':pending.credential_digest})
            state['proof_receipt'] = receipt
            self._put(db,'enrollment',request_id,state)
            return receipt

    def enroll_possession(self, request_id: str, capability: str, assertion: dict) -> dict:
        from sleeptalk.remote_pairing import EnrollmentDescriptor, pending_credential, verify_possession
        raw = canonical(assertion)
        assertion_digest = hashlib.sha256(raw).hexdigest()
        with self.transaction() as db:
            state = self._enrollment(db,request_id,capability,active=True)
            if state['status'] not in {'pending','awaiting_confirmation'}:
                raise ProtocolError('Pairing is not awaiting possession proof.')
            descriptor = EnrollmentDescriptor.model_validate(state['descriptor'])
            pending = pending_credential(decode(state['pending']),request_id)
            if state['assertion_digest']:
                if state['assertion_digest'] != assertion_digest:
                    raise ProtocolError('Pairing proof conflicts with the accepted proof.')
            else:
                verify_possession(raw,descriptor=descriptor,pending=pending,nonce=decode(state['proof_nonce']))
                state.update(status='awaiting_confirmation',assertion_digest=assertion_digest,proof_nonce=None)
                self._put(db,'enrollment',request_id,state)
            return self.signed({'operation':'pairing_status','request_id':request_id,'status':state['status'],
                'fingerprint':pending.fingerprint,'assertion_digest':assertion_digest})

    def confirm_enrollment(self, request_id: str, fingerprint: str, *, current_binding: str) -> dict:
        from sleeptalk.remote_pairing import pending_credential
        from fido2.webauthn import AttestedCredentialData
        with self.transaction() as db:
            state = self._enrollment(db,request_id,active=True)
            if state['status'] != 'awaiting_confirmation' or state['descriptor']['runtime_binding_digest'] != current_binding:
                raise ProtocolError('Pairing is not ready for local confirmation.')
            pending = pending_credential(decode(state['pending']),request_id)
            if not secrets.compare_digest(pending.fingerprint,fingerprint):
                raise ProtocolError('Pairing comparison did not match.')
            operator_id = identifier()
            d = state['descriptor']
            self._put(db,'operator',operator_id,{'credential':state['pending'],'workspaces':{d['workspace_id']:current_binding},
                'origin':d['setup_origin'],'revision':1,'revoked':False,'enrollment_id':request_id})
            state.update(status='confirmed',operator_id=operator_id,key=None,proof_nonce=None,
                         credential_id=encode(AttestedCredentialData(decode(state['pending'])).credential_id))
            self._put(db,'enrollment',request_id,state)
            return {'operator_id':operator_id,'workspace_id':d['workspace_id'],'host_id':self.host_id}

    def enrollment_status(self, request_id: str, *, capability: str | None = None, client_nonce: str = '') -> dict:
        from sleeptalk.remote_pairing import EnrollmentDescriptor, pending_credential, registration_options
        if client_nonce and len(decode(client_nonce)) != 16:
            raise ProtocolError('Invalid status nonce.')
        with self.transaction() as db:
            state = self._enrollment(db,request_id,capability)
            if state['status'] not in {'confirmed','cancelled','expired'} and state['descriptor']['expires_at'] <= self.clock():
                state.update(status='expired',key=None,proof_nonce=None)
                self._put(db,'enrollment',request_id,state)
            payload = {'operation':'pairing_status','request_id':request_id,'status':state['status'],'client_nonce':client_nonce}
            if state['status'] == 'open':
                payload['publicKey'] = registration_options(EnrollmentDescriptor.model_validate(state['descriptor']))
            if state['pending']:
                payload['fingerprint'] = pending_credential(decode(state['pending']),request_id).fingerprint
            if state['status'] == 'confirmed':
                payload.update(operator_id=state['operator_id'],credential_id=state['credential_id'],
                               workspace_id=state['descriptor']['workspace_id'],host_id=self.host_id)
            return self.signed(payload)

    def routes(self) -> list[dict]:
        with self.transaction() as db:
            self._reconcile(db)
            states = self._all(db,'request') + self._all(db,'enrollment')
            return [{'request_id':request_id,'capability_hash':state['descriptor']['capability_hash'],
                     'expires_at':state['descriptor']['expires_at']} for request_id,state in states
                    if state['descriptor']['expires_at'] > self.clock()]

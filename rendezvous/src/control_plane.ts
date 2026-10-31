import { DurableObject } from "cloudflare:workers";

import { constantTimeEqual, sha256 } from "./access_protocol";
import {
  SESSION_CAP_SECONDS,
  SETUP_SESSION_CAP_SECONDS,
  createTicketRecord,
  type TicketKind,
} from "./ticket";
import {
  parseHostedServiceConfig,
  type HostedServiceConfig,
  type ServiceEnvironment,
  type ServicePolicy,
} from "./service_config";
import {
  TurnCredentialError,
  generateIceServers,
  type IceServerConfig,
  revokeTurnCredential,
  type TurnRequirement,
  turnUsernames,
} from "./turn";

export interface ControlPlaneEnvironment extends ServiceEnvironment {
  INSTALLATIONS: DurableObjectNamespace<Installation>;
  SERVICE_CONTROL: DurableObjectNamespace<ServiceControl>;
  TICKETS: DurableObjectNamespace;
}

type DenialCode =
  | "service_disabled"
  | "service_misconfigured"
  | "enrollment_limited"
  | "grant_invalid"
  | "grant_revoked"
  | "mint_limited"
  | "setup_limited"
  | "mint_in_progress"
  | "concurrency_limited"
  | "relay_allowance_exhausted"
  | "control_conflict"
  | "turn_unavailable";

export type ControlResult<T> =
  | { ok: true; value: T }
  | { ok: false; code: DenialCode; retryAt?: number };

interface EnrollmentRecord {
  installationId: string;
  fingerprint: string;
}

interface RelayPermit {
  units: number;
  source: string;
}

interface ServiceControlState {
  v: 1;
  mode: "running" | "halted";
  revision: number;
  reason: string | null;
  /** Written by older deployments as one ever-growing list; now ignored. */
  enrollments?: unknown;
  permits?: unknown;
}

type AttemptOutcome = "in_flight" | "issued" | "direct_only" | "failed" | "unknown";

interface CredentialAttempt {
  attemptId: string;
  startedAt: number;
  finishedAt: number | null;
  outcome: AttemptOutcome;
}

interface Reservation {
  requestId: string;
  reservationId: string;
  routingId: string;
  permitId: string;
  state: "preparing" | "live";
  /** Absent on reservations made before setup links existed: a call. */
  kind?: TicketKind;
  /** Relay units were charged for this call when its browser redeemed. */
  charged?: boolean;
  initializeBy: number;
  absoluteEnd: number;
  hostAttempts: CredentialAttempt[];
  browserAttempts: CredentialAttempt[];
  /** TURN usernames issued for this call, revoked when it is released. */
  turnUsernames?: string[];
}

type Grant =
  | { kind: "active"; secretHash: string; createdAt: number }
  | { kind: "revoked"; revokedAt: number; reason: string };

interface InstallationState {
  v: 1;
  grant: Grant;
  mintTimes: number[];
  setupMintTimes?: number[];
  reservations: Reservation[];
  /** Keyed network of the enrolling host; absent for older enrollments. */
  sourceFingerprint?: string;
  /** Relay units charged today (UTC day). */
  relayUsage?: { period: string; units: number };
}

export interface EnrollmentCommand {
  installationId: string;
  secretHash: string;
  requestId: string;
  sourceFingerprint: string;
  now: number;
}

export interface MintCommand {
  installationId: string;
  secret: string;
  requestId: string;
  origin: string;
  ttlSeconds: number;
  kind?: TicketKind;
  now: number;
}

export interface MintedTicket {
  token: string;
  routingId: string;
  joinUrl: string;
  agentSignalUrl: string;
  agentAccessKey: string;
  expiresAt: string;
}

export interface CallAdmission {
  browserIceServers: IceServerConfig[];
  hostIceServers: IceServerConfig[];
}

const STATE_KEY = "state";
// ServiceControl keeps each day's counters and idempotency records under
// "d:<UTC day>:" keys, one small value each, and an alarm drops old days.
const DAY_PREFIX = "d:";
const RETAINED_DAYS = 2;
const DAY_MS = 24 * 60 * 60 * 1000;
const MINUTE_MS = 60 * 1000;
const INITIALIZE_GRACE_MS = 20 * 1000;
const CALL_GRACE_MS = 60 * 1000;
const ATTEMPTS_PER_TICKET = 4;
// TURN credentials are minted when the browser redeems, so they only need to
// outlive the session that starts then.
const TURN_CREDENTIAL_TTL_SECONDS = SESSION_CAP_SECONDS + 60;
const MAX_ATTEMPTS_PER_PEER = 2;
// Setup links carry no media, so they don't use TURN or call slots. Their own
// small limits stop a leaked install credential from minting them in bulk.
const SETUP_MINTS_PER_HOUR = 6;
const SETUP_MINTS_PER_DAY = 20;
const SETUP_CONCURRENT = 2;
const HOUR_MS = 60 * MINUTE_MS;

export class ServiceControl extends DurableObject<ControlPlaneEnvironment> {
  async admitEnrollment(command: {
    requestId: string;
    installationId: string;
    fingerprint: string;
    now: number;
  }): Promise<ControlResult<{ admitted: true }>> {
    const config = configured(this.env);
    if (!config.ok) return config;
    if (config.value.emergencyDisabled) return denied("service_disabled");
    const result = await this.ctx.storage.transaction(async (storage) => {
      const repeated = await findRecent<EnrollmentRecord>(
        storage,
        command.now,
        `enroll:${command.requestId}`,
      );
      if (repeated) {
        return repeated.installationId === command.installationId &&
          repeated.fingerprint === command.fingerprint
          ? { ok: true as const, value: { admitted: true as const } }
          : denied("enrollment_limited");
      }
      const state = await loadControlState(storage, config.value);
      if (state.mode !== "running") return denied("service_disabled");
      const day = utcDay(command.now);
      const prefixKey = dayKey(day, `enrollments:${command.fingerprint}`);
      const globalKey = dayKey(day, "enrollments");
      const prefixCount = (await storage.get<number>(prefixKey)) ?? 0;
      const globalCount = (await storage.get<number>(globalKey)) ?? 0;
      if (
        prefixCount >= config.value.policy.enrollmentPerPrefixPerDay ||
        globalCount >= config.value.policy.enrollmentGlobalPerDay
      ) {
        return denied("enrollment_limited", nextUtcDay(command.now));
      }
      await storage.put({
        [dayKey(day, `enroll:${command.requestId}`)]: {
          installationId: command.installationId,
          fingerprint: command.fingerprint,
        } satisfies EnrollmentRecord,
        [prefixKey]: prefixCount + 1,
        [globalKey]: globalCount + 1,
      });
      return { ok: true as const, value: { admitted: true as const } };
    });
    await this.scheduleCleanup();
    return result;
  }

  /**
   * Charge relay units for one redeemed call against its source network's
   * daily budget and the global backstop. Idempotent per permit.
   */
  async reserveRelay(command: {
    permitId: string;
    units: number;
    source: string;
    now: number;
  }): Promise<ControlResult<{ period: string }>> {
    const config = configured(this.env);
    if (!config.ok) return config;
    if (!Number.isSafeInteger(command.units) || command.units < 1) {
      return denied("service_misconfigured");
    }
    if (config.value.emergencyDisabled) return denied("service_disabled");
    const result = await this.ctx.storage.transaction(async (storage) => {
      const state = await loadControlState(storage, config.value);
      if (state.mode !== "running") return denied("service_disabled");
      const period = utcDay(command.now);
      for (const day of recentDays(command.now)) {
        if (await storage.get<RelayPermit>(dayKey(day, `permit:${command.permitId}`))) {
          return { ok: true as const, value: { period: day } };
        }
      }
      const globalKey = dayKey(period, "relay");
      const sourceKey = dayKey(period, `relay:${command.source}`);
      const globalUsed = (await storage.get<number>(globalKey)) ?? 0;
      const sourceUsed = (await storage.get<number>(sourceKey)) ?? 0;
      if (
        globalUsed + command.units > config.value.policy.relayAttemptsPerDay ||
        sourceUsed + command.units > config.value.policy.relayAttemptsPerSourcePerDay
      ) {
        return denied("relay_allowance_exhausted", nextUtcDay(command.now));
      }
      await storage.put({
        [dayKey(period, `permit:${command.permitId}`)]: {
          units: command.units,
          source: command.source,
        } satisfies RelayPermit,
        [globalKey]: globalUsed + command.units,
        [sourceKey]: sourceUsed + command.units,
      });
      return { ok: true as const, value: { period } };
    });
    await this.scheduleCleanup();
    return result;
  }

  async status(): Promise<ControlResult<{
    mode: "running" | "halted";
    revision: number;
    reason: string | null;
    emergencyDisabled: boolean;
    relayAttemptsUsed: number;
    relayAttemptsLimit: number;
  }>> {
    const config = configured(this.env);
    if (!config.ok) return config;
    const state = await this.state(config.value);
    const used = await this.ctx.storage.get<number>(dayKey(utcDay(Date.now()), "relay"));
    return {
      ok: true,
      value: {
        mode: config.value.emergencyDisabled ? "halted" : state.mode,
        revision: state.revision,
        reason: state.reason,
        emergencyDisabled: config.value.emergencyDisabled,
        relayAttemptsUsed: used ?? 0,
        relayAttemptsLimit: config.value.policy.relayAttemptsPerDay,
      },
    };
  }

  /** Drop day records older than the retention window. */
  async alarm(): Promise<void> {
    const cutoff = recentDays(Date.now()).at(-1) ?? utcDay(Date.now());
    for (;;) {
      const batch = await this.ctx.storage.list({ prefix: DAY_PREFIX, limit: 512 });
      const stale = [...batch.keys()].filter((key) => key.slice(DAY_PREFIX.length, DAY_PREFIX.length + 10) < cutoff);
      if (stale.length) await this.ctx.storage.delete(stale);
      if (stale.length < batch.size || batch.size < 512) break;
    }
    const state = await this.ctx.storage.get<ServiceControlState>(STATE_KEY);
    if (state && (state.enrollments !== undefined || state.permits !== undefined)) {
      await this.ctx.storage.put(STATE_KEY, compactControlState(state));
    }
    await this.scheduleCleanup(true);
  }

  private async scheduleCleanup(force = false): Promise<void> {
    if (!force && (await this.ctx.storage.getAlarm()) !== null) return;
    await this.ctx.storage.setAlarm(nextUtcDay(Date.now()) + 5 * MINUTE_MS);
  }

  async change(command: {
    action: "halt" | "resume";
    expectedRevision: number;
    reason: string;
  }): Promise<ControlResult<{ mode: "running" | "halted"; revision: number }>> {
    const config = configured(this.env);
    if (!config.ok) return config;
    if (config.value.emergencyDisabled && command.action === "resume") {
      return denied("service_disabled");
    }
    return this.ctx.storage.transaction(async (storage) => {
      const state = await loadControlState(storage, config.value);
      if (state.revision !== command.expectedRevision) return denied("control_conflict");
      const next: ServiceControlState = {
        ...compactControlState(state),
        mode: command.action === "halt" ? "halted" : "running",
        revision: state.revision + 1,
        reason: command.reason,
      };
      await storage.put(STATE_KEY, next);
      return { ok: true, value: { mode: next.mode, revision: next.revision } };
    });
  }

  private async state(config: HostedServiceConfig): Promise<ServiceControlState> {
    const current = await this.ctx.storage.get<ServiceControlState>(STATE_KEY);
    if (current) return current;
    const initial = initialControlState(config);
    await this.ctx.storage.put(STATE_KEY, initial);
    return initial;
  }
}

export class Installation extends DurableObject<ControlPlaneEnvironment> {
  async enroll(command: EnrollmentCommand): Promise<ControlResult<{ enrolled: true }>> {
    const existing = await this.state();
    if (existing?.grant.kind === "revoked") return denied("grant_revoked");
    if (existing?.grant.kind === "active") {
      return constantTimeEqual(existing.grant.secretHash, command.secretHash)
        ? { ok: true, value: { enrolled: true as const } }
        : denied("grant_invalid");
    }
    const admission = await this.env.SERVICE_CONTROL.getByName("global").admitEnrollment({
      requestId: command.requestId,
      installationId: command.installationId,
      fingerprint: command.sourceFingerprint,
      now: command.now,
    });
    if (!admission.ok) return admission;
    return this.ctx.storage.transaction(async (storage) => {
      const current = await storage.get<InstallationState>(STATE_KEY);
      if (current?.grant.kind === "revoked") return denied("grant_revoked");
      if (current?.grant.kind === "active") {
        return constantTimeEqual(current.grant.secretHash, command.secretHash)
          ? { ok: true, value: { enrolled: true as const } }
          : denied("grant_invalid");
      }
      const state: InstallationState = {
        v: 1,
        grant: {
          kind: "active",
          secretHash: command.secretHash,
          createdAt: command.now,
        },
        mintTimes: [],
        reservations: [],
        sourceFingerprint: command.sourceFingerprint,
      };
      await storage.put(STATE_KEY, state);
      return { ok: true, value: { enrolled: true as const } };
    });
  }

  async mint(command: MintCommand): Promise<ControlResult<MintedTicket>> {
    const config = configured(this.env);
    if (!config.ok) return config;
    if (config.value.publicOrigin !== command.origin) return denied("service_misconfigured");
    // The grant is checked here, in this installation's own object, before the
    // one global control object is asked anything: junk mint requests never
    // reach it.
    const secretHash = await sha256(command.secret);
    const stored = await this.ctx.storage.get<InstallationState>(STATE_KEY);
    if (!stored || stored.grant.kind !== "active") {
      return denied(stored?.grant.kind === "revoked" ? "grant_revoked" : "grant_invalid");
    }
    if (!constantTimeEqual(stored.grant.secretHash, secretHash)) return denied("grant_invalid");
    const service = await this.env.SERVICE_CONTROL.getByName("global").status();
    if (!service.ok || service.value.mode !== "running") {
      return denied("service_disabled");
    }
    const reservationResult = await this.reserveMint(
      command,
      secretHash,
      config.value.policy,
    );
    if (!reservationResult.ok) return reservationResult;
    const reservation = reservationResult.value;
    const kind = command.kind ?? "call";
    // Nothing is charged and no TURN credential exists until a browser
    // redeems the link: a minted-and-abandoned link costs the service nothing.

    const token = randomToken();
    const agentKey = randomToken();
    const record = createTicketRecord({
      now: command.now,
      ttlSeconds: command.ttlSeconds,
      agentKey,
      ticketHash: await sha256(token),
      installationId: command.installationId,
      reservationId: reservation.reservationId,
      initializeBy: reservation.initializeBy,
      kind,
    });
    const room = this.env.TICKETS.getByName(reservation.routingId);
    let initialized: Response;
    try {
      initialized = await room.fetch(
        new Request("https://ticket.internal/initialize", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(record),
        }),
      );
    } catch {
      await this.scheduleReconciliation();
      return denied("mint_in_progress", reservation.initializeBy);
    }
    if (!initialized.ok) {
      await this.scheduleReconciliation();
      return denied("mint_in_progress", reservation.initializeBy);
    }
    await this.markLive(reservation.reservationId);
    const finalAdmission = await this.validateMint(
      reservation.reservationId,
      secretHash,
    );
    if (!finalAdmission.ok) {
      await room.fetch(new Request("https://ticket.internal/finish", { method: "POST" }));
      await this.release(reservation.reservationId);
      return finalAdmission;
    }

    const signalUrl = new URL(
      `/api/tickets/${reservation.routingId}/signal`,
      command.origin,
    );
    signalUrl.protocol = "wss:";
    return {
      ok: true,
      value: {
        token,
        routingId: reservation.routingId,
        joinUrl: `${command.origin}/#/${kind}/${reservation.routingId}/${token}`,
        agentSignalUrl: signalUrl.toString(),
        agentAccessKey: agentKey,
        expiresAt: new Date(record.expiresAt).toISOString(),
      },
    };
  }

  /**
   * A browser redeemed this call's link: charge its relay units, then issue
   * TURN credentials for both peers. The host receives its credentials over
   * its authenticated signaling socket, never in the mint response.
   */
  async admitCall(command: {
    installationId: string;
    reservationId: string;
    requirement: TurnRequirement;
    now: number;
  }): Promise<ControlResult<CallAdmission>> {
    const config = configured(this.env);
    if (!config.ok) return config;
    const status = await this.env.SERVICE_CONTROL.getByName("global").status();
    if (!status.ok || status.value.mode !== "running") return denied("service_disabled");
    const charge = await this.chargeRelay(command, config.value.policy);
    if (!charge.ok) return charge;
    const browser = await this.issueIce(command.reservationId, "browser", command.requirement);
    if (!browser.ok) return browser;
    const host = await this.issueIce(command.reservationId, "host", "preferred");
    if (!host.ok) return host;
    const current = await this.state();
    if (!current || current.grant.kind !== "active") return denied("grant_revoked");
    const stillReserved = current.reservations.some(
      (reservation) => reservation.reservationId === command.reservationId,
    );
    if (!stillReserved) return denied("grant_invalid");
    const after = await this.env.SERVICE_CONTROL.getByName("global").status();
    if (!after.ok || after.value.mode !== "running") return denied("service_disabled");
    return {
      ok: true,
      value: { browserIceServers: browser.value, hostIceServers: host.value },
    };
  }

  /** Admit the browser to a setup room: no TURN, but the same live checks. */
  async admitSetupBrowser(command: {
    reservationId: string;
  }): Promise<ControlResult<{ admitted: true }>> {
    const status = await this.env.SERVICE_CONTROL.getByName("global").status();
    if (!status.ok || status.value.mode !== "running") return denied("service_disabled");
    const current = await this.state();
    if (!current || current.grant.kind !== "active") return denied("grant_revoked");
    const reserved = current.reservations.some(
      (reservation) =>
        reservation.reservationId === command.reservationId && reservation.kind === "setup",
    );
    return reserved ? { ok: true, value: { admitted: true as const } } : denied("grant_invalid");
  }

  async release(reservationId: string): Promise<void> {
    const usernames = await this.ctx.storage.transaction(async (storage) => {
      const state = await storage.get<InstallationState>(STATE_KEY);
      if (!state) return [] as string[];
      const released = state.reservations.find((reservation) => reservation.reservationId === reservationId);
      const reservations = state.reservations.filter(
        (reservation) => reservation.reservationId !== reservationId,
      );
      if (reservations.length !== state.reservations.length) {
        await storage.put(STATE_KEY, { ...state, reservations });
      }
      return released?.turnUsernames ?? [];
    });
    // The call is over: its relay credentials stop working now, not when
    // their TTL runs out, so they can't carry traffic for anyone after it.
    await Promise.allSettled(usernames.map((username) => revokeTurnCredential(this.env, username)));
    await this.scheduleReconciliation();
  }

  async revoke(command: { reason: string; now: number }): Promise<{
    revoked: true;
    routingIds: string[];
  }> {
    const routingIds = await this.ctx.storage.transaction(async (storage) => {
      const state = await storage.get<InstallationState>(STATE_KEY);
      const ids = state?.reservations.map((reservation) => reservation.routingId) ?? [];
      const next: InstallationState = {
        v: 1,
        grant: { kind: "revoked", revokedAt: command.now, reason: command.reason },
        mintTimes: state?.mintTimes ?? [],
        reservations: state?.reservations ?? [],
      };
      await storage.put(STATE_KEY, next);
      return ids;
    });
    await Promise.allSettled(
      routingIds.map((routingId) =>
        this.env.TICKETS.getByName(routingId).fetch(
          new Request("https://ticket.internal/finish", { method: "POST" }),
        ),
      ),
    );
    return { revoked: true, routingIds };
  }

  async inspect(): Promise<{
    grant: "missing" | "active" | "revoked";
    mintCountDay: number;
    setupMintCountDay: number;
    relayUnitsDay: number;
    reservations: Array<Pick<Reservation, "reservationId" | "routingId" | "state" | "absoluteEnd" | "kind">>;
  }> {
    const state = await this.state();
    return {
      grant: state?.grant.kind ?? "missing",
      mintCountDay: state?.mintTimes.filter((value) => Date.now() - value < DAY_MS).length ?? 0,
      setupMintCountDay:
        state?.setupMintTimes?.filter((value) => Date.now() - value < DAY_MS).length ?? 0,
      relayUnitsDay:
        state?.relayUsage?.period === utcDay(Date.now()) ? state.relayUsage.units : 0,
      reservations:
        state?.reservations.map(({ reservationId, routingId, state, absoluteEnd, kind }) => ({
          reservationId,
          routingId,
          state,
          absoluteEnd,
          kind: kind ?? "call",
        })) ?? [],
    };
  }

  async alarm(): Promise<void> {
    const now = Date.now();
    const state = await this.state();
    if (!state) return;
    const retained: Reservation[] = [];
    let changed = false;
    for (const reservation of state.reservations) {
      if (now >= reservation.absoluteEnd) {
        changed = true;
        continue;
      }
      if (reservation.state === "live" || now < reservation.initializeBy) {
        retained.push(reservation);
        continue;
      }
      try {
        const response = await this.env.TICKETS.getByName(reservation.routingId).fetch(
          new Request("https://ticket.internal/status"),
        );
        if (response.ok) {
          retained.push({ ...reservation, state: "live" });
          changed = true;
        } else {
          changed = true;
        }
      } catch {
        retained.push(reservation);
      }
    }
    if (changed) {
      await this.ctx.storage.put(STATE_KEY, { ...state, reservations: retained });
    }
    await this.scheduleReconciliation();
  }

  private async reserveMint(
    command: MintCommand,
    secretHash: string,
    policy: ServicePolicy,
  ): Promise<ControlResult<Reservation>> {
    return this.ctx.storage.transaction(async (storage) => {
      const state = await storage.get<InstallationState>(STATE_KEY);
      if (!state || state.grant.kind !== "active") {
        return denied(state?.grant.kind === "revoked" ? "grant_revoked" : "grant_invalid");
      }
      if (!constantTimeEqual(state.grant.secretHash, secretHash)) return denied("grant_invalid");
      const mintTimes = state.mintTimes.filter((value) => command.now - value < DAY_MS);
      const reservations = state.reservations.filter(
        (reservation) => reservation.absoluteEnd > command.now,
      );
      const repeated = reservations.find(
        (reservation) => reservation.requestId === command.requestId,
      );
      if (repeated) return denied("mint_in_progress", repeated.initializeBy);
      const kind = command.kind ?? "call";
      const sameKind = reservations.filter((reservation) => (reservation.kind ?? "call") === kind);
      const setupMintTimes = (state.setupMintTimes ?? []).filter(
        (value) => command.now - value < DAY_MS,
      );
      if (kind === "setup") {
        const lastHour = setupMintTimes.filter((value) => command.now - value < HOUR_MS);
        if (lastHour.length >= SETUP_MINTS_PER_HOUR || setupMintTimes.length >= SETUP_MINTS_PER_DAY) {
          return denied(
            "setup_limited",
            lastHour.length >= SETUP_MINTS_PER_HOUR ? Math.min(...lastHour) + HOUR_MS : nextUtcDay(command.now),
          );
        }
      } else if (
        mintTimes.filter((value) => command.now - value < MINUTE_MS).length >=
          policy.mintsPerMinute ||
        mintTimes.length >= policy.mintsPerDay
      ) {
        return denied("mint_limited", command.now + MINUTE_MS);
      }
      if (sameKind.length >= (kind === "setup" ? SETUP_CONCURRENT : policy.concurrentCalls)) {
        return denied(
          "concurrency_limited",
          Math.min(...sameKind.map((reservation) => reservation.absoluteEnd)),
        );
      }
      const reservation: Reservation = {
        requestId: command.requestId,
        reservationId: randomToken(18),
        routingId: randomToken(18),
        permitId: randomToken(18),
        state: "preparing",
        kind,
        initializeBy: command.now + INITIALIZE_GRACE_MS,
        absoluteEnd:
          command.now +
          command.ttlSeconds * 1000 +
          (kind === "setup" ? SETUP_SESSION_CAP_SECONDS : SESSION_CAP_SECONDS) * 1000 +
          CALL_GRACE_MS,
        hostAttempts: [],
        browserAttempts: [],
      };
      await storage.put(STATE_KEY, {
        ...state,
        mintTimes: kind === "call" ? [...mintTimes, command.now] : mintTimes,
        setupMintTimes: kind === "setup" ? [...setupMintTimes, command.now] : setupMintTimes,
        reservations: [...reservations, reservation],
      });
      await storage.setAlarm(reservation.initializeBy);
      return { ok: true, value: reservation };
    });
  }

  private async chargeRelay(
    command: { installationId: string; reservationId: string; now: number },
    policy: ServicePolicy,
  ): Promise<ControlResult<{ charged: true }>> {
    const { installationId, reservationId, now } = command;
    const period = utcDay(now);
    // Take the installation's share first, so concurrent redemptions from
    // one installation cannot both pass its budget; give it back if the
    // source or global budget refuses.
    const local = await this.ctx.storage.transaction(async (storage) => {
      const state = await storage.get<InstallationState>(STATE_KEY);
      if (!state || state.grant.kind !== "active") {
        return denied(state?.grant.kind === "revoked" ? "grant_revoked" : "grant_invalid");
      }
      const index = state.reservations.findIndex(
        (reservation) => reservation.reservationId === reservationId,
      );
      if (index < 0) return denied("grant_invalid");
      const reservation = state.reservations[index];
      if (reservation.charged) {
        return {
          ok: true as const,
          value: { reservation, source: sourceOf(state, installationId), alreadyCharged: true },
        };
      }
      const used = state.relayUsage?.period === period ? state.relayUsage.units : 0;
      if (used + ATTEMPTS_PER_TICKET > policy.relayAttemptsPerInstallationPerDay) {
        return denied("relay_allowance_exhausted", nextUtcDay(now));
      }
      const reservations = [...state.reservations];
      reservations[index] = { ...reservation, charged: true };
      await storage.put(STATE_KEY, {
        ...state,
        reservations,
        relayUsage: { period, units: used + ATTEMPTS_PER_TICKET },
      });
      return {
        ok: true as const,
        value: { reservation, source: sourceOf(state, installationId), alreadyCharged: false },
      };
    });
    if (!local.ok) return local;
    const relay = await this.env.SERVICE_CONTROL.getByName("global").reserveRelay({
      permitId: local.value.reservation.permitId,
      units: ATTEMPTS_PER_TICKET,
      source: local.value.source,
      now,
    });
    if (relay.ok) return { ok: true, value: { charged: true as const } };
    if (!local.value.alreadyCharged) {
      await this.ctx.storage.transaction(async (storage) => {
        const state = await storage.get<InstallationState>(STATE_KEY);
        if (!state) return;
        const reservations = state.reservations.map((reservation) =>
          reservation.reservationId === reservationId
            ? { ...reservation, charged: false }
            : reservation,
        );
        const units = state.relayUsage?.period === period ? state.relayUsage.units : 0;
        await storage.put(STATE_KEY, {
          ...state,
          reservations,
          relayUsage: { period, units: Math.max(0, units - ATTEMPTS_PER_TICKET) },
        });
      });
    }
    return relay;
  }

  private async validateMint(
    reservationId: string,
    secretHash: string,
  ): Promise<ControlResult<{ valid: true }>> {
    const status = await this.env.SERVICE_CONTROL.getByName("global").status();
    if (!status.ok || status.value.mode !== "running") {
      return denied("service_disabled");
    }
    const state = await this.state();
    if (!state || state.grant.kind !== "active") {
      return denied(state?.grant.kind === "revoked" ? "grant_revoked" : "grant_invalid");
    }
    if (!constantTimeEqual(state.grant.secretHash, secretHash)) {
      return denied("grant_invalid");
    }
    return state.reservations.some(
      (reservation) => reservation.reservationId === reservationId,
    )
      ? { ok: true, value: { valid: true as const } }
      : denied("grant_invalid");
  }

  private async issueIce(
    reservationId: string,
    peer: "host" | "browser",
    requirement: TurnRequirement,
  ): Promise<ControlResult<IceServerConfig[]>> {
    const attempt = await this.startAttempt(reservationId, peer);
    if (!attempt.ok) return attempt;
    try {
      const iceServers = await generateIceServers(
        this.env,
        TURN_CREDENTIAL_TTL_SECONDS,
        fetch,
        requirement,
      );
      const outcome = iceServers.some((server) =>
        server.urls.some((url) => url.toLowerCase().startsWith("turn")),
      )
        ? "issued"
        : "direct_only";
      await this.finishAttempt(reservationId, peer, attempt.value, outcome, turnUsernames(iceServers));
      return { ok: true, value: iceServers };
    } catch (error) {
      await this.finishAttempt(reservationId, peer, attempt.value, "unknown");
      if (!(error instanceof TurnCredentialError)) console.error("TURN issuance failed");
      return denied("turn_unavailable");
    }
  }

  private async startAttempt(
    reservationId: string,
    peer: "host" | "browser",
  ): Promise<ControlResult<string>> {
    return this.ctx.storage.transaction(async (storage) => {
      const state = await storage.get<InstallationState>(STATE_KEY);
      if (!state || state.grant.kind !== "active") {
        return denied(state?.grant.kind === "revoked" ? "grant_revoked" : "grant_invalid");
      }
      const index = state.reservations.findIndex(
        (reservation) => reservation.reservationId === reservationId,
      );
      if (index < 0) return denied("grant_invalid");
      const reservation = state.reservations[index];
      const attempts = peer === "host" ? reservation.hostAttempts : reservation.browserAttempts;
      if (attempts.some((value) => value.outcome === "in_flight")) {
        return denied("mint_in_progress");
      }
      if (attempts.length >= MAX_ATTEMPTS_PER_PEER) return denied("turn_unavailable");
      const attemptId = randomToken(12);
      const attempt: CredentialAttempt = {
        attemptId,
        startedAt: Date.now(),
        finishedAt: null,
        outcome: "in_flight",
      };
      const updated = {
        ...reservation,
        ...(peer === "host"
          ? { hostAttempts: [...attempts, attempt] }
          : { browserAttempts: [...attempts, attempt] }),
      };
      const reservations = [...state.reservations];
      reservations[index] = updated;
      await storage.put(STATE_KEY, { ...state, reservations });
      return { ok: true, value: attemptId };
    });
  }

  private async finishAttempt(
    reservationId: string,
    peer: "host" | "browser",
    attemptId: string,
    outcome: Exclude<AttemptOutcome, "in_flight">,
    usernames: string[] = [],
  ): Promise<void> {
    await this.ctx.storage.transaction(async (storage) => {
      const state = await storage.get<InstallationState>(STATE_KEY);
      if (!state) return;
      const index = state.reservations.findIndex(
        (reservation) => reservation.reservationId === reservationId,
      );
      if (index < 0) return;
      const reservation = state.reservations[index];
      const attempts = peer === "host" ? reservation.hostAttempts : reservation.browserAttempts;
      const updatedAttempts = attempts.map((attempt) =>
        attempt.attemptId === attemptId && attempt.outcome === "in_flight"
          ? { ...attempt, outcome, finishedAt: Date.now() }
          : attempt,
      );
      const reservations = [...state.reservations];
      reservations[index] = {
        ...reservation,
        ...(usernames.length ? { turnUsernames: [...new Set([...(reservation.turnUsernames ?? []), ...usernames])] } : {}),
        ...(peer === "host"
          ? { hostAttempts: updatedAttempts }
          : { browserAttempts: updatedAttempts }),
      };
      await storage.put(STATE_KEY, { ...state, reservations });
    });
  }

  private async markLive(reservationId: string): Promise<void> {
    await this.ctx.storage.transaction(async (storage) => {
      const state = await storage.get<InstallationState>(STATE_KEY);
      if (!state) return;
      const reservations = state.reservations.map((reservation) =>
        reservation.reservationId === reservationId
          ? { ...reservation, state: "live" as const }
          : reservation,
      );
      await storage.put(STATE_KEY, { ...state, reservations });
    });
    await this.scheduleReconciliation();
  }

  private async state(): Promise<InstallationState | undefined> {
    return this.ctx.storage.get<InstallationState>(STATE_KEY);
  }

  private async scheduleReconciliation(): Promise<void> {
    const state = await this.state();
    if (!state?.reservations.length) {
      await this.ctx.storage.deleteAlarm();
      return;
    }
    await this.ctx.storage.setAlarm(
      Math.min(
        ...state.reservations.map((reservation) =>
          reservation.state === "preparing"
            ? reservation.initializeBy
            : reservation.absoluteEnd,
        ),
      ),
    );
  }
}

function configured(env: ServiceEnvironment): ControlResult<HostedServiceConfig> {
  const result = parseHostedServiceConfig(env);
  return result.ok ? { ok: true, value: result.config } : denied("service_misconfigured");
}

function denied(code: DenialCode, retryAt?: number): ControlResult<never> {
  return { ok: false, code, ...(retryAt === undefined ? {} : { retryAt }) };
}

function initialControlState(config: HostedServiceConfig): ServiceControlState {
  return {
    v: 1,
    mode: config.environment === "production" ? "halted" : config.initialMode,
    revision: 1,
    reason: "initial policy",
  };
}

function compactControlState(state: ServiceControlState): ServiceControlState {
  return { v: 1, mode: state.mode, revision: state.revision, reason: state.reason };
}

function sourceOf(state: InstallationState, installationId: string): string {
  // Installations enrolled before sources were recorded are their own source.
  return state.sourceFingerprint ?? `installation:${installationId}`;
}

function dayKey(day: string, name: string): string {
  return `${DAY_PREFIX}${day}:${name}`;
}

/** Today and the retained days before it, newest first. */
function recentDays(now: number): string[] {
  return Array.from({ length: RETAINED_DAYS }, (_, index) => utcDay(now - index * DAY_MS));
}

async function findRecent<T>(
  storage: DurableObjectTransaction,
  now: number,
  name: string,
): Promise<T | undefined> {
  for (const day of recentDays(now)) {
    const value = await storage.get<T>(dayKey(day, name));
    if (value !== undefined) return value;
  }
  return undefined;
}

async function loadControlState(
  storage: DurableObjectTransaction,
  config: HostedServiceConfig,
): Promise<ServiceControlState> {
  return (await storage.get<ServiceControlState>(STATE_KEY)) ?? initialControlState(config);
}

function utcDay(now: number): string {
  return new Date(now).toISOString().slice(0, 10);
}

function nextUtcDay(now: number): number {
  const date = new Date(now);
  return Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate() + 1);
}

function randomToken(byteLength = 32): string {
  const bytes = crypto.getRandomValues(new Uint8Array(byteLength));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

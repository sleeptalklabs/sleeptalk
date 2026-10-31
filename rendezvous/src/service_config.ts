import type { TurnEnvironment } from "./turn";

export type DeploymentEnvironment = "staging" | "production";
export type ServiceMode = "running" | "halted";

export interface ServicePolicy {
  enrollmentPerPrefixPerDay: number;
  enrollmentGlobalPerDay: number;
  mintsPerMinute: number;
  mintsPerDay: number;
  concurrentCalls: number;
  relayAttemptsPerDay: number;
  relayAttemptsPerInstallationPerDay: number;
  relayAttemptsPerSourcePerDay: number;
}

export interface HostedServiceConfig extends TurnEnvironment {
  environment: DeploymentEnvironment;
  publicOrigin: string;
  enrollmentHashKey: string;
  adminToken: string;
  emergencyDisabled: boolean;
  initialMode: ServiceMode;
  allowStunOnly: boolean;
  policy: ServicePolicy;
}

export interface ServiceEnvironment extends TurnEnvironment {
  DEPLOYMENT_ENV?: string;
  PUBLIC_ORIGIN?: string;
  ENROLLMENT_HASH_KEY?: string;
  ADMIN_TOKEN?: string;
  EMERGENCY_DISABLE?: string;
  INITIAL_SERVICE_MODE?: string;
  ALLOW_STUN_ONLY?: string;
  ENROLLMENT_PER_PREFIX_PER_DAY?: string;
  ENROLLMENT_GLOBAL_PER_DAY?: string;
  MINTS_PER_MINUTE?: string;
  MINTS_PER_DAY?: string;
  CONCURRENT_CALLS?: string;
  RELAY_ATTEMPTS_PER_DAY?: string;
  RELAY_ATTEMPTS_PER_INSTALLATION_PER_DAY?: string;
  RELAY_ATTEMPTS_PER_SOURCE_PER_DAY?: string;
}

export type ConfigResult =
  | { ok: true; config: HostedServiceConfig }
  | { ok: false; missing: string[]; invalid: string[] };

export function parseHostedServiceConfig(env: ServiceEnvironment): ConfigResult {
  const missing: string[] = [];
  const invalid: string[] = [];
  const environment = env.DEPLOYMENT_ENV?.trim();
  if (!environment) missing.push("DEPLOYMENT_ENV");
  else if (environment !== "staging" && environment !== "production") {
    invalid.push("DEPLOYMENT_ENV");
  }

  const publicOrigin = parseHttpsOrigin(env.PUBLIC_ORIGIN);
  if (!env.PUBLIC_ORIGIN?.trim()) missing.push("PUBLIC_ORIGIN");
  else if (!publicOrigin) invalid.push("PUBLIC_ORIGIN");

  const enrollmentHashKey = env.ENROLLMENT_HASH_KEY?.trim() ?? "";
  if (!enrollmentHashKey) missing.push("ENROLLMENT_HASH_KEY");
  const adminToken = env.ADMIN_TOKEN?.trim() ?? "";
  if (!adminToken) missing.push("ADMIN_TOKEN");

  const initialModeValue = env.INITIAL_SERVICE_MODE?.trim();
  if (!initialModeValue) missing.push("INITIAL_SERVICE_MODE");
  else if (initialModeValue !== "running" && initialModeValue !== "halted") {
    invalid.push("INITIAL_SERVICE_MODE");
  }

  const allowStunOnly = env.ALLOW_STUN_ONLY === "true";
  const turnId = env.TURN_KEY_ID?.trim() ?? "";
  const turnToken = env.TURN_KEY_API_TOKEN?.trim() ?? "";
  if (Boolean(turnId) !== Boolean(turnToken)) invalid.push("TURN_CONFIGURATION");
  if (environment === "production" && (!turnId || !turnToken)) {
    missing.push("TURN_KEY_ID", "TURN_KEY_API_TOKEN");
  }
  if (environment === "staging" && !allowStunOnly && (!turnId || !turnToken)) {
    missing.push("TURN_KEY_ID", "TURN_KEY_API_TOKEN");
  }

  const policyEntries = {
    enrollmentPerPrefixPerDay: parsePositiveInt(
      "ENROLLMENT_PER_PREFIX_PER_DAY",
      env.ENROLLMENT_PER_PREFIX_PER_DAY,
      missing,
      invalid,
    ),
    enrollmentGlobalPerDay: parsePositiveInt(
      "ENROLLMENT_GLOBAL_PER_DAY",
      env.ENROLLMENT_GLOBAL_PER_DAY,
      missing,
      invalid,
    ),
    mintsPerMinute: parsePositiveInt(
      "MINTS_PER_MINUTE",
      env.MINTS_PER_MINUTE,
      missing,
      invalid,
    ),
    mintsPerDay: parsePositiveInt(
      "MINTS_PER_DAY",
      env.MINTS_PER_DAY,
      missing,
      invalid,
    ),
    concurrentCalls: parsePositiveInt(
      "CONCURRENT_CALLS",
      env.CONCURRENT_CALLS,
      missing,
      invalid,
    ),
    relayAttemptsPerDay: parsePositiveInt(
      "RELAY_ATTEMPTS_PER_DAY",
      env.RELAY_ATTEMPTS_PER_DAY,
      missing,
      invalid,
    ),
    relayAttemptsPerInstallationPerDay: parsePositiveInt(
      "RELAY_ATTEMPTS_PER_INSTALLATION_PER_DAY",
      env.RELAY_ATTEMPTS_PER_INSTALLATION_PER_DAY,
      missing,
      invalid,
    ),
    relayAttemptsPerSourcePerDay: parsePositiveInt(
      "RELAY_ATTEMPTS_PER_SOURCE_PER_DAY",
      env.RELAY_ATTEMPTS_PER_SOURCE_PER_DAY,
      missing,
      invalid,
    ),
  };

  const validEnvironment =
    environment === "staging" || environment === "production" ? environment : null;
  const validInitialMode =
    initialModeValue === "running" || initialModeValue === "halted"
      ? initialModeValue
      : null;
  if (
    missing.length ||
    invalid.length ||
    !validEnvironment ||
    !publicOrigin ||
    !validInitialMode ||
    policyEntries.enrollmentPerPrefixPerDay === undefined ||
    policyEntries.enrollmentGlobalPerDay === undefined ||
    policyEntries.mintsPerMinute === undefined ||
    policyEntries.mintsPerDay === undefined ||
    policyEntries.concurrentCalls === undefined ||
    policyEntries.relayAttemptsPerDay === undefined ||
    policyEntries.relayAttemptsPerInstallationPerDay === undefined ||
    policyEntries.relayAttemptsPerSourcePerDay === undefined
  ) {
    return {
      ok: false,
      missing: [...new Set(missing)],
      invalid: [...new Set(invalid)],
    };
  }

  return {
    ok: true,
    config: {
      environment: validEnvironment,
      publicOrigin,
      enrollmentHashKey,
      adminToken,
      emergencyDisabled: env.EMERGENCY_DISABLE === "true",
      initialMode: validInitialMode,
      allowStunOnly,
      policy: {
        enrollmentPerPrefixPerDay: policyEntries.enrollmentPerPrefixPerDay,
        enrollmentGlobalPerDay: policyEntries.enrollmentGlobalPerDay,
        mintsPerMinute: policyEntries.mintsPerMinute,
        mintsPerDay: policyEntries.mintsPerDay,
        concurrentCalls: policyEntries.concurrentCalls,
        relayAttemptsPerDay: policyEntries.relayAttemptsPerDay,
        relayAttemptsPerInstallationPerDay: policyEntries.relayAttemptsPerInstallationPerDay,
        relayAttemptsPerSourcePerDay: policyEntries.relayAttemptsPerSourcePerDay,
      },
      ...(turnId ? { TURN_KEY_ID: turnId, TURN_KEY_API_TOKEN: turnToken } : {}),
    },
  };
}

function parsePositiveInt(
  name: string,
  raw: string | undefined,
  missing: string[],
  invalid: string[],
): number | undefined {
  const value = raw?.trim();
  if (!value) {
    missing.push(name);
    return undefined;
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    invalid.push(name);
    return undefined;
  }
  return parsed;
}

function parseHttpsOrigin(raw: string | undefined): string | null {
  if (!raw?.trim()) return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) {
      return null;
    }
    if (url.pathname !== "/") return null;
    return url.origin;
  } catch {
    return null;
  }
}

export type CallRoute =
  | { kind: "dev"; roomId: string }
  | { kind: "ticket"; routingId: string; token: string };

const TICKET = "[A-Za-z0-9_-]{32,128}";
const ROUTING_ID = "[A-Za-z0-9_-]{20,64}";

export function parseCallRoute(hash: string): CallRoute | null {
  const dev = hash.match(/^#\/dev\/([A-Za-z0-9_-]{8,64})$/);
  if (dev) return { kind: "dev", roomId: dev[1] };
  const ticket = hash.match(new RegExp(`^#\\/call\\/(${ROUTING_ID})\\/(${TICKET})$`));
  return ticket ? { kind: "ticket", routingId: ticket[1], token: ticket[2] } : null;
}

export interface SetupRoute {
  routingId: string;
  token: string;
  hostKey: string;
}

// The host's 65-byte P-256 public key is 87 base64url characters.
const HOST_KEY = "[A-Za-z0-9_-]{87}";

/** The settings page on a phone: #/settings/<routing id>/<ticket>/<host key>. */
export function parseSettingsRoute(hash: string): SetupRoute | null {
  const match = hash.match(new RegExp(`^#\\/settings\\/(${ROUTING_ID})\\/(${TICKET})\\/(${HOST_KEY})$`));
  return match ? { routingId: match[1], token: match[2], hostKey: match[3] } : null;
}

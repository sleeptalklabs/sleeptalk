import type { Installation, ServiceControl } from "../src/control_plane";
import type { TicketRoom } from "../src/worker";

declare global {
  namespace Cloudflare {
    interface Env {
      INSTALLATIONS: DurableObjectNamespace<Installation>;
      SERVICE_CONTROL: DurableObjectNamespace<ServiceControl>;
      TICKETS: DurableObjectNamespace<TicketRoom>;
    }
  }
}

export {};

export type BrowserContext = "embedded" | "browser";

export interface BrowserRuntimePolicy {
  context: BrowserContext;
  iceTransportPolicy: "all" | "relay";
}

/** Derive connectivity from the browser that actually redeems the link. */
export function browserRuntimePolicy(userAgent: string): BrowserRuntimePolicy {
  const ios = /iPhone|iPad|iPod/i.test(userAgent);
  const android = /Android/i.test(userAgent);
  const chatAppHint =
    /Telegram|MicroMessenger|WhatsApp|Slack|FBAN|FBAV|Instagram|Line\/|KAKAOTALK/i.test(
      userAgent,
    );
  const iosWebview =
    ios &&
    /AppleWebKit/i.test(userAgent) &&
    !/Safari|CriOS|FxiOS|EdgiOS/i.test(userAgent);
  const androidWebview = android && /;\s*wv\)|\bVersion\/4\.0\b/i.test(userAgent);

  if (chatAppHint || iosWebview || androidWebview) {
    return { context: "embedded", iceTransportPolicy: "relay" };
  }
  return { context: "browser", iceTransportPolicy: "all" };
}

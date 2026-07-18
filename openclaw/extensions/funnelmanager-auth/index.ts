/**
 * Funnel Manager auth bridge for OpenClaw.
 *
 * Associates channel device ids (e.g. Telegram sender ids) with Funnel
 * Manager profiles stored in the auth service, and makes sure every
 * funnelmanager MCP tool call carries the session token of the profile the
 * agent is acting for:
 *
 * - `before_tool_call`: resolves the current sender's channel identity, asks
 *   the auth service's internal endpoint for a session token, and injects it
 *   as the `session_token` tool argument. Unlinked senders are blocked (the
 *   auth service records a pending channel request an admin can assign from
 *   the Funnel Manager hub).
 * - `funnelmanager_session_token` dynamic tool: same lookup, exposed to the
 *   agent for harnesses whose native tool path cannot rewrite arguments
 *   (e.g. Codex-native MCP projection) — the agent passes the returned value
 *   as `session_token` explicitly.
 * - `channel_pairing_requested`: reports new unpaired DM senders as pending
 *   channel requests so admins see them before any tool call happens.
 * - `message_received`: additionally reports the sender's identity (throttled
 *   per identity) so channels that were paired with OpenClaw before this
 *   plugin existed — and therefore never fire a pairing event — still show up
 *   as pending channel requests without waiting for a tool call.
 *
 * The token is minted by the auth service for the linked profile; the MCP
 * server forwards it to the search/leads backends, which enforce the OPA
 * authorization policy per request.
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

// Plain JSON Schema (structurally what TypeBox produces) — the extension dir
// cannot resolve the host's `typebox` package, and the tool takes no inputs.
const EMPTY_OBJECT_SCHEMA = {
  type: "object",
  properties: {},
  additionalProperties: false,
} as const;

const DEFAULT_AUTH_BACKEND_URL = "http://auth-backend:8002";

// Tool names served by the funnelmanager MCP server. Projections may prefix
// them (server name, mcp__ namespace, ...), so matching also accepts known
// suffixes and anything containing "funnelmanager".
const FM_TOOL_NAMES = [
  "get_leads",
  "recent_leads",
  "leads_stats",
  "similarity_search",
];

type ChannelIdentity = {
  channel: string;
  deviceId: string;
  displayName?: string;
};

type ProfileSession = {
  token: string;
  username: string;
  role: string;
};

type SessionResult =
  | { status: "ok"; token: ProfileSession }
  | { status: "pending"; message: string }
  | { status: "error"; message: string };

function isFunnelmanagerTool(name: unknown): boolean {
  if (typeof name !== "string" || !name) return false;
  const lowered = name.toLowerCase();
  if (lowered.includes("funnelmanager")) return true;
  for (const tool of FM_TOOL_NAMES) {
    if (lowered === tool) return true;
    if (
      lowered.endsWith(`_${tool}`) ||
      lowered.endsWith(`.${tool}`) ||
      lowered.endsWith(`:${tool}`) ||
      lowered.endsWith(`/${tool}`)
    ) {
      return true;
    }
  }
  return false;
}

/** Best-effort identity from a session key like `agent:main:telegram:dm:12345`. */
function identityFromSessionKey(sessionKey: unknown): ChannelIdentity | null {
  if (typeof sessionKey !== "string" || !sessionKey) return null;
  const parts = sessionKey.split(":").filter(Boolean);
  const knownChannels = [
    "telegram",
    "discord",
    "slack",
    "whatsapp",
    "imessage",
    "matrix",
    "signal",
    "feishu",
  ];
  for (let i = 0; i < parts.length; i += 1) {
    const channel = parts[i].toLowerCase();
    if (!knownChannels.includes(channel)) continue;
    // The trailing segment is the most specific identifier (sender for DMs).
    const tail = parts[parts.length - 1];
    if (tail && tail.toLowerCase() !== channel) {
      return { channel, deviceId: tail };
    }
  }
  return null;
}

export default definePluginEntry({
  id: "funnelmanager-auth",
  name: "Funnel Manager Auth",
  description:
    "Fetches per-profile Funnel Manager session tokens for channel senders and attaches them to funnelmanager MCP tool calls.",
  register(api: any) {
    const config = (api?.pluginConfig ?? {}) as Record<string, unknown>;
    const authBackendUrl = String(
      config.authBackendUrl || DEFAULT_AUTH_BACKEND_URL,
    ).replace(/\/+$/, "");
    const log = api?.logger ?? console;

    // sessionKey -> identity, learned from inbound message hooks (most
    // reliable source: ctx.channel + ctx.senderId).
    const identityBySessionKey = new Map<string, ChannelIdentity>();

    function rememberIdentity(ctx: any): ChannelIdentity | null {
      const identity = identityFromContext(ctx);
      if (identity && ctx?.sessionKey) {
        identityBySessionKey.set(String(ctx.sessionKey), identity);
      }
      return identity;
    }

    function identityFromContext(ctx: any): ChannelIdentity | null {
      if (!ctx) return null;
      const channel = ctx.channel ?? ctx.messageProvider;
      const senderId = ctx.senderId ?? ctx.channelContext?.sender?.id;
      if (channel && senderId) {
        return { channel: String(channel).toLowerCase(), deviceId: String(senderId) };
      }
      return null;
    }

    function resolveIdentity(ctx: any): ChannelIdentity | null {
      const direct = identityFromContext(ctx);
      if (direct) return direct;
      const sessionKey = ctx?.sessionKey ? String(ctx.sessionKey) : "";
      if (sessionKey && identityBySessionKey.has(sessionKey)) {
        return identityBySessionKey.get(sessionKey) ?? null;
      }
      return identityFromSessionKey(sessionKey);
    }

    // No client-side token cache: the auth backend already reuses one session
    // per channel identity (its Redis openclaw_token: cache) and revokes it on
    // unlink/reassign, so fetching per tool call is cheap and never serves a
    // stale/revoked token.
    async function fetchSession(identity: ChannelIdentity): Promise<SessionResult> {
      let response: Response;
      try {
        response = await fetch(`${authBackendUrl}/internal/openclaw/session`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            channel: identity.channel,
            device_id: identity.deviceId,
            display_name: identity.displayName ?? "",
          }),
        });
      } catch (error) {
        return {
          status: "error",
          message: `Funnel Manager auth service unreachable: ${String(error)}`,
        };
      }
      if (response.status === 403) {
        return {
          status: "pending",
          message:
            `This ${identity.channel} chat (device id ${identity.deviceId}) is not ` +
            "linked to a Funnel Manager profile. A pending channel request was " +
            "recorded — an admin must assign it to a user in the Funnel Manager " +
            "hub before Funnel Manager tools can be used here.",
        };
      }
      if (!response.ok) {
        return {
          status: "error",
          message: `Funnel Manager auth service error (${response.status})`,
        };
      }
      let payload: any = null;
      try {
        payload = await response.json();
      } catch {
        return { status: "error", message: "Funnel Manager auth returned invalid JSON" };
      }
      const token = String(payload?.access_token ?? "");
      if (!token) {
        return { status: "error", message: "Funnel Manager auth returned no token" };
      }
      return {
        status: "ok",
        token: {
          token,
          username: String(payload?.username ?? ""),
          role: String(payload?.role ?? ""),
        },
      };
    }

    // identityKey -> last time it was reported to the auth service. Throttles
    // the per-message report below to one call per identity per interval.
    const reportedAt = new Map<string, number>();
    const REPORT_INTERVAL_MS = 60 * 60 * 1000;

    function reportIdentity(identity: ChannelIdentity): void {
      const key = `${identity.channel}|${identity.deviceId}`;
      const now = Date.now();
      if (now - (reportedAt.get(key) ?? 0) < REPORT_INTERVAL_MS) return;
      reportedAt.set(key, now);
      // Fire-and-forget: for unlinked senders the auth service upserts a
      // pending channel request; for linked ones it just reuses the cached
      // session. fetchSession reports failures in its result, never throws.
      void fetchSession(identity);
    }

    // Learn channel identities from inbound traffic, and surface unlinked
    // senders as pending channel requests even if they never call a
    // funnelmanager tool (e.g. chats paired with OpenClaw before this plugin
    // was installed).
    api.on("message_received", async (_event: any, ctx: any) => {
      try {
        const identity = rememberIdentity(ctx);
        if (identity) reportIdentity(identity);
      } catch {
        /* observation only */
      }
    });

    // Surface unpaired DM senders as pending channel requests immediately.
    api.on("channel_pairing_requested", async (event: any) => {
      try {
        const channel = String(event?.channel ?? "").toLowerCase();
        const senderId = String(event?.senderId ?? "");
        if (!channel || !senderId) return;
        await fetch(`${authBackendUrl}/internal/openclaw/session`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel, device_id: senderId, display_name: "" }),
        });
      } catch (error) {
        log.warn?.(`funnelmanager-auth: pairing report failed: ${String(error)}`);
      }
    });

    // Inject the sender's session token into funnelmanager MCP tool calls.
    api.on(
      "before_tool_call",
      async (event: any, ctx: any) => {
        if (!isFunnelmanagerTool(event?.toolName)) return;
        const identity = resolveIdentity(ctx);
        if (!identity) {
          // System-originated run (heartbeat/cron) or unknown route: leave the
          // call untouched — the MCP server applies its own fallback policy.
          return;
        }
        const result = await fetchSession(identity);
        if (result.status === "ok") {
          return {
            params: { ...(event?.params ?? {}), session_token: result.token.token },
          };
        }
        return { block: true, blockReason: result.message };
      },
      { priority: 100 },
    );

    // Explicit token fetch for harnesses whose native MCP path cannot rewrite
    // tool arguments: the agent calls this and passes session_token itself.
    api.registerTool((toolCtx: any) => ({
      name: "funnelmanager_session_token",
      description:
        "Get the Funnel Manager session token for the person this conversation " +
        "belongs to. Pass the returned session_token value as the session_token " +
        "argument of every funnelmanager MCP tool call. Tokens expire — fetch a " +
        "fresh one if a tool reports an expired/invalid token. Never invent or " +
        "reuse tokens across conversations.",
      parameters: EMPTY_OBJECT_SCHEMA,
      async execute() {
        const identity =
          identityFromContext(toolCtx) ??
          (toolCtx?.sessionKey
            ? identityBySessionKey.get(String(toolCtx.sessionKey)) ??
              identityFromSessionKey(String(toolCtx.sessionKey))
            : null);
        if (!identity) {
          return {
            content: [
              {
                type: "text",
                text:
                  "No channel identity for this session (system-originated run?). " +
                  "Funnel Manager tools require a linked channel sender.",
              },
            ],
            isError: true,
          };
        }
        const result = await fetchSession(identity);
        if (result.status !== "ok") {
          return {
            content: [{ type: "text", text: result.message }],
            isError: true,
          };
        }
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                session_token: result.token.token,
                username: result.token.username,
                role: result.token.role,
                channel: identity.channel,
                device_id: identity.deviceId,
              }),
            },
          ],
        };
      },
    }));
  },
});

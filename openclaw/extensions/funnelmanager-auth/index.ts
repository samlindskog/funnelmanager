/**
 * Funnel Manager auth bridge for OpenClaw.
 *
 * Associates channel device ids (e.g. Telegram sender ids) with Funnel
 * Manager profiles stored in the auth service, and makes sure every
 * funnelmanager MCP tool call carries the session token of the profile the
 * agent is acting for:
 *
 * - `before_tool_call`: resolves the current sender's channel identity and
 *   enforces that every funnelmanager MCP tool call carries that sender's
 *   current session token as the `session_token` argument. It never rewrites
 *   params — Codex-native MCP runs hooks on a report-only PreToolUse relay
 *   that denies any rewrite — so tokenless or mismatched-token calls are
 *   blocked with instructions to fetch the token via
 *   `funnelmanager_session_token` and pass it explicitly. Unlinked senders
 *   are blocked (the auth service records a pending channel request an admin
 *   can assign from the Funnel Manager hub).
 * - `funnelmanager_session_token` dynamic tool: same lookup, exposed to the
 *   agent — the required first step of every funnelmanager flow; the agent
 *   passes the returned value as `session_token` explicitly on each call.
 * - `channel_pairing_requested`: reports new unpaired DM senders — including
 *   the pairing code — as pending channel requests, so the whole onboarding
 *   (approve pairing + assign profile) can happen in the hub UI.
 * - `message_received`: additionally reports the sender's identity (throttled
 *   per identity) so channels that were paired with OpenClaw before this
 *   plugin existed — and therefore never fire a pairing event — still show up
 *   as pending channel requests without waiting for a tool call.
 * - HTTP route `POST /api/funnelmanager/pairing/approve` (gateway-token auth):
 *   called by the auth service when an admin approves a pairing in the hub;
 *   applies the same approval as `openclaw pairing approve <code>`.
 * - HTTP route `POST /api/funnelmanager/agents/sync` (gateway-token auth):
 *   called by the auth service whenever a user's channel links change
 *   (assign/unlink/delete). Full-sync: ensures a per-user agent
 *   (`user-<username>`, workspace under `~/.openclaw/workspaces/`) exists in
 *   openclaw.json and that the user's per-sender DM peer bindings exactly
 *   match the given sender list; senders in `removed` additionally get their
 *   DM pairing revoked (offboarding). Config writes hot-apply via the file
 *   watcher.
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

const DEFAULT_AUTH_BACKEND_URL = "http://auth:8002";

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
  // Never match the plugin's own escape-hatch tool: it exists so the agent can
  // fetch a token when the harness can't inject one, so gating it on a token
  // would deadlock that recovery path (Codex-native relay denies any hook
  // params rewrite, including on this tool).
  if (lowered.includes("session_token")) return false;
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

/**
 * Deterministic OpenClaw agent id for a funnelmanager username. Usernames may
 * contain dots but agent ids may not (`^[a-z0-9][a-z0-9_-]{0,63}$`), so every
 * out-of-alphabet char is replaced with "-" and a short djb2 hash of the raw
 * username is appended. The hash is added not only when sanitizing changed the
 * name but also when the raw name already ends in a 6-hex tail — otherwise
 * "a.b" (→ "user-a-b-<hash>") could be impersonated by registering the literal
 * username "a-b-<hash>". The "user-" prefix guarantees no clash with the
 * built-in "main" agent.
 */
function agentIdForUsername(username: string): string {
  const sanitized = username.replace(/[^a-z0-9_-]/g, "-");
  let id = `user-${sanitized}`;
  if (sanitized !== username || /-[0-9a-f]{6}$/.test(username)) {
    let hash = 5381;
    for (let i = 0; i < username.length; i += 1) {
      hash = ((hash * 33) ^ username.charCodeAt(i)) >>> 0;
    }
    id += `-${hash.toString(16).padStart(6, "0").slice(-6)}`;
  }
  return id;
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

    // Surface unpaired DM senders as pending channel requests immediately,
    // carrying the pairing code so an admin can approve the pairing from the
    // hub (no CLI needed).
    api.on("channel_pairing_requested", async (event: any) => {
      try {
        const channel = String(event?.channel ?? "").toLowerCase();
        const senderId = String(event?.senderId ?? "");
        if (!channel || !senderId) return;
        const code = String(event?.code ?? "").trim();
        const meta = event?.metadata ?? {};
        const displayName = String(
          meta?.displayName ?? meta?.name ?? meta?.username ?? "",
        );
        if (code) {
          await fetch(`${authBackendUrl}/internal/openclaw/pairing-request`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              channel,
              device_id: senderId,
              code,
              display_name: displayName,
            }),
          });
          return;
        }
        // No code on the event — fall back to recording a plain channel request.
        await fetch(`${authBackendUrl}/internal/openclaw/session`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel, device_id: senderId, display_name: displayName }),
        });
      } catch (error) {
        log.warn?.(`funnelmanager-auth: pairing report failed: ${String(error)}`);
      }
    });

    // Hub-driven pairing approval: the auth service calls this route (gateway
    // token auth) when an admin approves a pairing request in the hub. It
    // performs the same approval as `openclaw pairing approve <code>`.
    api.registerHttpRoute({
      path: "/api/funnelmanager/pairing/approve",
      auth: "gateway",
      match: "exact",
      handler: async (req: any, res: any) => {
        const respond = (status: number, payload: Record<string, unknown>) => {
          res.statusCode = status;
          res.setHeader("Content-Type", "application/json; charset=utf-8");
          res.end(JSON.stringify(payload));
        };
        if (String(req?.method ?? "").toUpperCase() !== "POST") {
          respond(405, { approved: false, reason: "POST only" });
          return;
        }
        let body: any = null;
        try {
          const chunks: Buffer[] = [];
          let size = 0;
          for await (const chunk of req) {
            size += chunk.length;
            if (size > 64 * 1024) throw new Error("body too large");
            chunks.push(chunk);
          }
          body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
        } catch (error) {
          respond(400, { approved: false, reason: `invalid body: ${String(error)}` });
          return;
        }
        const channel = String(body?.channel ?? "").toLowerCase().trim();
        const code = String(body?.code ?? "").trim();
        if (!channel || !code) {
          respond(422, { approved: false, reason: "channel and code are required" });
          return;
        }
        try {
          const { approveChannelPairingCode } = await import(
            "openclaw/plugin-sdk/conversation-runtime"
          );
          const result = await approveChannelPairingCode({ channel, code });
          if (!result) {
            respond(200, {
              approved: false,
              reason: "pairing code not found (expired or already approved)",
            });
            return;
          }
          log.info?.(
            `funnelmanager-auth: pairing approved via hub (${channel}:${String(result.id)})`,
          );
          respond(200, { approved: true, id: String(result.id) });
        } catch (error) {
          respond(500, { approved: false, reason: String(error) });
        }
      },
    });

    // Per-user agent + binding sync: the auth service calls this route
    // (gateway token auth) whenever a user's channel links change (assign /
    // unlink / user delete). Full-sync semantics: after the call the user's
    // agent exists in openclaw.json and the user's per-sender peer bindings
    // are exactly the given sender list. Config writes hot-apply via the
    // gateway's file watcher; the agent's workspace is created lazily on its
    // first reply, so declaring it here is sufficient.
    api.registerHttpRoute({
      path: "/api/funnelmanager/agents/sync",
      auth: "gateway",
      match: "exact",
      handler: async (req: any, res: any) => {
        const respond = (status: number, payload: Record<string, unknown>) => {
          res.statusCode = status;
          res.setHeader("Content-Type", "application/json; charset=utf-8");
          res.end(JSON.stringify(payload));
        };
        if (String(req?.method ?? "").toUpperCase() !== "POST") {
          respond(405, { synced: false, reason: "POST only" });
          return;
        }
        let body: any = null;
        try {
          const chunks: Buffer[] = [];
          let size = 0;
          for await (const chunk of req) {
            size += chunk.length;
            if (size > 64 * 1024) throw new Error("body too large");
            chunks.push(chunk);
          }
          body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
        } catch (error) {
          respond(400, { synced: false, reason: `invalid body: ${String(error)}` });
          return;
        }
        const username =
          typeof body?.username === "string" ? body.username.trim() : "";
        if (!username || !Array.isArray(body?.senders)) {
          respond(422, { synced: false, reason: "username and senders[] are required" });
          return;
        }
        const parseSenders = (
          raw: unknown[],
        ): { channel: string; deviceId: string }[] | null => {
          const parsed: { channel: string; deviceId: string }[] = [];
          for (const entry of raw as any[]) {
            const channel =
              typeof entry?.channel === "string"
                ? entry.channel.trim().toLowerCase()
                : "";
            const deviceId =
              typeof entry?.device_id === "string" ? entry.device_id.trim() : "";
            if (!channel || !deviceId) return null;
            parsed.push({ channel, deviceId });
          }
          return parsed;
        };
        const senders = parseSenders(body.senders);
        // Optional: senders being offboarded — their DM pairing (allow-from
        // entry) is revoked so they must re-pair before reaching the bot at
        // all, instead of silently falling through to the default agent.
        const removed = parseSenders(
          Array.isArray(body?.removed) ? body.removed : [],
        );
        if (!senders || !removed) {
          respond(422, {
            synced: false,
            reason: "each sender needs a non-empty channel and device_id",
          });
          return;
        }
        const agentId = agentIdForUsername(username);
        // True when the binding routes to this sender's DMs: a peer-tier match
        // on the same channel with peer kind "direct" (or legacy "dm").
        const isPeerMatch = (binding: any, channel: string, deviceId: string) => {
          const peer = binding?.match?.peer;
          if (!peer) return false;
          const kind = String(peer.kind ?? "");
          if (kind !== "direct" && kind !== "dm") return false;
          return (
            String(binding.match.channel ?? "") === channel &&
            String(peer.id ?? "") === deviceId
          );
        };
        try {
          const { updateConfig } = await import(
            "openclaw/plugin-sdk/config-mutation"
          );
          let bindingCount = 0;
          let applied = false;
          let lastError: unknown = null;
          // updateConfig is compare-and-swap on the config file hash and
          // throws on a concurrent write — retry a couple of times.
          for (let attempt = 0; attempt < 3 && !applied; attempt += 1) {
            if (attempt > 0) {
              await new Promise((resolve) => setTimeout(resolve, 100));
            }
            try {
              await updateConfig((draft: any) => {
                // Agents: the moment agents.list materializes, the implicit
                // "main" agent must be pinned explicitly or it disappears.
                if (!draft.agents || typeof draft.agents !== "object") {
                  draft.agents = {};
                }
                if (!Array.isArray(draft.agents.list)) draft.agents.list = [];
                if (draft.agents.list.length === 0) {
                  draft.agents.list.push({ id: "main", default: true });
                }
                const workspace = `~/.openclaw/workspaces/${agentId}`;
                const existing = draft.agents.list.find(
                  (agent: any) => agent?.id === agentId,
                );
                if (existing) {
                  existing.name = username;
                  existing.workspace = workspace;
                } else if (senders.length > 0) {
                  // Only materialize the agent when the user actually has
                  // senders — an empty sync (user delete) must not create one.
                  draft.agents.list.push({ id: agentId, name: username, workspace });
                }
                // Bindings: upsert one peer binding per sender (stealing the
                // binding if the sender was previously another user's), then
                // drop this agent's DM peer bindings for senders no longer in
                // the list. Only direct/dm-kind peers are pruned — hand-added
                // group/channel peers, channel-wide (peer-less) bindings, and
                // other agents' bindings are never touched.
                if (!Array.isArray(draft.bindings)) draft.bindings = [];
                for (const sender of senders) {
                  const bound = draft.bindings.find((binding: any) =>
                    isPeerMatch(binding, sender.channel, sender.deviceId),
                  );
                  if (bound) {
                    bound.agentId = agentId;
                  } else {
                    draft.bindings.push({
                      agentId,
                      match: {
                        channel: sender.channel,
                        peer: { kind: "direct", id: sender.deviceId },
                      },
                    });
                  }
                }
                draft.bindings = draft.bindings.filter((binding: any) => {
                  if (binding?.agentId !== agentId) return true;
                  const kind = String(binding?.match?.peer?.kind ?? "");
                  if (kind !== "direct" && kind !== "dm") return true;
                  return senders.some((sender) =>
                    isPeerMatch(binding, sender.channel, sender.deviceId),
                  );
                });
                bindingCount = draft.bindings.filter(
                  (binding: any) => binding?.agentId === agentId,
                ).length;
                // updateConfig writes the mutator's RETURN VALUE as the next
                // config — forgetting this writes an empty file (the gateway's
                // size-drop guard rejects it, but the sync still fails).
                return draft;
              });
              applied = true;
            } catch (error) {
              lastError = error;
            }
          }
          if (!applied) {
            respond(500, {
              synced: false,
              reason: `config update failed: ${String(lastError)}`,
            });
            return;
          }
          // Offboarding: revoke the removed senders' DM pairing so they must
          // re-pair to reach the bot at all. Idempotent (a missing allow-from
          // entry is a no-op), so a failed sync can simply be retried.
          if (removed.length > 0) {
            const { removeChannelAllowFromStoreEntry } = await import(
              "openclaw/plugin-sdk/conversation-runtime"
            );
            for (const sender of removed) {
              await removeChannelAllowFromStoreEntry({
                channel: sender.channel,
                entry: sender.deviceId,
              });
            }
          }
          log.info?.(
            `funnelmanager-auth: agent sync applied (${agentId}: ${senders.length} sender(s), ${removed.length} unpaired)`,
          );
          respond(200, {
            synced: true,
            agent_id: agentId,
            bindings: bindingCount,
            unpaired: removed.length,
          });
        } catch (error) {
          respond(500, { synced: false, reason: String(error) });
        }
      },
    });

    // Enforce the sender's session token on funnelmanager MCP tool calls.
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
        if (result.status !== "ok") {
          return { block: true, blockReason: result.message };
        }
        const provided = (event?.params ?? {}).session_token;
        if (typeof provided !== "string" || !provided) {
          return {
            block: true,
            blockReason:
              "Funnel Manager tools require an explicit session_token " +
              "argument. Call funnelmanager_session_token first and pass its " +
              "session_token value in every funnelmanager tool call.",
          };
        }
        // Validate but never rewrite: the call proceeds only with the
        // sender's current session token, and only untouched — Codex-native
        // surfaces deny any hook params rewrite.
        if (provided === result.token.token) return;
        return {
          block: true,
          blockReason:
            "The session_token passed does not match the current Funnel " +
            "Manager session for this sender (stale, expired, or from " +
            "another conversation). Call funnelmanager_session_token for a " +
            "fresh token and pass its session_token value explicitly.",
        };
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

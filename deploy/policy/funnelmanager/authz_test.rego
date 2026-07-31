package funnelmanager.authz_test

import rego.v1

import data.funnelmanager.authz
import data.funnelmanager.envoy

# --------------------------------------------------------------------------
# Helpers — inputs shaped like Envoy ext_authz CheckRequests
# --------------------------------------------------------------------------

spiffe(ns, sa) := sprintf("spiffe://cluster.local/ns/%s/sa/%s", [ns, sa])

# Unsigned JWTs: Istio validates signatures; OPA only reads claims.
jwt(claims) := io.jwt.encode_sign({"typ": "JWT", "alg": "HS256"}, claims, {
	"kty": "oct",
	"k": "dGVzdC1rZXktdGVzdC1rZXktdGVzdC1rZXktMTIzNA",
})

prod_iss := "https://kc.x9bc433.win/realms/funnelmanager"

dev_iss := "https://kc.x9bc433.win/realms/funnelmanager-dev"

admin_user_token(aud, azp) := jwt({
	"iss": prod_iss,
	"aud": aud,
	"azp": azp,
	"sub": "u-admin",
	"preferred_username": "admin",
	"realm_access": {"roles": ["admin"]},
})

# A normal, least-privilege human: a specific -access role list, NEVER admin.
user_token(aud, azp, roles) := jwt({
	"iss": prod_iss,
	"aud": aud,
	"azp": azp,
	"sub": "u-e2e",
	"preferred_username": "e2e-canary",
	"realm_access": {"roles": roles},
})

http_input(src_ns, src_sa, dst_ns, dst_sa, method, path, token) := {"attributes": {
	"source": {"principal": spiffe(src_ns, src_sa)},
	"destination": {"principal": spiffe(dst_ns, dst_sa)},
	"request": {"http": {
		"method": method,
		"path": path,
		"host": "x9bc433.win",
		"headers": {"authorization": sprintf("Bearer %s", [token])},
	}},
}}

http_input_noauth(src_ns, src_sa, dst_ns, dst_sa, method, path) := {"attributes": {
	"source": {"principal": spiffe(src_ns, src_sa)},
	"destination": {"principal": spiffe(dst_ns, dst_sa)},
	"request": {"http": {
		"method": method,
		"path": path,
		"host": "x9bc433.win",
		"headers": {},
	}},
}}

tcp_input(src_ns, src_sa, dst_ns, dst_sa) := {"attributes": {
	"source": {"principal": spiffe(src_ns, src_sa)},
	"destination": {"principal": spiffe(dst_ns, dst_sa)},
}}

gateway_input(host, method, path, headers) := {"attributes": {
	# Real ingress-gateway ext_authz: no source principal, and the
	# destination principal is the TLS SNI hostname (not a SPIFFE workload).
	"source": {"address": {"socketAddress": {"address": "203.0.113.7"}}},
	"destination": {"principal": host},
	"request": {"http": {
		"method": method,
		"path": path,
		"host": host,
		"headers": headers,
	}},
}}

# --------------------------------------------------------------------------
# 1. Internal happy path — both identities valid
# --------------------------------------------------------------------------

test_internal_happy_path_search_to_leads if {
	authz.allow with input as http_input(
		"prod", "search", "prod", "leads", "POST", "/api/leads",
		admin_user_token("leads", "search"),
	)
}

test_envoy_result_allows if {
	envoy.result.allowed with input as http_input(
		"prod", "search", "prod", "leads", "POST", "/api/leads",
		admin_user_token("leads", "search"),
	)
}

# --------------------------------------------------------------------------
# 2. Wrong audience rejected
# --------------------------------------------------------------------------

test_wrong_audience_denied if {
	not authz.allow with input as http_input(
		"prod", "search", "prod", "leads", "POST", "/api/leads",
		admin_user_token("search", "search"), # aud names the wrong service
	)
}

test_wrong_audience_reason if {
	reasons := authz.deny_reasons with input as http_input(
		"prod", "search", "prod", "leads", "POST", "/api/leads",
		admin_user_token("search", "search"),
	)
	"missing or invalid token (issuer/audience)" in reasons
}

# --------------------------------------------------------------------------
# 3. Anonymous webhook allowed without a JWT (workload still stamped)
# --------------------------------------------------------------------------

test_webhook_anonymous_from_gateway_allowed if {
	authz.allow with input as http_input_noauth(
		"istio-ingress", "istio-ingress", "prod", "leads",
		"POST", "/api/leads/webhooks/apollo/somesecret",
	)
}

test_webhook_from_random_workload_denied if {
	not authz.allow with input as http_input_noauth(
		"prod", "mail", "prod", "leads",
		"POST", "/api/leads/webhooks/apollo/somesecret",
	)
}

test_gateway_cannot_reach_leads_beyond_webhooks if {
	not authz.allow with input as http_input(
		"istio-ingress", "istio-ingress", "prod", "leads",
		"GET", "/api/leads/stats",
		admin_user_token("leads", "search"),
	)
}

# --------------------------------------------------------------------------
# 4. Delegation (azp) constraints — incl. the agent path
# --------------------------------------------------------------------------

# Agent flow: client-credentials principal exchanged by mcp toward leads.
# Same rules as interactive users: its service account must hold a granted
# realm role (assigned in Keycloak).
agent_token_with_role := jwt({
	"iss": prod_iss,
	"aud": "leads",
	"azp": "mcp",
	"sub": "svc-agent-example",
	"preferred_username": "service-account-agent-example",
	"realm_access": {"roles": ["admin"]},
})

agent_token_without_role := jwt({
	"iss": prod_iss,
	"aud": "leads",
	"azp": "mcp",
	"sub": "svc-agent-example",
	"preferred_username": "service-account-agent-example",
	"realm_access": {"roles": ["default-roles-funnelmanager"]},
})

test_agent_via_mcp_with_role_allowed if {
	authz.allow with input as http_input(
		"prod", "mcp", "prod", "leads", "GET", "/api/leads/stats",
		agent_token_with_role,
	)
}

test_agent_without_granted_role_denied if {
	not authz.allow with input as http_input(
		"prod", "mcp", "prod", "leads", "GET", "/api/leads/stats",
		agent_token_without_role,
	)
}

test_frontend_azp_rejected_at_leads if {
	# A browser token (azp frontend) must never be replayed straight to
	# leads — hops exchange, so azp must be search or mcp there.
	not authz.allow with input as http_input(
		"prod", "search", "prod", "leads", "GET", "/api/leads/stats",
		admin_user_token("leads", "frontend"),
	)
}

# --------------------------------------------------------------------------
# 5. Dedicated-dependency isolation (TCP branch)
# --------------------------------------------------------------------------

test_owner_reaches_its_dependency if {
	authz.allow with input as tcp_input("prod", "leads", "prod", "mongo")
	authz.allow with input as tcp_input("prod", "search", "prod", "app-db")
	authz.allow with input as tcp_input("prod", "milvus", "prod", "etcd")
}

test_foreign_workload_rejected_by_dependency if {
	not authz.allow with input as tcp_input("prod", "mail", "prod", "mongo")
	not authz.allow with input as tcp_input("prod", "search", "prod", "mail-db")
	not authz.allow with input as tcp_input("prod", "leads", "prod", "app-db")
}

test_gateway_rejected_by_every_dependency if {
	not authz.allow with input as tcp_input("istio-ingress", "istio-ingress", "prod", "app-db")
	not authz.allow with input as tcp_input("istio-ingress", "istio-ingress", "prod", "mongo")
}

test_cross_env_dependency_rejected if {
	not authz.allow with input as tcp_input("dev", "leads", "prod", "mongo")
}

# --------------------------------------------------------------------------
# 6. Environment separation + misc
# --------------------------------------------------------------------------

test_dev_issuer_rejected_in_prod if {
	not authz.allow with input as http_input(
		"prod", "search", "prod", "leads", "POST", "/api/leads",
		jwt({
			"iss": dev_iss, "aud": "leads", "azp": "search",
			"sub": "u", "realm_access": {"roles": ["admin"]},
		}),
	)
}

test_mcp_dark_by_default if {
	# No caller is listed for mcp yet — even a valid token is refused until
	# an agent-runner workload is added to data.config.callers.mcp.
	not authz.allow with input as http_input(
		"prod", "search", "prod", "mcp", "POST", "/mcp",
		admin_user_token("mcp", "frontend"),
	)
}

test_probe_paths_anonymous if {
	authz.allow with input as http_input_noauth("istio-ingress", "istio-ingress", "prod", "search", "GET", "/healthz")
}

test_segment_boundary_prefix if {
	# /api/search grant must not authorize /api/searchesX-style cousins.
	not authz._prefix_match("/api/search/search", "/api/search/searches")
	authz._prefix_match("/api/search/search", "/api/search/search")
}

# --------------------------------------------------------------------------
# 7. Gateway branch
# --------------------------------------------------------------------------

test_gateway_authenticated_api_allowed if {
	authz.allow with input as gateway_input(
		"x9bc433.win", "GET", "/api/search/searches",
		{"authorization": sprintf("Bearer %s", [jwt({
			"iss": prod_iss, "aud": ["search", "mail"], "azp": "frontend",
			"sub": "u-admin", "realm_access": {"roles": ["admin"]},
		})])},
	)
}

test_gateway_tokenless_api_denied if {
	not authz.allow with input as gateway_input("x9bc433.win", "GET", "/api/search/searches", {})
}

test_gateway_spa_shell_anonymous if {
	authz.allow with input as gateway_input("x9bc433.win", "GET", "/login", {})
}

# The extracted search SPA (searchui) serves its static shell anonymously at
# /search/, exactly like mailui (/mail/) and agentsui (/agents/). Without the
# data.json legs (route + caller + anonymous) the sidecar/gateway would deny
# every visitor before any auth logic. Assert both the route resolution and
# the anonymous GET allow.
test_gateway_searchui_shell_resolves if {
	authz.gateway_service == "searchui" with input as gateway_input("x9bc433.win", "GET", "/search/", {})
}

test_gateway_searchui_shell_anonymous if {
	authz.allow with input as gateway_input("x9bc433.win", "GET", "/search/", {})
}

# Sidecar branch: the ingress gateway workload is an allowed caller for the
# searchui pod (config.callers.searchui), so the fronted shell request is
# admitted at the pod's sidecar too (caller_ok + anonymous GET).
test_searchui_pod_reachable_from_gateway if {
	authz.allow with input as http_input_noauth(
		"istio-ingress", "istio-ingress", "prod", "searchui",
		"GET", "/search/",
	)
}

test_gateway_mixed_case_www_host_normalized if {
	# Host normalization lowercases BEFORE trimming "www." so any
	# capitalization of the prefix still resolves the environment.
	authz.allow with input as gateway_input("WWW.X9bc433.win", "GET", "/login", {})
}

test_gateway_unknown_host_denied if {
	not authz.allow with input as gateway_input("evil.example.net", "GET", "/api/search/searches", {})
}

test_gateway_keycloak_host_passes if {
	authz.allow with input as gateway_input(
		"kc.x9bc433.win", "GET",
		"/realms/funnelmanager/.well-known/openid-configuration", {},
	)
}

test_gateway_grafana_host_passes if {
	# Grafana enforces its own Keycloak OIDC login; the gateway admits the host.
	authz.allow with input as gateway_input("grafana.x9bc433.win", "GET", "/login", {})
}

# Browser RUM ingest (Grafana Faro -> Alloy faro.receiver): the SPAs POST
# tokenless to /telemetry/collect (gateway URLRewrites it to /collect for the
# receiver; OPA evaluates the PRE-rewrite path). Anonymous for POST/OPTIONS only.
test_gateway_telemetry_collect_post_anonymous if {
	authz.allow with input as gateway_input("x9bc433.win", "POST", "/telemetry/collect", {})
}

test_gateway_telemetry_collect_options_anonymous if {
	# CORS preflight (same-origin won't preflight, but the receiver registers it).
	authz.allow with input as gateway_input("x9bc433.win", "OPTIONS", "/telemetry/collect", {})
}

test_gateway_telemetry_collect_get_denied if {
	# Only POST/OPTIONS are anonymous; a GET has no anonymous entry and no token.
	not authz.allow with input as gateway_input("x9bc433.win", "GET", "/telemetry/collect", {})
}

test_gateway_telemetry_collect_resolves_alloy if {
	authz.gateway_service == "alloy" with input as gateway_input("x9bc433.win", "POST", "/telemetry/collect", {})
}

test_gateway_telemetry_collect_no_widening if {
	# Guard against prefix widening: the anonymous entry is exactly
	# /telemetry/collect. A sibling suffix (no segment boundary) and the bare
	# parent segment must NOT inherit the anonymous allow.
	not authz.allow with input as gateway_input("x9bc433.win", "POST", "/telemetry/collectXYZ", {})
	not authz.allow with input as gateway_input("x9bc433.win", "POST", "/telemetry", {})
}

test_bootstrap_default_deny if {
	r := envoy.result with input as {"attributes": {"source": {"principal": ""}, "destination": {"principal": ""}}}
	r.allowed == false
}

# --------------------------------------------------------------------------
# 8. leads-canary (east-west backend canary)
# --------------------------------------------------------------------------
# leads-canary reuses the stable `leads` ServiceAccount, so OPA resolves
# `service := dst.sa = leads` for every request that lands on a canary pod —
# identical to stable leads. OPA is AGNOSTIC to the `x-fm-canary` marker (the
# east-west VirtualService does the steering; the SA/audience verdict is the
# same for stable and canary leads). There is deliberately NO
# `config.callers["leads-canary"]` entry — leads-canary never has its own
# audience/SA, so such an entry would be dead policy that OPA never consults.
# The test below asserts the REAL wire path a canary search takes.

# REAL wire path: a canary search request is served by a `search-canary` pod,
# which reuses the `search` SA, and its `search->leads` call (carrying the
# propagated marker, invisible to OPA) lands on a leads-canary pod running under
# the `leads` SA. To OPA this is exactly src.sa=search -> dst.sa=leads, aud
# leads — authorized like any search->leads call.
test_canary_search_sa_reaches_leads_canary_pod if {
	authz.allow with input as http_input(
		"prod", "search", "prod", "leads", "POST", "/api/leads",
		admin_user_token("leads", "search"),
	)
}

# --------------------------------------------------------------------------
# 9. search-access includes search's dependencies (leads + mail contacts)
# --------------------------------------------------------------------------
# A role grants everything its service depends on. search calls leads for every
# result path, so a NON-ADMIN search-access user's exchanged token authorizes the
# search->leads hop via the normal grant check — no policy bypass. Before this,
# the whole leads surface (similarity, hydration, credits, enrich, even starting a
# search) was admin-only because search forwarded the human token, which lacked a
# leads grant.

# THE FIX: a plain search-access user reaches leads on the search->leads hop.
test_search_access_user_reaches_leads_similarity if {
	authz.allow with input as http_input(
		"prod", "search", "prod", "leads", "POST", "/api/leads/similarity-search",
		user_token("leads", "search", ["search-access"]),
	)
}

test_search_access_user_reaches_leads_hydration if {
	authz.allow with input as http_input(
		"prod", "search", "prod", "leads", "POST", "/api/leads",
		user_token("leads", "search", ["search-access"]),
	)
}

# The dependency grant does NOT let a search-access user hit leads on any OTHER
# hop: caller_ok still fences leads to search/mcp. A mail workload carrying a
# search-access token cannot reach leads.
test_dependency_grant_still_fenced_by_caller if {
	not authz.allow with input as http_input(
		"prod", "mail", "prod", "leads", "POST", "/api/leads/similarity-search",
		user_token("leads", "mail", ["search-access"]),
	)
}

# And a search-access token replayed with a browser azp is still rejected at
# leads (azp_ok), and a non-leads audience is still rejected (token_valid) —
# expanding the grant did not drop the exchange constraints.
test_dependency_grant_still_enforces_azp_and_aud if {
	not authz.allow with input as http_input(
		"prod", "search", "prod", "leads", "POST", "/api/leads/similarity-search",
		user_token("leads", "frontend", ["search-access"]),
	)
	not authz.allow with input as http_input(
		"prod", "search", "prod", "leads", "POST", "/api/leads/similarity-search",
		user_token("search", "search", ["search-access"]),
	)
}

# A user WITHOUT search-access (e.g. mail-access only) gets no leads dependency
# grant — leads stays denied for them.
test_non_search_user_still_denied_at_leads if {
	not authz.allow with input as http_input(
		"prod", "search", "prod", "leads", "POST", "/api/leads/similarity-search",
		user_token("leads", "search", ["mail-access"]),
	)
}

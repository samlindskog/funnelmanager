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
	"source": {"principal": ""},
	"destination": {"principal": spiffe("istio-ingress", "istio-ingress")},
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

test_bootstrap_default_deny if {
	r := envoy.result with input as {"attributes": {"source": {"principal": ""}, "destination": {"principal": ""}}}
	r.allowed == false
}

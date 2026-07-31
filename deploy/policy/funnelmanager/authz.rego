# Funnel Manager authorization core.
#
# Called by every Envoy (sidecars in prod + the edge gateway) through
# ext_authz. Default deny. Two branches:
#   - HTTP: both identities checked — the calling WORKLOAD (mTLS SPIFFE
#     principal) against the caller allowlists, and the ORIGINATING
#     PRINCIPAL (JWT, already signature-verified by Istio
#     RequestAuthentication) against issuer/audience/azp/role-grant rules.
#   - TCP (no HTTP attributes): dedicated-dependency isolation — the
#     (source workload, destination workload) pair must be listed in
#     data.funnelmanager.config.pairings.
#
# Everything environment- or deployment-specific lives in data documents
# (data.json): adding a user role, a caller, a pairing, or an anonymous
# path is a one-line data change, never a policy rewrite.
package funnelmanager.authz

import rego.v1

config := data.funnelmanager.config

# --------------------------------------------------------------------------
# Request digest
# --------------------------------------------------------------------------

is_http if input.attributes.request.http

http := input.attributes.request.http

method := http.method

# Envoy's :path includes the query string; rules match on the bare path.
path := split(http.path, "?")[0]

# Lowercase BEFORE trimming so a mixed-case "WWW." prefix is still stripped.
host := trim_prefix(lower(object.get(http, "host", "")), "www.")

_workload(principal) := {"ns": parts[2], "sa": parts[4]} if {
	parts := split(trim_prefix(principal, "spiffe://"), "/")
	count(parts) >= 5
	parts[1] == "ns"
	parts[3] == "sa"
}

src := _workload(input.attributes.source.principal)

dst := _workload(input.attributes.destination.principal)

# The destination service is its ServiceAccount name (one SA per workload).
service := dst.sa

env := dst.ns

# At the edge gateway there is no mTLS peer: Envoy populates
# destination.principal with the TLS SNI hostname (not a workload SPIFFE) and
# leaves source.principal unset. So "we are at the gateway" == the destination
# did not parse as a SPIFFE workload. Sidecar hops always have a SPIFFE dst.
at_gateway if not dst

# --------------------------------------------------------------------------
# JWT (decode only — Istio already validated the signature)
# --------------------------------------------------------------------------

bearer := t if {
	auth := object.get(http, ["headers", "authorization"], "")
	lower(substring(auth, 0, 7)) == "bearer "
	t := substring(auth, 7, count(auth) - 7)
}

claims := c if [_, c, _] := io.jwt.decode(bearer)

audiences := claims.aud if is_array(claims.aud)

audiences := [claims.aud] if is_string(claims.aud)

roles contains role if some role in object.get(claims, ["realm_access", "roles"], [])

# --------------------------------------------------------------------------
# Sidecar HTTP branch
# --------------------------------------------------------------------------

token_valid_for(svc, environment) if {
	claims.iss == config.issuers[environment]
	some aud in audiences
	aud == svc
}

# azp must be one of the clients allowed to mint/exchange tokens for this
# service (delegation constraint — Keycloak stamps the exchanging client
# into azp). Services without an entry accept any azp.
azp_ok_for(svc) if {
	allowed := config.azp_allow[svc]
	claims.azp in allowed
}

azp_ok_for(svc) if not config.azp_allow[svc]

# Caller allowlists. ns "env" means "same namespace as the destination"
# (prod calls prod, same-namespace); anything else is a literal namespace.
# An entry with path_prefixes only admits the caller on those paths
# (e.g. the gateway may reach leads solely for the Apollo webhooks).
caller_ok if {
	some entry in config.callers[service]
	_caller_ns_ok(entry)
	entry.sa == src.sa
	_caller_paths_ok(entry)
}

_caller_ns_ok(entry) if {
	entry.ns == "env"
	src.ns == dst.ns
}

_caller_ns_ok(entry) if {
	entry.ns != "env"
	entry.ns == src.ns
}

_caller_paths_ok(entry) if not entry.path_prefixes

_caller_paths_ok(entry) if {
	some prefix in entry.path_prefixes
	startswith(path, prefix)
}

# Anonymous allowlist — generated from the @anonymous code annotations
# (fm_runtime); the caller allowlist above still applies (workload-stamped).
anonymous_ok_for(svc) if {
	some entry in config.anonymous
	entry.service in {svc, "*"}
	method in entry.methods
	some prefix in entry.prefixes
	_prefix_match(prefix, path)
}

_prefix_match(prefix, _) if prefix == "/"

_prefix_match(prefix, p) if p == prefix

_prefix_match(prefix, p) if startswith(p, sprintf("%s/", [trim_suffix(prefix, "/")]))

# Role grants — the legacy {service, methods, path_prefix} shape, evaluated
# against the destination service. Path matching is exact or
# segment-boundary prefix (/api/search does not authorize /api/searches).
grant_ok_for(svc) if {
	some role in roles
	some grant in data.funnelmanager.roles[role].grants
	grant.service in {svc, "*"}
	_grant_method_ok(grant)
	_prefix_match(grant.path_prefix, path)
}

_grant_method_ok(grant) if "*" in grant.methods

_grant_method_ok(grant) if method in grant.methods

allow if {
	is_http
	not at_gateway
	caller_ok
	anonymous_ok_for(service)
}

allow if {
	is_http
	not at_gateway
	caller_ok
	token_valid_for(service, env)
	azp_ok_for(service)
	grant_ok_for(service)
}

# --------------------------------------------------------------------------
# Edge gateway HTTP branch
# --------------------------------------------------------------------------
# Internet clients carry no mTLS workload identity, so the gateway check is
# route-shaped: the host picks the environment, the longest matching route
# prefix picks the logical service, then the same anonymous/JWT rules run.
# The sidecar at the destination re-checks everything with full workload
# context — this is the outer wall, not the only wall.

gateway_env := config.hosts[host]

_route_matches := {prefix |
	some prefix, _ in config.routes
	startswith(path, prefix)
}

_longest(prefixes) := m if {
	some m in prefixes
	every other in prefixes {
		count(other) <= count(m)
	}
}

gateway_service := config.routes[_longest(_route_matches)]

allow if {
	is_http
	at_gateway
	gateway_env
	anonymous_ok_for(gateway_service)
}

allow if {
	is_http
	at_gateway
	gateway_env
	token_valid_for(gateway_service, gateway_env)
	grant_ok_for(gateway_service)
}

# Keycloak's own host: pre-auth by design (login pages, token endpoint,
# JWKS). Not part of the app route table; NetworkPolicies fence it.
allow if {
	is_http
	at_gateway
	host == config.keycloak_host
}

# Grafana's own host: Grafana enforces Keycloak OIDC login itself, so the
# gateway admits the host wholesale (same posture as Keycloak's host).
allow if {
	is_http
	at_gateway
	host == config.grafana_host
}

# --------------------------------------------------------------------------
# TCP branch — dedicated-dependency isolation
# --------------------------------------------------------------------------
# One data line per (owner -> dependency) pair, same-environment only.
# The gateway is deliberately absent from every pairing.

allow if {
	not is_http
	some pair in config.pairings
	pair.from == src.sa
	pair.to == dst.sa
	src.ns == dst.ns
}

# --------------------------------------------------------------------------
# Denial summary (surfaced in the 403 body for operators)
# --------------------------------------------------------------------------

deny_reasons contains "unknown or unauthorized calling workload" if {
	is_http
	not at_gateway
	not caller_ok
}

deny_reasons contains "missing or invalid token (issuer/audience)" if {
	is_http
	not at_gateway
	caller_ok
	not anonymous_ok_for(service)
	not token_valid_for(service, env)
}

deny_reasons contains "delegation (azp) not permitted for this service" if {
	is_http
	not at_gateway
	caller_ok
	token_valid_for(service, env)
	not azp_ok_for(service)
}

deny_reasons contains "no role grant covers this request" if {
	is_http
	not at_gateway
	caller_ok
	token_valid_for(service, env)
	azp_ok_for(service)
	not grant_ok_for(service)
}

deny_reasons contains "gateway: unknown host or route" if {
	is_http
	at_gateway
	not gateway_env
	not host == config.keycloak_host
	not host == config.grafana_host
}

deny_reasons contains "gateway: authentication or authorization failed" if {
	is_http
	at_gateway
	gateway_env
	not anonymous_ok_for(gateway_service)
	not grant_ok_for(gateway_service)
}

deny_reasons contains "tcp: workload pair not in the dependency allowlist" if {
	not is_http
	not allow
}

summary := concat("; ", sort(deny_reasons))

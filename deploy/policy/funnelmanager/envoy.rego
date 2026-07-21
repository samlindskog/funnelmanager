# Envoy ext_authz entrypoint — the OPA DaemonSet serves
# data.funnelmanager.envoy.result (see plugins.envoy_ext_authz_grpc.path in
# deploy/infrastructure/opa/daemonset.yaml). Fail closed: anything the authz
# package does not explicitly allow is a 403 with the reasons in the body.
package funnelmanager.envoy

import rego.v1

import data.funnelmanager.authz

default result := {
	"allowed": false,
	"http_status": 403,
	"body": "denied by funnelmanager policy (no rule matched)",
}

result := {"allowed": true} if authz.allow

result := {
	"allowed": false,
	"http_status": 403,
	"body": sprintf("denied by funnelmanager policy: %s", [authz.summary]),
} if {
	not authz.allow
	count(authz.deny_reasons) > 0
}

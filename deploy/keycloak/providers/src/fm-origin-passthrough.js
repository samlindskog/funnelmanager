/*
 * fm_origin propagation across RFC 8693 token exchange (Keycloak 26.2).
 *
 * WHY: hardcoded per-client fm_origin mappers cannot propagate origin across
 * hops. The `agents` client mints fm_origin=agent on the first hop, but every
 * subsequent exchange is performed by a DIFFERENT client (mcp->search,
 * search->leads, ...). A hardcoded-claim mapper on those clients re-stamps
 * 'user' and the agent origin is lost — so "alice (via agent)" never reaches
 * the final audience.
 *
 * WHAT: this mapper carries the INBOUND subject token's fm_origin claim onto
 * the newly issued token, defaulting to "user". Assigned (via the `fm-origin`
 * client scope) to every service client that performs exchanges, so an
 * agent-initiated origin survives every downstream hop.
 *
 * SECURITY: it reads the `subject_token` ONLY on an actual token-exchange grant
 * (grant_type contains "token-exchange"). Keycloak validates the subject_token's
 * signature as part of processing a token-exchange request, so during that grant
 * the form's subject_token is the validated inbound token — never a
 * caller-supplied `fm_origin`/`claims` parameter. The grant_type gate is
 * load-bearing: the mapper pipeline also runs for authorization_code / password /
 * refresh_token / client_credentials on any client carrying the fm-origin scope
 * (incl. the public `frontend`), and WITHOUT this gate an attacker could stuff an
 * unsigned `subject_token=<h>.<base64 {"fm_origin":"agent"}>.<s>` form field onto
 * such a request and self-stamp fm_origin — i.e. the claim would be forgeable.
 * On a normal login (non-exchange grant) it returns "user".
 *
 * Requires Keycloak feature `scripts` (preview) + this provider JAR in
 * /opt/keycloak/providers. The `agents` client keeps its own hardcoded
 * fm_origin=agent mapper (the mint) and does NOT carry this scope.
 *
 * Available script bindings: user, realm, token, userSession, keycloakSession.
 * The subject token is not exposed as a session note during exchange, so we
 * read it from the exchange request's decoded form parameters.
 */
var ORIGIN = "user";
try {
    var form = keycloakSession.getContext().getHttpRequest().getDecodedFormParameters();
    // Gate: only trust subject_token on a genuine token-exchange grant, where
    // Keycloak has validated its signature. Excludes authorization_code /
    // password / refresh_token / client_credentials (none contain
    // "token-exchange"), closing the unsigned-param forgery vector. A too-broad
    // miss here would only fail SAFE (origin resets to "user"), never forge.
    var grantType = form.getFirst("grant_type");
    var isExchange = grantType !== null && grantType.indexOf("token-exchange") >= 0;
    var subjectToken = isExchange ? form.getFirst("subject_token") : null;
    if (subjectToken !== null && subjectToken.indexOf(".") > 0) {
        var parts = subjectToken.split(".");
        if (parts.length >= 2) {
            var payload = parts[1];
            while (payload.length % 4 !== 0) {
                payload += "=";
            }
            var Base64 = Java.type("java.util.Base64");
            var JavaString = Java.type("java.lang.String");
            var json = new JavaString(Base64.getUrlDecoder().decode(payload), "UTF-8");
            var claims = JSON.parse(json);
            if (claims.fm_origin === "agent") {
                ORIGIN = "agent";
            }
        }
    }
} catch (e) {
    ORIGIN = "user";
}
ORIGIN;

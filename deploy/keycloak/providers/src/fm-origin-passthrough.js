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
 * SECURITY: it reads ONLY the `subject_token` being exchanged (the token
 * Keycloak already validated for this exchange) — never a caller-supplied
 * `fm_origin`/`claims` request parameter — so a client cannot forge origin.
 * On a normal login (no subject_token present) it returns "user".
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
    var subjectToken = form.getFirst("subject_token");
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

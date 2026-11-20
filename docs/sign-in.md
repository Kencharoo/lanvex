# Signing in with a directory or single sign-on

Besides its own users, Lanvex lets people sign in with their account from:

- a **directory**: Active Directory or another LDAP server (OpenLDAP, FreeIPA, Samba AD, …). They use
  the same name and password form as Lanvex's own users;
- **single sign-on** with OpenID Connect: Keycloak, Microsoft Entra ID, Authentik, Authelia, Google
  Workspace and any other OpenID Connect provider. The sign-in page then shows a button for it.

Either way, **groups decide the role**: members of the administrators' group become administrators,
members of the read-only group read-only users, and anyone in neither group cannot sign in, even with
the right password. The role is set again at every sign-in, so moving someone between groups takes
effect the next time they sign in (sessions already open keep their role until they end).

Lanvex creates the account in its user list on the first sign-in, marked *Directory* or *SSO*. In
Lanvex you can deactivate such an account (it then cannot sign in, whatever the directory says) or
delete it; its role and password are not set in Lanvex. Lanvex's own users keep working, also while the
directory or provider is unreachable, so keep the first administrator as a way in. A Lanvex user and a
directory account never share a name: if Lanvex already has a local user with that name, the local
user is the one that signs in with it, and single sign-on with that name is refused.

All settings go into `.env`; restart Lanvex after changing them (`lanvexctl restart`, or
`docker compose up -d`). Lanvex Core follows: it trusts whoever is signed in to Lanvex.

## Active Directory

```
LANVEX_LDAP_URL=ldaps://dc1.school.example ldaps://dc2.school.example
LANVEX_LDAP_TYPE=ad
LANVEX_LDAP_BIND_DN=CN=svc-lanvex,OU=Service Accounts,DC=school,DC=example
LANVEX_LDAP_BIND_PASSWORD=…
LANVEX_LDAP_USER_BASE=OU=Staff,DC=school,DC=example
LANVEX_LDAP_ADMIN_GROUP=CN=Lanvex Admins,OU=Groups,DC=school,DC=example
LANVEX_LDAP_READONLY_GROUP=CN=IT Staff,OU=Groups,DC=school,DC=example
```

- People sign in with their account name (`mmueller`) or their user principal name
  (`mmueller@school.example`).
- Membership counts through nested groups.
- The service account needs no rights beyond reading users and groups (any domain user can).
- Use `ldaps://` (port 636), or `ldap://` with `LANVEX_LDAP_STARTTLS=true`. Without either, passwords
  cross the network unencrypted.

## OpenLDAP and other LDAP directories

```
LANVEX_LDAP_URL=ldap://ldap.school.example
LANVEX_LDAP_STARTTLS=true
LANVEX_LDAP_BIND_DN=cn=lanvex,ou=services,dc=school,dc=example
LANVEX_LDAP_BIND_PASSWORD=…
LANVEX_LDAP_USER_BASE=ou=people,dc=school,dc=example
LANVEX_LDAP_ADMIN_GROUP=cn=net-admins,ou=groups,dc=school,dc=example
LANVEX_LDAP_READONLY_GROUP=cn=net-readers,ou=groups,dc=school,dc=example
```

- People sign in with their `uid`. Another attribute or more conditions: `LANVEX_LDAP_USER_FILTER`,
  with `{username}` where the name goes, for example
  `(&(objectClass=inetOrgPerson)(|(uid={username})(mail={username})))`.
- Group membership is the user's `memberOf` where the directory keeps it, or else the group's
  `member` or `uniqueMember` (the user's DN) or `memberUid` (the user's uid). Nested groups count only
  through `memberOf`.

| Setting | Default | Notes |
|---|---|---|
| `LANVEX_LDAP_URL` | — | `ldap://` or `ldaps://`; several, separated by spaces, are tried in order |
| `LANVEX_LDAP_TYPE` | `ldap` | `ad` for Active Directory |
| `LANVEX_LDAP_STARTTLS` | `false` | Encrypt an `ldap://` connection |
| `LANVEX_LDAP_CA_FILE` | the system's | The CA that signed the directory's certificate, as a PEM file inside the container (mount it). The certificate is always checked |
| `LANVEX_LDAP_BIND_DN` / `_PASSWORD` | — | Lanvex's own account, for finding users |
| `LANVEX_LDAP_USER_BASE` | — | Where users are searched for, with everything below it |
| `LANVEX_LDAP_USER_FILTER` | by type | See above |
| `LANVEX_LDAP_ADMIN_GROUP` / `_READONLY_GROUP` | — | Full DNs; at least one |
| `LANVEX_LDAP_TIMEOUT_SECONDS` | `10` | Per connection and answer |

**Settings → Sign-in → Test directory** checks that Lanvex reaches the directory and its account can
read the user base.

## Single sign-on (OpenID Connect)

Register Lanvex with the provider as a confidential web application:

- **Redirect URI:** `https://lanvex.school.example/api/v1/auth/sso/callback`
  (`LANVEX_PUBLIC_URL` followed by `/api/v1/auth/sso/callback`);
- **Scopes:** `openid profile email`, and a **groups claim** in the ID token.

```
LANVEX_PUBLIC_URL=https://lanvex.school.example
LANVEX_OIDC_ISSUER=https://sso.school.example/realms/school
LANVEX_OIDC_CLIENT_ID=lanvex
LANVEX_OIDC_CLIENT_SECRET=…
LANVEX_OIDC_ADMIN_GROUPS=["net-admins"]
LANVEX_OIDC_READONLY_GROUPS=["it-staff"]
LANVEX_OIDC_NAME=School account
```

| Setting | Default | Notes |
|---|---|---|
| `LANVEX_OIDC_ISSUER` | — | The provider's issuer URL; Lanvex reads `/.well-known/openid-configuration` from it |
| `LANVEX_OIDC_CLIENT_ID` / `_SECRET` | — | From registering Lanvex |
| `LANVEX_OIDC_ADMIN_GROUPS` / `_READONLY_GROUPS` | `[]` | JSON lists of values of the groups claim; at least one group |
| `LANVEX_OIDC_GROUPS_CLAIM` | `groups` | The claim that lists the groups |
| `LANVEX_OIDC_USERNAME_CLAIM` | `preferred_username` | Falls back to `email`, then the subject |
| `LANVEX_OIDC_SCOPES` | `openid profile email` | Add a scope if the provider sends groups only with it |
| `LANVEX_OIDC_NAME` | `Single sign-on` | The button's label |

Per provider:

- **Keycloak:** add a *Group Membership* mapper to the client (token claim name `groups`). Groups
  arrive as paths (`/net-admins`); Lanvex accepts them with or without the leading `/`.
- **Microsoft Entra ID:** issuer `https://login.microsoftonline.com/<tenant id>/v2.0`; under *Token
  configuration* add the groups claim (security groups). Entra sends group **object IDs**, so list
  those. `preferred_username` is the user principal name.
- **Authentik:** an OAuth2/OpenID provider for Lanvex; its default scopes include the `groups` claim.
- **Google Workspace** sends no groups claim; it suits only with a provider in between that adds one.

How Lanvex checks the sign-in: the authorization code flow with PKCE, a fresh state and nonce per
attempt (kept encrypted in a cookie for ten minutes), the ID token's signature against the provider's
published keys, its issuer, audience, expiry and nonce. If the ID token carries no groups claim, Lanvex
asks the provider's userinfo endpoint. Every sign-in, and every refused one, is in the audit log with
its method.

## Troubleshooting

| Message | Meaning |
|---|---|
| *This account is in none of the groups allowed to use Lanvex* | Right password or provider sign-in, but neither group. Check the group DNs (directory) or the groups claim's values (SSO) |
| *The directory did not answer. Local accounts can still sign in* | The directory is unreachable, refused Lanvex's account, or its certificate did not check out. Lanvex's log says which; *Test directory* too |
| *Lanvex already has a local user with this name* | A Lanvex user has the name the provider sends. Rename or delete the local user, or change `LANVEX_OIDC_USERNAME_CLAIM` |
| *The sign-in took too long or started in another browser* | The attempt is older than ten minutes, or cookies are blocked for Lanvex |
| *Single sign-on did not work* | The provider refused the code, or the ID token did not check out (clock skew, wrong client ID, wrong issuer). Lanvex's log has the reason |

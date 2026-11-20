#!/usr/bin/env bash
# TEST SETUP: OpenLDAP (slapd) on this machine for backend/tests/test_ldap_live.py, with STARTTLS
# on a self-signed certificate. For CI and development machines only: it replaces slapd's database.
#   scripts/ldap-test-server.sh   then   LANVEX_TEST_LDAP_URL=ldap://localhost LANVEX_TEST_LDAP_CA=$PWD/.ldap-test/ca.crt
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
out="$here/.ldap-test"
mkdir -p "$out"

sudo debconf-set-selections <<'SEL'
slapd slapd/no_configuration boolean false
slapd slapd/domain string school.example
slapd shared/organization string School
slapd slapd/password1 password admin-password
slapd slapd/password2 password admin-password
slapd slapd/purge_database boolean true
slapd slapd/move_old_database boolean true
SEL
if ! command -v slapd >/dev/null; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq slapd ldap-utils >/dev/null
else
  sudo DEBIAN_FRONTEND=noninteractive dpkg-reconfigure -f noninteractive slapd >/dev/null 2>&1
fi
if ! pgrep -x slapd >/dev/null; then
  sudo service slapd start >/dev/null 2>&1 || sudo slapd -h "ldap:/// ldapi:///" -u openldap -g openldap
fi
for _ in $(seq 30); do ldapsearch -x -H ldap://localhost -b dc=school,dc=example -s base >/dev/null 2>&1 && break; sleep 1; done

openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=localhost \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" -keyout "$out/ldap.key" -out "$out/ca.crt" 2>/dev/null
sudo install -d -o openldap -g openldap -m 0750 /etc/ldap/certs
sudo install -o openldap -g openldap -m 0640 "$out/ldap.key" /etc/ldap/certs/lanvex-test.key
sudo install -o openldap -g openldap -m 0644 "$out/ca.crt" /etc/ldap/certs/lanvex-test.crt
sudo ldapmodify -Q -Y EXTERNAL -H ldapi:/// >/dev/null <<'MOD'
dn: cn=config
replace: olcTLSCertificateFile
olcTLSCertificateFile: /etc/ldap/certs/lanvex-test.crt
-
replace: olcTLSCertificateKeyFile
olcTLSCertificateKeyFile: /etc/ldap/certs/lanvex-test.key
MOD
ldapadd -x -H ldap://localhost -D cn=admin,dc=school,dc=example -w admin-password \
  -f "$here/backend/tests/ldap/school.ldif" >/dev/null
echo "OpenLDAP ready on ldap://localhost (STARTTLS, CA $out/ca.crt)"

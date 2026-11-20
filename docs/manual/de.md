# Lanvex-Handbuch

Für Administratoren, die Lanvex installieren und betreiben, und für alle, die damit arbeiten. Dieses
Handbuch beschreibt Lanvex 1.4. Jedes Kapitel endet mit einem Verweis auf das Referenzdokument mit
allen Einzelheiten und Einstellungen (auf Englisch). Die Oberfläche von Lanvex ist englisch; ihre
Beschriftungen stehen hier so, wie sie dort erscheinen. [English](en.md)

1. [Was Lanvex tut](#1-was-lanvex-tut)
2. [Installation](#2-installation)
3. [Erste Schritte](#3-erste-schritte)
4. [Das Netz dokumentieren](#4-das-netz-dokumentieren)
5. [Server](#5-server)
6. [Netzwerkeinstellungen eines Servers ändern](#6-netzwerkeinstellungen-eines-servers-ändern)
7. [Netze auf einem Server: VLANs, DHCP, Zugriff, DNS-Filter](#7-netze-auf-einem-server-vlans-dhcp-zugriff-dns-filter)
8. [Switches](#8-switches)
9. [Discovery und Topologie](#9-discovery-und-topologie)
10. [Überwachung und Alarme](#10-überwachung-und-alarme)
11. [Benutzer und Anmeldung](#11-benutzer-und-anmeldung)
12. [Lanvex betreiben](#12-lanvex-betreiben)
13. [Fehlersuche](#13-fehlersuche)

---

## 1. Was Lanvex tut

Lanvex ist eine Konsole für Ihr Netz: Netze (Präfixe), IP-Adressen, VLANs, Geräte, Server und
Switches. Es hält drei Dinge auseinander:

| | Was es ist | Wo es liegt |
|---|---|---|
| **Dokumentiert** | Wie das Netz sein soll | Lanvex Core (wird mit Lanvex installiert) oder eine vorhandene NetBox |
| **Tatsächlich** | Was Server und Switches wirklich haben | Lanvex liest es über SSH und SNMP |
| **Ausstehend** | Änderungen, die auf Freigabe warten oder gerade laufen | Lanvex, unter *Automation* |

Was Sie in Lanvex anlegen oder bearbeiten (ein Netz, eine IP-Adresse, ein VLAN), wird sofort in die
Dokumentation geschrieben, und Lanvex kennzeichnet es so: *Recorded in Lanvex Core*, nie
„ausgerollt". Server und Switches werden nur durch eine Änderung verändert, die ein Administrator
vorbereitet, geprüft und freigegeben hat, und nur dort, wo Änderungen eingeschaltet sind.
Standardmäßig sind sie aus.

**Rollen.** *Administratoren* dürfen alles ändern. Benutzer mit *Read-only* sehen alles und ändern
nichts, in Lanvex wie in Lanvex Core.

**Navigation.** *Overview*, *Networks*, *IP Addresses*, *VLANs*, *Devices*, *Servers* und *Switches*
umfassen den Bestand. Danach folgen *Alerts*, *Topology*, *Discovery*, *Automation* (jede Änderung an
Servern und Switches) und *Activity & Audit Logs*. Unter *Settings* liegen Ihr Konto, die Benutzer,
die Anmeldung, die Überwachung und die Webhooks. *Lanvex Core* öffnet die Dokumentationsanwendung mit
derselben Anmeldung.

---

## 2. Installation

**Voraussetzung** ist ein Linux-Server mit Docker und Docker Compose v2 oder mit Podman und
podman-compose (auch rootless).

```bash
curl -fsSLO https://raw.githubusercontent.com/Kencharoo/lanvex/main/lanvexctl
sudo bash lanvexctl install          # Podman rootless: bash lanvexctl install
```

`lanvexctl` lädt die neueste Version, prüft sie gegen ihre veröffentlichte SHA-256-Summe und fragt:

1. **Wo die Dokumentation liegt**: *Lanvex Core, jetzt installiert* (empfohlen), oder eine vorhandene
   NetBox (Adresse und API-Token).
2. **Wie Lanvex erreicht wird**: über HTTPS hinter einem Reverse Proxy, oder über reines HTTP in einem
   vertrauenswürdigen Netz.
3. **Den Port**, auf dem Lanvex lauscht.
4. **Den Namen des ersten Administrators.**

Es erzeugt alle Passwörter und Schlüssel in `.env`, baut und startet die Container, wartet, bis sie
gesund sind, und zeigt, wie Sie sich anmelden. Beim ersten Start richtet Lanvex Core seine Datenbank
ein; das dauert einige Minuten.

**HTTPS.** Stellen Sie einen Reverse Proxy vor Lanvex. Mit Caddy auf demselben Rechner:

```
lanvex.example.net {
    reverse_proxy 127.0.0.1:8080
}
```

und in `.env`: `LANVEX_HTTP_PORT=127.0.0.1:8080` und `LANVEX_TRUSTED_PROXIES=["172.31.250.1/32"]`.
Ohne die zweite Zeile erkennt Lanvex nicht, dass der Browser HTTPS benutzt hat, und lehnt die
Anmeldung ab. Änderungen an `.env` übernimmt `lanvexctl restart`.

**Eine eigene NetBox** braucht einen Benutzer und ein Token mit den Berechtigungen aus
[operations.md](../operations.md#netbox-token-permissions) und für Server-Netze drei benutzerdefinierte
Felder ([networks.md](../networks.md#your-own-netbox-instead-of-lanvex-core)). In Lanvex Core ist all das
eingerichtet.

Referenz: [README](../../README.md#installation), [operations.md](../operations.md),
[core.md](../core.md).

---

## 3. Erste Schritte

1. **Melden Sie sich an** mit dem Administrator, den der Installer angelegt hat. Das Passwort ändern
   Sie unter *Settings, Your account*.
2. **Prüfen Sie die Verbindung zur Dokumentation.** Unten in der Navigation stehen *Lanvex Core* oder
   *NetBox* mit Version und einem grünen Punkt, wenn Lanvex sie erreicht.
3. **Legen Sie Benutzer an** unter *Settings, Users*: Name, Passwort und Rolle. Für die Anmeldung über
   Ihr Verzeichnis oder Single Sign-on siehe [Kapitel 11](#11-benutzer-und-anmeldung).
4. **Öffnen Sie Lanvex Core** aus der Navigation. Sie sind dort als Sie selbst angemeldet;
   Administratoren sind in Lanvex Core Superuser, Read-only-Benutzer können ansehen, aber nichts
   ändern. *Back to Lanvex* oben führt zurück.

Alles, was ein Administrator in Lanvex oder über Lanvex ändert, steht in *Activity & Audit Logs*: wer,
was, wann, von welcher Adresse und mit welchem Ergebnis.

---

## 4. Das Netz dokumentieren

| Seite | Was Sie dort tun |
|---|---|
| **Networks** | Präfixe nach VRF und Hierarchie durchsehen, mit ihrer Auslastung. *Add network* führt in vier Schritten durch das Anlegen, mit laufenden Prüfungen (Host-Bits, Doppelte im VRF, Lage zwischen über- und untergeordneten Netzen, gültiges Gateway). Die Seite eines Netzes zeigt freie Bereiche und die nächsten freien Adressen; beim Löschen tippen Sie das Präfix ein und sehen, was mitgeht |
| **IP Addresses** | Suchen und filtern, eine bestimmte Adresse anlegen oder die nächste freie nehmen, den Status ändern, eine Adresse einer Geräteschnittstelle zuweisen oder lösen. Die primäre IP eines Geräts ist geschützt |
| **VLANs** | VLANs anlegen, bearbeiten und löschen; IDs und Namen werden je VLAN-Gruppe geprüft |
| **Devices** | Der Bestand mit Schnittstellen, 802.1Q-Modi, Adressen und VLANs. Die dokumentierten VLANs einer Schnittstelle lassen sich hier bearbeiten |

Das **Gateway** eines Netzes ist eine IP-Adresse darin mit dem Tag `lanvex-gateway`; Lanvex setzt und
liest sie für Sie. Standorte, Racks, Kabel, Leitungen, virtuelle Maschinen und alles Weitere
dokumentieren Sie direkt in **Lanvex Core**.

---

## 5. Server

Lanvex liest Linux-Server über SSH: Schnittstellen, Adressen, Routen, DNS, LLDP-Nachbarn,
DHCP-Leases und welcher Netzwerk-Stack was verwaltet (netplan mit systemd-networkd, NetworkManager,
ifupdown). Zum Lesen braucht es keine Rechte.

### Einen Server hinzufügen

1. Legen Sie auf dem Server ein Konto für Lanvex an: `sudo useradd -m -s /bin/sh lanvex`.
2. In Lanvex: *Servers, Add server*. Lanvex holt den SSH-Host-Schlüssel des Servers und zeigt seinen
   Fingerabdruck. Vergleichen Sie ihn mit `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`,
   ausgeführt auf dem Server über eine Konsole, der Sie bereits vertrauen, und bestätigen Sie ihn.
3. Lassen Sie Lanvex einen Schlüssel erzeugen. Tragen Sie die angezeigte Zeile
   (`restrict ssh-ed25519 …`) in `~lanvex/.ssh/authorized_keys` ein. `restrict` erlaubt keine
   Weiterleitungen und kein Terminal.
4. *Test connection*, dann den Server lesen.

Auf Appliances und NAS-Systemen mit schreibgeschütztem Root-Dateisystem nehmen Sie ein vorhandenes
Konto und legen den Schlüssel in dessen Home-Verzeichnis
([operations.md](../operations.md#adding-a-server)).

Ändert sich der Host-Schlüssel des Servers, verbindet sich Lanvex nicht mehr, bis Sie den neuen
Fingerabdruck auf dieselbe Weise geprüft haben.

### Lesen, Verlauf

Lanvex liest jeden Server einmal pro Stunde (je Server einstellbar: 15 Minuten bis täglich, oder aus)
und bei *Read now*. *History* listet jeden Zustand, in dem der Server war, und was sich geändert hat,
in Worten: Adressen, Verbindungen, MTU, Gateway, Routen, DNS. Nach zwei fehlgeschlagenen Anmeldungen
in Folge pausieren die geplanten Lesevorgänge von selbst, damit der Schutz des Servers gegen
Passwortraten (OpenSSH, fail2ban) Lanvex' Adresse nicht sperrt. Beheben Sie die Ursache und lesen Sie
einmal von Hand; der Zeitplan läuft dann weiter.

### Vergleich mit der Dokumentation

Verknüpfen Sie den Server mit seinem Gerät oder seiner virtuellen Maschine (Lanvex schlägt eines mit
gleichem Namen vor). Die Serverseite vergleicht dann:

| Was | Dokumentiert als |
|---|---|
| **Adressen** | IP-Adressen auf den Schnittstellen des Geräts |
| **Standard-Gateway** | Das Gateway des Netzes (Präfixes), in dem das Gateway des Servers liegt |
| **DNS-Server, Suchdomänen** | `dns_servers` und `dns_search` unter dem Schlüssel `lanvex` im Config Context des Geräts |
| **Statische Routen** | `routes` unter demselben Schlüssel: eine Liste aus `{"to": …, "via": …}` |

```json
{
  "lanvex": {
    "dns_servers": ["10.10.0.53", "10.10.0.54"],
    "dns_search": ["schule.example"],
    "routes": [{"to": "10.50.0.0/16", "via": "10.10.0.254"}]
  }
}
```

Einstellungen, die viele Server teilen, gehören in einen Config Context in Lanvex Core, zugeordnet
nach Standort, Rolle oder Tag; die eines einzelnen Servers in seine lokalen Config-Context-Daten.

Jeder Unterschied lässt sich über sein Menü in beide Richtungen schließen:

- **Record in Lanvex Core…**: Der Server hat recht. Lanvex zeigt genau, welche Änderungen es vornehmen
  würde (die Adresse auf der Schnittstelle anlegen, eine Präfixlänge korrigieren, eine Adresse lösen,
  das Gateway des Präfixes setzen, DNS und Routen in die lokalen Config-Context-Daten des Geräts
  schreiben), und nimmt genau diese vor. Haben sich Dokumentation oder Server inzwischen geändert,
  erhalten Sie stattdessen eine neue Vorschau.
- **Change the server to match…**: Die Dokumentation hat recht. Das öffnet eine Server-Änderung, in der
  der Unterschied schon zurückgenommen ist ([Kapitel 6](#6-netzwerkeinstellungen-eines-servers-ändern)).

Referenz: [operations.md](../operations.md#adding-a-server),
[changes.md](../changes.md#starting-from-a-difference).

---

## 6. Netzwerkeinstellungen eines Servers ändern

Lanvex ändert jeweils eine Schnittstelle: IPv4-Adressen oder DHCP, das Standard-Gateway, DNS-Server und
Suchdomänen, IPv6 und statische Routen. Außerdem legt es Bridges und Bonds an und entfernt sie.
Unterstützt:

| Netzwerk-Stack | Typisches System | Lanvex bearbeitet |
|---|---|---|
| netplan (systemd-networkd oder NetworkManager) | Ubuntu Server | Die netplan-Datei, die die Schnittstelle definiert |
| NetworkManager-Keyfiles | Fedora, RHEL | Das Profil der Schnittstelle |
| ifupdown | Debian | Die Abschnitte der Schnittstelle in `/etc/network/interfaces` oder einer Datei, die sie einbindet |

### Änderungen einschalten

Änderungen sind dreifach ausgeschaltet, und jede Stufe muss eingeschaltet werden:

1. **Für die Installation**: `LANVEX_AUTOMATION_ENABLED=true` in `.env`, dann `lanvexctl restart`.
2. **Das Hilfsprogramm auf dem Server.** Das Einzige, was Lanvex als root ausführt, ist ein kleines
   Hilfsprogramm. Auf der Serverseite, *Configuration changes*, *Install the helper*: *Copy to the
   server*, dann die angezeigten Befehle als Administrator auf dem Server ausführen. Der erste Befehl
   prüft die Prüfsumme der Datei: Brechen Sie ab, wenn er nicht `OK` ausgibt. Dann *Check now*.
3. **Für den Server**: *Turn on* im selben Abschnitt.

Bringt ein Lanvex-Update ein neues Hilfsprogramm mit, zeigt jeder Server *Outdated version*. Kopieren
Sie die neue Datei und führen Sie die Befehle erneut aus; Lanvex kann sie nicht selbst ersetzen.

### Eine Änderung vornehmen

1. Serverseite, **Change IP settings**. Wählen Sie die Schnittstelle; das Formular beginnt mit dem,
   was sie jetzt hat.
2. Bearbeiten Sie die Adressen (`192.0.2.10/24`) und das Gateway, oder wählen Sie DHCP. DNS, IPv6 und
   statische Routen bleiben, wie sie sind, solange Sie nicht *Also change …* ankreuzen.
3. Optional: einen Namen angeben, der danach noch aufgelöst werden muss.
4. **Prepare change.** Die Änderungsseite zeigt alte und neue Werte, den genauen Datei-Diff, die
   Befehle und Warnungen. Auf dem Server hat sich noch nichts geändert.
5. **Approve and apply**, mit dem eingetippten Servernamen. Mit
   `LANVEX_CHANGES_REQUIRE_SECOND_ADMIN=true` gibt ein anderer Administrator frei.

### Was dann geschieht

1. Das Hilfsprogramm prüft, dass sich die Datei seit der Vorschau nicht geändert hat, sichert sie und
   **stellt einen Rollback-Timer auf dem Server**, standardmäßig zwei Minuten, bevor es etwas schreibt.
2. Es schreibt die neue Konfiguration und wendet sie an.
3. Lanvex verbindet sich neu und prüft: Anmeldung möglich, Adressen vorhanden, Standardroute, Gateway
   antwortet (Ping oder wenigstens ARP), DNS, IPv6 und Routen wie angefordert.
4. Bestehen alle Prüfungen, bestätigt Lanvex, und der Timer wird aufgehoben. Schlägt eine fehl, kommt
   die vorige Konfiguration sofort zurück. Erreicht Lanvex den Server gar nicht, stellt der Timer des
   Servers sie von selbst wieder her.

Die Änderungsseite verfolgt jeden Schritt. Ihr Status ist immer einer von *Waiting for approval*,
*Approved, queued*, *Applying*, *Verifying*, *Applied and verified*, *Rolled back*, *Not applied*,
*Cancelled* oder *Needs attention*. Nichts heißt „angewendet", bevor der Server es bestätigt hat.
*Needs attention* bedeutet, dass Lanvex nicht herausfinden konnte, was geschehen ist; prüfen Sie den
Server an seiner Konsole, dann *Check again*.

Die Dokumentation ändert eine Server-Änderung nicht. Danach bietet die Änderungsseite an, das Ergebnis
festzuhalten.

### Sicherheitsregeln

- **Lanvex' eigene Verbindung.** Die Adresse, über die Lanvex verbindet, muss als statische Adresse auf
  der Schnittstelle bleiben. Um einen Server auf eine neue Adresse umzuziehen: die neue hinzufügen, die
  Serveradresse in Lanvex ändern, die alte in einer zweiten Änderung entfernen.
- **Indirekte Verbindungen** (Portweiterleitung, NAT, Lanvex auf dem Server selbst): Tragen Sie unter
  *Edit server* eine *Verification address* ein, eine eigene Adresse des Servers, die Lanvex direkt
  erreicht. Ohne sie werden Änderungen abgelehnt.
- **Der Server, auf dem Lanvex läuft**: Schalten Sie *My browser confirms changes* ein. Nach den
  Prüfungen fragt die Änderungsseite *Does everything still work?*; nur *Keep the change* bestätigt.
  Benutzen Sie Lanvex dafür über eine eigene Adresse dieses Servers.
- **Geschützte Schnittstellen** (*Edit server*, Namen oder Muster wie `vlan-*`): Schnittstellen, die ein
  anderes Werkzeug verwaltet. Lanvex ändert sie nie.

### Bridges und Bonds

*Add bridge or bond* legt eine Bridge oder einen Bond aus Schnittstellen ohne Adressen an (ungenutzte
Netzwerkports): Mitglieder, Bond-Modus (active-backup funktioniert mit jedem Switch; 802.3ad braucht
LACP an den Switchports) und IPv4 (keine, statisch oder DHCP). Das läuft als Änderung wie jede andere.
Von Lanvex angelegte Bridges und Bonds stehen auf der Serverseite und lassen sich wieder entfernen.

Referenz: [changes.md](../changes.md).

---

## 7. Netze auf einem Server: VLANs, DHCP, Zugriff, DNS-Filter

Ein Linux-Server kann der Router für mehrere Netze sein: je Netz eine VLAN-Schnittstelle auf seinem
Trunk zum Switch, eine Adresse darin, DHCP für die Geräte und Regeln, was sie erreichen dürfen.

1. **Netz dokumentieren**: Serverseite, *Networks*, *Add network*: VLAN-ID, Schnittstellenname, Trunk,
   die Adresse des Servers im Netz und der DHCP-Bereich. Das landet sofort in Lanvex Core.
2. **Vergleichen.** Jedes Netz zeigt, ob es schon auf dem Server ist, abweicht oder entfernt wird.
3. **Änderung vorbereiten.** Eine Änderung umfasst alle Netze des Servers; die Vorschau zeigt jede
   Datei und jeden Befehl.
4. **Freigeben.** Sie läuft wie jede Änderung, mit Prüfungen, dass jede VLAN-Schnittstelle ihre Adresse
   hat und DHCP antwortet.

| Thema | Kurz |
|---|---|
| **DHCP** | dnsmasq, oder Kea, wo es schon läuft. Der Bereich ist ein IP-Bereich mit dem Tag `lanvex-dhcp`, mit optionaler Lease-Dauer. Eine feste Adresse ist eine IP-Adresse mit MAC-Adresse. *Make fixed* macht aus einem Lease eine |
| **Was Geräte erreichen dürfen** | Standardmäßig nur DHCP und DNS auf dem Server. Je Netz: das Internet (maskiert), andere Lanvex-Netze, andere Netze oder einzelne Adressen mit Ports, Dienste auf dem Server. Mit firewalld, ufw oder Lanvex' eigener nftables-Tabelle |
| **DNS-Filter** | Gesperrte Domänen (mit allen Namen darunter) und Ausnahmen, je Netz. Braucht Lanvex' DHCP-Bereich im Netz |
| **Netzverwaltung** | *Import from Netzverwaltung* dokumentiert ihre Netze in Lanvex Core; *Prepare takeover* ersetzt sie in einer Änderung, und ein Rollback stellt sie genau so wieder her, wie sie war |

**Der Server braucht** NetworkManager, netplan oder ifupdown auf dem Trunk, den dnsmasq-Dienst (oder
Kea, einmal eingerichtet), und der Switch muss das VLAN zum Port des Servers führen. Lanvex schreibt
nur eigene Dateien und rührt Schnittstellen, Zonen oder dnsmasq-Dateien anderer Werkzeuge nie an.

Referenz: [networks.md](../networks.md).

---

## 8. Switches

Lanvex liest verwaltete Switches über SNMP: Ports, VLANs, Link-Aggregationen, die MAC-Adresstabelle und
LLDP-Nachbarn.

**Hinzufügen**: *Switches, Add switch*: Adresse, SNMP-Version und Zugangsdaten, *Test connection*,
*Add and read*. Nehmen Sie einen SNMPv3-Benutzer mit Authentifizierung und Verschlüsselung und einer
Leseansicht auf den ganzen Baum; SNMPv2c nur in einem Management-Netz. Schalten Sie LLDP auf den
Switches ein. Verknüpfen Sie den Switch mit seinem Gerät in der Dokumentation, damit seine Ports mit
den Kabeln verglichen werden.

**Was Sie sehen**: jeden Port mit Zustand, Geschwindigkeit und VLANs, und welches Gerät angeschlossen
ist, benannt nach den Servern, die Lanvex liest, und der Dokumentation (oder nach dem Hersteller, aus
der MAC-Adresse). Jeder Port wird mit dem dokumentierten Kabel verglichen: *Documented*, *Different
cable*, *No cable recorded* (*Record cable…* legt es nach einer Vorschau an), *Unknown device*,
*Several devices*.

**Die VLANs eines Ports ändern** (ungetaggtes VLAN und getaggte VLANs) braucht
`LANVEX_AUTOMATION_ENABLED=true`, *Allow VLAN changes from Lanvex* am Switch und SNMP-Schreibzugriff
auf die Q-BRIDGE-MIB. Lanvex zeigt die SET-Anfragen in Reihenfolge, lässt Sie den Portnamen eintippen,
prüft, dass sich der Port nicht geändert hat, schreibt Schritt für Schritt, liest den Port zurück und
nimmt die geschriebenen Schritte zurück, wenn der Switch einen ablehnt. Uplinks, Link-Aggregationen und
der Port, über den der Switch Lanvex erreicht, werden nie geändert. Lanvex speichert die Konfiguration
des Switches nicht: Speichern Sie sie am Switch, wenn sie einen Neustart überdauern soll.

Referenz: [switches.md](../switches.md).

---

## 9. Discovery und Topologie

**Discovery** listet, was Server und Switches um sich herum sehen, verglichen mit der Dokumentation:

- *Hosts seen*: Hosts aus den Nachbartabellen der Server und aus DHCP-Leases, mit dem Switchport, an
  dem sie hängen. Adressen, die der Dokumentation fehlen, lassen sich nach einer Vorschau festhalten.
- *Connections seen over LLDP*: das Gerät am anderen Ende jeder Serverschnittstelle. Ein fehlendes
  Kabel lässt sich festhalten.
- *Switch ports*: was an jedem Port hängt, standardmäßig nur dort, wo es von der Dokumentation abweicht.

LLDP braucht einen Empfänger auf dem Server: systemd-networkd (Ubuntu Server) hat einen; sonst
installieren Sie `lldpd` und nehmen Lanvex' Konto in die Gruppe auf, die ihn fragen darf (`adm` auf
Debian und Ubuntu).

**Topology** zeichnet Geräte, Standorte, Leitungen und die Kabel dazwischen, wie dokumentiert: Core über
Access über Servern, Server gestapelt unter ihrem Switch, Geräte, die Lanvex liest, markiert.
Verbindungen, die Switches sehen, aber kein Kabel festhält, erscheinen gepunktet. Dazu gehören eine
Suche, ein Detailbereich je Gerät und eine Kabelliste.

---

## 10. Überwachung und Alarme

Lanvex überwacht jeden Server und Switch, den es liest:

| Alarm | Wann |
|---|---|
| **Unreachable** | Der SSH-Port (Server) oder SNMP (Switches) hat dreimal in Folge nicht geantwortet. Geprüft alle zwei Minuten, ohne Anmeldung |
| **Read failed** | Ein Lesevorgang wurde abgelehnt: Anmeldung verweigert, Host-Schlüssel geändert, Adresse nicht erlaubt |
| **Drift** | Adressen, Gateway, DNS, Routen oder Lanvex-Netze weichen von der Dokumentation ab. Was nichts dokumentiert, ist kein Drift |
| **DHCP pool** | Ein Lanvex-Bereich ist zu 90 % belegt (kritisch, wenn voll) |
| **Unknown device** | Ein Gerät hat ein Lease oder erscheint an einem Switchport und ist nirgends dokumentiert |
| **Port down**, **Device moved** | An Switches: ein Uplink oder ein Port mit LLDP-Nachbarn ist ausgefallen; ein dokumentiertes Gerät hängt an einem anderen Port |
| **Change failed** | Eine Änderung wurde zurückgerollt, ist fehlgeschlagen oder braucht Aufmerksamkeit |

*Alerts* listet sie; die Navigation zählt die offenen, die niemand bestätigt hat. Prüfungen,
Schwellen und Ausnahmen (Schnittstellen, Netze, `vlan:<id>`) stehen unter *Settings, Monitoring*.

**Nachrichten** gehen raus, wenn ein Alarm beginnt, sich ändert und endet:

- **E-Mail**: ein SMTP-Server in `.env` (`LANVEX_SMTP_HOST`, `_PORT`, `_SECURITY`, `_USERNAME`,
  `_PASSWORD`, `_FROM`, und `LANVEX_PUBLIC_URL` für Links), dann Empfänger und die niedrigste Stufe
  unter *Settings, Monitoring*.
- **Webhooks** (*Settings, Webhooks*): JSON, ntfy, Gotify, Slack oder Mattermost, Discord, Microsoft
  Teams, jeweils mit ihrer niedrigsten Stufe. *Test* sendet eine Testnachricht.

Referenz: [monitoring.md](../monitoring.md).

---

## 11. Benutzer und Anmeldung

**Lokale Benutzer** verwalten Sie unter *Settings, Users*: anlegen, Rolle ändern, deaktivieren,
deaktivierte löschen. Lanvex behält immer mindestens einen aktiven Administrator. Auf der
Kommandozeile: `lanvexctl user create NAME [--admin]`, `deactivate`, `delete`.

**Verzeichnis und Single Sign-on.** Benutzer können sich auch mit ihrem Konto aus dem Active Directory
oder einem anderen LDAP-Verzeichnis anmelden (im selben Formular) oder über OpenID Connect (Keycloak,
Microsoft Entra ID, Authentik, …; eine Schaltfläche auf der Anmeldeseite). Gruppen bestimmen die Rolle,
bei jeder Anmeldung neu: Mitglieder der Administratorengruppe werden Administratoren, Mitglieder der
Read-only-Gruppe Read-only-Benutzer, alle anderen können sich nicht anmelden. Konten entstehen bei der
ersten Anmeldung und lassen sich in Lanvex deaktivieren. Lokale Benutzer funktionieren weiter, auch
wenn das Verzeichnis ausfällt; behalten Sie daher den ersten Administrator.

Die Einstellungen stehen in `.env` (`LANVEX_LDAP_*`, `LANVEX_OIDC_*`); *Settings, Sign-in* zeigt, was
eingerichtet ist, die Redirect-URI, die beim Anbieter einzutragen ist, und *Test directory*.

Referenz: [sign-in.md](../sign-in.md).

---

## 12. Lanvex betreiben

| Befehl | |
|---|---|
| `lanvexctl status` | Version, Verzeichnis, Adresse, Zustand jedes Containers |
| `lanvexctl logs [DIENST]` | Logs (`lanvex`, `core`, `core-worker`, …) |
| `lanvexctl backup [DATEI]` | Beide Datenbanken, die Uploads von Lanvex Core und `.env` in einer Datei |
| `lanvexctl restore DATEI` | In diese Installation oder nach einer Neuinstallation in eine frische |
| `lanvexctl update` | Sichert, installiert die neueste Version neben der laufenden, ersetzt die Container |
| `lanvexctl start`, `stop`, `restart` | `restart` übernimmt auch Änderungen an `.env` |
| `lanvexctl uninstall [--purge]` | Entfernt die Container; `--purge` auch die Daten (Sicherungen bleiben) |

**Sicherungen** enthalten `.env` mit den Schlüsseln, die die Zugangsdaten der Server entschlüsseln:
Bewahren Sie die Datei wie ein Passwort auf. `lanvexctl update` legt vor jedem Update eine an.

**Nach einem Update** lesen Sie die Versionshinweise in [CHANGELOG.md](../../CHANGELOG.md). Bringt es
ein neues Hilfsprogramm mit, installieren Sie es auf jedem Server neu, der Änderungen annimmt
([Kapitel 6](#änderungen-einschalten)).

**Einstellungen** sind Umgebungsvariablen in `.env`; alle stehen in
[configuration.md](../configuration.md).

---

## 13. Fehlersuche

| Meldung oder Symptom | Was zu tun ist |
|---|---|
| Die Anmeldung meldet, Sitzungen gingen nur über HTTPS | HTTPS über einen Reverse Proxy nutzen und seine Adresse in `LANVEX_TRUSTED_PROXIES` eintragen; oder mit reinem HTTP in einem vertrauenswürdigen Netz installieren |
| „Lanvex Core is not answering" | Es startet (der erste Start dauert Minuten) oder ist gestoppt: `lanvexctl status`, `lanvexctl logs core` |
| „NetBox denied the request" | Dem Token fehlt eine Berechtigung für diese Aktion ([operations.md](../operations.md#netbox-token-permissions)) |
| Server: *Host key changed* | Neu installierter Server oder ein Abfangversuch. Den neuen Fingerabdruck auf dem Server prüfen, dann *Verify a new host key* |
| Server: *Sign-in failed* | Der Schlüssel fehlt in `authorized_keys`, der Benutzer stimmt nicht, oder `AllowUsers` in der sshd-Konfiguration schließt ihn aus |
| Server: *Scheduled reads are paused* | Zwei fehlgeschlagene Anmeldungen in Folge, oder der Server lehnt Lanvex' Adresse vorerst ab. Ursache beheben, einmal von Hand lesen |
| Hilfsprogramm: *Not allowed through sudo*, *Python 3 missing*, *Outdated version* | Die Installationsbefehle von der Serverseite erneut ausführen; `python3` installieren |
| Änderung *Rolled back*, „Gateway … answers" fehlgeschlagen | Für das Gateway hat auf dieser Verbindung nichts geantwortet: Tippfehler, falsches Netz, oder das Gateway ist aus |
| Änderung *Needs attention* | Lanvex konnte nicht herausfinden, was geschah. `/var/lib/lanvex/changes/` auf dem Server prüfen, dann *Check again* |
| „The directory did not answer" | Verzeichnis nicht erreichbar, Dienstkonto abgelehnt oder Zertifikatsproblem; *Settings, Sign-in, Test directory* und das Log nennen den Grund |
| „Stored credentials could not be decrypted" | `LANVEX_SECRET_KEY` wurde geändert. Den alten Schlüssel in `LANVEX_SECRET_KEY_FALLBACKS` eintragen |

Mehr: [operations.md](../operations.md#troubleshooting), [core.md](../core.md#troubleshooting),
[changes.md](../changes.md#troubleshooting), [sign-in.md](../sign-in.md#troubleshooting).
Was getestet ist und was nicht: [status.md](../status.md).

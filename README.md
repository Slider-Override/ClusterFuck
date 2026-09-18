
<img width="1539" height="1256" alt="ClusterFuck" src="https://github.com/user-attachments/assets/08a2617e-f407-4edd-b6ae-1b023b688cc0" />



# ClusterFuck · Version 1

Gleichberechtigte Docker-Nodes mit eigenem WebUI für iperf3-Netzwerktests. Jeder Node kann Tests auf sich selbst und anderen verbundenen Nodes starten. Kein dauerhafter Master und kein zentraler Dienst.

## Funktionen

- Einzel- und Teamstarts mit eigener Zielauswahl je Teilnehmer.
- Live-Kurven und Ergebnisse aller direkt konfigurierten Nodes auf jedem WebUI.
- Eigener iperf3-Server für Tests zwischen Standorten.
- Bearbeitbare Ziele (Hostname/IPv4/IPv6 und Port), Nodes und gespeicherte Testprofile.
- Dauer, Messintervall, Streams, TCP/UDP, UDP-Bitrate, Upload/Download/beide Richtungen, Anlaufphase, IP-Version, Zero-copy, TCP No-delay.
- Manuelle Pause und wöchentliche Sperrzeiten einschließlich Regeln über Mitternacht und Sommerzeit.
- Sperren verhindern ausgehende Tests und stoppen den eingebauten iperf3-Server einschließlich laufender eingehender Tests.
- Gegenseitige Prüfung der öffentlichen API-Adresse und des iperf3-TCP-Endpunkts.
- HTTPS, Zertifikat-Pinning zwischen Nodes, WebUI-Anmeldung und lokale Speicherung ohne Geheimnisse im Repository/Image.
- Ein Container pro normaler Installation. Zusätzliche Nodes werden ausdrücklich separat gestartet.

## Voraussetzungen

Linux mit Docker Engine und Docker Compose v2. Unterstützte Image-Architekturen: amd64 und arm64. Für Teamstarts Systemuhren per NTP synchronisieren. Jeder Standort braucht einen erreichbaren API-Port und die gewünschten iperf3-Ports. DDNS aktualisiert die Adresse; Portfreigaben und Firewallregeln richtest du am jeweiligen Standort ein.

## Direkt aus dem Repository bauen (ein Container)

```bash
git clone https://github.com/Slider-Override/ClusterFuck.git
cd ClusterFuck
cp .env.example .env
docker compose -p clusterfuck-a up -d --build
```

Bei einem privaten Repository wird für das Klonen dein normaler GitHub-Zugang benötigt. Keine Zugangsdaten in die Clone-URL oder `.env.example` eintragen.

WebUI: **https://DEINE-HOST-IP:8443**. Standardmäßig wird ein eigenes HTTPS-Zertifikat erstellt. Du kannst stattdessen ein vertrauenswürdiges Zertifikat als `/data/tls.crt` und `/data/tls.key` bereitstellen. Im lokalen Test musst du das erzeugte Zertifikat im Browser selbst prüfen und ihm vertrauen. Die automatisch erzeugten Zugangsdaten liest du ausschließlich lokal:

```bash
docker compose -p clusterfuck-a exec node python -c "import json; print(json.load(open('/data/credentials.json'))['password'])"
```

Mit dem Passwort anmelden. Andere Nodes, Testziele und Sperrzeiten im WebUI konfigurieren. Das Volume enthält Passwort, Peer-Tokens, TLS-Schlüssel, Konfiguration und Ergebnisse; es bleibt bei Container-Neuerstellung erhalten.

## Ports und Name auswählen

In `.env`:

```dotenv
NODE_NAME=Standort-A
WEB_HOST_PORT=8443
IPERF_HOST_PORT=5201
WEB_INTERNAL_PORT=8443
IPERF_INTERNAL_PORT=5201
BIND_ADDRESS=0.0.0.0
```

Host- und Containerports können unabhängig geändert werden. Router-Portfreigaben müssen auf die gewählten Host-Ports zeigen. Für TCP-Tests iperf-TCP freigeben; für UDP-Tests zusätzlich iperf-UDP. Die API verwendet TCP mit HTTPS. Kein Docker-Socket, kein privilegierter Container und kein Host-Netzwerk erforderlich.

## Einen zweiten Test-Node selbst starten

Die gleiche Compose-Datei ein zweites Mal unter einem anderen Projektnamen starten. Zwei Projektnamen bedeuten zwei unabhängige Volumes. Ein zweiter Node wird durch die normale Installation niemals automatisch erstellt.

**Einmalig ein gemeinsames Testnetz erstellen:**

```bash
docker network create clusterfuck-test
```

**Node A** (z.B. nach dem normalen Start):

```bash
docker network connect --alias cf-a clusterfuck-test "$(docker compose -p clusterfuck-a ps -q node)"
```

**Node B ausdrücklich separat starten**; die Ports gelten nur für diesen Befehl:

```bash
NODE_NAME=Standort-B WEB_HOST_PORT=8444 IPERF_HOST_PORT=5202 \
docker compose -p clusterfuck-b up -d --build
docker network connect --alias cf-b clusterfuck-test "$(docker compose -p clusterfuck-b ps -q node)"
```

Die beiden WebUIs erreichst du unter `https://HOST-IP:8443` und `https://HOST-IP:8444`. Das Passwort für B liest du mit dem Passwortbefehl oben und `-p clusterfuck-b` aus.

Im WebUI von B dessen Pairing-Daten anzeigen. Auf A einen Peer mit Adresse `https://cf-b:8443`, Token und Fingerabdruck von B anlegen. Umgekehrt auf B `https://cf-a:8443` mit den Pairing-Daten von A eintragen. Intern verwenden beide **8443 für die API und 5201 für iperf3**, unabhängig von den Host-Ports 8443/8444 und 5201/5202.

Auf A folgende Ziele erstellen:

| Name | Host | Port | Zugehöriger Node |
| --- | --- | --- | --- |
| Standort B | cf-b | 5201 | Peer B |
| Standort A | cf-a | 5201 | Dieser Node |

Für einen Teamlauf auf A: Teilnehmer A → Ziel B und Teilnehmer B → Ziel A auswählen. Jeder Teilnehmer erhält eigene Optionen für das Ziel; die Testparameter gelten gemeinsam für diesen Lauf. Ziele und Peerlisten werden lokal konfiguriert, nicht automatisch synchronisiert. Damit B auch Gruppen starten kann, dieselben Ziele mit der passenden Node-Zuordnung auf B anlegen.

**Wichtig nach Neuerstellung der Testcontainer:** Die manuell ergänzte Netzwerkverbindung wird nicht in Compose gespeichert. `docker network connect` erneut ausführen, wenn ein Container ersetzt wurde. Ein reiner Neustart erhält die Verbindung.

Diese Simulation prüft die Software über Docker-Netzwerke, keine tatsächliche WAN-Bandbreite oder Router-Portweiterleitung.

## Nodes an zwei echten Standorten verbinden

1. Gleiche Anwendung an beiden Standorten starten, jeweils mit eigenem Namen und Volume.
2. API und iperf-Ports weiterleiten. DDNS-Hostname oder feste öffentliche IP verwenden.
3. Pairing-Daten über einen sicheren separaten Weg austauschen.
4. Auf A die HTTPS-Adresse von B mit dessen Token und Zertifikatfingerabdruck eintragen; auf B umgekehrt.
5. Auf jedem Node eigene öffentliche API-Adresse, iperf-Host und öffentlichen iperf-Port eintragen.
6. Testziele anlegen; eigene Nodes zuordnen, damit deren Empfangs-Sperrzeiten vor dem Start geprüft werden.
7. Verbindungstest starten. Der andere Node prüft die bereits konfigurierte öffentliche Adresse aus seiner Perspektive.

Alle gewünschten Nodes müssen direkt aufeinander konfiguriert werden, wenn jedes WebUI alle Standorte anzeigen und steuern soll. Es gibt keine automatische Erkennung, keinen Relay und keine zentrale Registry. Ohne erreichbaren Peer ist die externe Erreichbarkeit noch nicht geprüft. Der iperf-Check prüft den TCP-Protokollhandshake und meldet bereit/belegt/nicht erreichbar; er misst keine Bandbreite und bestätigt keine UDP-Portfreigabe.

## Sperrzeiten und Bereitschaft

Im WebUI unter Konfiguration eine Sperrzeit hinzufügen, z.B. Freitag 19:00–23:00, Zeitzone `Europe/Berlin`, Bezeichnung Gaming. Start inklusive, Ende exklusiv. Für 23:00–02:00 gehört die Regel zum Tag des Beginns. Gleiche Start- und Endzeit bedeutet eine 24-Stunden-Sperre ab der Startzeit des gewählten Tages.

- Tests werden abgelehnt, wenn sie einschließlich Anlaufphase und 10 Sekunden Verbindungsreserve eine Sperrzeit berühren würden.
- Neue Sperren oder manuelle Pausen beenden laufende Tests; der iperf3-Server wird innerhalb ungefähr einer Sekunde gestoppt.
- Während der Sperre bleibt das WebUI erreichbar. Die Sperre lässt sich durch andere Nodes nicht überschreiben.
- Auch separat gestartete iperf-Clients können den eingebauten Server während der Sperre nicht nutzen.
- Externe Server ohne ClusterFuck können keine Sperrzeiten melden. Deren Verwaltung bleibt beim Betreiber.

## Teamstart und Live-Werte

Der auslösende Node prüft die Teilnehmer und zugeordnete Empfänger, reserviert die Teilnehmer und bestätigt dann den gemeinsamen Startzeitpunkt, etwa 15 Sekunden in der Zukunft. Jeder Node startet seinen eigenen Prozess. Die tatsächliche Startzeit wird gespeichert. Abweichungen der Systemuhren über eine Sekunde verhindern den Teamstart.

Bei fehlgeschlagener Vorbereitung wird der Lauf abgebrochen und erfolgreiche Reservierungen werden zurückgenommen. Ein Netzwerkabbruch genau während der Startbestätigung kann trotzdem einen Teilstart verursachen; ein verteiltes System ohne zentralen Dienst garantiert keinen atomaren Start. Vorbereitete, unbestätigte Reservierungen laufen nach 30 Sekunden ab. In V1 beendet jeder Node seinen bereits bestätigten Test auch bei Verlust der Steuerverbindung selbständig. Abbruch ist für jeden noch erreichbaren Teilnehmer im WebUI möglich.

Ein ausgehender Test je Node; gleichzeitig kann der eingebaute Server einen eingehenden Test bedienen. Mehrere unabhängige Tests gegen denselben externen Server brauchen unterschiedliche Ports mit jeweils eigener iperf3-Serverinstanz. `-P` bezeichnet mehrere Streams innerhalb eines Tests, nicht mehrere unabhängige Tests. V1 stellt einen eigenen iperf3-Serverport je Node bereit.

Live-Werte werden im Messintervall erzeugt und im WebUI ungefähr sekündlich aktualisiert. Bei nicht erreichbaren Peers werden deren Live-Werte nicht als aktuell angezeigt. Nach Wiederverbindung wird der aktuelle Zustand geladen. Lokal bleiben die letzten 100 Testläufe gespeichert; das WebUI zeigt je Node die letzten 30. Abgeschlossene Kurven werden auf maximal 120 Punkte verdichtet. Unterstützte Optionen stehen im Formular; beliebige Shellbefehle oder frei eingegebene iperf-Argumente werden nicht ausgeführt.

## Image von GitHub installieren und aktualisieren

Der Workflow `.github/workflows/release.yaml` testet die Anwendung und baut anschließend Images für amd64 und arm64 in GHCR. Ein Push auf `main` veröffentlicht `edge`; ein Tag `v1.0.0` veröffentlicht `1.0.0` und `1.0`. Vor dem Veröffentlichen muss auch der echte Docker-/iperf3-Smoke-Test bestanden sein.

Das Image ist bei der ersten Veröffentlichung in GHCR zunächst privat. Für Downloads ohne Login muss die Package-Sichtbarkeit in GitHub ausdrücklich auf öffentlich gesetzt werden. Die Repository-Sichtbarkeit kann unabhängig davon privat bleiben. **Auch ein öffentliches Image enthält Anwendungscode; es enthält keine Laufzeitkonfiguration oder Zugangsdaten.**

Nach erfolgreichem Image-Build in `.env` den vorhandenen Tag einstellen und starten:

```bash
docker compose -p clusterfuck-a pull
docker compose -p clusterfuck-a up -d --no-build
```

Zum direkten Download brauchst du nur `compose.yaml` und deine lokale `.env`. Ein privates Image erfordert einen lokalen `docker login ghcr.io` mit Leseberechtigung; dessen Zugangsdaten gehören nicht in dieses Repository. In GitHub Actions verwendet der Workflow das automatisch bereitgestellte `GITHUB_TOKEN`, ohne einen Tokenwert in Dateien abzulegen.

Für Updates den gewünschten vorhandenen Image-Tag in `.env` einstellen und dieselben zwei Befehle ausführen. Das Volume bleibt erhalten. Zum Zurücksetzen der Anwendung einen vorherigen Image-Tag verwenden. `docker compose down -v` löscht das lokale Volume samt Einstellungen und Zugangsdaten.

## Sicherheit und lokale Daten

Die WebUI ist passwortgeschützt. Peer-Tokens können Status lesen und Tests steuern, aber keine Zugangsdaten abrufen oder die Node-Konfiguration verändern. Browseränderungen benötigen eine Sitzung und einen CSRF-Token. Peer-Zertifikate werden **vor** der Übertragung des Tokens gegen den hinterlegten SHA256-Fingerabdruck geprüft. Bei Zertifikatswechsel muss der neue Fingerabdruck auf allen Peers aktualisiert werden. Pairing-Daten nicht über eine ungesicherte Verbindung austauschen.

HTTP ist nur für ausdrücklich lokale Tests über `TLS_ENABLED=false` **und** `ALLOW_INSECURE_PEERS=true` möglich. Standard bleibt HTTPS. Die Container laufen ohne Root und zusätzliche Linux-Capabilities. Beim Linux-Start liegen lokale Geheimnisdateien mit Modus 600 in einem Verzeichnis mit Modus 700. Geheimnisdateien und lokale `.env` sind aus Git und Docker-Build-Kontext ausgeschlossen. Konfigurationsantworten enthalten keine gespeicherten Peer-Tokens. Kein Telemetrie- oder externer Erreichbarkeitsdienst.

Der iperf3-Datenport verwendet standardmäßig keine eigene Authentifizierung. Außerhalb von Sperrzeiten kann ein erreichbarer direkter iperf3-Client ihn nutzen. Bei Bedarf den Port per Firewall auf deine Standorte beschränken. Dies betrifft nicht die authentifizierte Steuerungs-API.

## Entwicklung und Tests

Python 3.12 oder neuer, ohne zusätzliche Python-Abhängigkeiten unter Linux; Zeitzonendaten müssen vorhanden sein. Unter Windows wird zusätzlich `tzdata` benötigt. iperf3 muss `--json-stream` unterstützen; das Dockerfile prüft dies beim Build.

```bash
python -m unittest discover -s tests -v
node --check clusterfuck/static/app.js
docker compose config --quiet
docker build -t clusterfuck:test .
python tests/docker_smoke.py
```

Der letzte Befehl ist ausschließlich ein Test: Er erstellt zwei temporäre Container und ein Testnetz und entfernt sie danach. Die normale Installation startet weiterhin nur einen Node. Der Smoke-Test prüft echte iperf3-Messungen, HTTPS-Pinning, Teamstarts mit Live-Werten, gegenseitige Verbindung und die Sperrung direkter eingehender Tests.

## Quellen

- [iperf3-Aufruf und Optionen](https://software.es.net/iperf/invoking.html)
- [Docker-Compose-Netzwerke](https://docs.docker.com/compose/how-tos/networking/)
- [GitHub Container Registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)

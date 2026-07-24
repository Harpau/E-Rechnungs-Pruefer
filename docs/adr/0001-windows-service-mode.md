# ADR 0001: Windows-Dienst als getrennte Betriebsart

- Status: angenommen
- Datum: 2026-07-22

## Kontext

Der vorhandene Windows-Installer installiert eine nicht privilegierte Desktop-/Tray-Anwendung in das Profil des
angemeldeten Benutzers. Der optionale HKCU-Autostart beginnt erst nach der Anmeldung. Für unbeaufsichtigte lokale
Automatisierungen wird zusätzlich ein Backend benötigt, das bereits vor einer Benutzeranmeldung über den Windows
Service Control Manager (SCM) läuft. Desktop und Dienst verwenden denselben FastAPI-/Prüfcode und denselben festen
Loopback-Port, dürfen aber niemals gleichzeitig als Backend laufen.

## Entscheidung

### Paketierung und Konto

Es gibt zwei getrennte, signierte Installer mit unterschiedlichen App-IDs:

1. Der bestehende benutzerbezogene Desktop-/Tray-Installer bleibt ohne Administratorrechte und mit optionalem
   HKCU-Autostart erhalten.
2. Ein administrativer Dienst-Installer installiert unveränderliche Dateien unter `%ProgramFiles%` und
   Maschinenzustand unter `%ProgramData%\E-Rechnungs-Pruefer`.

Der Dienst läuft als `NT AUTHORITY\LocalService`, nicht als `LocalSystem`. Zusätzlich wird der eigene Service-SID
`NT SERVICE\ERechnungsPrueferService` aktiviert. Konfiguration, Token und technische Logs erhalten geschützte DACLs nur
für diesen Service-SID, `SYSTEM` und lokale Administratoren. Eine konkret ermittelte Node-RED-Identität darf
explizit mit Leserechten am Token provisioniert werden; Gruppen wie `Users`, `Authenticated Users` oder
`Everyone` erhalten keinen Tokenzugriff.

### Konfiguration und Token

Die Maschinenkonfiguration ist eine streng validierte JSON-Datei. Sie erlaubt keine Bind-Adresse; der Dienst
bindet fest an `127.0.0.1`. Konfiguration und API-Token werden vor dem ersten Import von `app.main` in die
Prozessumgebung übernommen, weil Settings und Sicherheitsmiddleware beim Import ausgewertet werden.

Tokenanlage und -rotation verwenden eine Datei im selben Verzeichnis, setzen vor der Veröffentlichung die
endgültige DACL und ersetzen das Ziel atomar. Updates erhalten Konfiguration und Token. Ein vorhandenes
Desktop-Token wird nur nach ausdrücklicher Zustimmung validiert, kopiert und mit der Maschinen-DACL neu geschützt.
Die Deinstallation erhält den Maschinenzustand standardmäßig und löscht ihn nur nach einer klaren Bestätigung.
Der ProgramData-Pfad stammt aus der Windows-Known-Folder-API. Besitzer, Reparse-Points/Junctions und Hardlinks
werden vor einer Übernahme geprüft; administrative Initialisierung normalisiert die Besitzer der dauerhaften
Konfigurations- und Tokendateien auf die lokale Administratorengruppe.

### Browserzugang und IPC

Ein kleiner interaktiver Startmenü-Client spricht über eine lokale Named Pipe mit dem Dienst. Die Pipe lehnt
Remoteclients ab; ihre DACL authentifiziert interaktive Windows-Anmeldungen und den Service-SID. Sie unterstützt
nur den fest definierten Befehl zum Öffnen der Oberfläche. Die erste Instanz bleibt für die gesamte Dienstlaufzeit
offen, und Clientrechte schließen das Erzeugen einer konkurrierenden Pipe-Instanz aus.

Der Dienst antwortet mit einem zufälligen Bootstrap-Token, das höchstens 60 Sekunden gilt und genau einmal
verwendet werden kann. Der HTTP-Bootstrap tauscht es gegen ein zufälliges, `HttpOnly`-/`SameSite=Strict`-Cookie
mit begrenzter Lebensdauer und leitet auf eine tokenfreie URL um. Das dauerhafte Bearer-Token wird weder über die
Pipe noch in einer URL, einem Browser-Cookie, Browser-Speicher oder normalen Logs veröffentlicht.

### Ausschluss, SCM und Beenden

Desktop und Dienst konkurrieren um einen maschinenweiten, explizit geschützten Backend-Mutex. Zusätzlich wird der
konfigurierte feste Port vor dem App-Import ausschließlich auf `127.0.0.1` reserviert. Bereits ein einzelner
Konflikt bricht den Start ab; es gibt keinen Ausweichport und keinen parallelen Backendbetrieb.

Die Dienstimplementierung verwendet pywin32 direkt für SCM-Status und Steuerbefehle. Gegenüber einem externen
Wrapper vermeidet dies eine weitere ausführbare Vertrauens- und Signaturgrenze und erlaubt korrekte
`START_PENDING`-, `RUNNING`-, `STOP_PENDING`- und `STOPPED`-Meldungen. Das Desktop-Shutdown-Ereignis wird nicht zur
Dienststeuerung verwendet. Unerwartet beendete Web- oder IPC-Threads führen zu einem technischen Dienstfehler und
damit in die SCM-Recovery. Der Dienst öffnet in Session 0 weder Tray, Browser noch MessageBox.

Standard ist `Automatic (Delayed Start)`; eine abgewählte Installeroption konfiguriert `Manual`. Für die ersten
beiden unerwarteten Ausfälle wird eine verzögerte Wiederherstellung durch Neustart eingerichtet. Technische Logs
rotieren unter `%ProgramData%` und enthalten keine Zugriffstokens, Authorization-Header, Rechnungsbytes oder
fachlichen Rechnungsinhalte.

Beim Stoppen werden keine neuen Requests angenommen und Uvicorn wird geordnet beendet. Laufende KoSIT-Aufrufe
bleiben durch das konfigurierte KoSIT-Zeitlimit begrenzt; die Dienstkonfiguration begrenzt dieses auf 300 Sekunden.
Die SCM-Wartegrenze beträgt KoSIT-Zeitlimit plus 15 Sekunden. Ein Überschreiten ist ein technischer Dienstfehler,
keine KoSIT-Ablehnung der Rechnung.

### Installation, Update und Migration

Der Dienst-Installer prüft vor Änderungen Dienstzustand, Desktopinstallationen und produktspezifische Autostarts
aller lokalen Benutzerprofile einschließlich nicht geladener und Entra-ID-Profile, laufende Altprozesse in allen
Sitzungen, Portbelegung, ProgramData und vorhandene Tokens. Die einzige zulässige Quarantänekopie wird aus dem
Migrationsbeleg des ursprünglichen interaktiven Kontos abgeleitet, nach der erhöhten Prüfung in einem festen
Installer-Zustand gegen Änderung versiegelt und nicht erneut aus dem HKCU eines abweichenden UAC-Administratorkontos
ermittelt. Die erhöhte Inventur akzeptiert ausschließlich feste lokale Laufwerke und hält jede Pfadkomponente
no-follow ohne Schreib- oder Löschfreigabe geöffnet. Offline-Hives werden unter diesem Lock in eine administrative
Momentaufnahme kopiert, sodass `RegLoadKey` niemals das benutzerschreibbare Original öffnen muss. UNC-/Gerätepfade,
Reparse-Points/Junctions, Hardlinks und nicht eindeutig prüfbare `NTUSER.DAT`-/`NTUSER.MAN`-Hives führen zum
geschlossenen Abbruch. Eine
weitere benutzerbezogene Altinstallation blockiert den Moduswechsel geschlossen. Ein Wechsel vom Desktopmodus
beendet die Tray-App des ursprünglichen Kontos kontrolliert,
entfernt den HKCU-Autostart, deaktiviert die alte Backend-EXE transaktional und übernimmt ein Token nur mit
Opt-in. Bei Updates wird ein laufender Dienst über SCM gestoppt und `STOPPED` abgewartet, bevor ein vollständig
entpackter Staging-Baum atomar aktiviert wird; der vorige Baum bleibt bis zum Commit rollbackfähig. Starttyp,
Beschreibung, Service-SID und Recovery werden über SCM-APIs gesichert. Nur ein zuvor laufender Dienst wird wieder
gestartet. Bei Deinstallation wird der Dienst gestoppt und vollständig aus dem SCM entfernt. Eine ausdrücklich
gewählte Löschung des Maschinenzustands inventarisiert und verifiziert die ausschließlich bekannten
ProgramData-Dateien erneut und bricht bei fremden Einträgen, Pfadumleitungen, Hardlinks oder zu breiten DACLs ab;
unbekannte Inhalte werden nie rekursiv gelöscht.

## Folgen

- Desktop-, API-, PDF-, XML-, KoSIT- und Node-RED-Verträge bleiben gemeinsam und unverändert.
- pywin32 wird ausschließlich für die Windows-Dienstartefakte als zusätzliche Laufzeitkomponente gebündelt.
- Die konkrete produktive Node-RED-Serviceidentität kann nicht aus dem Repository abgeleitet werden und muss vor
  der Tokenprovisionierung auf dem Zielsystem geprüft werden. Soll der gesamte Ablauf vor Anmeldung funktionieren,
  muss auch Node-RED als Windows-Dienst unter dieser dokumentierten Identität laufen.
- Wirksame DACLs, SCM-Konfiguration, Updates, Deinstallation, Authenticode und Start vor Anmeldung benötigen neben
  plattformunabhängigen Regressionstests weiterhin eine entbehrliche Windows-VM und einen manuellen Neustarttest.

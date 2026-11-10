# LAN demo build - test report (2026-11-10)

**Automated LAN test: 39 / 39 passed - on ONE PC (real sockets and real mDNS multicast on the Wi-Fi address 10.0.0.124, three separate packaged-app instances). This is NOT a physical multi-laptop test; nothing has been tested on separate laptops.**

Final build: `20261110-142348-v1.3.0` (build info 2026-11-10 14:23 IST, commit 8f7e66b + uncommitted work). Evidence: `test-evidence\lan-e2e.out`, screenshots, `audit-package-final.json`.
Earlier attempts are kept: a driver error (report-id lookup), a test-method error (`crypto.subtle` does not exist on a plain-HTTP LAN origin; hashing moved to the test driver) and a wrong assertion (the ordinary storage view is 200 by design; test corrected to check the real requirement) - none was an application defect.

## Passed (automated, final build)
* Host launcher (Start-DemoHost.ps1): backend on the Wi-Fi address (not loopback only), dedicated port 47831 (not 8001/55686/58732), mDNS advertisement, per-participant accounts created, real STAGING Google Drive.
* Three packaged clients with NO settings and NO address typed found the host by mDNS, verified it, and showed its login page at the host's LAN address; no company settings file, no local database or backend on any client.
* Independent accounts: three different random passwords (none equals the host admin or the development default), each participant forced to choose their own, each signs in with their own account; header chip `Staging . host . Connected`.
* A uploads a synthetic report -> `Stored centrally`; B sees it without uploading; uploader recorded as Priya; B uploads the exact same file -> only ONE canonical document; the duplicate is recorded once with Arun as uploader and the original referenced; C sees the same duplicate information and page.
* Processing completed centrally (final state `REVIEW_REQUIRED` for the blank synthetic PDF); all three clients see identical document, extraction, storage, expense-report/audit and exception results (1 exception).
* C downloads the central copy: SHA-256 equals the original. Excel generated centrally (copy kept in the Drive `Excel Exports` folder); B and C download the identical valid workbook.
* Role limits: a participant is refused storage jobs, users and audit logs (403); the ordinary storage view contains no Drive id/location/checksum/URL.
* Host stopped: all three clients show the outage alert, none switched to a local database. Host restarted with `-Resume` (same database, same Drive folder): all three recover by themselves; the shared document and its central copy are still there.
* Package: audit PASSED, 0 of 8 staging secret values in the installer content, the only endpoint literal is the pre-existing dev-mode `localhost:5200` (no 55686, no test IP, no demo port).
* Electron unit tests 48/48 (discovery, manual-address validation, verified-host filtering, demo-defaults precedence). Earlier backend regression results (SQLite/PostgreSQL) from the previous report are unchanged: no backend code was changed for this build.
* Firewall script dry-run (`-WhatIf`): shows the narrow rule (TCP, one port, Private profile, local subnet, python.exe only); nothing was created. Not run as administrator.

## NOT tested / limits
* No physical multi-laptop test; Wi-Fi client isolation, other laptops' firewalls and company multicast filtering are unknown until the procedure in `LAN-DEMO-GUIDE.md` (section D) is run.
* This PC's Wi-Fi profile is **Public**: other laptops could NOT connect to a host on it until the network is Private and the narrow rule exists (administrator). The host launcher prints this warning.
* The manual-address page was unit-tested (validation) but not driven through the UI in an automated run; the firewall rule was not created (no administrator rights).
* Plain HTTP, unauthenticated advertisement: synthetic documents and demo-only passwords only. Not the company server; no company URL configured; production authentication untouched.

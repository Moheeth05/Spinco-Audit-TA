# Spinco TA Audit 1.3.0 - LAN DEMO guide (staging, synthetic documents only)

One designated laptop is the **host** (backend + database + connection to the STAGING Google Shared Drive). Every other laptop installs the same setup EXE and finds the host by itself.
Not the company server; no company URL is configured anywhere. Plain HTTP on the local network: **use synthetic documents and your own demo account only.**

## A. Participant (every other laptop) - 3 steps
1. Run `Spinco TA Audit Setup 1.3.0.exe` (or the portable `Spinco TA Audit 1.3.0.exe`). It is **unsigned**: SmartScreen may warn ("More info" > "Run anyway"), IT can allow it by SHA-256 (`release\SHA256SUMS.txt`).
2. Be on the **same Wi-Fi** as the host laptop. Open **Spinco TA Audit**. A short notice explains it is a demo connection; the app searches for the host (about 8 seconds) and shows the sign-in page. The title reads `[DEVELOPMENT - INSECURE - SYNTHETIC DATA ONLY]` and the header chip shows `Staging . <host address> . Connected`.
3. Sign in with **your own** account (the host gives you your line only: e-mail + one-time password). The first sign-in makes you choose your own new password.

**If the app says "No Spinco demo host was found on this network":** ask the host for the **manual address** (shown on the host laptop, like `10.0.0.124:47831`), type it into the box on that page and press *Connect*. It is remembered.
Still blocked? Run `.\Test-DemoNetwork.ps1 -HostAddress <address>` (no administrator needed): it says whether the Wi-Fi isolates laptops, whether the host's firewall blocks the port, or whether the backend is not running.

## B. Host laptop
Needs: the demo build folder, `D:\Bills\backend` with its virtual environment, the staging sign-in files in `D:\SPINCO-SECRETS` (they stay on this laptop only; participants never receive them).
1. **Network profile:** the Wi-Fi network must be **Private** (Settings > Network > Wi-Fi > the network > Network profile). On a **Public** profile Windows blocks every incoming connection and no other laptop can connect. Changing it may need administrator/company approval.
2. **One narrow firewall rule** (administrator, once): `.\Add-DemoFirewallRule.ps1` - inbound TCP, the demo port only, **Private profile only**, **local subnet only**, the backend's python.exe only. Use `-WhatIf` first to see it; `-Remove` deletes it afterwards. Windows Firewall stays ON; nothing else is touched. Discovery (mDNS, UDP 5353) uses Windows' built-in rules; if company policy blocks it, participants use the manual address.
3. **Start:** `.\Start-DemoHost.ps1 -Participants "Priya,Arun,Meena,Rahul"` - it creates a new demo database and a new `SPINCO_DEMO_<date>` folder in the staging Shared Drive, listens on the dedicated demo port (default **47831**, next free port if taken; never 8001/55686/58732), advertises the host (mDNS) and prints the host addresses, the **manual address** and warnings (Public profile, missing firewall rule).
4. **Restart after a crash/reboot:** `.\Start-DemoHost.ps1 -Resume D:\SpincoTA-artifacts\central-staging\lan-demo-<time>` - same database, same Drive folder, same accounts; clients reconnect by themselves.
5. Stop: Ctrl+C (backend and advertisement stop together).

## C. Individual demo accounts
`Start-DemoHost.ps1` creates one account per name in `-Participants` through the backend's own admin interface: e-mail `<name>@demo.spinco.local`, role **AUDITOR** (upload, view, re-process, validate, export - no administration), and its **own random password** (never shared, never a default). They are printed once on the host console and saved in `<run folder>\participants.csv` (host laptop only). Hand each person **only their own line**.
The host's administrator password is random too (`<run folder>\host-admin.txt`) and is not given to participants. Every upload records the uploader's own name; duplicates show who uploaded them and when.
Add people later: `D:\Bills\backend\.venv\Scripts\python.exe create_demo_users.py --url http://127.0.0.1:<port> --admin-email intern@spincotech.com --admin-password <host admin password> --names "New Person" --out more.csv`.

## D. Physical multi-laptop test procedure (NOT yet performed - do this before relying on the demo)
Use the real Wi-Fi, 1 host + at least 2 participants.
1. Host: set the Wi-Fi to Private, add the firewall rule (section B), run `Start-DemoHost.ps1`; note the manual address and any warnings.
2. Each participant: `Test-DemoNetwork.ps1 -HostAddress <manual address>` - expect `TCP: connected` and `Spinco backend answered`. **A timeout here with the host's rule present means Wi-Fi client isolation** (a guest/corporate Wi-Fi that separates laptops): use another network or a phone hotspot.
3. Each participant: install the EXE, open the app. Expected: automatic discovery (or manual address), login page, `Connected` chip.
4. Sign in with each person's own account. A uploads a synthetic PDF; B sees it; B uploads the same file -> *Duplicates* on every laptop shows the attempt with B's name; C downloads the document; A generates the Excel export and B and C download it.
5. Host: Ctrl+C -> every laptop shows the red outage bar (and nothing changes locally). `Start-DemoHost.ps1 -Resume <run folder>` -> every laptop reconnects.
Record what failed (discovery vs manual vs blocked) - that tells which network rule is missing.

## E. Limits (read before the demo)
* Automatic discovery needs the Wi-Fi to pass multicast (mDNS). Many guest/corporate Wi-Fi networks do not; the manual address needs only normal peer-to-peer TCP, which "client isolation" also blocks.
* HTTP is unencrypted on the Wi-Fi: anyone on that network could read traffic. Synthetic documents and demo-only passwords only.
* The advertisement is unauthenticated: a malicious device could pretend to be the host. Check the host name/address shown in the header chip against the host laptop.
* The host laptop must stay on and awake; its Drive sign-in is the staging account.

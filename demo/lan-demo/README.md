# Spinco TA Audit 1.3.0 - LAN DEMO build (staging, synthetic documents only)

Unsigned. Built from commit `8f7e66b` plus uncommitted work. Not committed, pushed, published or deployed. Earlier installers (1.2.7, 1.2.8, the two earlier 1.3.0 builds) are untouched.
**Physical multi-laptop connectivity has NOT been tested** - only the automated one-PC LAN test below.

| What | Where |
|---|---|
| Installer (same EXE for every participant) | `release\Spinco TA Audit Setup 1.3.0.exe` |
| Portable executable | `release\Spinco TA Audit 1.3.0.exe` |
| SHA-256 / sizes / times | `release\SHA256SUMS.txt` |
| Host + participant + user + physical-test instructions | `LAN-DEMO-GUIDE.md` |
| Host launcher / user creation / advertiser | `tools\Start-DemoHost.ps1`, `tools\create_demo_users.py`, `tools\demo_advertise.py` |
| Narrow firewall rule (administrator, Private profile, one port, local subnet) | `tools\Add-DemoFirewallRule.ps1` (`-WhatIf`, `-Remove`) |
| Participant network diagnostic | `tools\Test-DemoNetwork.ps1` |
| Automated LAN test driver and results | `tools\lan_e2e.py`, `test-evidence\` |
| Full build output | `20261110-142348-v1.3.0\` |

**How it works:** the host advertises `_spinco-ta-demo._tcp` (mDNS) with its private IPv4 addresses and a dedicated port (default 47831, next free if taken). The demo build contains `resources\demo-defaults.json`,
so an installation with no settings of its own searches the Wi-Fi (about 8 s), verifies each candidate through `/api/health`, and connects. Any `central-backend.json` / `SPINCO_REMOTE_BACKEND_URL` overrides it
(the company build does not contain the file, so the company-server configuration stays separate and unconfigured). Manual address fallback on the connection page. No local database in any case.

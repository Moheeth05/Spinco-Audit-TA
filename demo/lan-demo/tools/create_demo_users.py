"""Creates INDIVIDUAL demo accounts on the demo host through the backend's own admin API (role AUDITOR: upload, view, re-process, validate, export; no admin rights).
Every account gets its OWN random password (never a shared or default one). The credentials are printed ONCE on the host's console and saved in a host-only file;
hand each person only their own line. usage: python create_demo_users.py --url http://127.0.0.1:47831 --admin-email E --admin-password P --names "Priya,Arun" --out participants.csv"""
import argparse
import csv
import re
import secrets
import sys

import httpx


def make_password():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    core = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{core[:4]}-{core[4:8]}-{core[8:]}-{secrets.randbelow(90) + 10}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--admin-email", required=True)
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--names", required=True, help="comma separated display names")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    http = httpx.Client(base_url=args.url, timeout=30)
    r = http.post("/api/auth/login", json={"email": args.admin_email, "password": args.admin_password})
    r.raise_for_status()
    headers = {"X-CSRF-Token": r.json()["csrf_token"]}
    rows = []
    for name in [n.strip() for n in args.names.split(",") if n.strip()]:
        slug = re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".") or "user"
        email = f"{slug}@demo.spinco.local"
        password = make_password()
        resp = http.post("/api/users", headers=headers, json={"name": name, "email": email, "role": "AUDITOR", "password": password})
        if resp.status_code == 409 or (resp.status_code == 400 and "exist" in resp.text.lower()):
            print(f"skipped {email}: already exists", file=sys.stderr)
            continue
        resp.raise_for_status()
        rows.append((name, email, password))
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "email", "password (first sign-in forces a new one)"])
        writer.writerows(rows)
    print("\nPARTICIPANT ACCOUNTS (each person gets ONLY their own line; shown once; saved on this laptop only):")
    for name, email, password in rows:
        print(f"  {name:<16} {email:<34} {password}")
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()

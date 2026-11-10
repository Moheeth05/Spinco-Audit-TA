"""A fake Google Drive for TESTING OUR OWN CODE ONLY. It speaks the small subset of the Drive REST v3 API that
app/services/drive/client.py uses, in memory, through httpx.MockTransport. A test that passes against this proves our client and
lifecycle logic behaves as designed; it proves NOTHING about how Google's real service behaves (that is what the live spike is for).

Features: service-account JWT verification (so the signing code is really exercised), generateIds, folders and files with parents and
trash, resumable upload with caller-chosen ids (409 when the id exists), Range downloads, md5/size, appProperties search, the change
feed with page tokens and expiry, and fault injection (HTTP errors, dropped connections, a lost response after a successful upload).
"""

import base64
import hashlib
import itertools
import json
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

FOLDER = "application/vnd.google-apps.folder"


def _unescape(value: str) -> str:
    return value.replace("\\'", "'").replace("\\\\", "\\")


def _split_top(text: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for char in text:
        depth += (char == "(") - (char == ")")
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [p for p in parts if p]


def project(data, fields: str):
    """What Google does with a `fields` mask: only the named fields come back (`a,b(c,d),e(f(g))`), lists are projected element by element, and a field
    that is not in the mask is ABSENT - so a request that forgets a field it later reads fails in the fake exactly as it would against Google."""
    if isinstance(data, list):
        return [project(item, fields) for item in data]
    if not isinstance(data, dict):
        return data
    out = {}
    for part in _split_top(fields):
        if "(" in part:
            name, inner = part.split("(", 1)
            if name in data:
                out[name] = project(data[name], inner[:-1])
        elif part in data:
            out[part] = data[part]
    return out


# What Google returns for a files resource when no `fields` mask is given.
_DEFAULT_FILE_FIELDS = "kind,id,name,mimeType"


class FakeDrive:
    def __init__(self, root_id: str = "ROOTFOLDER0000001", *, service_account_public_key=None, drive_id: str | None = None):
        self.drive_id = drive_id
        self.public_key = service_account_public_key
        self.files: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.changes: list[dict] = []
        self.sessions: dict[str, dict] = {}
        self.faults: list[dict] = []
        self.read_only = False
        self.incomplete: list[dict] = []  # listings that answer `incompleteSearch: true` with a PARTIAL result (see incomplete_next)
        self.ignore_drive_scope = False  # simulate a Drive that leaks an out-of-scope item into a drive-scoped listing (defence-in-depth tests)
        self.permissions: dict[str, list[dict]] = {}  # file id -> [{"type","role"}]; tests set {"type": "anyone"} to model a public link
        self.min_valid_change_token = 0
        self.token_requests = 0
        self.lose_response_after_next_upload = False
        self.corrupt_next_upload = False
        self._id_counter = itertools.count(1)  # next() is atomic, so concurrent tests can never be handed the same id
        self._clock = 0
        self._add(root_id, "Spinco TA Audit", FOLDER, [])

    # ------------------------------------------------------------------ public helpers for tests

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def fail_next(self, times: int, status: int = 503, reason: str = "backendError", *, retry_after: str | None = None, match=None, body=None) -> None:
        """`body`, when given, is returned verbatim as the JSON error body (to model Google's richer error shapes); otherwise a simple one is built."""
        self.faults.append({"times": times, "kind": "status", "status": status, "reason": reason, "retry_after": retry_after, "match": match, "body": body})

    def incomplete_next(self, times: int = 1, *, match=None) -> None:
        """The next `times` file LISTINGS (that `match` accepts, if given) report `incompleteSearch: true` and return only part of the result."""
        self.incomplete.append({"times": times, "match": match})

    def drop_connection_next(self, times: int, *, match=None, exc=httpx.ConnectError) -> None:
        self.faults.append({"times": times, "kind": "raise", "match": match, "exc": exc})

    def by_name(self, name: str) -> list[dict]:
        return [f for f in self.files.values() if f["name"] == name]

    def file_count(self, *, include_trashed: bool = True, only_files: bool = True) -> int:
        return sum(1 for f in self.files.values() if (not only_files or f["mimeType"] != FOLDER) and (include_trashed or not f["trashed"]))

    def external_trash(self, file_id: str) -> None:
        self.files[file_id]["trashed"] = True
        self._changed(file_id)

    def external_untrash(self, file_id: str) -> None:
        self.files[file_id]["trashed"] = False
        self._changed(file_id)

    def external_delete(self, file_id: str) -> None:
        del self.files[file_id]
        self._changed(file_id, removed=True)

    def external_rename(self, file_id: str, name: str) -> None:
        self.files[file_id]["name"] = name
        self._changed(file_id)

    def external_move(self, file_id: str, parent_id: str) -> None:
        self.files[file_id]["parents"] = [parent_id]
        self._changed(file_id)

    def external_move_to_drive(self, file_id: str, other_drive_id: str | None) -> None:
        """Somebody moves the file out of this Shared Drive into another drive (or into a personal My Drive: `None`). The file itself stays readable; this
        drive's feed reports it as removed from the drive, as Google's does."""
        old_drive = self.files[file_id].get("driveId")
        if other_drive_id:
            self.files[file_id]["driveId"] = other_drive_id
        else:
            self.files[file_id].pop("driveId", None)
        self._changed(file_id, removed=True, drive_id=old_drive)

    def external_modify(self, file_id: str, content: bytes) -> None:
        f = self.files[file_id]
        f["content"], f["md5Checksum"], f["size"] = content, hashlib.md5(content).hexdigest(), str(len(content))
        self._changed(file_id)

    # ------------------------------------------------------------------ internals

    def _now(self) -> str:
        self._clock += 1
        return f"2026-11-01T00:{self._clock // 60:02d}:{self._clock % 60:02d}.000Z"

    def _add(self, file_id, name, mime, parents, *, content=None, app_properties=None) -> dict:
        record = {
            "id": file_id, "name": name, "mimeType": mime, "parents": list(parents), "trashed": False, "explicitlyTrashed": False,
            "appProperties": dict(app_properties or {}), "createdTime": self._now(), "modifiedTime": self._now(), "ownedByMe": True,
            "capabilities": {"canEdit": True, "canTrash": True, "canAddChildren": mime == FOLDER, "canMoveItemWithinDrive": True, "canDownload": True},
            "content": content,
        }
        if self.drive_id:
            record["driveId"] = self.drive_id
        if content is not None:
            record["md5Checksum"], record["size"] = hashlib.md5(content).hexdigest(), str(len(content))
        self.files[file_id] = record
        return record

    def _api(self, record: dict) -> dict:
        return {k: v for k, v in record.items() if k != "content" and not (k == "parents" and not v)}

    def _changed(self, file_id: str, removed: bool = False, *, drive_id: str | None = None) -> None:
        entry = {"fileId": file_id, "removed": removed, "time": self._now()}
        if drive_id or self.drive_id:
            entry["driveId"] = drive_id or self.drive_id
        if not removed:
            entry["file"] = self._api(self.files[file_id])
        self.changes.append(entry)

    @staticmethod
    def _masked(data, query: dict):
        """A files resource, cut down to the requested `fields` mask (or Google's default fields when there is none)."""
        return project(data, query.get("fields") or _DEFAULT_FILE_FIELDS)

    def _reply(self, status: int, body=None, headers=None, content: bytes | None = None) -> httpx.Response:
        if content is not None:
            return httpx.Response(status, content=content, headers=headers)
        return httpx.Response(status, json=body if body is not None else {}, headers=headers)

    def _error(self, status: int, reason: str, message: str = "error", headers=None) -> httpx.Response:
        return self._reply(status, {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}, headers)

    # ------------------------------------------------------------------ request handling

    def _verify_assertion(self, assertion: str) -> bool:
        try:
            header_b64, claims_b64, signature_b64 = assertion.split(".")
            pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
            claims = json.loads(base64.urlsafe_b64decode(pad(claims_b64)))
            if self.public_key is not None:
                self.public_key.verify(base64.urlsafe_b64decode(pad(signature_b64)), f"{header_b64}.{claims_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())
            return claims["exp"] > time.time() - 5 and bool(claims["iss"]) and bool(claims["scope"]) and claims["aud"].startswith("https://")
        except (ValueError, KeyError, InvalidSignature):
            return False

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        entry = {"method": request.method, "path": url.path, "query": query, "auth": request.headers.get("authorization", ""), "range": request.headers.get("range")}
        if request.method == "PATCH":
            entry["body"] = json.loads(request.content or b"{}")  # what a metadata update asked for (tests check, e.g., that archive never sets `trashed`)
        self.requests.append(entry)

        if url.hostname == "oauth2.googleapis.com" and url.path == "/token":
            for fault in self.faults:  # only faults that explicitly target the token endpoint (a `match` that returns True) apply here
                if fault["times"] > 0 and fault["match"] is not None and fault["match"](request):
                    fault["times"] -= 1
                    if fault["kind"] == "raise":
                        raise fault.get("exc", httpx.ConnectError)("injected connection failure", request=request)
                    return self._error(fault["status"], fault["reason"], "injected failure")
            self.token_requests += 1
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer" or not self._verify_assertion(form.get("assertion", "")):
                return self._reply(400, {"error": "invalid_grant", "error_description": "Invalid JWT"})
            return self._reply(200, {"access_token": f"fake-token-{self.token_requests}", "expires_in": 3600, "token_type": "Bearer"})

        for fault in self.faults:
            if fault["times"] > 0 and (fault["match"] is None or fault["match"](request)):
                fault["times"] -= 1
                if fault["kind"] == "raise":
                    raise fault.get("exc", httpx.ConnectError)("injected connection failure", request=request)
                headers = {"Retry-After": fault["retry_after"]} if fault.get("retry_after") else None
                if fault.get("body") is not None:
                    return self._reply(fault["status"], fault["body"], headers)
                return self._error(fault["status"], fault["reason"], "injected failure", headers)

        if not entry["auth"].startswith("Bearer fake-token-"):
            return self._error(401, "authError", "Invalid Credentials")
        if self.read_only and (request.method in ("PATCH", "PUT") or (request.method == "POST" and not url.path.endswith("/changes"))):
            return self._error(403, "insufficientFilePermissions", "The user does not have sufficient permissions for this file.")

        path = url.path
        if path == "/drive/v3/files/generateIds" and request.method == "GET":
            ids = []
            for _ in range(int(query.get("count", 1))):
                ids.append(f"fakeid{next(self._id_counter):012d}")
            return self._reply(200, {"ids": ids, "space": "drive", "kind": "drive#generatedIds"})
        if path == "/drive/v3/about":
            return self._reply(200, {"storageQuota": {"limit": "16106127360", "usage": "0", "usageInDrive": "0"}})
        shared_drive = re.fullmatch(r"/drive/v3/drives/([A-Za-z0-9_-]+)", path)
        if shared_drive:  # drives.get: only a fake that models a Shared Drive knows it
            if not self.drive_id or shared_drive.group(1) != self.drive_id:
                return self._error(404, "notFound", "Shared drive not found")
            return self._reply(200, {"id": self.drive_id, "name": "Spinco TA Audit", "hidden": False, "capabilities": {"canAddChildren": True, "canListChildren": True}})
        permissions = re.fullmatch(r"/drive/v3/files/([A-Za-z0-9_-]+)/permissions", path)
        if permissions:
            if permissions.group(1) not in self.files:
                return self._error(404, "notFound", "File not found")
            return self._reply(200, {"permissions": self.permissions.get(permissions.group(1), [{"type": "user", "role": "writer"}])})
        if path == "/drive/v3/changes/startPageToken":
            return self._reply(200, {"startPageToken": self._token(len(self.changes), query.get("driveId"))})
        if path == "/drive/v3/changes":
            return self._list_changes(query)
        if path == "/upload/drive/v3/files":
            return self._upload(request, query)
        if path == "/drive/v3/files" and request.method == "GET":
            return self._list(query, request)
        if path == "/drive/v3/files" and request.method == "POST":
            body = json.loads(request.content)
            if body.get("id") in self.files:
                return self._error(409, "conflict", "A file with the given ID already exists.")
            record = self._add(body.get("id") or f"fakefolder{len(self.files):08d}", body["name"], body.get("mimeType", FOLDER), body.get("parents", []), app_properties=body.get("appProperties"))
            self._changed(record["id"])
            return self._reply(200, self._masked(self._api(record), query))
        match = re.fullmatch(r"/drive/v3/files/([A-Za-z0-9_-]+)", path)
        if match:
            return self._file(match.group(1), request, query)
        return self._error(404, "notFound", "unknown route")

    def _file(self, file_id: str, request: httpx.Request, query: dict) -> httpx.Response:
        record = self.files.get(file_id)
        if record is None:
            return self._error(404, "notFound", "File not found")
        if request.method == "GET" and query.get("alt") == "media":
            content = record["content"] or b""
            rng = request.headers.get("range")
            if rng:
                start, _, end = rng.replace("bytes=", "").partition("-")
                start_i = int(start or 0)
                end_i = int(end) if end else len(content) - 1
                chunk = content[start_i : end_i + 1]
                return self._reply(206, headers={"Content-Range": f"bytes {start_i}-{start_i + len(chunk) - 1}/{len(content)}", "Content-Type": "application/pdf"}, content=chunk)
            return self._reply(200, headers={"Content-Type": "application/pdf"}, content=content)
        if request.method == "GET":
            return self._reply(200, self._masked(self._api(record), query))
        if request.method == "PATCH":
            body = json.loads(request.content or b"{}")
            if "name" in body:
                record["name"] = body["name"]
            if "trashed" in body:
                record["trashed"] = record["explicitlyTrashed"] = bool(body["trashed"])
            if "appProperties" in body:
                record["appProperties"].update(body["appProperties"])
            for parent in [p for p in query.get("removeParents", "").split(",") if p]:
                if parent in record["parents"]:
                    record["parents"].remove(parent)
            for parent in [p for p in query.get("addParents", "").split(",") if p]:
                if parent not in record["parents"]:
                    record["parents"].append(parent)
            record["modifiedTime"] = self._now()
            self._changed(file_id)
            return self._reply(200, self._masked(self._api(record), query))
        return self._error(405, "methodNotAllowed", "method")

    def _list(self, query: dict, request: httpx.Request) -> httpx.Response:
        q = query.get("q", "")
        parent = re.search(r"'([^']+)' in parents", q)
        name = re.search(r"name = '((?:\\.|[^'\\])*)'", q)
        prop = re.search(r"appProperties has \{ key='((?:\\.|[^'\\])*)' and value='((?:\\.|[^'\\])*)' \}", q)
        corpora, scoped_drive = query.get("corpora", "user"), query.get("driveId")
        if corpora == "drive" and not scoped_drive:
            return self._error(400, "invalid", "The driveId parameter must be specified if corpora is set to drive.")
        items = []
        for record in self.files.values():
            if corpora == "drive" and not self.ignore_drive_scope and record.get("driveId") != scoped_drive:
                continue  # a drive-scoped listing only ever sees items of that drive
            if parent and parent.group(1) not in record["parents"]:
                continue
            if "trashed = false" in q and record["trashed"]:
                continue
            if name and record["name"] != _unescape(name.group(1)):
                continue
            if f"mimeType = '{FOLDER}'" in q and record["mimeType"] != FOLDER:
                continue
            if prop and record["appProperties"].get(_unescape(prop.group(1))) != _unescape(prop.group(2)):
                continue
            items.append(self._api(record))
        size = int(query.get("pageSize", 100))
        start = int(query.get("pageToken", 0) or 0)
        page = items[start : start + size]
        body = {"files": page}
        if start + size < len(items):
            body["nextPageToken"] = str(start + size)
        for rule in self.incomplete:
            if rule["times"] > 0 and (rule["match"] is None or rule["match"](request)):
                rule["times"] -= 1
                body = {"files": page[: len(page) // 2], "incompleteSearch": True}  # PARTIAL: some documents were not searched
                break
        return self._reply(200, project(body, query["fields"]) if query.get("fields") else body)

    @staticmethod
    def _token(position: int, scope: str | None) -> str:
        """A change token is only valid for the scope (a Shared Drive, or the whole account) it was issued for: here the scope is part of the token."""
        return f"{position}~{scope}" if scope else str(position)

    def _list_changes(self, query: dict) -> httpx.Response:
        raw = query.get("pageToken", "")
        number, _, issued_for = raw.partition("~")
        scope = query.get("driveId")
        if not number.isdigit() or (issued_for or None) != (scope or None):
            return self._error(400, "invalidPageToken", "The page token was issued for a different scope or is malformed")
        token = int(number)
        if token < self.min_valid_change_token or token > len(self.changes):
            return self._error(410, "pageTokenExpired", "The page token is no longer valid")
        size = int(query.get("pageSize", 100))
        # A drive-scoped feed carries only that drive's changes (each entry names the drive it happened in, as Google's does).
        page = self.changes[token : token + size]
        if scope:
            page = [c for c in page if c.get("driveId") == scope]
        body = {"changes": page}
        if token + size < len(self.changes):
            body["nextPageToken"] = self._token(token + size, scope)
        else:
            body["newStartPageToken"] = self._token(len(self.changes), scope)
        return self._reply(200, project(body, query["fields"]) if query.get("fields") else body)

    def _upload(self, request: httpx.Request, query: dict) -> httpx.Response:
        if request.method == "POST":
            meta = json.loads(request.content)
            if meta.get("id") in self.files:
                return self._error(409, "conflict", "A file with the given ID already exists.")
            session = f"sess-{len(self.sessions) + 1}"
            self.sessions[session] = {**meta, "_fields": query.get("fields")}  # the `fields` mask of the initiation applies to the final answer
            return self._reply(200, {}, headers={"Location": f"https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id={session}"})
        meta = self.sessions.get(query.get("upload_id", ""))
        if meta is None:
            return self._error(404, "notFound", "unknown upload session")
        record = self._add(meta["id"], meta["name"], meta.get("mimeType", "application/pdf"), meta.get("parents", []), content=request.content, app_properties=meta.get("appProperties"))
        if self.corrupt_next_upload:
            self.corrupt_next_upload = False
            record["md5Checksum"] = "0" * 32
        self._changed(record["id"])
        if self.lose_response_after_next_upload:
            self.lose_response_after_next_upload = False
            raise httpx.ReadError("injected: the response was lost after the upload succeeded", request=request)
        return self._reply(200, self._masked(self._api(record), {"fields": meta.get("_fields")}))

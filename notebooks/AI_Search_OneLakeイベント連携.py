# Generated from the adjacent .ipynb; edit that notebook, then run
# python scripts/export_notebooks.py
# Reading copy: IPython magic commands are commented out.
# Import the .ipynb into Fabric for execution and parameter-cell metadata.

# %% [markdown]
# # Activator → OneLake削除イベント → AI Search
# 最初のコードセルをFabricのパラメーターセルに指定します。6引数をActivatorの動的プロパティから渡してください。eventTimeはSystem.IngestionTime、blobUrlはblobUrlへ割り当てます。既定はdry-run。手動検証Notebookとは独立して実行します。

# %%
eventId = ""
eventType = ""
eventTime = ""
eventSource = ""
eventSubject = ""
blobUrl = ""

# %% [markdown]
# 依存ライブラリはFabric Environmentに登録し、このNotebookにアタッチしてください（requirements.txt参照）。イベントごとのinline installは行いません。

# %%
"""Shared implementation embedded in the Notebook and Fabric UDF."""

import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlsplit

import requests
from azure.identity import ClientSecretCredential


class ProbeError(RuntimeError):
    pass


def prepare_onelake_event(event_id, event_type, event_time, event_source,
                         event_subject, blob_url):
    required = (event_id, event_type, event_time, event_source, event_subject, blob_url)
    if any(not isinstance(value, str) or not value.strip() for value in required):
        raise ProbeError("All six event parameters must be non-empty strings.")
    event_id, event_type, event_time, event_source, event_subject, blob_url = (
        value.strip() for value in required
    )
    scopes = {"Microsoft.Fabric.OneLake.FileDeleted": "file",
              "Microsoft.Fabric.OneLake.FolderDeleted": "directory"}
    if event_type not in scopes:
        raise ProbeError("Only FileDeleted and FolderDeleted events are accepted.")
    try:
        timestamp = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProbeError("eventTime must be an ISO 8601 timestamp.") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    event_time = timestamp.isoformat()
    if not blob_url.startswith("https://"):
        raise ProbeError("blobUrl must be an HTTPS OneLake Blob or DFS URL.")
    target = source_url(blob_url, "", "")
    context = re.fullmatch(
        r"/(?:tenants/)?([^/]+)/workspaces/([^/]+)/items/([^/]+)", event_source,
    )
    if not context:
        raise ProbeError("eventSource must be /tenants/tenant/workspaces/workspace/items/item "
                         "or /tenant/workspaces/workspace/items/item.")
    workspace, lakehouse = target.split("/")[3:5]
    if (workspace, lakehouse) != (context[2], context[3]):
        raise ProbeError("Event source workspace/item does not match blobUrl.")
    if not event_subject.startswith("/Files/"):
        raise ProbeError("eventSubject must start with /Files/.")
    subject_target = source_url(
        f"https://onelake.dfs.fabric.microsoft.com/{workspace}/{lakehouse}{event_subject}",
        "", "",
    )
    if target != subject_target:
        raise ProbeError("eventSubject does not match blobUrl.")
    return {"event_id": event_id, "event_type": event_type, "event_time": event_time,
            "event_source": event_source, "source": blob_url, "scope": scopes[event_type]}


def log_lookup_result(found, event_id=None):
    summary = {
        "event_id": event_id, "index": found["index"],
        "matched_count": found["matched_count"], "scanned": found["scanned"],
        "complete": found["complete"],
    }
    logging.info("AI_SEARCH_LOOKUP_SUMMARY %s", json.dumps(summary, ensure_ascii=False))
    for document in found["documents"]:
        logging.info("AI_SEARCH_LOOKUP_DOCUMENT %s", json.dumps({
            "event_id": event_id, "index": found["index"],
            "chunk_id": document["chunk_id"], "file_name": document["file_name"],
        }, ensure_ascii=False))


def process_onelake_event(probe, event, path_field, enable_delete=False,
                         max_scan=1000, max_delete=20):
    if type(enable_delete) is not bool:
        raise ProbeError("enable_delete must be a boolean deployment setting.")
    if type(max_delete) is not int or not 1 <= max_delete <= 100:
        raise ProbeError("max_delete must be an integer from 1 to 100.")
    logging.info("OneLake event start id=%s type=%s", event["event_id"], event["event_type"])
    found = probe.find(event["source"], event["scope"], path_field, max_scan=max_scan)
    log_lookup_result(found, event["event_id"])
    ids = found["document_ids"]
    if len(ids) > max_delete:
        raise ProbeError("Event matched more than max_delete IDs; no deletion was started.")
    report = {"event_id": event["event_id"], "event_type": event["event_type"],
              "event_time": event["event_time"], "dry_run": not enable_delete,
              "lookup": found}
    if not ids:
        logging.warning("OneLake event id=%s matched no documents; check mapping or replay.",
                        event["event_id"])
        report["outcome"] = "no_matches"
        return report
    report["deletion"] = probe.delete(ids, dry_run=not enable_delete,
                                     confirmation=f"DELETE {probe.index} {len(ids)}",
                                     existing_before=ids)
    report["outcome"] = "deleted" if enable_delete else "dry_run"
    logging.info("OneLake event complete id=%s outcome=%s",
                 event["event_id"], report["outcome"])
    return report


def source_url(source: str, workspace: str, lakehouse: str) -> str:
    """Normalize global OneLake DFS/Blob URLs without guessing name/GUID aliases."""
    if not isinstance(source, str) or not source.strip():
        raise ProbeError("source must be a non-empty OneLake path.")
    source = source.strip()
    parsed = urlsplit(source)
    if parsed.scheme == "https":
        if parsed.netloc.lower() not in (
                "onelake.dfs.fabric.microsoft.com", "onelake.blob.fabric.microsoft.com"):
            raise ProbeError("Only the global OneLake DFS and Blob hosts are supported.")
        if parsed.query or parsed.fragment:
            raise ProbeError("Source URLs must not contain query strings or fragments.")
        path = unquote(parsed.path)
    elif parsed.scheme == "abfss":
        if parsed.hostname != "onelake.dfs.fabric.microsoft.com" or not parsed.username:
            raise ProbeError("Expected abfss://workspace@onelake.dfs.fabric.microsoft.com/...")
        if parsed.query or parsed.fragment:
            raise ProbeError("ABFSS URLs must not contain query strings or fragments.")
        path = "/" + unquote(parsed.username) + unquote(parsed.path)
    elif not parsed.scheme:
        if not workspace or not lakehouse:
            raise ProbeError("workspace and lakehouse are required for relative paths.")
        if not source.startswith("Files/"):
            raise ProbeError("Relative source must start with Files/.")
        path = f"/{workspace}/{lakehouse}/{source}"
    else:
        raise ProbeError("Supported sources: Files/..., HTTPS DFS/Blob URL, ABFSS URL.")
    parts = path.strip("/").split("/")
    if len(parts) < 4 or parts[2] != "Files" or any(p in ("", ".", "..") for p in parts):
        raise ProbeError("Expected /workspace/lakehouse/Files/non-empty-path; no traversal.")
    return "https://onelake.dfs.fabric.microsoft.com/" + "/".join(parts)


class SearchProbe:
    def __init__(self, endpoint, index, tenant_id, client_id, client_secret,
                 api_version="2025-09-01", budget_seconds=70):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname
                or not parsed.hostname.endswith(".search.windows.net")
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or parsed.username or parsed.port is not None):
            raise ProbeError("endpoint must be https://<service>.search.windows.net")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,127}", index):
            raise ProbeError("Invalid index name.")
        if not all((tenant_id, client_id, client_secret)):
            raise ProbeError("Tenant ID, client ID and Key Vault secret are required.")
        self.endpoint = endpoint.rstrip("/")
        self.index = index
        self.api_version = api_version
        self.credential = ClientSecretCredential(tenant_id, client_id, client_secret)
        self.session = requests.Session()
        self.deadline = time.monotonic() + budget_seconds
        self._schema = None

    def close(self):
        self.session.close()
        self.credential.close()

    def request(self, method, suffix, payload=None, allow_missing=False, select=None):
        remaining = self.deadline - time.monotonic()
        if remaining < 1:
            raise ProbeError("Operation time budget exceeded; narrow the scope and retry.")
        token = self.credential.get_token("https://search.azure.com/.default").token
        remaining = self.deadline - time.monotonic()
        if remaining < 1:
            raise ProbeError("Operation time budget exceeded during authentication.")
        params = {"api-version": self.api_version}
        if select:
            params["$select"] = select
        try:
            response = self.session.request(
                method,
                f"{self.endpoint}/indexes/{quote(self.index, safe='')}{suffix}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                json=payload, timeout=(min(5, remaining), min(15, remaining)),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            logging.error("AI Search transport failed: %s", type(exc).__name__)
            raise ProbeError("AI Search transport failure; check network access.") from exc
        if allow_missing and response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            request_id = response.headers.get("request-id", "")
            logging.error("AI Search HTTP %s request-id=%s", response.status_code, request_id)
            raise ProbeError(f"AI Search HTTP {response.status_code}; request-id={request_id}. "
                             "Check RBAC, endpoint, index and API version.")
        return response.json()

    def schema(self):
        if self._schema is None:
            self._schema = self.request("GET", "")
        return self._schema

    def fields(self, path_field=None):
        fields = self.schema()["fields"]
        keys = [f for f in fields if f.get("key")]
        if len(keys) != 1:
            raise ProbeError("Index must have exactly one key field.")
        key = keys[0]
        if key.get("retrievable") is False:
            raise ProbeError("Key field must be retrievable.")
        if path_field is not None:
            path = next((f for f in fields if f["name"] == path_field), None)
            if not path or path["type"] != "Edm.String" or path.get("retrievable") is False:
                raise ProbeError("path_field must be a retrievable, top-level Edm.String field.")
        return key["name"]

    def inspect(self):
        return {
            "index": self.index,
            "key_field": self.fields(),
            "string_fields": [
                {k: f.get(k) for k in ("name", "type", "retrievable", "filterable")}
                for f in self.schema()["fields"] if f["type"] == "Edm.String"
            ],
        }

    def find(self, source, scope, path_field, workspace="", lakehouse="",
             max_scan=1000, exact_filter=False):
        if scope not in ("file", "directory"):
            raise ProbeError("scope must be file or directory.")
        if type(max_scan) is not int or not 1 <= max_scan <= 10000:
            raise ProbeError("max_scan must be an integer from 1 to 10000.")
        target = source_url(source, workspace, lakehouse).rstrip("/")
        key = self.fields(path_field)
        payload = {"search": "*", "select": ",".join(dict.fromkeys((key, path_field))),
                   "top": min(1000, max_scan + 1)}
        if exact_filter:
            if scope != "file":
                raise ProbeError("exact_filter is only supported for a file.")
            field = next(f for f in self.schema()["fields"] if f["name"] == path_field)
            if field.get("filterable") is not True or field.get("normalizer"):
                raise ProbeError("exact_filter requires a filterable field without normalizer.")
            # Exact filters need the actual stored spelling, including percent encoding.
            payload["filter"] = f"{path_field} eq '{source.replace(chr(39), chr(39) * 2)}'"
            if not source.startswith("https://"):
                raise ProbeError("exact_filter requires the exact stored HTTPS DFS URL.")
        scanned = 0
        matched = {}
        while True:
            result = self.request("POST", "/docs/search", payload)
            for doc in result["value"]:
                scanned += 1
                if scanned > max_scan:
                    raise ProbeError("Scan limit exceeded; result is incomplete and is NOT "
                                     "returned. Increase max_scan or use exact_filter.")
                raw_path = doc.get(path_field)
                if raw_path is None:
                    continue
                if not isinstance(raw_path, str):
                    raise ProbeError("Unexpected non-string source path in index.")
                stored = source_url(raw_path, "", "").rstrip("/")
                is_match = stored == target if scope == "file" else stored.startswith(target + "/")
                if is_match:
                    if not isinstance(doc.get(key), str) or not doc[key]:
                        raise ProbeError("Matched document has no valid key.")
                    matched[doc[key]] = {
                        "id": doc[key], "chunk_id": doc[key],
                        "file_name": stored.rsplit("/", 1)[-1],
                        "source_path": raw_path,
                    }
            continuation = result.get("@search.nextPageParameters")
            if not continuation:
                if result.get("@odata.nextLink"):
                    raise ProbeError("Search continuation has no nextPageParameters.")
                break
            payload = continuation
        return {"index": self.index, "key_field": key, "target": target, "scope": scope,
                "scanned": scanned, "complete": True, "matched_count": len(matched),
                "document_ids": list(matched), "documents": list(matched.values())}

    def existing(self, ids):
        found = []
        for document_id in ids:
            if self.request("GET", f"/docs/{quote(document_id, safe='')}",
                            allow_missing=True, select=self.fields()) is not None:
                found.append(document_id)
        return found

    def existing_batch(self, ids):
        key = self.fields()
        field = next(field for field in self.schema()["fields"] if field["name"] == key)
        if field.get("filterable") is not True or field.get("normalizer"):
            raise ProbeError("Batch existence checks require a filterable key without normalizer.")
        delimiter = next((value for value in (",", "|", ";", "~")
                          if all(value not in document_id for document_id in ids)), None)
        if delimiter is None:
            raise ProbeError("No safe delimiter for batch existence check.")
        values = delimiter.join(ids).replace("'", "''")
        payload = {"search": "*", "filter": f"search.in({key}, '{values}', '{delimiter}')",
                   "select": key, "top": len(ids)}
        found = set()
        while True:
            result = self.request("POST", "/docs/search", payload)
            for document in result["value"]:
                document_id = document.get(key)
                if not isinstance(document_id, str) or document_id not in ids or document_id in found:
                    raise ProbeError("Unexpected or duplicate key in batch existence response.")
                found.add(document_id)
            continuation = result.get("@search.nextPageParameters")
            if not continuation:
                if result.get("@odata.nextLink"):
                    raise ProbeError("Batch existence continuation has no nextPageParameters.")
                break
            payload = continuation
        return [document_id for document_id in ids if document_id in found]

    def delete(self, document_ids, dry_run=True, confirmation="", existing_before=None):
        if (not isinstance(document_ids, list) or not document_ids
                or any(not isinstance(i, str) or not i for i in document_ids)):
            raise ProbeError("document_ids must be a non-empty list of non-empty strings.")
        ids = list(dict.fromkeys(document_ids))
        if len(ids) > 100:
            raise ProbeError("This verification probe supports at most 100 IDs per call.")
        if type(dry_run) is not bool:
            raise ProbeError("dry_run must be boolean.")
        if not dry_run and confirmation != f"DELETE {self.index} {len(ids)}":
            raise ProbeError(f"Required confirmation: DELETE {self.index} {len(ids)}")
        key = self.fields()
        if existing_before is not None and (
                not isinstance(existing_before, list)
                or any(not isinstance(value, str) or value not in ids
                       for value in existing_before)
                or len(set(existing_before)) != len(existing_before)):
            raise ProbeError("existing_before must be unique requested IDs from lookup.")
        # Check batch-query support before sending a destructive request.
        field = next(field for field in self.schema()["fields"] if field["name"] == key)
        if not dry_run and (field.get("filterable") is not True or field.get("normalizer")):
            raise ProbeError("Batch existence checks require a filterable key without normalizer.")
        before = self.existing(ids) if existing_before is None else list(existing_before)
        report = {"index": self.index, "key_field": key, "dry_run": dry_run,
                  "requested_ids": ids, "existing_before": before,
                  "absent_before": [i for i in ids if i not in before]}
        report["before_check"] = "individual_get" if existing_before is None else "lookup_result"
        if dry_run:
            return report
        actions = [{"@search.action": "delete", key: i} for i in ids]
        if len(json.dumps({"value": actions}).encode("utf-8")) > 15 * 1024 * 1024:
            raise ProbeError("Delete payload is too large.")
        result = self.request("POST", "/docs/index", {"value": actions})
        results = result["value"]
        if (len(results) != len(ids) or {r.get("key") for r in results} != set(ids)
                or any(r.get("status") is not True for r in results)):
            logging.error("Delete batch failed or returned incomplete per-document results.")
            raise ProbeError("Delete batch failed or incomplete. Some IDs may already have "
                             "been deleted; inspect the index before retrying.")
        report["delete_results"] = results
        remaining = ids
        for attempt in range(4):
            remaining = self.existing_batch(remaining)
            if not remaining:
                break
            if attempt < 3:
                time.sleep(2)
        report["remaining_ids"] = remaining
        report["verified_absent"] = not remaining
        report["verified_deleted_ids"] = [i for i in before if i not in remaining]
        if remaining:
            raise ProbeError("Delete accepted but absence not verified within polling window; "
                             "rerun existence checks. The indexer may also recreate documents.")
        return report

# %%
SEARCH_ENDPOINT = "https://YOUR-SERVICE.search.windows.net"
INDEX_NAME = "idx-onelake-udf"
TENANT_ID = "YOUR-TENANT-ID"
CLIENT_ID = "YOUR-CLIENT-ID"
KEY_VAULT_URL = "https://YOUR-VAULT.vault.azure.net"
SECRET_NAME = "ai-search-client-secret"
EVENT_PATH_FIELD = "metadata_storage_path"
EVENT_ENABLE_DELETE = False
EVENT_MAX_SCAN = 1000
EVENT_MAX_DELETE = 20

# %%
event = prepare_onelake_event(
    eventId, eventType, eventTime, eventSource, eventSubject, blobUrl,
)
import notebookutils
secret = notebookutils.credentials.getSecret(KEY_VAULT_URL, SECRET_NAME)
probe = SearchProbe(SEARCH_ENDPOINT, INDEX_NAME, TENANT_ID, CLIENT_ID, secret)
try:
    result = process_onelake_event(
        probe, event, EVENT_PATH_FIELD, EVENT_ENABLE_DELETE,
        EVENT_MAX_SCAN, EVENT_MAX_DELETE,
    )
finally:
    probe.close()
print(json.dumps(result, ensure_ascii=False))
notebookutils.notebook.exit(json.dumps(result, ensure_ascii=False))

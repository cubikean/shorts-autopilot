"""Minimal Notion REST client for the queue (works with any workspace via an
internal integration token — no Notion SDK needed)."""
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Property names (French, as shown in Notion).
STATUS_TODO, STATUS_RUNNING, STATUS_READY, STATUS_REJECTED, STATUS_ERROR = (
    "À traiter", "En cours", "Prêt", "Rejeté", "Erreur",
)
# Shorts "Publication" select: you move a short from À publier to Validé, `publish` does the rest.
PUB_TODO, PUB_VALIDATED, PUB_PUBLISHED, PUB_REJECTED, PUB_ERROR = (
    "À publier", "Validé", "Publié", "Rejeté", "Erreur",
)
PUB_COLORS = {PUB_TODO: "blue", PUB_VALIDATED: "orange", PUB_PUBLISHED: "green", PUB_REJECTED: "gray", PUB_ERROR: "red"}


def page_id_from(value: str) -> str:
    """Accept a Notion URL or a raw id and return the 32-hex id."""
    match = re.findall(r"[0-9a-f]{32}", value.replace("-", "").lower())
    if not match:
        raise ValueError(f"no Notion page id found in {value!r}")
    return match[-1]


def _title(text: str) -> Dict:
    return {"title": [{"text": {"content": str(text)[:2000]}}]}


def _text(text: Optional[str]) -> Dict:
    return {"rich_text": [{"text": {"content": str(text)[:2000]}}] if text else []}


def _select(name: str) -> Dict:
    return {"select": {"name": str(name).replace(",", " ")[:100]}}


def _plain(prop: Dict) -> str:
    return "".join(part.get("plain_text", "") for part in prop.get(prop["type"], []))


class Notion:
    def __init__(self, token: str):
        if not token:
            raise RuntimeError("NOTION_TOKEN is not set. Create an internal integration at https://www.notion.so/my-integrations.")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, body: Optional[Dict] = None) -> Dict:
        # Creating a page is the only non-idempotent call: retrying it after the request
        # may have reached Notion could create a duplicate row (and a duplicate upload).
        idempotent = method != "POST" or path.endswith("/query")
        for attempt in range(5):
            try:
                resp = self.session.request(method, f"{API}/{path}", json=body, timeout=60)
            except requests.RequestException as e:
                # Network blip: retry instead of aborting the whole run mid-video.
                if attempt == 4 or not (idempotent or isinstance(e, requests.exceptions.ConnectTimeout)):
                    raise RuntimeError(f"Notion {method} {path}: network error: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(float(resp.headers.get("Retry-After", 2 ** attempt)))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Notion {method} {path} failed [{resp.status_code}]: {resp.text}")
            return resp.json()
        raise RuntimeError(f"Notion {method} {path}: still rate-limited after retries")

    # --- setup ---------------------------------------------------------------

    def create_databases(self, parent_page_id: str) -> Tuple[str, str]:
        """Create the "Vidéos à traiter" and "Shorts" databases under a page shared with the integration."""
        videos = self._request("POST", "databases", {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": "Vidéos à traiter"}}],
            "properties": {
                "Nom": {"title": {}},
                "URL": {"url": {}},
                "Plateforme": {"select": {"options": [{"name": "YouTube", "color": "red"}, {"name": "Twitch", "color": "purple"}]}},
                "Chaîne": {"select": {"options": []}},
                "Publiée": {"date": {}},
                "Vues": {"number": {"format": "number"}},
                "Score": {"number": {"format": "number"}},
                "Début (s)": {"number": {"format": "number"}},
                "Fin (s)": {"number": {"format": "number"}},
                "Statut": {"select": {"options": [
                    {"name": STATUS_TODO, "color": "blue"}, {"name": STATUS_RUNNING, "color": "yellow"},
                    {"name": STATUS_READY, "color": "green"}, {"name": STATUS_REJECTED, "color": "gray"},
                    {"name": STATUS_ERROR, "color": "red"},
                ]}},
                "Clé": {"rich_text": {}},
                "Erreur": {"rich_text": {}},
            },
        })
        shorts = self._request("POST", "databases", {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": "Shorts"}}],
            "properties": {
                "Titre": {"title": {}},
                "Description": {"rich_text": {}},
                "Hashtags": {"rich_text": {}},
                "Accroche": {"rich_text": {}},
                "Score": {"number": {"format": "number"}},
                "Début (s)": {"number": {"format": "number"}},
                "Fin (s)": {"number": {"format": "number"}},
                "Fichier": {"rich_text": {}},
                "Publication": {"select": {"options": [{"name": name, "color": color} for name, color in PUB_COLORS.items()]}},
                "Date de publication": {"date": {}},
                "Erreur publication": {"rich_text": {}},
                "Vidéo source": {"relation": {"database_id": videos["id"], "type": "dual_property", "dual_property": {}}},
            },
        })
        # Give the auto-created back-relation on the videos database a readable name.
        schema = self._request("GET", f"databases/{videos['id']}")
        for name, prop in schema["properties"].items():
            if prop["type"] == "relation" and name != "Shorts":
                self._request("PATCH", f"databases/{videos['id']}", {"properties": {name: {"name": "Shorts"}}})
        return videos["id"], shorts["id"]

    # --- videos queue ----------------------------------------------------------

    def has_key(self, database_id: str, key: str) -> bool:
        result = self._request("POST", f"databases/{database_id}/query", {
            "filter": {"property": "Clé", "rich_text": {"equals": key}},
            "page_size": 1,
        })
        return bool(result.get("results"))

    def add_video(self, database_id: str, candidate: Dict) -> str:
        props = {
            "Nom": _title(candidate["title"]),
            "URL": {"url": candidate["url"]},
            "Plateforme": _select(candidate["platform"]),
            "Chaîne": _select(candidate["channel"] or "?"),
            "Vues": {"number": candidate["views"]},
            "Score": {"number": candidate["score"]},
            "Statut": _select(STATUS_TODO),
            "Clé": _text(candidate["key"]),
        }
        if isinstance(candidate.get("published"), datetime):
            props["Publiée"] = {"date": {"start": candidate["published"].isoformat()}}
        if candidate.get("start") is not None:
            props["Début (s)"] = {"number": candidate["start"]}
            props["Fin (s)"] = {"number": candidate["end"]}
        page = self._request("POST", "pages", {"parent": {"database_id": database_id}, "properties": props})
        return page["id"]

    def todo(self, database_id: str, limit: int) -> List[Dict]:
        """Highest-score videos waiting to be processed, as plain dicts."""
        result = self._request("POST", f"databases/{database_id}/query", {
            "filter": {"property": "Statut", "select": {"equals": STATUS_TODO}},
            "sorts": [{"property": "Score", "direction": "descending"}],
            "page_size": max(1, min(limit, 100)),
        })
        rows = []
        for page in result.get("results", []):
            p = page["properties"]
            rows.append({
                "page_id": page["id"],
                "title": _plain(p["Nom"]),
                "url": p["URL"]["url"],
                "platform": (p["Plateforme"]["select"] or {}).get("name"),
                "channel": (p["Chaîne"]["select"] or {}).get("name"),
                "key": _plain(p["Clé"]),
                "start": p["Début (s)"]["number"],
                "end": p["Fin (s)"]["number"],
            })
        return rows

    def requeue_running(self, database_id: str) -> int:
        """Put back videos left "En cours" by a run that died (crash, reboot, task time limit)."""
        result = self._request("POST", f"databases/{database_id}/query", {
            "filter": {"property": "Statut", "select": {"equals": STATUS_RUNNING}},
            "page_size": 100,
        })
        for page in result.get("results", []):
            self.set_status(page["id"], STATUS_TODO)
        return len(result.get("results", []))

    def set_status(self, page_id: str, status: str, error: Optional[str] = None) -> None:
        props = {"Statut": _select(status), "Erreur": _text(error)}
        self._request("PATCH", f"pages/{page_id}", {"properties": props})

    # --- shorts ------------------------------------------------------------------

    def add_short(self, database_id: str, video_page_id: str, short: Dict, description: str) -> str:
        page = self._request("POST", "pages", {
            "parent": {"database_id": database_id},
            "properties": {
                "Titre": _title(short.get("title") or "Short"),
                "Description": _text(description),
                "Hashtags": _text(" ".join(short.get("hashtags") or [])),
                "Accroche": _text(short.get("hook_sentence")),
                "Score": {"number": short.get("score")},
                "Début (s)": {"number": round(float(short["start_time"]), 1)},
                "Fin (s)": {"number": round(float(short["end_time"]), 1)},
                "Fichier": _text(short.get("clip_url")),
                "Publication": _select(PUB_TODO),
                "Vidéo source": {"relation": [{"id": video_page_id}]},
            },
        })
        return page["id"]

    # --- publishing ----------------------------------------------------------------

    def ensure_publish_schema(self, database_id: str, link_columns: List[str]) -> List[str]:
        """Add what publishing needs to an existing Shorts database; returns the changes made."""
        props = self._request("GET", f"databases/{database_id}")["properties"]
        changes, patch = [], {}

        # The API ignores option renames, so options are only ever added (existing ones sent back as is).
        options = props["Publication"]["select"]["options"]
        names = {o["name"] for o in options}
        new_options = [{"id": o["id"], "name": o["name"], "color": o["color"]} for o in options]
        for name, color in PUB_COLORS.items():
            if name not in names:
                new_options.append({"name": name, "color": color})
                changes.append(f"option {name!r} added")
        if changes:
            patch["Publication"] = {"select": {"options": new_options}}

        wanted = {"Date de publication": {"date": {}}, "Erreur publication": {"rich_text": {}}, "Stats": {"rich_text": {}}}
        wanted.update({column: {"url": {}} for column in link_columns})
        if "TikTok" in link_columns:
            # TikTok inbox drafts can't carry a caption: this is what you paste in the app.
            wanted["Légende TikTok"] = {"formula": {
                "expression": 'prop("Titre") + "\\n\\n" + prop("Description") + "\\n\\n" + prop("Hashtags")',
            }}
        for name, spec in wanted.items():
            if name not in props:
                patch[name] = spec
                changes.append(f"column {name!r} added")
        if patch:
            self._request("PATCH", f"databases/{database_id}", {"properties": patch})
        return changes

    def validated_shorts(self, database_id: str, link_columns: List[str], limit: int) -> List[Dict]:
        """Shorts marked Validé, earliest scheduled date first, then best score."""
        result = self._request("POST", f"databases/{database_id}/query", {
            "filter": {"property": "Publication", "select": {"equals": PUB_VALIDATED}},
            "sorts": [
                {"property": "Date de publication", "direction": "ascending"},
                {"property": "Score", "direction": "descending"},
            ],
            "page_size": max(1, min(limit, 100)),
        })
        return [self._short_row(page, link_columns) for page in result.get("results", [])]

    def shorts_with_links(self, database_id: str, link_columns: List[str]) -> List[Dict]:
        """Every short posted on at least one platform (daily stats refresh)."""
        rows, cursor = [], None
        while True:
            body = {"filter": {"or": [{"property": c, "url": {"is_not_empty": True}} for c in link_columns]}, "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            result = self._request("POST", f"databases/{database_id}/query", body)
            rows += [self._short_row(page, link_columns) for page in result.get("results", [])]
            if not result.get("has_more"):
                return rows
            cursor = result["next_cursor"]

    @staticmethod
    def _short_row(page: Dict, link_columns: List[str]) -> Dict:
        p = page["properties"]
        return {
            "page_id": page["id"],
            "title": _plain(p["Titre"]),
            "hook": _plain(p["Accroche"]),
            "description": _plain(p["Description"]),
            "hashtags": _plain(p["Hashtags"]),
            "file": _plain(p["Fichier"]),
            "publish_at": (p["Date de publication"]["date"] or {}).get("start"),
            "links": {column: p[column]["url"] for column in link_columns},
            "stats": _plain(p["Stats"]) if "Stats" in p else "",
        }

    def set_stats(self, page_id: str, text: str, links: Optional[Dict[str, str]] = None) -> None:
        props = {"Stats": _text(text)}
        props.update({column: {"url": url} for column, url in (links or {}).items()})
        self._request("PATCH", f"pages/{page_id}", {"properties": props})

    def set_publication(self, page_id: str, status: str, error: Optional[str] = None) -> None:
        props = {"Publication": _select(status), "Erreur publication": _text(error)}
        self._request("PATCH", f"pages/{page_id}", {"properties": props})

    def set_link(self, page_id: str, column: str, url: str) -> None:
        self._request("PATCH", f"pages/{page_id}", {"properties": {column: {"url": url}}})

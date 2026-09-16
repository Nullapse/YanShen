"""Import exam papers from a public URL or a logged-in Fenbi session.

The Fenbi solution page is an Angular shell.  The useful question data is
loaded by the page with a logged-in request, so the importer accepts a
user-authorized, filtered local cookie jar and reproduces the API/static JSON
requests directly.  The browser bridge remains only as a network fallback.

``normalize_fenbi_payload`` deliberately follows Fenbi's real
``materials``/``solutions``/``materialKeys`` shape instead of guessing from
arbitrary metadata fields.  A paper without material正文 is rejected so a
failed fetch cannot silently become an unusable paper.

The resulting reference answers are deliberately marked as unreviewed by the
caller.  They are comparison material only; the AI solver prompt continues to
require an independent Shenlun solution from the original materials.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from ..paths import user_data_dir
from .paper_builder import parse_raw_paper_text

FENBI_PAGE_HOSTS = frozenset({"spa.fenbi.com", "www.fenbi.com", "fenbi.com"})
FENBI_API_HOST = "tiku.fenbi.com"
FENBI_STATIC_HOST_SUFFIXES = (".fbstatic.cn", ".fenbi.com")
FENBI_PROVIDER = "粉笔"
MAX_FETCH_BYTES = 8 * 1024 * 1024
MAX_BRIDGE_BYTES = 12 * 1024 * 1024
MAX_COOKIE_BYTES = 512 * 1024
IMPORT_SESSION_TTL_SECONDS = 30 * 60
FENBI_SESSION_FILE = "fenbi-session.json"
FENBI_COOKIE_DOMAIN = ".fenbi.com"


class UrlImportError(ValueError):
    """A user-actionable error raised while inspecting or normalizing a URL."""


@dataclass(frozen=True)
class SourceInfo:
    source_url: str
    provider: str
    source_kind: str
    key: str
    routecs: str
    requires_browser_session: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _normalise_cookie_domain(value: str) -> str:
    domain = str(value or "").strip().lower().rstrip(".")
    if not domain:
        return FENBI_COOKIE_DOMAIN
    return domain if domain.startswith(".") else f".{domain}"


def _fenbi_cookie_domain_allowed(domain: str) -> bool:
    bare = _normalise_cookie_domain(domain).lstrip(".")
    return bare == "fenbi.com" or bare.endswith(".fenbi.com")


def _valid_cookie_name(value: str) -> bool:
    return bool(re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", str(value or "")))


def _cookie_entry(name, value, *, domain=FENBI_COOKIE_DOMAIN, path="/", secure=False, expires=None):
    name = str(name or "").strip()
    value = str(value if value is not None else "")
    if not _valid_cookie_name(name) or any(char in value for char in "\r\n;"):
        return None
    domain = _normalise_cookie_domain(domain)
    if not _fenbi_cookie_domain_allowed(domain):
        return None
    try:
        expires_value = float(expires) if expires not in (None, "", 0) else None
    except (TypeError, ValueError):
        expires_value = None
    if expires_value is not None and expires_value < time.time():
        return None
    secure_value = secure if isinstance(secure, bool) else str(secure).strip().lower() in {"1", "true", "yes"}
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": str(path or "/").strip() or "/",
        "secure": secure_value,
        "expires": expires_value,
    }


def _cookie_json_items(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if "name" in value and "value" in value:
            return [value]
        for key in ("cookies", "items", "data"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
        # A few cookie tools export {"name": "value", ...} instead of an
        # array.  Treat only scalar values as cookies so metadata is ignored.
        return [{"name": key, "value": item} for key, item in value.items() if isinstance(item, (str, int, float))]
    return []


def parse_fenbi_credentials(cookie_text: str = "", device_id: str = "") -> dict:
    """Parse Cookie-Editor JSON or a Cookie request header for Fenbi only.

    The returned value is deliberately a structured, filtered cookie jar.  It
    is safe for the importer to select cookies by request host and path without
    ever passing credentials to an unrelated URL.
    """

    raw = html_lib.unescape(str(cookie_text or "")).lstrip("\ufeff").strip()
    if len(raw.encode("utf-8")) > MAX_COOKIE_BYTES:
        raise UrlImportError("Cookie 数据过大，已停止读取")

    entries = []
    if raw:
        parsed = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            for item in _cookie_json_items(parsed):
                if not isinstance(item, dict):
                    continue
                entry = _cookie_entry(
                    item.get("name"),
                    item.get("value", ""),
                    domain=item.get("domain", FENBI_COOKIE_DOMAIN),
                    path=item.get("path", "/"),
                    secure=item.get("secure", False),
                    expires=item.get("expirationDate", item.get("expires")),
                )
                if entry:
                    entries.append(entry)
        else:
            header = re.sub(r"^Cookie\s*:\s*", "", raw, flags=re.I)
            for item in header.split(";"):
                name, separator, value = item.strip().partition("=")
                if separator:
                    entry = _cookie_entry(name, value)
                    if entry:
                        entries.append(entry)

    # Last value wins, matching browser cookie semantics for duplicate names.
    deduped = {}
    for entry in entries:
        deduped[(entry["name"], entry["domain"], entry["path"])] = entry
    entries = list(deduped.values())

    supplied_device_id = str(device_id or "").strip()
    if supplied_device_id and (len(supplied_device_id) > 256 or any(char in supplied_device_id for char in "\r\n&=?")):
        raise UrlImportError("DeviceSid 格式无效")
    inferred_device_id = supplied_device_id
    if not inferred_device_id:
        for entry in entries:
            if entry["name"].lower() in {"device_id", "devicesid", "deviceid"}:
                inferred_device_id = entry["value"].strip()
                if inferred_device_id:
                    break
    if not entries and not inferred_device_id:
        raise UrlImportError("请粘贴 Cookie-Editor 导出的粉笔 Cookie，或填写 DeviceSid")
    return {
        "cookies": entries,
        "device_id": inferred_device_id,
        "saved_at": time.time(),
    }


def fenbi_credentials_summary(credentials: dict | None) -> dict:
    credentials = credentials if isinstance(credentials, dict) else {}
    cookies = credentials.get("cookies") if isinstance(credentials.get("cookies"), list) else []
    return {
        "configured": bool(cookies or credentials.get("device_id")),
        "cookie_count": len(cookies),
        "device_id_present": bool(str(credentials.get("device_id") or "").strip()),
        "saved_at": credentials.get("saved_at"),
    }


class FenbiCredentialStore:
    """Store an explicitly user-authorized Fenbi session outside the repo.

    This is a local convenience store, not an account vault.  The file is
    created with mode 0600 and contains only filtered Fenbi cookies.  Nothing
    is included in paper drafts or application logs.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else user_data_dir() / FENBI_SESSION_FILE
        self._lock = threading.RLock()

    def load(self) -> dict | None:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8")
                data = json.loads(raw)
            except (OSError, json.JSONDecodeError):
                return None
            if not isinstance(data, dict):
                return None
            try:
                return parse_fenbi_credentials(
                    json.dumps(data.get("cookies", []), ensure_ascii=False),
                    data.get("device_id", ""),
                )
            except UrlImportError:
                return None

    def save(self, credentials: dict) -> dict:
        normalized = parse_fenbi_credentials(
            json.dumps(credentials.get("cookies", []), ensure_ascii=False),
            credentials.get("device_id", ""),
        )
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        with self._lock:
            try:
                fd, temp_path = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(parent))
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(normalized, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.path)
                os.chmod(self.path, 0o600)
            finally:
                if temp_path:
                    try:
                        Path(temp_path).unlink()
                    except FileNotFoundError:
                        pass
        return normalized

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


FENBI_CREDENTIALS = FenbiCredentialStore()


class _VisibleTextParser(HTMLParser):
    """Small dependency-free HTML-to-text converter for public source pages."""

    _ignored = frozenset({"script", "style", "noscript", "template", "svg"})
    _break_before = frozenset({"address", "article", "blockquote", "br", "dd", "div", "dt", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "pre", "section", "tr"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._ignored:
            self._ignored_depth += 1
            return
        if not self._ignored_depth and tag in self._break_before and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._ignored:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if not self._ignored_depth and tag in self._break_before and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        text = html_lib.unescape("".join(self.parts)).replace("\xa0", " ")
        lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def html_to_text(value: str) -> str:
    value = str(value or "")
    if "<" not in value or ">" not in value:
        return html_lib.unescape(value).strip()
    parser = _VisibleTextParser()
    try:
        parser.feed(value)
        parser.close()
        return parser.text()
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(value))).strip()


def _text(value, *, fallback_keys=()) -> str:
    """Extract readable text from Fenbi's mixed HTML/object/list fields."""

    if value is None:
        return ""
    if isinstance(value, str):
        return html_to_text(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        values = [_text(item, fallback_keys=fallback_keys) for item in value]
        return "\n".join(item for item in values if item).strip()
    if isinstance(value, dict):
        keys = tuple(fallback_keys) + (
            "text", "content", "html", "value", "body", "title", "name", "answerText", "answer", "solution",
        )
        for key in keys:
            if key in value and value[key] not in (None, "", [], {}):
                result = _text(value[key], fallback_keys=fallback_keys)
                if result:
                    return result
        values = []
        for key, item in value.items():
            if key in {"id", "globalId", "type", "status", "idx", "index"}:
                continue
            result = _text(item, fallback_keys=fallback_keys)
            if result and result not in values:
                values.append(result)
        return "\n".join(values).strip()
    return str(value).strip()


def _first(mapping, keys, default=""):
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _first_text(mapping, keys, default="") -> str:
    return _text(_first(mapping, keys, default), fallback_keys=keys)


def _unwrap(value):
    if not isinstance(value, dict):
        return value
    current = value
    for _ in range(3):
        nested = current.get("data")
        if isinstance(nested, dict) and (
            any(key in nested for key in ("questions", "solutions", "materials", "staticUrl", "name"))
            or not any(key in current for key in ("questions", "solutions", "materials", "staticUrl"))
        ):
            current = nested
            continue
        break
    return current


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        for key in ("items", "list", "data", "values"):
            if isinstance(value.get(key), list):
                return value[key]
        return list(value.values())
    return [value]


def _safe_int(value, default=0):
    if isinstance(value, bool):
        return default
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _safe_score(value, default=0.0):
    try:
        number = float(str(value if value is not None else default).strip())
        return number if number == number else default
    except (TypeError, ValueError):
        return default


def _material_number(value, default):
    number = _safe_int(value, 0)
    return number if number > 0 else default


def _normalise_key(value) -> str:
    return str(value or "").strip().lower()


def _reference_values(value):
    """Yield scalar references from the several shapes used by Fenbi."""

    if isinstance(value, str):
        parts = re.split(r"[,，、\s]+", value.strip())
        return [part for part in parts if part]
    return _as_list(value)


def _material_indexes(prompt: str, item: dict, material_lookup=None, material_count=0) -> list[int]:
    material_lookup = material_lookup or {}
    values = []

    def add_value(value, *, zero_based=False):
        if isinstance(value, dict):
            value = _first(value, ("globalId", "global_id", "key", "id", "materialNumber", "number", "idx", "index"), "")
        key = _normalise_key(value)
        if key and key in material_lookup:
            number = material_lookup[key]
        else:
            number = _safe_int(value, 0)
            if zero_based and number >= 0:
                number += 1
        if number > 0 and (not material_count or number <= material_count) and number not in values:
            values.append(number)

    for key in ("materialKeys", "material_keys"):
        raw = item.get(key) if isinstance(item, dict) else None
        for value in _reference_values(raw):
            add_value(value)
    for key in ("materialNumbers", "material_numbers", "materialIds", "materialsIndex"):
        raw = item.get(key) if isinstance(item, dict) else None
        for value in _reference_values(raw):
            add_value(value)
    raw_indexes = item.get("materialIndexes") if isinstance(item, dict) else None
    for value in _reference_values(raw_indexes):
        add_value(value, zero_based=True)

    for match in re.finditer(r"(?:给定)?(?:资料|材料)\s*([0-9一二两三四五六七八九十]+)", prompt):
        raw_number = match.group(1)
        if raw_number.isdigit():
            number = int(raw_number)
        else:
            number = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}.get(raw_number, 0)
        if number and number not in values:
            values.append(number)
    return values


def _find_candidates(objects, keys):
    candidates = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for key in keys:
            value = obj.get(key)
            if value not in (None, "", [], {}):
                candidates.extend(_as_list(value))
    return candidates


def _find_collection(objects, keys):
    """Return the first explicit collection, without mixing metadata arrays."""

    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for key in keys:
            value = obj.get(key)
            if value in (None, "", [], {}):
                continue
            candidates = _as_list(value)
            if candidates:
                return candidates
    return []


def _source_name(meta: dict, source_url: str) -> str:
    name = _first_text(meta, ("name", "paperName", "examName", "exerciseName", "title"))
    if name and len(name) < 240:
        return name
    info = parse_source_url(source_url)
    return f"粉笔申论套卷 {info.key}"


def _infer_year(name: str, meta: dict) -> int:
    year = _safe_int(_first(meta, ("year", "examYear")), 0)
    if 1900 <= year <= 2100:
        return year
    match = re.search(r"20\d{2}", name or "")
    return int(match.group(0)) if match else 2024


def _infer_region(name: str, meta: dict) -> str:
    region = _first_text(meta, ("region", "province", "examArea", "area"))
    if region and len(region) <= 20:
        return region
    for candidate in ("浙江", "江苏", "山东", "广东", "四川", "湖北", "湖南", "河南", "河北", "北京", "上海", "重庆", "天津", "安徽", "福建", "江西", "陕西", "山西", "辽宁", "吉林", "黑龙江", "国家", "全国"):
        if candidate in name:
            return candidate
    return "全国"


def _infer_question_node(item: dict) -> dict:
    for key in ("question", "questionVO", "questionData", "questionInfo", "questionDetail"):
        value = item.get(key)
        if isinstance(value, dict):
            return value
    return item


def _is_reference_label(value) -> bool:
    label = _normalise_key(value).replace("_", "").replace("-", "")
    return label in {"reference", "referenceanswer", "answerreference", "参考答案"} or "参考答案" in label


def _accessory_reference(mapping: dict) -> str:
    if not isinstance(mapping, dict):
        return ""
    for key in ("solutionAccessories", "solution_accessories"):
        for accessory in _as_list(mapping.get(key)):
            if not isinstance(accessory, dict):
                continue
            label = _first(accessory, ("label", "name", "key", "type"), "")
            if not _is_reference_label(label):
                continue
            result = _first_text(accessory, ("content", "text", "html", "value", "answer"))
            if result:
                return result
    return ""


def _explicit_answer(mapping: dict) -> str:
    if not isinstance(mapping, dict):
        return ""
    for key in ("referenceAnswer", "reference_answer", "standardAnswer"):
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            result = _text(value, fallback_keys=("answer_text", "answerText", "content", "text", "html"))
            if result:
                return result
    correct = mapping.get("correctAnswer")
    if isinstance(correct, dict):
        result = _first_text(correct, ("answer", "content", "text", "html", "value"))
        if result:
            return result
    elif correct not in (None, "", [], {}):
        result = _text(correct)
        if result:
            return result
    for key in ("answerText", "answerContent", "answer"):
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            result = _text(value, fallback_keys=("answer_text", "answerText", "content", "text", "html"))
            if result:
                return result
    return ""


def _reference_answer_text(item: dict, question: dict) -> tuple[str, str]:
    """Return one candidate answer, preserving the source field priority."""

    for mapping in (item, question):
        result = _accessory_reference(mapping)
        if result:
            return result, "solutionAccessories.reference"
    for mapping in (item, question):
        result = _explicit_answer(mapping)
        if result:
            return result, "correctAnswer/referenceAnswer"
    for mapping in (item, question):
        value = mapping.get("solution") if isinstance(mapping, dict) else None
        if value not in (None, "", [], {}):
            result = _text(value, fallback_keys=("content", "text", "html", "solution"))
            if result:
                return result, "solution"
    return "", ""


def _score_value(item: dict, question: dict) -> int:
    for mapping in (item, question):
        value = _first(mapping, ("score", "point", "points", "totalScore", "scoreValue"))
        score = _safe_int(value, 0)
        if score > 0:
            return score
    return 20


def _static_objects(meta_payload, static_payloads):
    objects = []
    for value in [meta_payload, *(_as_list(static_payloads))]:
        value = _unwrap(value)
        if isinstance(value, dict) and isinstance(value.get("data"), list):
            values = value["data"]
        else:
            values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                objects.append(_unwrap(item))
    return objects


def normalize_fenbi_payload(meta_payload, static_payloads=None, source_url="", score_trees=None) -> dict:
    """Normalize a Fenbi API response plus its static JSON files.

    Fenbi has changed field names between exercise types.  This function uses
    a conservative set of aliases and never invents an answer when the
    payload contains no answer field.
    """

    if not source_url:
        raise UrlImportError("缺少粉笔原始 URL，无法建立来源关联")
    info = parse_source_url(source_url)
    objects = _static_objects(meta_payload, static_payloads)
    if not objects:
        raise UrlImportError("粉笔返回的数据为空，请确认页面已加载完成")

    meta = _unwrap(meta_payload)
    if not isinstance(meta, dict):
        meta = objects[0]
    paper_name = _source_name(meta, source_url)
    year = _infer_year(paper_name, meta)
    region = _infer_region(paper_name, meta)
    exam_type = _first_text(meta, ("examType", "exam_type", "subject", "categoryName"), "申论") or "申论"
    paper_category = _first_text(meta, ("paperCategory", "paper_category", "subType", "typeName"))

    material_candidates = _find_collection(objects, ("materials", "materialList", "materialInfos", "material"))
    if not material_candidates:
        # Only accept an explicitly named material-text field from the static
        # payload.  Generic `content`/`html` metadata is often the Angular
        # shell or a title, and importing it as material text caused the old
        # importer to produce plausible-looking but unusable papers.
        for obj in objects[1:]:
            fallback = _first_text(obj, ("materialText", "materialsText", "material_content", "materials_content"))
            if len(fallback) >= 20:
                material_candidates = [{"content": fallback, "title": "给定资料1"}]
                break
    materials = []
    seen_materials = set()
    material_lookup = {}
    for index, value in enumerate(material_candidates, start=1):
        if not isinstance(value, dict):
            content = _text(value)
            title = f"给定资料{index}"
            raw_number = index
            raw_keys = []
        else:
            content = _first_text(value, (
                "content", "materialText", "materialContent", "material_content", "text", "html", "body",
                "paragraphs", "sections",
            ))
            title = _first_text(value, ("title", "name", "materialName", "label"), f"给定资料{index}")
            raw_number = _first(value, ("materialNumber", "material_number", "number", "order", "idx", "index"), index)
            raw_keys = [
                _first(value, ("globalId", "global_id"), ""),
                _first(value, ("key", "materialKey"), ""),
                _first(value, ("id",), ""),
            ]
        content = content.strip()
        if not content or content in seen_materials:
            continue
        seen_materials.add(content)
        material_number = _material_number(raw_number, len(materials) + 1)
        materials.append({
            "material_number": material_number,
            "title": title or f"给定资料{len(materials) + 1}",
            "content": content,
            "material_text": content,
            "source_url": source_url,
        })
        for raw_key in raw_keys:
            key = _normalise_key(raw_key)
            if key:
                material_lookup[key] = material_number
    materials.sort(key=lambda item: item["material_number"])

    question_candidates = _find_collection(objects, ("solutions", "questions", "questionList", "question_list"))
    if not question_candidates:
        question_candidates = [
            value for value in _find_collection(objects, ("items",))
            if isinstance(value, dict) and any(
                key in value for key in ("content", "prompt", "stem", "question", "questionVO", "questionData")
            )
        ]
    questions = []
    seen_questions = set()
    for index, value in enumerate(question_candidates, start=1):
        if not isinstance(value, dict):
            value = {"content": value}
        question = _infer_question_node(value)
        source_global_id = str(_first(value, ("globalId", "key"), "") or _first(question, ("globalId", "id", "questionId"), "") or "").strip()
        prompt = _first_text(question, ("prompt", "stem", "questionText", "questionContent", "content", "title"))
        if not prompt:
            prompt = _first_text(value, ("prompt", "stem", "questionText", "questionContent", "content", "title"))
        prompt = prompt.strip()
        if not prompt:
            continue
        identity = (_first_text(value, ("globalId", "id", "questionId")) or prompt)[:500]
        if identity in seen_questions:
            continue
        seen_questions.add(identity)
        requirements = _first_text(value, ("requirements", "requirement", "answerRequirement", "answer_requirements"))
        if not requirements:
            requirements = _first_text(question, ("requirements", "requirement", "answerRequirement", "answer_requirements"))
        if not requirements:
            requirement_match = re.search(r"(?:要求|作答要求)[：:]\s*(.+)", prompt, re.S)
            requirements = requirement_match.group(1).strip() if requirement_match else "全面、准确、有条理。"
        score = _score_value(value, question)
        word_limit = _first_text(value, ("wordLimit", "word_limit", "字数要求", "answerWordLimit")) or _first_text(question, ("wordLimit", "word_limit", "字数要求", "answerWordLimit"))
        if not word_limit:
            limit_match = re.search(r"((?:不超过|不少于|控制在)?\s*\d+(?:[-—~～至]\d+)?\s*字(?:以内|以下|左右)?)", prompt + " " + requirements)
            word_limit = limit_match.group(1).strip() if limit_match else ""
        material_numbers = _material_indexes(prompt, value, material_lookup, len(materials))
        if material_numbers:
            chosen = [item for item in materials if item["material_number"] in material_numbers]
        else:
            chosen = materials
        q_materials = "\n\n".join(f"{item['title']}\n{item['content']}" for item in chosen)
        answer, answer_source = _reference_answer_text(value, question)
        # A Fenbi solution URL contributes exactly one reference answer per
        # question. Keep its source identity stable even when upstream payloads
        # expose generic provider labels such as “参考答案”.
        organization = FENBI_PROVIDER
        reference = None
        if answer:
            scoring_points = _first_text(value, ("scoringPoints", "scorePoints", "keyPoints", "keywords", "points"))
            score_tree = (score_trees or {}).get(source_global_id)
            reference = {
                "organization": organization,
                "answer_text": answer,
                "scoring_points": scoring_points,
                "notes": f"来自粉笔 URL 自动导入字段 {answer_source}；内容未核验；导入后作为本题唯一内容评分来源。",
                "score": score,
                "is_reviewed": 0,
            }
            if score_tree:
                reference["score_tree"] = score_tree
                reference["notes"] += " 已采集粉笔踩分树。"
        number = _safe_int(
            _first(value, ("questionNumber", "question_number", "number", "no", "order"), "")
            or _first(question, ("questionNumber", "question_number", "number", "no", "order"), ""),
            index,
        )
        question_type = _first_text(value, ("questionType", "question_type", "typeName")) or _first_text(question, ("questionType", "question_type", "typeName"))
        original_text = _first_text(value, ("originalText", "original_text", "rawText", "rawQuestion", "questionHtml")) or prompt
        if requirements and requirements not in original_text:
            original_text = f"{original_text}\n要求：{requirements}"
        questions.append({
            "question_number": number if number > 0 else index,
            "title": _first_text(value, ("title", "name"), f"第{index}题"),
            "question_type": question_type,
            "prompt": prompt,
            "original_text": original_text,
            "requirements": requirements,
            "word_limit": word_limit,
            "score": score,
            "materials": q_materials,
            "material_numbers": material_numbers,
            "reference_answer": reference,
            "source_url": source_url,
            "source_global_id": source_global_id,
            "source_kind": info.source_kind,
            "source_note": "由粉笔 URL 自动导入；每题仅保存一份粉笔参考答案，AI 只做评分与诊断。",
        })

    if not questions:
        raise UrlImportError("粉笔数据中没有识别到题目；请确认 URL 是套卷解析页并重新导入")
    if not materials:
        raise UrlImportError("粉笔数据中没有识别到材料正文，已停止导入，避免生成无材料的错误题目")

    reference_count = sum(1 for question in questions if question.get("reference_answer"))

    return {
        "source_url": source_url,
        "source_kind": info.source_kind,
        "source_provider": info.provider,
        "paper_name": paper_name,
        "year": year,
        "region": region,
        "exam_type": exam_type,
        "paper_category": paper_category,
        "materials": materials,
        "questions": questions,
        "import_diagnostics": {
            "source_objects": len(objects),
            "static_payloads": len(_as_list(static_payloads)),
            "materials": len(materials),
            "questions": len(questions),
            "candidate_references": reference_count,
            "score_tree_count": sum(1 for question in questions if question.get("reference_answer", {}).get("score_tree")),
        },
        "import_note": "URL 自动导入完成。粉笔参考答案未视为事实，AI 解题时必须以原始材料和 Shenlun.skill 独立推导。",
    }


def parse_source_url(source_url: str) -> SourceInfo:
    """Validate and classify a supported source URL."""

    raw = str(source_url or "").strip()
    if not raw:
        raise UrlImportError("请输入套卷 URL")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise UrlImportError("URL 必须以 http:// 或 https:// 开头")
    if parsed.username or parsed.password:
        raise UrlImportError("URL 不能包含账号或密码信息")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UrlImportError("URL 端口无效") from exc
    if port not in (None, 80, 443):
        raise UrlImportError("为安全起见，不支持非标准 URL 端口")
    if host not in FENBI_PAGE_HOSTS:
        raise UrlImportError("当前先支持粉笔套卷解析页（spa.fenbi.com/ti/exam/solution/...）")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[0] != "ti" or parts[1] != "exam" or parts[2] not in {"solution", "exercise"}:
        raise UrlImportError("未识别到粉笔套卷解析路径，应为 /ti/exam/solution/<套卷Key>")
    key = parts[3]
    if not key or len(key) > 300:
        raise UrlImportError("粉笔套卷 Key 无效")
    routecs = (parse_qs(parsed.query).get("routecs") or ["shenlun"])[0].strip() or "shenlun"
    # Keep only the routing selector.  Fenbi links may acquire transient
    # tracking/auth query parameters; they must not be copied into our local
    # session or database.
    clean_url = urlunparse((parsed.scheme.lower(), host, parsed.path, "", urlencode({"routecs": routecs}), ""))
    return SourceInfo(
        source_url=clean_url,
        provider=FENBI_PROVIDER,
        source_kind=f"fenbi_{parts[2]}",
        key=key,
        routecs=routecs,
        requires_browser_session=True,
    )


def build_fenbi_solution_api_url(info: SourceInfo, device_id: str = "") -> str:
    """Build the API request observed in the Fenbi web client."""

    endpoint = "getExercise" if info.source_kind == "fenbi_exercise" else "getSolution"
    params = {
        "format": "html",
        "key": info.key,
        "routecs": info.routecs,
        "kav": "125",
        "av": "127",
        "hav": "125",
        "app": "web",
        "apcid": "0",
        "gav": "2",
        "deviceId": str(device_id or "").strip(),
    }
    return f"https://{FENBI_API_HOST}/combine/exercise/{endpoint}?{urlencode(params)}"


def _cookie_path_matches(request_path: str, cookie_path: str) -> bool:
    cookie_path = cookie_path or "/"
    if not cookie_path.startswith("/"):
        cookie_path = "/"
    if request_path == cookie_path:
        return True
    if not request_path.startswith(cookie_path):
        return False
    return cookie_path.endswith("/") or request_path[len(cookie_path):].startswith("/")


def _cookie_header_for_url(credentials: dict | None, target_url: str) -> str:
    """Select only matching Fenbi cookies for one request host/path."""

    if not isinstance(credentials, dict):
        return ""
    parsed = urlparse(target_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not (host == FENBI_API_HOST or host.endswith(".fenbi.com") or host.endswith(".fbstatic.cn")):
        return ""
    request_path = parsed.path or "/"
    selected = []
    for cookie in credentials.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        domain = _normalise_cookie_domain(cookie.get("domain", FENBI_COOKIE_DOMAIN)).lstrip(".")
        if host != domain and not host.endswith(f".{domain}"):
            continue
        if not _cookie_path_matches(request_path, str(cookie.get("path") or "/")):
            continue
        if cookie.get("secure") and parsed.scheme.lower() != "https":
            continue
        expires = cookie.get("expires")
        if expires not in (None, "", 0):
            try:
                if float(expires) < time.time():
                    continue
            except (TypeError, ValueError):
                pass
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") if cookie.get("value") is not None else "")
        if _valid_cookie_name(name) and not any(char in value for char in "\r\n;"):
            selected.append((len(str(cookie.get("path") or "/")), name, value))
    # Prefer the most specific path and avoid sending duplicate names.
    selected.sort(key=lambda item: item[0], reverse=True)
    header = []
    seen = set()
    for _, name, value in selected:
        if name in seen:
            continue
        seen.add(name)
        header.append(f"{name}={value}")
    return "; ".join(header)


def _read_url(url: str, *, timeout=20, max_bytes=MAX_FETCH_BYTES, credentials=None, referer=""):
    parsed = urlparse(url)
    headers = {
        "Accept": "application/json, text/plain, text/html;q=0.9, */*;q=0.8",
        "User-Agent": "GongkaoPaperImporter/1.0",
    }
    cookie_header = _cookie_header_for_url(credentials, url)
    if cookie_header:
        headers["Cookie"] = cookie_header
    if referer and (urlparse(referer).hostname or "").lower().endswith(".fenbi.com"):
        headers["Referer"] = referer
    if parsed.hostname and parsed.hostname.lower() == FENBI_API_HOST:
        headers["Origin"] = "https://spa.fenbi.com"
    request = Request(
        url,
        headers=headers,
    )
    with urlopen(request, timeout=timeout) as response:
        content_length = _safe_int(response.headers.get("Content-Length"), 0)
        if content_length > max_bytes:
            raise UrlImportError("来源响应过大，已停止读取")
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise UrlImportError("来源响应超过安全大小限制")
        return data, response.headers.get_content_type() if response.headers else ""


def _decode_response_text(data, errors: str = "replace") -> str:
    """Decode response bytes, stripping UTF-8 BOM if present without requiring the utf-8-sig codec."""
    if not isinstance(data, (bytes, bytearray)):
        return str(data or "")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors)


def _decode_response_json(data):
    """Safely parse JSON from bytes or str, handling UTF-8 BOM cleanly."""
    if isinstance(data, (bytes, bytearray)):
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        try:
            return json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return json.loads(data.decode("utf-8", "replace"))
    return json.loads(data)


def _post_fenbi_json(url: str, payload, credentials=None, referer="", timeout=30) -> dict:
    """Post a JSON payload to a Fenbi host using only filtered local cookies."""

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not (host == FENBI_API_HOST or host.endswith(".fenbi.com")):
        raise UrlImportError("只允许向粉笔域名提交登录态请求")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": "GongkaoPaperImporter/1.0",
    }
    cookie_header = _cookie_header_for_url(credentials, url)
    if cookie_header:
        headers["Cookie"] = cookie_header
    if referer and (urlparse(referer).hostname or "").lower().endswith(".fenbi.com"):
        headers["Referer"] = referer
    if host == FENBI_API_HOST:
        headers["Origin"] = "https://spa.fenbi.com"
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_FETCH_BYTES + 1)
            if len(raw) > MAX_FETCH_BYTES:
                raise UrlImportError("粉笔提交响应过大，已停止读取")
            try:
                return _decode_response_json(raw)
            except (json.JSONDecodeError, ValueError):
                return {"_raw": _decode_response_text(raw, "replace")}
    except HTTPError as exc:
        raw = exc.read(MAX_FETCH_BYTES + 1)
        try:
            body = _decode_response_json(raw)
        except (json.JSONDecodeError, ValueError):
            body = {"_raw": _decode_response_text(raw, "replace")}
        body["_http_status"] = exc.code
        return body


def _extract_static_urls(meta: dict, base_url: str, routecs: str = "") -> list[str]:
    if not isinstance(meta, dict):
        return []
    raw = _first(meta, ("staticUrl", "static_url", "staticURL"), None)
    static_type = 0
    if isinstance(raw, dict):
        static_type = _safe_int(raw.get("type"), 0)
        raw = _first(raw, ("urls", "url", "items"), [])
    urls = []
    for value in _as_list(raw):
        if isinstance(value, dict):
            nested = _first(value, ("url", "href", "src"), "")
            if not nested and _first(value, ("urls",), None):
                for nested_url in _extract_static_urls({"staticUrl": value}, base_url, routecs):
                    if nested_url not in urls:
                        urls.append(nested_url)
                continue
            value = nested
        if not isinstance(value, str) or not value.strip():
            continue
        static_url = urljoin(base_url, value.strip())
        parsed = urlparse(static_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host:
            continue
        try:
            port = parsed.port
        except ValueError:
            continue
        if port not in (None, 80, 443):
            continue
        if not (host == "fbstatic.cn" or host.endswith(".fbstatic.cn") or host == "fenbi.com" or host.endswith(".fenbi.com")):
            continue
        if static_type == 1:
            separator = "&" if "?" in static_url else "?"
            static_url = f"{static_url}{separator}{urlencode({'routecs': routecs, 'type': '1'})}"
        if static_url not in urls:
            urls.append(static_url)
    return urls


def _fenbi_static_payloads(meta, base_url: str, routecs: str, credentials=None) -> list[dict]:
    if not isinstance(meta, dict):
        return []
    payloads = []
    for static_url in _extract_static_urls(meta, base_url, routecs):
        static_data, _ = _read_url(static_url, credentials=credentials, referer=base_url)
        payloads.append(_decode_response_json(static_data))
    return payloads


def _fenbi_common_params(device_id: str) -> dict:
    return {
        "kav": "125",
        "av": "127",
        "hav": "125",
        "app": "web",
        "apcid": "0",
        "gav": "2",
        "deviceId": str(device_id or "").strip(),
    }


def _normalise_fenbi_score_tree(node) -> dict | None:
    if not isinstance(node, dict):
        return None
    children = []
    for child in node.get("children") or []:
        normalized = _normalise_fenbi_score_tree(child)
        if normalized:
            children.append(normalized)
    return {
        "id": node.get("id"),
        "name": html_to_text(node.get("name") or ""),
        "score": _safe_score(node.get("score"), 0.0),
        "full_mark": _safe_score(node.get("fullMark"), 0.0),
        "comment": html_to_text(node.get("comment") or ""),
        "show": node.get("show") is not False,
        "children": children,
    }


def extract_fenbi_score_trees(payload) -> dict[str, dict]:
    """Extract all Fenbi score-analysis trees from a getSolution payload."""

    data = _unwrap(payload)
    if not isinstance(data, dict):
        return {}
    user_answers = data.get("userAnswers")
    if not isinstance(user_answers, dict):
        return {}
    trees = {}
    for key, user_answer in user_answers.items():
        if not isinstance(user_answer, dict):
            continue
        analysis = user_answer.get("analysisVO")
        if not isinstance(analysis, dict):
            continue
        tree = _normalise_fenbi_score_tree(analysis.get("scoreAnalysisVO"))
        if not tree:
            continue
        trees[str(key)] = tree
        question_id = analysis.get("questionId")
        if question_id is not None:
            trees[str(question_id)] = tree
    return trees


def _auto_submit_fenbi_exercise(
    info: SourceInfo,
    credentials: dict | None,
    original_source_url: str,
    examcatid: str,
    exercise_payload: dict,
) -> bool:
    """Submit short placeholder answers to unlock every Fenbi score tree."""

    exercise = _unwrap(exercise_payload)
    if not isinstance(exercise, dict) or not exercise.get("updateUserAnswerUrl") or not exercise.get("submitUrl"):
        return False
    status = exercise.get("status")
    if status not in (0, None):
        return False
    static_urls = _extract_static_urls(exercise, original_source_url, info.routecs)
    if not static_urls:
        return False
    static_data, _ = _read_url(static_urls[0], credentials=credentials, referer=info.source_url)
    static_payload = _decode_response_json(static_data)
    questions = static_payload.get("questions") or []
    updates = []
    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            continue
        global_id = str(_first(question, ("globalId", "key", "id"), "") or "").strip()
        if not global_id:
            continue
        question_type = _safe_int(_first(question, ("type",), 21), 21)
        answer_type = 204 if question_type in {21, 22, 23, 24, 25} else 203
        updates.append(
            {
                "key": global_id,
                "time": 0,
                "answer": {
                    "answer": f"研申导入占位作答 {index}",
                    "type": answer_type,
                },
            }
        )
    if not updates:
        return False

    device_id = str((credentials or {}).get("device_id") or "").strip()
    common = _fenbi_common_params(device_id)
    common["routecs"] = info.routecs
    update_url = exercise["updateUserAnswerUrl"] + ("&" if "?" in exercise["updateUserAnswerUrl"] else "?") + urlencode(common)
    response = _post_fenbi_json(update_url, updates, credentials=credentials, referer=info.source_url)
    if response.get("_http_status") in {409, 400} or response.get("code") not in (None, 1):
        return False

    submit_common = dict(common)
    if examcatid:
        submit_common["examcatid"] = examcatid
    submit_url = exercise["submitUrl"] + ("&" if "?" in exercise["submitUrl"] else "?") + urlencode(submit_common)
    _post_fenbi_json(submit_url, {}, credentials=credentials, referer=info.source_url)
    return True


def _credential_required_result(info: SourceInfo, message: str) -> dict:
    return {
        "ok": False,
        "draft": None,
        "source": info.as_dict(),
        # Keep the old flag for clients that still know how to use the
        # browser bridge, while the current UI prefers the simpler credential
        # form and direct server-side import.
        "requires_browser_bridge": True,
        "requires_credentials": True,
        "message": message,
    }


def fetch_source_draft(source_url: str, credentials: dict | None = None, auto_submit: bool = True) -> dict:
    """Fetch a Fenbi paper with the user's local session and normalize it."""

    info = parse_source_url(source_url)
    if info.provider != FENBI_PROVIDER:
        raise UrlImportError("暂不支持此来源")
    try:
        device_id = str((credentials or {}).get("device_id") or "").strip()
        examcatid = (parse_qs(urlparse(source_url).query).get("examcatid") or [""])[0]
        solution_info = info
        exercise_payload = None
        exercise_static_payloads = []
        if info.source_kind == "fenbi_exercise":
            exercise_api = build_fenbi_solution_api_url(info, device_id)
            exercise_data, _ = _read_url(exercise_api, credentials=credentials, referer=info.source_url)
            exercise_payload = _decode_response_json(exercise_data)
            exercise = _unwrap(exercise_payload)
            exercise_static_payloads = _fenbi_static_payloads(
                exercise,
                source_url,
                info.routecs,
                credentials,
            )
            if auto_submit and isinstance(exercise, dict) and exercise.get("status") in {0, None}:
                try:
                    _auto_submit_fenbi_exercise(
                        info,
                        credentials,
                        source_url,
                        examcatid,
                        exercise_payload,
                    )
                    time.sleep(1)
                except (UrlImportError, URLError, TimeoutError, json.JSONDecodeError):
                    pass
            solution_info = SourceInfo(
                source_url=info.source_url,
                provider=info.provider,
                source_kind="fenbi_solution",
                key=info.key,
                routecs=info.routecs,
                requires_browser_session=True,
            )

        api_url = build_fenbi_solution_api_url(solution_info, device_id)
        data, _ = _read_url(api_url, credentials=credentials, referer=info.source_url)
        payload = _decode_response_json(data)
        if isinstance(payload, dict):
            api_message = _first_text(payload, ("message", "msg", "error"))
            api_has_content = any(key in payload for key in ("data", "staticUrl", "static_url", "materials", "solutions", "questions"))
            if api_message and not api_has_content and any(
                marker in api_message.lower() for marker in ("devicesid", "登录", "无效", "权限", "auth")
            ):
                return _credential_required_result(info, api_message)
        meta = _unwrap(payload)
        static_payloads = _fenbi_static_payloads(
            meta,
            info.source_url,
            info.routecs,
            credentials,
        )
        score_trees = extract_fenbi_score_trees(payload)
        try:
            draft = normalize_fenbi_payload(
                payload,
                static_payloads,
                info.source_url,
                score_trees=score_trees,
            )
        except UrlImportError:
            if info.source_kind != "fenbi_exercise" or not exercise_payload or not exercise_static_payloads:
                raise
            draft = normalize_fenbi_payload(
                exercise_payload,
                exercise_static_payloads,
                info.source_url,
                score_trees=score_trees,
            )
        return {"ok": True, "draft": draft, "source": info.as_dict(), "requires_browser_bridge": False}
    except HTTPError as exc:
        if exc.code in {401, 403, 453}:
            return _credential_required_result(
                info,
                "粉笔登录态或 DeviceSid 无效/已过期。请在下方重新粘贴 Cookie-Editor 导出的粉笔 Cookie；成功后以后只需输入 URL。",
            )
        raise UrlImportError(f"粉笔接口返回 HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        return {
            "ok": False,
            "draft": None,
            "source": info.as_dict(),
            "requires_browser_bridge": True,
            "message": f"服务器无法直接访问粉笔接口（{exc}），可改用浏览器导入桥接。",
        }
    except json.JSONDecodeError as exc:
        raise UrlImportError("粉笔接口或静态资料返回的不是有效 JSON，请确认 URL 对应套卷解析页") from exc


def build_fenbi_bookmarklet(session_id: str, callback_url: str) -> str:
    """Return a one-click bookmarklet for a browser already logged into Fenbi.

    It fetches only the current tab's Fenbi API/static JSON with browser
    credentials, then posts the payload to the local app. It never sends
    cookies or passwords to this app.
    """

    session_literal = json.dumps(str(session_id), ensure_ascii=False)
    callback_literal = json.dumps(str(callback_url), ensure_ascii=False)
    script = f"""javascript:(async()=>{{
const session={session_literal};
const callback={callback_literal};
const page=new URL(location.href);
const fenbiHosts=new Set({json.dumps(sorted(FENBI_PAGE_HOSTS), ensure_ascii=False)});
const pageHost=page.hostname.toLowerCase().replace(/\\.$/,'');
if(!fenbiHosts.has(pageHost)){{alert('请在已登录的粉笔套卷解析页点击此书签脚本；不要直接在研申本地页面点击。');return;}}
const parts=page.pathname.split('/').filter(Boolean);
const mode=parts.includes('exercise')?'exercise':'solution';
const marker=parts.indexOf(mode);
const key=decodeURIComponent(marker>=0&&parts[marker+1]?parts[marker+1]:parts[parts.length-1]||'');
if(!key)throw new Error('未找到粉笔套卷 Key');
const routecs=page.searchParams.get('routecs')||'shenlun';
const deviceId=localStorage.getItem('deviceSid')||localStorage.getItem('device_id')||'';
const api=new URL('https://{FENBI_API_HOST}/combine/exercise/'+(mode==='exercise'?'getExercise':'getSolution'));
[['format','html'],['key',key],['routecs',routecs],['kav','125'],['av','127'],['hav','125'],['app','web'],['apcid','0'],['gav','2'],['deviceId',deviceId]].forEach(([k,v])=>api.searchParams.set(k,v));
const getJson=async url=>{{const response=await fetch(url,{{credentials:'include',headers:{{Accept:'application/json'}}}});if(!response.ok)throw new Error('粉笔接口 HTTP '+response.status);return response.json();}};
const post=async payload=>{{const response=await fetch(callback,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{session_id:session,source_url:location.href,payload}})}});const result=await response.json();if(!result.ok)throw new Error(result.error||'本地导入失败');return result;}};
try{{
 const metaPayload=await getJson(api.toString());
 const meta=metaPayload&&metaPayload.data?metaPayload.data:metaPayload;
 const urls=meta&&meta.staticUrl&&Array.isArray(meta.staticUrl.urls)?meta.staticUrl.urls:[];
 const staticPayloads=[];
 for(const value of urls){{const url=typeof value==='string'?value:value&&value.url;if(url)staticPayloads.push(await getJson(new URL(url,page.href).toString()));}}
 const result=await post({{meta,static_payloads:staticPayloads}});
 alert('已导入 '+((result.draft&&result.draft.questions)||[]).length+' 道题，正在打开研申预览页');
 if(result.redirect)location.href=result.redirect;
}}catch(error){{
 try{{const result=await post({{raw_text:document.body?document.body.innerText:'',fallback_error:String(error)}});alert('已用页面文字导入，正在打开研申预览页');if(result.redirect)location.href=result.redirect;}}
 catch(fallbackError){{alert('导入失败：'+(fallbackError.message||error.message||error));}}
}}
}})();void 0"""
    return re.sub(r"\s+", " ", script).strip()


class ImportSessionStore:
    """Short-lived in-memory handoff; no source credentials are persisted."""

    def __init__(self, ttl_seconds=IMPORT_SESSION_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}

    def _purge(self):
        cutoff = time.time() - self.ttl_seconds
        for key, value in list(self._sessions.items()):
            if value.get("created_at", 0) < cutoff:
                self._sessions.pop(key, None)

    def create(self, source: SourceInfo, draft=None) -> str:
        session_id = uuid.uuid4().hex
        with self._lock:
            self._purge()
            self._sessions[session_id] = {"created_at": time.time(), "source": source.as_dict(), "draft": draft}
        return session_id

    def get(self, session_id: str):
        with self._lock:
            self._purge()
            item = self._sessions.get(str(session_id or ""))
            return dict(item) if item else None

    def set_draft(self, session_id: str, draft: dict) -> bool:
        with self._lock:
            self._purge()
            item = self._sessions.get(str(session_id or ""))
            if not item:
                return False
            item["draft"] = draft
            return True


IMPORT_SESSIONS = ImportSessionStore()


def draft_from_bridge_payload(payload: dict, source_url: str) -> dict:
    """Normalize the browser bridge body, including a text fallback."""

    if not isinstance(payload, dict):
        raise UrlImportError("浏览器桥接数据格式无效")
    if payload.get("raw_text"):
        parsed = parse_raw_paper_text(str(payload["raw_text"]))
        if not parsed.get("questions"):
            raise UrlImportError("当前页面文字中没有识别到题目，请等待解析页加载后再导入")
        if not parsed.get("materials"):
            raise UrlImportError("当前页面文字中没有识别到材料正文，已停止导入，请等待材料加载后再导入")
        info = parse_source_url(source_url)
        parsed.update({
            "source_url": info.source_url,
            "source_kind": info.source_kind,
            "source_provider": info.provider,
            "import_note": "由浏览器页面文字自动导入；粉笔参考答案用于报告展示，AI 只做评分与诊断。",
        })
        for question in parsed.get("questions", []):
            question["source_url"] = info.source_url
            question["source_kind"] = info.source_kind
            question["source_note"] = "由粉笔 URL 自动导入；参考答案为未核验候选。"
            reference = question.get("reference_answer")
            if isinstance(reference, dict):
                reference["is_reviewed"] = 0
                reference["organization"] = FENBI_PROVIDER
                reference["notes"] = "来自粉笔 URL 自动导入；内容未核验；导入后作为本题唯一内容评分来源，AI 仅负责合理划点和语义判分。"
        return parsed
    score_trees = extract_fenbi_score_trees(payload.get("analysis_payload", payload.get("meta", payload)))
    return normalize_fenbi_payload(
        payload.get("meta", payload),
        payload.get("static_payloads", []),
        source_url,
        score_trees=score_trees,
    )


def public_import_summary(result: dict) -> dict:
    """Return a UI-safe summary without exposing raw API payloads."""

    draft = result.get("draft") if isinstance(result, dict) else None
    if not draft:
        return result
    return {
        **result,
        "draft": {
            **draft,
            "materials": [{"material_number": item.get("material_number"), "title": item.get("title"), "content": item.get("content", "")} for item in draft.get("materials", [])],
            "questions": [
                {
                    **question,
                    "prompt": question.get("prompt", ""),
                    "reference_answer": question.get("reference_answer"),
                }
                for question in draft.get("questions", [])
            ],
        },
    }

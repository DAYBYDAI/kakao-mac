#!/usr/bin/env python3
"""kakao-mac helper: find the local KakaoTalk (macOS) database key for the
logged-in user and run read-only kakaocli commands with it.

Adapted from NomaDamas/k-skill `kakaotalk-mac/scripts/kakaotalk_mac.py` (MIT).
Attribution is in THIRD_PARTY_NOTICES.md at the repository root.

What this file does:
- Reads only. It never writes to the KakaoTalk database and cannot send messages.
- Derives the database key from this Mac's hardware UUID and the logged-in
  Kakao user id (the same inputs the KakaoTalk app uses), then checks the key by
  opening the local database through `kakaocli query`.
- Stores the result in ~/.kakao-mac/auth.json (mode 0600). The key is never printed.
- Uses no network.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import multiprocessing as mp
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


EMPTY_ACCOUNT_HASH = (
    "31bca02094eb78126a517b206a88c73cfa9ec6f704c7030d18212cace820f025"
    "f00bf0ea68dbf3f3a5436ca63b53bf7bf80ad8d5de7d8359d0b7fed9dbc3ab99"
)
HEX_DATABASE_PATTERN = re.compile(r"^[0-9a-f]{78}(?:\.db)?$")
DIRECT_USER_ID_KEYS = ("userId", "user_id", "KAKAO_USER_ID", "userID")
DEFAULT_MAX_USER_ID = 1_000_000_000
DEFAULT_CHUNK_SIZE = 500_000
HOME_DIR = Path(os.environ.get("KAKAO_MAC_HOME") or (Path.home() / ".kakao-mac")).expanduser()
DEFAULT_CACHE_PATH = HOME_DIR / "auth.json"
READ_ONLY_COMMANDS = ("chats", "messages", "search", "schema")
ROOM_TYPE_LABELS = {0: "1:1", 1: "단톡", 2: "채널", 3: "오픈DM", 4: "오픈챗", 5: "나와의 채팅"}
QUERY_TIMEOUT_SECONDS = 120
MEMBER_TITLE_NAMES = 3
STALE_DATABASE_SECONDS = 7 * 24 * 60 * 60


def kakaocli_bin() -> str:
    """kakaocli on PATH, or the copy built by hand into ~/.kakao-mac/bin."""
    return shutil.which("kakaocli") or str(HOME_DIR / "bin" / "kakaocli")


class AuthResolutionError(RuntimeError):
    pass


@dataclass
class DetectionState:
    uuid: str
    candidate_user_ids: list[int]
    active_account_hash: str | None
    database_files: list[Path]
    active_account_hashes: list[str] = field(default_factory=list)


@dataclass
class ResolvedAuth:
    user_id: int
    uuid: str
    database_path: Path
    database_name: str
    key: str
    source: str


def parse_plist_xml(xml_text: str) -> Any:
    tokens = tokenize_plist_xml(xml_text)
    if not tokens:
        raise AuthResolutionError("plist XML was empty")
    index = 0
    if tokens[index] != ("start", "plist"):
        raise AuthResolutionError("plist XML did not start with <plist>")
    value, index = _parse_plist_tokens(tokens, index + 1)
    if tokens[index] != ("end", "plist"):
        raise AuthResolutionError("plist XML did not end with </plist>")
    return value


def tokenize_plist_xml(xml_text: str) -> list[tuple[str, str]]:
    normalized = re.sub(r"<\?xml[^>]*\?>", "", xml_text)
    normalized = re.sub(r"<!DOCTYPE[^>]*>", "", normalized)
    normalized = re.sub(r"<([A-Za-z0-9]+)\s*/>", r"<\1></\1>", normalized)
    normalized = (
        normalized.replace("\r", "")
    )
    tokens: list[tuple[str, str]] = []
    position = 0
    for match in re.finditer(r"<(/?)([A-Za-z0-9]+)(?: [^>]*)?>", normalized):
        text = normalized[position : match.start()]
        stripped = _unescape_xml(text).strip()
        if stripped:
            tokens.append(("text", stripped))
        token_type = "end" if match.group(1) else "start"
        tokens.append((token_type, match.group(2)))
        position = match.end()
    trailing = _unescape_xml(normalized[position:]).strip()
    if trailing:
        tokens.append(("text", trailing))
    return [token for token in tokens if token[0] != "text" or token[1]]


def _parse_plist_tokens(tokens: list[tuple[str, str]], index: int) -> tuple[Any, int]:
    token_type, tag = tokens[index]
    if token_type != "start":
        raise AuthResolutionError(f"Unexpected token {tokens[index]!r}")

    if tag == "dict":
        result: dict[str, Any] = {}
        index += 1
        while tokens[index] != ("end", "dict"):
            if tokens[index] != ("start", "key"):
                raise AuthResolutionError(f"Expected dict key, got {tokens[index]!r}")
            key, index = _parse_scalar(tokens, index, "key", lambda value: value)
            value, index = _parse_plist_tokens(tokens, index)
            result[key] = value
        return result, index + 1

    if tag == "array":
        items: list[Any] = []
        index += 1
        while tokens[index] != ("end", "array"):
            value, index = _parse_plist_tokens(tokens, index)
            items.append(value)
        return items, index + 1

    if tag == "integer":
        return _parse_scalar(tokens, index, "integer", int)
    if tag == "real":
        return _parse_scalar(tokens, index, "real", float)
    if tag == "string":
        return _parse_scalar(tokens, index, "string", lambda value: value)
    if tag == "date":
        return _parse_scalar(tokens, index, "date", lambda value: value)
    if tag == "data":
        return _parse_scalar(tokens, index, "data", lambda value: value)
    if tag == "true":
        return True, index + 2
    if tag == "false":
        return False, index + 2
    raise AuthResolutionError(f"Unsupported plist tag: {tag}")


def _parse_scalar(
    tokens: list[tuple[str, str]],
    index: int,
    tag: str,
    caster: Callable[[str], Any],
) -> tuple[Any, int]:
    if tokens[index] != ("start", tag):
        raise AuthResolutionError(f"Expected <{tag}>, got {tokens[index]!r}")
    text = ""
    index += 1
    if tokens[index][0] == "text":
        text = tokens[index][1]
        index += 1
    if tokens[index] != ("end", tag):
        raise AuthResolutionError(f"Expected </{tag}>, got {tokens[index]!r}")
    return caster(text), index + 1


def _unescape_xml(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )


def collect_candidate_user_ids(plist_data: dict[str, Any]) -> list[int]:
    candidates: list[int] = []
    for key in DIRECT_USER_ID_KEYS:
        value = plist_data.get(key)
        if isinstance(value, int) and value > 0:
            candidates.append(value)
        elif isinstance(value, str) and value.isdigit():
            candidates.append(int(value))

    alert_ids = plist_data.get("AlertKakaoIDsList", [])
    if isinstance(alert_ids, list):
        for item in alert_ids:
            if isinstance(item, int) and item > 0:
                candidates.append(item)
            elif isinstance(item, str) and item.isdigit():
                numeric = int(item)
                if numeric > 0:
                    candidates.append(numeric)

    return unique_ints(candidates)


def find_active_account_hashes(plist_data: dict[str, Any]) -> list[str]:
    """SHA-512 hashes of the Kakao account ids this Mac's KakaoTalk has signed in with."""
    prefix = "DESIGNATEDFRIENDSREVISION:"
    hashes: list[str] = []
    for key, value in plist_data.items():
        if not key.startswith(prefix):
            continue
        hash_hex = key[len(prefix) :]
        if hash_hex == EMPTY_ACCOUNT_HASH:
            continue
        if not re.fullmatch(r"[0-9a-f]{128}", hash_hex):
            continue
        numeric_value = 0
        if isinstance(value, (int, float)):
            numeric_value = int(value)
        elif isinstance(value, str) and value.isdigit():
            numeric_value = int(value)
        if numeric_value != 0 and hash_hex not in hashes:
            hashes.append(hash_hex)
    return hashes


def find_active_account_hash(plist_data: dict[str, Any]) -> str | None:
    hashes = find_active_account_hashes(plist_data)
    return hashes[0] if hashes else None


def account_hash(user_id: int) -> str:
    return hashlib.sha512(str(user_id).encode("utf-8")).hexdigest()


def discover_database_files(container_path: Path) -> list[Path]:
    if not container_path.exists():
        return []
    return sorted(
        [path for path in container_path.iterdir() if path.is_file() and HEX_DATABASE_PATTERN.fullmatch(path.name)],
        key=lambda item: item.name,
    )


def unique_ints(values: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def recover_user_id_from_sha512(
    hex_hash: str,
    *,
    max_user_id: int = DEFAULT_MAX_USER_ID,
    workers: int | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int | None:
    if not re.fullmatch(r"[0-9a-f]{128}", hex_hash):
        raise ValueError("expected 128-char lowercase sha512 hex digest")
    if max_user_id < 0:
        raise ValueError("max_user_id must be non-negative")

    normalized_workers = max(1, workers or (os.cpu_count() or 1))
    if normalized_workers == 1:
        return _scan_user_id_range((0, max_user_id + 1, hex_hash))

    start_method = "fork" if "fork" in mp.get_all_start_methods() else mp.get_start_method()
    ctx = mp.get_context(start_method)
    job_iter = (
        (start, min(start + chunk_size, max_user_id + 1), hex_hash)
        for start in range(0, max_user_id + 1, chunk_size)
    )
    with ctx.Pool(processes=normalized_workers) as pool:
        for result in pool.imap_unordered(_scan_user_id_range, job_iter, chunksize=1):
            if result is not None:
                pool.terminate()
                return result
    return None


def _scan_user_id_range(job: tuple[int, int, str]) -> int | None:
    start, end, hex_hash = job
    target = hex_hash.encode("ascii")
    for user_id in range(start, end):
        if hashlib.sha512(str(user_id).encode("utf-8")).hexdigest().encode("ascii") == target:
            return user_id
    return None


def database_name(user_id: int, uuid: str) -> str:
    hawawa = ".".join([".", "F", str(user_id), "A", "F", uuid[::-1], ".", "|"])
    salt = hashed_device_uuid(uuid)[::-1].encode("utf-8")
    derived = hashlib.pbkdf2_hmac("sha256", hawawa.encode("utf-8"), salt, 100_000, 128)
    hex_value = derived.hex()
    return hex_value[28 : 28 + 78]


def secure_key(user_id: int, uuid: str) -> str:
    hashed = hashed_device_uuid(uuid)
    parts = ["A", hashed, "|", "F", uuid[:5], "H", str(user_id), "|", uuid[7:]]
    hawawa = "F".join(parts)[::-1].encode("utf-8")
    salt = uuid[int(len(uuid) * 0.3) :].encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", hawawa, salt, 100_000, 128).hex()


def hashed_device_uuid(uuid: str) -> str:
    uuid_bytes = uuid.encode("utf-8")
    combined = hashlib.sha1(uuid_bytes).digest() + hashlib.sha256(uuid_bytes).digest()
    return base64.b64encode(combined).decode("ascii")


def resolve_auth_state(
    state: DetectionState,
    *,
    verify_access: Callable[[ResolvedAuth], bool],
    cache_path: Path | None = None,
    user_id_override: int | None = None,
    max_user_id: int = DEFAULT_MAX_USER_ID,
    workers: int | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ResolvedAuth:
    if not state.database_files:
        raise AuthResolutionError(
            "No KakaoTalk database files were found. Either KakaoTalk for Mac has never been logged in on this "
            "macOS account, or macOS is blocking access. " + FULL_DISK_ACCESS_HINT
        )

    known_hashes = list(state.active_account_hashes)
    if state.active_account_hash and state.active_account_hash not in known_hashes:
        known_hashes.insert(0, state.active_account_hash)

    candidates = list(state.candidate_user_ids)
    if known_hashes:
        # KakaoTalk records which account ids have signed in on this Mac. Ids that do not match
        # (alert lists, leftovers) are not tried, so a stale database is never opened by accident.
        candidates = [user_id for user_id in candidates if account_hash(user_id) in known_hashes]
    if user_id_override is not None:
        candidates = [user_id_override, *candidates]
    candidates = unique_ints(candidates)

    for user_id in candidates:
        resolved = _try_resolved_auth(user_id, "candidate", state, verify_access)
        if resolved is not None:
            persist_auth_cache(resolved, cache_path)
            return resolved

    if known_hashes:
        print(
            "Looking for the logged-in account id by hashing numbers locally. "
            "This uses the CPU for up to a few minutes and sends nothing over the network.",
            file=sys.stderr,
            flush=True,
        )
    for hex_hash in known_hashes:
        recovered = recover_user_id_from_sha512(
            hex_hash,
            max_user_id=max_user_id,
            workers=workers,
            chunk_size=chunk_size,
        )
        if recovered is not None and recovered not in candidates:
            resolved = _try_resolved_auth(recovered, "hash-recovery", state, verify_access)
            if resolved is not None:
                persist_auth_cache(resolved, cache_path)
                return resolved

    raise AuthResolutionError(
        "Failed to resolve a working KakaoTalk auth key. "
        "Try a larger --max-user-id or pass --user-id explicitly."
    )


def _try_resolved_auth(
    user_id: int,
    source: str,
    state: DetectionState,
    verify_access: Callable[[ResolvedAuth], bool],
) -> ResolvedAuth | None:
    derived_name = database_name(user_id, state.uuid)
    key = secure_key(user_id, state.uuid)
    for database_path in prioritized_database_paths(state.database_files, derived_name):
        resolved = ResolvedAuth(
            user_id=user_id,
            uuid=state.uuid,
            database_path=database_path,
            database_name=derived_name,
            key=key,
            source=source,
        )
        if verify_access(resolved):
            return resolved
    return None


def prioritized_database_paths(database_files: Sequence[Path], derived_name: str) -> list[Path]:
    preferred_names = {derived_name, f"{derived_name}.db"}
    preferred = [path for path in database_files if path.name in preferred_names]
    fallback = [path for path in database_files if path.name not in preferred_names]
    return [*preferred, *fallback]


def load_cached_auth(cache_path: Path) -> ResolvedAuth | None:
    # The cache holds the database key. Use it only when it is a plain file owned by this user,
    # and tighten its mode if something loosened it.
    try:
        info = os.lstat(cache_path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return None
    if info.st_mode & 0o077:
        try:
            os.chmod(cache_path, 0o600)
        except OSError:
            return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        database_path = Path(payload["database_path"]).expanduser()
        user_id = int(payload["user_id"])
        uuid = str(payload["uuid"])
        database_name = str(payload["database_name"])
        key = str(payload["key"])
        source = str(payload.get("source", "cache"))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None

    if user_id <= 0 or not uuid or not database_name or not key or not database_path.exists():
        return None

    return ResolvedAuth(
        user_id=user_id,
        uuid=uuid,
        database_path=database_path,
        database_name=database_name,
        key=key,
        source=source,
    )


def persist_auth_cache(resolved: ResolvedAuth, cache_path: Path | None) -> None:
    if cache_path is None:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.parent == HOME_DIR:
        try:
            os.chmod(HOME_DIR, 0o700)
        except OSError:
            pass
    payload = {
        "user_id": resolved.user_id,
        "uuid": resolved.uuid,
        "database_path": str(resolved.database_path),
        "database_name": resolved.database_name,
        "key": resolved.key,
        "source": resolved.source,
    }
    # The cache holds the database key, so it is written to a fresh owner-only file and then swapped in.
    fd, tmp_name = tempfile.mkstemp(dir=str(cache_path.parent), prefix=".auth-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp_name, cache_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def platform_uuid() -> str:
    result = run_command(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], check=True)
    match = re.search(r'"IOPlatformUUID" = "([0-9A-F-]+)"', result.stdout)
    if not match:
        raise AuthResolutionError("Could not read IOPlatformUUID from ioreg output.")
    return match.group(1)


def convert_plist_to_xml(plist_path: Path) -> str:
    result = run_command(["/usr/bin/plutil", "-convert", "xml1", "-o", "-", str(plist_path)], check=True)
    return result.stdout


def read_plist_snapshot(plist_path: Path) -> dict[str, Any]:
    return parse_plist_xml(convert_plist_to_xml(plist_path))


def collect_detection_state(uuid_override: str | None = None) -> DetectionState:
    uuid = uuid_override or platform_uuid()
    snapshots = []
    for plist_path in preference_paths():
        if plist_path.exists():
            snapshots.append(read_plist_snapshot(plist_path))

    candidate_user_ids: list[int] = []
    account_hashes: list[str] = []
    for snapshot in snapshots:
        candidate_user_ids.extend(collect_candidate_user_ids(snapshot))
        for hex_hash in find_active_account_hashes(snapshot):
            if hex_hash not in account_hashes:
                account_hashes.append(hex_hash)

    return DetectionState(
        uuid=uuid,
        candidate_user_ids=unique_ints(candidate_user_ids),
        active_account_hash=account_hashes[0] if account_hashes else None,
        database_files=discover_database_files(container_path()),
        active_account_hashes=account_hashes,
    )


def current_account_hashes() -> list[str]:
    """Hashes of the account ids KakaoTalk has signed in with, read fresh from its preferences."""
    hashes: list[str] = []
    for plist_path in preference_paths():
        if not plist_path.exists():
            continue
        for hex_hash in find_active_account_hashes(read_plist_snapshot(plist_path)):
            if hex_hash not in hashes:
                hashes.append(hex_hash)
    return hashes


def preference_paths() -> list[Path]:
    pref_dir = (
        Path.home()
        / "Library"
        / "Containers"
        / "com.kakao.KakaoTalkMac"
        / "Data"
        / "Library"
        / "Preferences"
    )
    paths = sorted(pref_dir.glob("com.kakao.KakaoTalkMac*.plist"))
    global_pref = Path.home() / "Library" / "Preferences" / "com.kakao.KakaoTalkMac.plist"
    if global_pref.exists():
        paths.append(global_pref)
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            deduped.append(path)
    return deduped


def container_path() -> Path:
    return (
        Path.home()
        / "Library"
        / "Containers"
        / "com.kakao.KakaoTalkMac"
        / "Data"
        / "Library"
        / "Application Support"
        / "com.kakao.KakaoTalkMac"
    )


def verify_database_access(resolved: ResolvedAuth) -> bool:
    try:
        result = subprocess.run(
            [
                kakaocli_bin(),
                "query",
                "SELECT count(*) FROM sqlite_master",
                "--db",
                str(resolved.database_path),
                "--key",
                resolved.key,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def run_command(args: Sequence[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise AuthResolutionError(result.stderr.strip() or result.stdout.strip() or f"command failed: {' '.join(args[:2])}")
    return result


def redact_secret(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "<redacted>")


def run_query(auth: ResolvedAuth, sql: str) -> list[Any]:
    """Run one SELECT that is written in this file. Text typed by a user or taken
    from a chat is never placed into the SQL string."""
    try:
        result = subprocess.run(
            [kakaocli_bin(), "query", sql, "--db", str(auth.database_path), "--key", auth.key],
            capture_output=True,
            text=True,
            check=False,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # The original exception text carries the full command line, key included.
        raise AuthResolutionError("kakaocli query timed out") from None
    if result.returncode != 0:
        message = redact_secret(result.stderr.strip() or result.stdout.strip(), auth.key)
        if WRONG_KEY_MARKER in message:
            raise AuthResolutionError(WRONG_KEY_HINT)
        raise AuthResolutionError(message[:300] or "kakaocli query failed")
    text = result.stdout.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise AuthResolutionError("Could not parse kakaocli query output") from None
    if not isinstance(payload, list):
        raise AuthResolutionError("kakaocli query returned an unexpected shape")
    return payload


ROOMS_SQL = (
    "SELECT r.chatId, r.type, r.chatName, r.linkId, r.activeMembersCount, r.lastUpdatedAt, "
    "(SELECT l.linkName FROM NTOpenLink l WHERE l.linkId = r.linkId LIMIT 1), "
    "(SELECT m.content FROM NTChatMeta m WHERE m.chatId = r.chatId AND m.type = 3 LIMIT 1), "
    "(SELECT m.noticeText FROM NTChatMeta m WHERE m.chatId = r.chatId AND m.type IN (3, 15) "
    "AND m.noticeText IS NOT NULL AND m.noticeText != '' LIMIT 1), "
    "(SELECT COALESCE(NULLIF(u.friendNickName, ''), u.nickName) FROM NTUser u "
    "WHERE u.userId = r.directChatMemberUserId AND r.directChatMemberUserId != 0 LIMIT 1) "
    "FROM NTChatRoom r ORDER BY r.lastUpdatedAt DESC"
)
ROOMS_SQL_COLUMNS = 10


def nfc(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or ""))


def room_title(row: Sequence[Any]) -> str:
    chat_id, room_type, chat_name, link_id, _members, _last, link_name, meta_title, notice_text, direct_name = row
    title = nfc(chat_name).strip()
    if not title and room_type == 4 and link_id:
        title = nfc(link_name).strip()
    if not title:
        title = nfc(meta_title).strip()
    if not title and notice_text:
        try:
            title = nfc(json.loads(notice_text).get("name")).strip()
        except (ValueError, AttributeError, TypeError):
            title = ""
    if not title:
        title = nfc(direct_name).strip()
    if not title and room_type == 5:
        title = "나와의 채팅"
    return title


def member_titles(auth: ResolvedAuth, chat_ids: Sequence[int]) -> dict[int, str]:
    """Titles for rooms that store no name, built from who wrote there (other than me).
    Only integers from the database go into this SQL."""
    if not chat_ids:
        return {}
    id_list = ",".join(str(int(chat_id)) for chat_id in chat_ids)
    sql = (
        "SELECT m.chatId, COALESCE(NULLIF(u.friendNickName, ''), u.nickName), MAX(m.sentAt) "
        "FROM NTChatMessage m JOIN NTUser u ON u.userId = m.authorId "
        f"WHERE m.chatId IN ({id_list}) AND m.authorId != {int(auth.user_id)} "
        "GROUP BY m.chatId, m.authorId ORDER BY m.chatId, MAX(m.sentAt) DESC"
    )
    names: dict[int, list[str]] = {}
    for row in run_query(auth, sql):
        if not isinstance(row, list) or len(row) != 3:
            continue
        name = nfc(row[1]).strip()
        if not name:
            continue
        bucket = names.setdefault(int(row[0]), [])
        if name not in bucket:
            bucket.append(name)
    titles: dict[int, str] = {}
    for chat_id, bucket in names.items():
        title = ", ".join(bucket[:MEMBER_TITLE_NAMES])
        if len(bucket) > MEMBER_TITLE_NAMES:
            title += " 외"
        titles[chat_id] = title
    return titles


def format_timestamp(value: Any) -> str | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 10_000_000_000:  # stored in milliseconds
        number //= 1000
    try:
        # Local time with its UTC offset, e.g. 2026-10-10T17:42+09:00.
        return datetime.fromtimestamp(number).astimezone().isoformat(timespec="minutes")
    except (OverflowError, OSError, ValueError):
        return None


def list_rooms(
    auth: ResolvedAuth,
    *,
    query: str | None,
    limit: int,
    include_channels: bool,
    only_type: str | None = None,
) -> list[dict[str, Any]]:
    """Rooms with their real titles, newest first. Many rooms store an empty
    chatName, so the title is filled from the open-link name, room meta, or the
    other member's name ("title_source": "name"). Rooms that still have none get
    the names of the people who wrote there ("members"), or chat<id> ("unknown").
    The search text is matched here in Python."""
    needle = nfc(query).strip().casefold() if query else ""
    rooms: list[dict[str, Any]] = []
    untitled: list[int] = []
    for row in run_query(auth, ROOMS_SQL):
        if not isinstance(row, list) or len(row) != ROOMS_SQL_COLUMNS:
            # An empty list would read as "no rooms". Say that the layout is not the expected one.
            raise AuthResolutionError(
                "The KakaoTalk database layout is not what this tool expects. Check the KakaoTalk version."
            )
        room_type = row[1]
        if room_type == 2 and not include_channels:
            continue
        title = room_title(row)
        if not title:
            untitled.append(int(row[0]))
        rooms.append(
            {
                "chat_id": str(row[0]),
                "title": title,
                "title_source": "name" if title else "unknown",
                "type": ROOM_TYPE_LABELS.get(room_type, f"기타({room_type})"),
                "members": int(row[4] or 0),
                "last_at": format_timestamp(row[5]),
            }
        )
    derived = member_titles(auth, untitled)
    for room in rooms:
        if room["title"]:
            continue
        title = derived.get(int(room["chat_id"]))
        if title:
            room["title"], room["title_source"] = title, "members"
        else:
            room["title"] = f"chat{room['chat_id']}"
    if only_type:
        rooms = [room for room in rooms if room["type"] == only_type]
    if needle:
        rooms = [room for room in rooms if needle in room["title"].casefold()]
    return rooms[:limit]


def resolve_auth(
    *,
    refresh: bool,
    cache_path: Path,
    user_id_override: int | None,
    uuid_override: str | None,
    max_user_id: int,
    workers: int | None,
    chunk_size: int,
) -> ResolvedAuth:
    use_cache = not refresh and user_id_override is None and uuid_override is None
    if use_cache:
        cached = load_cached_auth(cache_path)
        if cached is not None:
            # Use the cache only for an account KakaoTalk still lists as signed in on this Mac.
            hashes = current_account_hashes()
            if not hashes or account_hash(cached.user_id) in hashes:
                return cached
            print(
                "The cached key belongs to an account KakaoTalk no longer lists. Resolving the key again.",
                file=sys.stderr,
                flush=True,
            )

    state = collect_detection_state(uuid_override)
    resolved = resolve_auth_state(
        state,
        verify_access=verify_database_access,
        cache_path=cache_path,
        user_id_override=user_id_override,
        max_user_id=max_user_id,
        workers=workers,
        chunk_size=chunk_size,
    )
    warn_if_other_database_is_newer(resolved, state.database_files)
    return resolved


def warn_if_other_database_is_newer(resolved: ResolvedAuth, database_files: Sequence[Path]) -> None:
    """More than one account may have used KakaoTalk on this Mac. If another database file was
    written much more recently than the one that opened, say so instead of guessing."""
    try:
        opened = resolved.database_path.stat().st_mtime
        newest = max(path.stat().st_mtime for path in database_files)
    except (OSError, ValueError):
        return
    if newest - opened > STALE_DATABASE_SECONDS:
        print(
            "Note: another KakaoTalk database on this Mac was updated more recently than the one opened. "
            "If you switched accounts, log in to the account you want and run: auth --refresh",
            file=sys.stderr,
            flush=True,
        )


def render_auth(resolved: ResolvedAuth, *, output_format: str, cache_path: Path) -> str:
    # The key, the hardware UUID and the account id stay in the cache file. No output format prints them.
    payload = {
        "status": "ok",
        "source": resolved.source,
        "database_path": str(resolved.database_path),
        "cache_path": str(cache_path),
    }
    if output_format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return "\n".join(
        [
            "KakaoTalk auth resolved",
            f"- source: {resolved.source}",
            f"- database: {resolved.database_path}",
            f"- cache: {cache_path} (owner-only file that holds the database key; do not print or share it)",
            "",
            "Next: check that the database opens, without printing any chat content:",
            f"  python3 {shlex.quote(str(Path(__file__).resolve()))} schema > /dev/null && echo READ_OK",
            "Read room names or messages only when the user asks for them.",
        ]
    )


WRONG_KEY_MARKER = "PRAGMA key failed"
WRONG_KEY_HINT = (
    "The saved key did not open the KakaoTalk database (the account may have changed, or KakaoTalk was "
    "logged in again). Run this helper's `auth --refresh`. Do not follow kakaocli's advice to install sqlcipher."
)


def explain_kakaocli_error(text: str) -> str:
    """kakaocli reports a wrong key as a cipher problem and suggests installing sqlcipher.
    Say what it means for this helper instead."""
    if WRONG_KEY_MARKER in text:
        return text.rstrip("\n") + "\n" + WRONG_KEY_HINT + "\n"
    return text


# Options that may be passed on to kakaocli, and whether each takes a value.
PASSTHROUGH_OPTIONS: dict[str, dict[str, bool]] = {
    "chats": {"--limit": True, "--json": False},
    # No --chat: kakaocli matches it as a substring of a display name and opens the first hit,
    # so a room is always picked by its id (find it with `rooms --query`).
    "messages": {"--chat-id": True, "--since": True, "--limit": True, "--json": False},
    "search": {"--limit": True, "--json": False},
    "schema": {},
}
PASSTHROUGH_POSITIONALS = {"chats": 0, "messages": 0, "search": 1, "schema": 0}
# kakaocli ignores a --since it cannot parse and then returns messages with no time limit,
# which reads more than the user asked for. Refuse such values here.
PASSTHROUGH_VALUE_RULES: dict[str, tuple[re.Pattern[str], str]] = {
    "--since": (
        re.compile(r"[0-9]+(?:\.[0-9]+)?[smhdw]"),
        "a number followed by s, m (minutes), h, d or w, such as 30m, 24h, 7d (a month is 30d)",
    ),
    "--limit": (re.compile(r"[1-9][0-9]{0,8}"), "a positive whole number"),
    "--chat-id": (re.compile(r"[0-9]{1,20}"), "the number shown as chat_id by `rooms`"),
}
PASSTHROUGH_USAGE = {
    "chats": "Options: --limit N, --json",
    "messages": "Options: --chat-id N, --since 30m|24h|7d, --limit N, --json. Without --chat-id it reads every room.",
    "search": "Usage: search 'TEXT' [--limit N] [--json]. It searches every room.",
    "schema": "Takes no options.",
}


def validate_forwarded_args(command: str, forwarded_args: Sequence[str]) -> list[str]:
    """Pass on only the options listed above. --db and --key are always set by this helper,
    so a caller can never point kakaocli at another file or supply another key.

    Each value is handed over joined to its option (--since=VALUE), so a value that happens to
    look like an option stays a value."""
    options = PASSTHROUGH_OPTIONS[command]
    checked: list[str] = []
    positionals = 0
    args = list(forwarded_args)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("-"):
            name, has_value, inline = arg.partition("=")
            if name not in options:
                hint = ""
                if command == "search":
                    hint = " (text to search for cannot start with '-')"
                elif command == "messages" and name == "--chat":
                    hint = " (pick the room by number: --chat-id, found with `rooms --query`)"
                raise AuthResolutionError(f"Unsupported option for {command}: {name}{hint}")
            if not options[name]:
                if has_value:
                    raise AuthResolutionError(f"Option {name} does not take a value")
                checked.append(name)
            else:
                if not has_value:
                    index += 1
                    if index >= len(args):
                        raise AuthResolutionError(f"Option {name} needs a value")
                    inline = args[index]
                rule = PASSTHROUGH_VALUE_RULES.get(name)
                if rule and not rule[0].fullmatch(inline):
                    raise AuthResolutionError(f"Option {name} takes {rule[1]}")
                checked.append(f"{name}={inline}")
        else:
            positionals += 1
            checked.append(arg)
        index += 1
    if positionals != PASSTHROUGH_POSITIONALS[command]:
        expected = PASSTHROUGH_POSITIONALS[command]
        raise AuthResolutionError(f"{command} takes {expected} plain argument(s), got {positionals}")
    return checked


def build_passthrough_command(command: str, auth: ResolvedAuth, forwarded_args: Sequence[str]) -> list[str]:
    if command not in READ_ONLY_COMMANDS:
        raise AuthResolutionError(
            f"Unsupported command '{command}'. Allowed read-only commands: {', '.join(READ_ONLY_COMMANDS)}"
        )
    forwarded_args = validate_forwarded_args(command, forwarded_args)
    return [
        kakaocli_bin(),
        command,
        *forwarded_args,
        "--db",
        str(auth.database_path),
        "--key",
        auth.key,
    ]


FULL_DISK_ACCESS_HINT = (
    "macOS blocked access to the KakaoTalk data folder. Give Full Disk Access to the app that runs this command "
    "(System Settings > Privacy & Security > Full Disk Access), restart that app, then try again."
)
KAKAOCLI_MISSING_HINT = "kakaocli is not installed. Install it with: brew install silver-flight-group/tap/kakaocli"


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()

    try:
        args, forwarded_args = parser.parse_known_args(argv)
        cache_path = Path(args.cache_path).expanduser()
        if not os.path.exists(kakaocli_bin()):
            raise AuthResolutionError(KAKAOCLI_MISSING_HINT)
        if args.command == "auth":
            if forwarded_args:
                raise AuthResolutionError(f"Unexpected auth arguments: {' '.join(forwarded_args)}")
            resolved = resolve_auth(
                refresh=args.refresh,
                cache_path=cache_path,
                user_id_override=args.user_id,
                uuid_override=args.uuid,
                max_user_id=args.max_user_id,
                workers=args.workers,
                chunk_size=args.chunk_size,
            )
            print(render_auth(resolved, output_format=args.format, cache_path=cache_path))
            return 0

        resolved = resolve_auth(
            refresh=args.refresh_auth,
            cache_path=cache_path,
            user_id_override=args.user_id,
            uuid_override=args.uuid,
            max_user_id=args.max_user_id,
            workers=args.workers,
            chunk_size=args.chunk_size,
        )
        if args.command == "rooms":
            if forwarded_args:
                raise AuthResolutionError(f"Unexpected rooms arguments: {' '.join(forwarded_args)}")
            rooms = list_rooms(
                resolved,
                query=args.query,
                limit=args.limit,
                include_channels=args.include_channels or args.room_type == ROOM_TYPE_LABELS[2],
                only_type=args.room_type,
            )
            print(json.dumps(rooms, ensure_ascii=False, indent=1))
            return 0
        # kakaocli echoes its own argv (including --key) in usage errors, so neither stream passes
        # through without the key being removed first.
        result = subprocess.run(
            build_passthrough_command(args.command, resolved, forwarded_args),
            capture_output=True,
            text=True,
        )
        if result.stdout:
            sys.stdout.write(redact_secret(result.stdout, resolved.key))
        if result.stderr:
            sys.stderr.write(explain_kakaocli_error(redact_secret(result.stderr, resolved.key)))
        return result.returncode
    except PermissionError:
        print(f"error: {FULL_DISK_ACCESS_HINT}", file=sys.stderr)
        return 3
    except (AuthResolutionError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "kakao-mac helper. Finds the local KakaoTalk (macOS) database key for the logged-in user "
            "and runs read-only kakaocli commands with it. It cannot send messages."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser("auth", help="Find and cache the working KakaoTalk database key.")
    add_auth_options(auth_parser)
    auth_parser.add_argument("--refresh", action="store_true", help="Ignore cached auth and resolve again.")
    auth_parser.add_argument("--format", choices=("text", "json"), default="text")

    rooms_parser = subparsers.add_parser("rooms", help="List chat rooms with their real titles and chat ids.")
    add_auth_options(rooms_parser)
    rooms_parser.add_argument("--refresh-auth", action="store_true", help="Refresh cached auth before running.")
    rooms_parser.add_argument(
        "--query",
        help="Keep rooms whose title contains this text. Matched locally; it is never placed into SQL.",
    )
    rooms_parser.add_argument("--limit", type=positive_int, default=30)
    rooms_parser.add_argument("--include-channels", action="store_true", help="Also list brand and ad channels.")
    rooms_parser.add_argument(
        "--type",
        dest="room_type",
        choices=sorted(set(ROOM_TYPE_LABELS.values())),
        help="Keep only rooms of this kind, for example '나와의 채팅' to find the chat with yourself.",
    )
    rooms_parser.add_argument("--json", action="store_true", help="Accepted and ignored: the output is always JSON.")

    for command in READ_ONLY_COMMANDS:
        # allow_abbrev off: a shortened option must reach the allow-list check, not match one of ours.
        passthrough = subparsers.add_parser(
            command,
            help=f"Run kakaocli {command} with cached/recovered auth.",
            description=f"Run kakaocli {command} with the saved key. {PASSTHROUGH_USAGE[command]}",
            epilog="The options listed under 'options' below belong to this helper (key lookup). "
            "Any other option is refused.",
            allow_abbrev=False,
        )
        add_auth_options(passthrough)
        passthrough.add_argument("--refresh-auth", action="store_true", help="Refresh cached auth before running.")

    return parser


def add_auth_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache-path", default=str(DEFAULT_CACHE_PATH))
    parser.add_argument("--user-id", type=int, help="Explicit Kakao user_id override.")
    parser.add_argument("--uuid", help="Explicit device UUID override.")
    parser.add_argument("--max-user-id", type=non_negative_int, default=DEFAULT_MAX_USER_ID)
    parser.add_argument("--workers", type=positive_int, default=None)
    parser.add_argument("--chunk-size", type=positive_int, default=DEFAULT_CHUNK_SIZE)


def non_negative_int(value: str) -> int:
    integer = int(value)
    if integer < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return integer


def positive_int(value: str) -> int:
    integer = int(value)
    if integer <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return integer


if __name__ == "__main__":
    raise SystemExit(main())

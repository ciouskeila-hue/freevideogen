#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import math
import mimetypes
import os
import random
import re
import subprocess
import sys
import time
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import hashlib
import requests

try:
    import undetected_chromedriver as uc
except Exception:
    uc = None


SITE_ORIGIN = "https://geminigen.ai"
API_BASE_URL = "https://api.geminigen.ai/api"
UAPI_BASE_URL = "https://api.geminigen.ai/uapi/v1"
HEALTH_URL = "https://api.geminigen.ai/health"
ANTI_BOT_SECRET_SALT = "&vTQm0&u"
ANTI_BOT_SECRET_KEY = "45NPBH$&"
LOCAL_STORAGE_KEY = "guard_stable_id"
DEFAULT_BUCKET_MS = 60_000
DEFAULT_SESSION_CACHE = Path("geminigen_session.json")
DEFAULT_OUT_JSON = Path("geminigen_last_video.json")
LOCALAPPDATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
DEFAULT_CHROME_LEVELDB = LOCALAPPDATA_DIR / "Google" / "Chrome" / "User Data" / "Default" / "Local Storage" / "leveldb"
DEFAULT_CHROME_BINARY_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    LOCALAPPDATA_DIR / "Google" / "Chrome" / "Application" / "chrome.exe",
]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


class GeminiGenError(RuntimeError):
    pass


VEO_MODELS = {"veo-2", "veo-3", "veo-3-fast"}
VEO_ASPECT_RATIO_MAP = {
    "landscape": "16:9",
    "portrait": "9:16",
    "16:9": "16:9",
    "9:16": "9:16",
}
RESOLUTION_ALIAS_MAP = {
    "c20p": "720p",
}


@dataclass
class AuthState:
    access_token: str | None
    refresh_token: str | None
    guard_stable_id: str | None
    turnstile_token: str | None
    user: dict[str, Any] | None


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sc(value: str) -> str:
    total = 0
    for char in value:
        total = ((total << 5) - total + ord(char)) & 0xFFFFFFFF
    signed = total if total < 0x80000000 else total - 0x100000000
    return f"{abs(signed):08x}"


def gv(value: str) -> int:
    total = 0
    for char in str(value):
        total = (total * 31 + ord(char)) & 0xFFFFFFFF
    return total


def base64url_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def is_valid_stable_id(value: str | None) -> bool:
    return bool(value) and len(value) == 22 and re.fullmatch(r"[A-Za-z0-9_-]{22}", value or "") is not None


def extract_balanced_json(text: str, start_idx: int) -> str | None:
    depth = 0
    in_string = False
    escape = False
    for idx in range(start_idx, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start_idx : idx + 1]
    return None


def iter_leveldb_text_files(leveldb_dir: Path) -> Iterable[Path]:
    files = list(leveldb_dir.glob("*.log")) + list(leveldb_dir.glob("*.ldb"))
    return sorted(files, key=lambda path: (path.stat().st_mtime, path.name))


def parse_auth_store_from_leveldb(leveldb_dir: Path) -> AuthState:
    if not leveldb_dir.exists():
        raise GeminiGenError(
            f"Chrome Local Storage directory not found: {leveldb_dir}\n"
            "请先运行 `python geminigen_video_client.py login --extract`，在打开的 Chrome 里登录 GeminiGen，"
            "登录完成后关闭 Chrome，再回到终端按 Enter 保存会话。"
        )

    auth_candidates: list[dict[str, Any]] = []
    guard_candidates: list[str] = []
    turnstile_candidates: list[str] = []

    for path in iter_leveldb_text_files(leveldb_dir):
        raw = path.read_bytes()
        text = raw.decode("latin1", errors="ignore")

        search_pos = 0
        while True:
            idx = text.find("authStore", search_pos)
            if idx == -1:
                break
            json_start = text.find('{"user"', idx)
            if json_start != -1:
                payload = extract_balanced_json(text, json_start)
                if payload:
                    try:
                        auth_candidates.append(json.loads(payload))
                    except json.JSONDecodeError:
                        pass
            search_pos = idx + 9

        for match in re.finditer(r"guard_stable_id.{0,64}?([A-Za-z0-9_-]{22})", text, re.DOTALL):
            guard_candidates.append(match.group(1))

        for match in re.finditer(r"cf\.turnstile\.u.{0,32}?([A-Za-z0-9._-]{40,})", text, re.DOTALL):
            turnstile_candidates.append(match.group(1))

    auth_payload = None
    for candidate in reversed(auth_candidates):
        if candidate.get("refresh_token") or candidate.get("access_token"):
            auth_payload = candidate
            break
    if auth_payload is None and auth_candidates:
        auth_payload = auth_candidates[-1]
    if auth_payload is None:
        raise GeminiGenError(
            f"没有从 Chrome 本地缓存中提取到 GeminiGen 登录态：{leveldb_dir}\n"
            "请运行 `python geminigen_video_client.py login --extract`，登录成功并关闭 Chrome 后再重试。"
        )

    guard_stable_id = None
    for candidate in reversed(guard_candidates):
        if is_valid_stable_id(candidate):
            guard_stable_id = candidate
            break

    turnstile_token = turnstile_candidates[-1] if turnstile_candidates else None

    return AuthState(
        access_token=auth_payload.get("access_token"),
        refresh_token=auth_payload.get("refresh_token"),
        guard_stable_id=guard_stable_id,
        turnstile_token=turnstile_token,
        user=auth_payload.get("user"),
    )


def load_auth_state(session_cache: Path | None, chrome_leveldb: Path) -> AuthState:
    if session_cache and session_cache.exists():
        payload = json.loads(session_cache.read_text(encoding="utf-8"))
        return AuthState(
            access_token=payload.get("access_token"),
            refresh_token=payload.get("refresh_token"),
            guard_stable_id=payload.get("guard_stable_id"),
            turnstile_token=payload.get("turnstile_token"),
            user=payload.get("user"),
        )
    return parse_auth_store_from_leveldb(chrome_leveldb)


def save_auth_state(path: Path, auth: AuthState) -> None:
    path.write_text(json.dumps(asdict(auth), indent=2, ensure_ascii=False), encoding="utf-8")


def open_login_browser() -> None:
    chrome_binary = find_chrome_binary()
    if not chrome_binary:
        raise GeminiGenError("找不到本机 Chrome，请先安装 Google Chrome。")
    subprocess.Popen([str(chrome_binary), f"{SITE_ORIGIN}/?hard=true"])


def jwt_expiry(token: str | None) -> int | None:
    if not token or token.count(".") < 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return int(exp) if isinstance(exp, (int, float, str)) else None
    except Exception:
        return None


def find_chrome_binary() -> Path | None:
    for candidate in DEFAULT_CHROME_BINARY_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def chrome_major_version(chrome_binary: Path) -> int | None:
    try:
        completed = subprocess.run(
            [str(chrome_binary), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        version_text = (completed.stdout or completed.stderr or "").strip()
        match = re.search(r"(\d+)\.", version_text)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    if os.name != "nt":
        return None
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(chrome_binary), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(chrome_binary), 0, size, buffer):
            return None
        value = ctypes.c_void_p()
        value_size = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(value_size)):
            return None
        data = ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
        ms = data[4]
        major = (ms >> 16) & 0xFFFF
        return int(major) if major else None
    except Exception:
        return None


class GeminiGenClient:
    def __init__(self, auth: AuthState):
        self.auth = auth
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Origin": SITE_ORIGIN,
                "Referer": f"{SITE_ORIGIN}/?hard=true",
            }
        )
        self.time_offset_ms = 0
        self.bootstrap_time_sync()
        if not is_valid_stable_id(self.auth.guard_stable_id):
            self.auth.guard_stable_id = self.generate_fallback_stable_id()

    def bootstrap_time_sync(self) -> None:
        started = time.time_ns() // 1_000_000
        response = self.session.get(HEALTH_URL, timeout=20)
        response.raise_for_status()
        ended = time.time_ns() // 1_000_000
        date_header = response.headers.get("X-Server-Time") or response.headers.get("Date")
        if not date_header:
            return
        if date_header.isdigit():
            server_ms = int(date_header)
        else:
            from email.utils import parsedate_to_datetime

            server_ms = int(parsedate_to_datetime(date_header).timestamp() * 1000)
        rtt_ms = ended - started
        self.time_offset_ms = server_ms + math.floor(rtt_ms / 2) - ended

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.time_offset_ms

    def generate_fallback_stable_id(self) -> str:
        random_hex = "".join(f"{random.randrange(0, 256):02x}" for _ in range(16))
        user_agent_hash = sc(USER_AGENT)
        screen_hash = sc("1920x1080")
        raw = f"{random_hex}.{user_agent_hash}.{screen_hash}"
        digest = sha256_hex(f"{ANTI_BOT_SECRET_SALT}:{raw}")
        return base64url_bytes(bytes.fromhex(digest))[:22]

    def compute_dom_fp(self) -> str:
        parts = [
            "100.0",
            "50.0",
            "100",
            "50",
            "100",
            "50",
            "0",
            "0",
            "100",
            "50",
            "1",
            "1920",
            "1080",
            "24",
            "24",
            "zh-CN",
            "zh-CN,zh,en-US,en",
            USER_AGENT,
            str(os.cpu_count() or ""),
            "",
            os.name,
            time.tzname[0] if time.tzname else "",
            str(-time.timezone // 60),
        ]
        merged = "".join(f"{gv(item):x}" for item in parts).replace(".", "").replace("-", "")
        merged = re.sub(r"[^0-9a-fA-F]", "", merged).lower()
        merged = (merged + ("0" * 64))[:64]
        return merged

    def compute_guard(self, api_path: str, method: str) -> str:
        stable_id = self.auth.guard_stable_id or self.generate_fallback_stable_id()
        time_bucket = self.now_ms() // DEFAULT_BUCKET_MS
        stable_key = sha256_hex(f"{ANTI_BOT_SECRET_KEY}:{stable_id}")[:32]
        signature = sha256_hex(f"{api_path}:{method.upper()}:{stable_key}:{time_bucket}:{ANTI_BOT_SECRET_KEY}")
        packet = bytearray()
        packet.append(1)
        packet.extend(bytes.fromhex(stable_key))
        packet.extend(time_bucket.to_bytes(4, "big", signed=False))
        packet.extend(bytes.fromhex(signature))
        packet.extend(bytes.fromhex(self.compute_dom_fp()))
        return base64url_bytes(bytes(packet))

    def build_headers(self, api_path: str, method: str, include_auth: bool = True) -> dict[str, str]:
        headers = {"x-guard-id": self.compute_guard(api_path, method)}
        if include_auth and self.auth.access_token:
            headers["Authorization"] = f"Bearer {self.auth.access_token}"
        return headers

    def fetch_turnstile_token(self, timeout_seconds: int = 120, max_attempts: int = 3) -> str:
        if uc is None:
            raise GeminiGenError("未安装 undetected-chromedriver，请先运行 `python -m pip install -r requirements.txt`。")
        chrome_binary = find_chrome_binary()
        if not chrome_binary:
            raise GeminiGenError("找不到本机 Chrome，请先安装 Google Chrome。")

        major = chrome_major_version(chrome_binary)
        last_error = "unknown error"
        for attempt in range(1, max_attempts + 1):
            driver = None
            try:
                options = uc.ChromeOptions()
                options.binary_location = str(chrome_binary)
                options.add_argument("--disable-popup-blocking")
                options.add_argument("--disable-notifications")
                options.add_argument("--disable-background-timer-throttling")
                options.add_argument("--disable-renderer-backgrounding")
                kwargs: dict[str, Any] = {
                    "options": options,
                    "use_subprocess": True,
                }
                if major:
                    kwargs["version_main"] = major
                driver = uc.Chrome(**kwargs)

                ready_deadline = time.time() + 15
                while time.time() < ready_deadline and not driver.window_handles:
                    time.sleep(0.5)
                if not driver.window_handles:
                    raise GeminiGenError("Chrome started without any window handles")

                driver.switch_to.window(driver.window_handles[0])
                driver.get(f"{SITE_ORIGIN}/?hard=true")

                turnstile_deadline = time.time() + 20
                while time.time() < turnstile_deadline:
                    state = driver.execute_script("return document.readyState")
                    has_turnstile = driver.execute_script("return typeof window.turnstile !== 'undefined'")
                    if state == "complete" and has_turnstile:
                        break
                    time.sleep(1)

                driver.execute_script(
                    """
window.__tg = { token: null, err: null, logs: [] };
const box = document.createElement('div');
box.id = 'codex-turnstile-box';
box.style.position = 'fixed';
box.style.top = '20px';
box.style.left = '20px';
box.style.zIndex = '999999';
box.style.minWidth = '320px';
box.style.minHeight = '80px';
box.style.background = '#fff';
document.body.appendChild(box);
if (!window.turnstile) {
  window.__tg.err = 'turnstile_missing';
} else {
  try {
    window.__tg.widget = window.turnstile.render('#codex-turnstile-box', {
      sitekey: '0x4AAAAAACDBydnKT0zYzh2H',
      callback: token => { window.__tg.token = token; window.__tg.logs.push('callback'); },
      'error-callback': err => { window.__tg.err = String(err); window.__tg.logs.push('error:' + String(err)); },
      'expired-callback': () => { window.__tg.err = 'expired'; window.__tg.logs.push('expired'); },
      theme: 'light',
      appearance: 'always'
    });
    window.__tg.logs.push('rendered:' + String(window.__tg.widget));
    try { window.turnstile.execute(window.__tg.widget); window.__tg.logs.push('executed'); } catch (e) { window.__tg.logs.push('execute-fail:' + String(e)); }
  } catch (e) {
    window.__tg.err = 'render:' + String(e);
    window.__tg.logs.push('render-fail:' + String(e));
  }
}
"""
                )
                started = time.time()
                last_state: dict[str, Any] | None = None
                while time.time() - started < timeout_seconds:
                    time.sleep(1)
                    if not driver.window_handles:
                        raise GeminiGenError("Chrome window closed while waiting for Turnstile token")
                    last_state = driver.execute_script(
                        """
return {
  tg: window.__tg,
  hidden: Array.from(document.querySelectorAll('input[type=hidden]'))
    .map(el => ({name: el.name, id: el.id, value: el.value}))
    .filter(el => el.name || el.id || el.value)
};
"""
                    )
                    tg = (last_state or {}).get("tg") or {}
                    token = tg.get("token") or next(
                        (
                            item["value"]
                            for item in (last_state or {}).get("hidden", [])
                            if item.get("name") == "cf-turnstile-response" and item.get("value")
                        ),
                        None,
                    )
                    if token:
                        self.auth.turnstile_token = token
                        return token
                    if tg.get("err"):
                        raise GeminiGenError(f"Turnstile error: {tg['err']}")
                raise GeminiGenError(f"Timed out waiting for turnstile token: {last_state}")
            except Exception as exc:
                last_error = str(exc)
                if attempt >= max_attempts:
                    break
                time.sleep(min(3 * attempt, 8))
            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    try:
                        driver.quit = lambda: None
                    except Exception:
                        pass
        raise GeminiGenError(f"Failed to fetch Turnstile token after {max_attempts} attempts: {last_error}")

    def refresh_access_token(self) -> dict[str, Any]:
        if not self.auth.refresh_token:
            raise GeminiGenError("Missing refresh_token")
        url = f"{API_BASE_URL}/refresh-token"
        headers = self.build_headers("/api/refresh-token", "post", include_auth=bool(self.auth.access_token))
        response = self.session.post(
            url,
            json={"refresh_token": self.auth.refresh_token},
            headers=headers,
            timeout=30,
        )
        if response.status_code >= 400:
            raise GeminiGenError(f"refresh-token failed: {response.status_code} {response.text}")
        payload = response.json()
        self.auth.access_token = payload.get("access_token")
        self.auth.refresh_token = payload.get("refresh_token", self.auth.refresh_token)
        return payload

    def ensure_fresh_access_token(self) -> None:
        exp = jwt_expiry(self.auth.access_token)
        if exp is None or exp <= int(time.time()) + 30:
            self.refresh_access_token()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        json_payload: Any | None = None,
        retry_on_auth: bool = True,
    ) -> Any:
        self.ensure_fresh_access_token()
        url = f"{API_BASE_URL}{path}"
        headers = self.build_headers(f"/api{path}", method)
        response = self.session.request(method.upper(), url, headers=headers, json=json_payload, timeout=30)
        if response.status_code in {401, 403} and retry_on_auth and path != "/refresh-token":
            self.refresh_access_token()
            return self.request_json(method, path, json_payload=json_payload, retry_on_auth=False)
        if response.status_code >= 400:
            raise GeminiGenError(f"{method.upper()} {path} failed: {response.status_code} {response.text}")
        return response.json() if response.text.strip() else None

    def fetch_history(self, uuid: str) -> dict[str, Any]:
        payload = self.request_json("get", f"/history/{uuid}")
        if not isinstance(payload, dict):
            raise GeminiGenError(f"Unexpected history payload for {uuid}: {payload!r}")
        return payload

    def iter_sse_events(self, response: requests.Response) -> Iterable[tuple[str, str]]:
        event_name = "message"
        data_lines: list[str] = []
        for raw_line in response.iter_lines(decode_unicode=True):
            if raw_line is None:
                continue
            line = raw_line.rstrip("\r")
            if not line:
                if data_lines:
                    yield event_name, "\n".join(data_lines)
                event_name = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            yield event_name, "\n".join(data_lines)

    def extract_video_url(self, payload: dict[str, Any]) -> str | None:
        generated_video = payload.get("generated_video")
        if isinstance(generated_video, list) and generated_video:
            first = generated_video[0]
            if isinstance(first, dict):
                return first.get("video_url") or first.get("url")
        for key in ("video_url", "url"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        result = payload.get("result")
        if isinstance(result, dict):
            return self.extract_video_url(result)
        return None

    def poll_history_until_done(
        self,
        uuid: str,
        *,
        initial_delay: int = 0,
        interval_seconds: int = 30,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        if initial_delay > 0:
            time.sleep(initial_delay)
        started = time.time()
        while True:
            history = self.fetch_history(uuid)
            status = history.get("status")
            if status in {2, 3}:
                return history
            if time.time() - started >= timeout_seconds:
                raise GeminiGenError(f"Timed out waiting for history {uuid} to complete")
            time.sleep(interval_seconds)

    def normalize_veo_aspect_ratio(self, aspect_ratio: str) -> str:
        normalized = VEO_ASPECT_RATIO_MAP.get(aspect_ratio)
        if normalized:
            return normalized
        raise GeminiGenError(
            "Veo 仅支持 16:9 或 9:16。你可以传 --aspect-ratio 16:9、9:16、landscape 或 portrait。"
        )

    def normalize_resolution(self, resolution: str) -> str:
        value = (resolution or "").strip().lower()
        if value in RESOLUTION_ALIAS_MAP:
            return RESOLUTION_ALIAS_MAP[value]
        return value or resolution

    def generate_veo_video(
        self,
        *,
        prompt: str,
        model: str,
        aspect_ratio: str,
        resolution: str,
        duration: int,
        mode: str | None = None,
        file_paths: list[Path] | None = None,
        poll_interval: int = 30,
        timeout_seconds: int = 300,
        include_turnstile: bool = False,
        auto_turnstile: bool = True,
        _turnstile_retry: bool = False,
    ) -> dict[str, Any]:
        self.ensure_fresh_access_token()
        if model not in VEO_MODELS:
            raise GeminiGenError(f"不支持的 Veo 模型：{model}")

        normalized_aspect_ratio = self.normalize_veo_aspect_ratio(aspect_ratio)
        normalized_resolution = self.normalize_resolution(resolution)
        files: list[tuple[str, tuple[Any, Any, str] | tuple[None, str]]] = []
        opened_files: list[Any] = []
        try:
            files.append(("prompt", (None, prompt)))
            files.append(("model", (None, model)))
            files.append(("resolution", (None, normalized_resolution)))
            files.append(("aspect_ratio", (None, normalized_aspect_ratio)))
            files.append(("duration", (None, str(duration))))
            if mode:
                files.append(("service_mode", (None, mode)))
            if include_turnstile and self.auth.turnstile_token:
                files.append(("turnstile_token", (None, self.auth.turnstile_token)))
            for path in file_paths or []:
                handle = path.open("rb")
                opened_files.append(handle)
                mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                files.append(("ref_images", (path.name, handle, mime_type)))

            headers = self.build_headers("/api/video-gen/veo", "post")
            headers["Accept"] = "application/json"
            response = self.session.post(
                f"{API_BASE_URL}/video-gen/veo",
                headers=headers,
                files=files,
                timeout=(120, timeout_seconds),
            )
            if response.status_code in {401, 403}:
                self.refresh_access_token()
                return self.generate_veo_video(
                    prompt=prompt,
                    model=model,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    duration=duration,
                    mode=mode,
                    file_paths=file_paths,
                    poll_interval=poll_interval,
                    timeout_seconds=timeout_seconds,
                    include_turnstile=include_turnstile,
                    auto_turnstile=auto_turnstile,
                    _turnstile_retry=_turnstile_retry,
                )
            raw_text = response.text or ""
            try:
                payload = response.json() if raw_text else None
            except json.JSONDecodeError:
                payload = raw_text

            if response.status_code < 200 or response.status_code >= 300:
                response_text_upper = raw_text.upper()
                if (
                    response.status_code == 400
                    and auto_turnstile
                    and not _turnstile_retry
                    and (
                        "TURNSTILE_REQUIRED" in response_text_upper
                        or "TURNSTILE_INVALID" in response_text_upper
                    )
                ):
                    self.fetch_turnstile_token()
                    return self.generate_veo_video(
                        prompt=prompt,
                        model=model,
                        aspect_ratio=aspect_ratio,
                        resolution=resolution,
                        duration=duration,
                        mode=mode,
                        file_paths=file_paths,
                        poll_interval=poll_interval,
                        timeout_seconds=timeout_seconds,
                        include_turnstile=True,
                        auto_turnstile=auto_turnstile,
                        _turnstile_retry=True,
                    )
                if isinstance(payload, dict):
                    detail = payload.get("detail")
                    if isinstance(detail, dict) and detail.get("error_message"):
                        raise GeminiGenError(str(detail["error_message"]))
                    for key in ("error_message", "message"):
                        value = payload.get(key)
                        if isinstance(value, str) and value:
                            raise GeminiGenError(value)
                raise GeminiGenError(f"video-gen/veo failed: {response.status_code} {raw_text}")

            if isinstance(payload, dict):
                if payload.get("uuid"):
                    return self.poll_history_until_done(
                        payload["uuid"],
                        interval_seconds=poll_interval,
                        timeout_seconds=timeout_seconds,
                    )
                return payload
            return {"raw": payload}
        finally:
            for handle in opened_files:
                handle.close()

    def generate_grok_video(
        self,
        *,
        prompt: str,
        model: str,
        aspect_ratio: str,
        resolution: str,
        duration: int,
        mode: str | None = None,
        file_paths: list[Path] | None = None,
        ref_history: list[str] | None = None,
        include_turnstile: bool = False,
        poll_interval: int = 30,
        timeout_seconds: int = 300,
        auto_turnstile: bool = True,
        _turnstile_retry: bool = False,
    ) -> dict[str, Any]:
        self.ensure_fresh_access_token()
        normalized_resolution = self.normalize_resolution(resolution)
        files: list[tuple[str, tuple[Any, Any, str] | tuple[None, str]]] = []
        opened_files: list[Any] = []
        try:
            files.append(("prompt", (None, prompt)))
            files.append(("model", (None, model)))
            files.append(("aspect_ratio", (None, aspect_ratio)))
            files.append(("resolution", (None, normalized_resolution)))
            files.append(("duration", (None, str(duration))))
            if mode:
                files.append(("mode", (None, mode)))
            if include_turnstile and self.auth.turnstile_token:
                files.append(("turnstile_token", (None, self.auth.turnstile_token)))
            for history_id in ref_history or []:
                files.append(("ref_history", (None, history_id)))
            for path in file_paths or []:
                handle = path.open("rb")
                opened_files.append(handle)
                mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                files.append(("files", (path.name, handle, mime_type)))

            headers = self.build_headers("/api/video-gen/grok-stream", "post")
            headers["Accept"] = "text/event-stream"
            response = self.session.post(
                f"{API_BASE_URL}/video-gen/grok-stream",
                headers=headers,
                files=files,
                stream=True,
                timeout=(120, timeout_seconds),
            )
            if response.status_code in {401, 403}:
                self.refresh_access_token()
                return self.generate_grok_video(
                    prompt=prompt,
                    model=model,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    duration=duration,
                    mode=mode,
                    file_paths=file_paths,
                    ref_history=ref_history,
                    include_turnstile=include_turnstile,
                    poll_interval=poll_interval,
                    timeout_seconds=timeout_seconds,
                )
            if response.status_code >= 400:
                response_text = response.text or ""
                if (
                    response.status_code == 400
                    and auto_turnstile
                    and not _turnstile_retry
                    and ("TURNSTILE_REQUIRED" in response_text or "TURNSTILE_INVALID" in response_text)
                ):
                    self.fetch_turnstile_token()
                    return self.generate_grok_video(
                        prompt=prompt,
                        model=model,
                        aspect_ratio=aspect_ratio,
                        resolution=resolution,
                        duration=duration,
                        mode=mode,
                        file_paths=file_paths,
                        ref_history=ref_history,
                        include_turnstile=True,
                        poll_interval=poll_interval,
                        timeout_seconds=timeout_seconds,
                        auto_turnstile=auto_turnstile,
                        _turnstile_retry=True,
                    )
                raise GeminiGenError(f"video-gen/grok-stream failed: {response.status_code} {response_text}")

            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                payload = response.json()
                if payload.get("uuid"):
                    return self.poll_history_until_done(
                        payload["uuid"],
                        initial_delay=int(payload.get("delay_seconds") or 0),
                        interval_seconds=poll_interval,
                        timeout_seconds=timeout_seconds,
                    )
                return payload

            queued_uuid = None
            queued_delay = 0
            live_uuid = None
            for event_name, data in self.iter_sse_events(response):
                if not data:
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if payload.get("uuid") and payload.get("delay_seconds") is not None:
                    queued_uuid = payload["uuid"]
                    queued_delay = int(payload.get("delay_seconds") or 0)
                    continue

                history_uuid = payload.get("history_uuid")
                if history_uuid:
                    live_uuid = history_uuid

                if event_name == "grok_video_finalising" and history_uuid:
                    return self.poll_history_until_done(
                        history_uuid,
                        interval_seconds=poll_interval,
                        timeout_seconds=timeout_seconds,
                    )

                if event_name == "grok_video_generation":
                    progress = (
                        payload.get("data", {})
                        .get("result", {})
                        .get("response", {})
                        .get("streamingVideoGenerationResponse", {})
                        .get("progress")
                    )
                    if progress is not None and int(progress) >= 100 and history_uuid:
                        return self.poll_history_until_done(
                            history_uuid,
                            interval_seconds=poll_interval,
                            timeout_seconds=timeout_seconds,
                        )

            final_uuid = live_uuid or queued_uuid
            if final_uuid:
                return self.poll_history_until_done(
                    final_uuid,
                    initial_delay=queued_delay,
                    interval_seconds=poll_interval,
                    timeout_seconds=timeout_seconds,
                )
            raise GeminiGenError("No UUID was returned by grok-stream")
        finally:
            for handle in opened_files:
                handle.close()

    def download_video(self, url: str, output_path: Path) -> None:
        response = self.session.get(url, stream=True, timeout=120)
        response.raise_for_status()
        with output_path.open("wb") as file_handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file_handle.write(chunk)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用本机 Chrome 登录态调用 GeminiGen 生成视频。")
    parser.add_argument("--session-cache", type=Path, default=DEFAULT_SESSION_CACHE, help=f"会话缓存 JSON 路径，默认：{DEFAULT_SESSION_CACHE}")
    parser.add_argument("--chrome-leveldb", type=Path, default=DEFAULT_CHROME_LEVELDB, help=f"Chrome Local Storage leveldb 目录，默认：{DEFAULT_CHROME_LEVELDB}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="打开 GeminiGen 登录页，并可在登录后提取本机会话")
    login.add_argument("--extract", action="store_true", help="登录并关闭 Chrome 后，把鉴权信息提取到会话缓存")

    auth = subparsers.add_parser("auth-info", help="显示当前提取到的登录状态")
    auth.add_argument("--refresh", action="store_true", help="显示前先刷新 access token")

    history = subparsers.add_parser("history", help="通过 UUID 查询一个历史任务")
    history.add_argument("--uuid", required=True, help="历史任务 UUID")

    generate = subparsers.add_parser("generate", help="生成视频")
    generate.add_argument("--prompt", required=True, help="视频提示词")
    generate.add_argument("--model", default="grok-video", help="视频模型，默认：grok-video")
    generate.add_argument("--api-key", help="可选 API Key（当前 Veo 默认走登录态 token 流程，此参数暂不必填）")
    generate.add_argument("--aspect-ratio", default="landscape", help="画面比例/方向，默认：landscape")
    generate.add_argument("--resolution", default="720p", help="分辨率，默认：720p（兼容别名：c20p=720p）")
    generate.add_argument("--duration", type=int, default=8, help="视频时长秒数，默认：8")
    generate.add_argument("--mode", help="可选模式，例如 ALLOW_ALL")
    generate.add_argument("--first-frame", type=Path, help="可选首帧图片路径，会作为第一个参考文件上传")
    generate.add_argument("--file", dest="files", action="append", type=Path, default=[], help="可选参考文件路径，可重复传入")
    generate.add_argument("--ref-history", action="append", default=[], help="可选参考历史任务 UUID，可重复传入")
    generate.add_argument("--include-turnstile", action="store_true", help="请求中携带从 Chrome 本地缓存提取到的最新 Turnstile token")
    generate.add_argument("--no-auto-turnstile", action="store_true", help="当接口要求 Turnstile 时，禁用自动打开浏览器获取 token")
    generate.add_argument("--poll-interval", type=int, default=30, help="查询任务状态的间隔秒数，默认：30")
    generate.add_argument("--timeout", type=int, default=300, help="整体等待超时时间秒数，默认：300")
    generate.add_argument("--out-json", type=Path, default=DEFAULT_OUT_JSON, help=f"最终 JSON 保存路径，默认：{DEFAULT_OUT_JSON}")
    generate.add_argument("--download", type=Path, help="可选视频下载保存路径")

    return parser


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "login":
            open_login_browser()
            print("已在 Chrome 中打开 GeminiGen。")
            print("请在浏览器里完成登录。登录完成后关闭 Chrome。")
            if args.extract:
                input("登录完成并关闭 Chrome 后，按 Enter 继续提取会话...")
                auth_state = load_auth_state(args.session_cache, args.chrome_leveldb)
                save_auth_state(args.session_cache, auth_state)
                print(f"已保存会话缓存：{args.session_cache}")
            return 0

        auth_state = load_auth_state(args.session_cache, args.chrome_leveldb)
        client = GeminiGenClient(auth_state)

        if args.command == "auth-info":
            if args.refresh:
                client.refresh_access_token()
                save_auth_state(args.session_cache, client.auth)
            print_json(asdict(client.auth))
            return 0

        if args.command == "history":
            client.ensure_fresh_access_token()
            save_auth_state(args.session_cache, client.auth)
            print_json(client.fetch_history(args.uuid))
            return 0

        if args.command == "generate":
            file_paths = list(args.files)
            if args.first_frame:
                file_paths.insert(0, args.first_frame)
            if args.model in VEO_MODELS:
                result = client.generate_veo_video(
                    prompt=args.prompt,
                    model=args.model,
                    aspect_ratio=args.aspect_ratio,
                    resolution=args.resolution,
                    duration=args.duration,
                    mode=args.mode,
                    file_paths=file_paths,
                    poll_interval=args.poll_interval,
                    timeout_seconds=args.timeout,
                    include_turnstile=args.include_turnstile,
                    auto_turnstile=not args.no_auto_turnstile,
                )
            else:
                result = client.generate_grok_video(
                    prompt=args.prompt,
                    model=args.model,
                    aspect_ratio=args.aspect_ratio,
                    resolution=args.resolution,
                    duration=args.duration,
                    mode=args.mode,
                    file_paths=file_paths,
                    ref_history=args.ref_history,
                    include_turnstile=args.include_turnstile,
                    poll_interval=args.poll_interval,
                    timeout_seconds=args.timeout,
                    auto_turnstile=not args.no_auto_turnstile,
                )
            save_auth_state(args.session_cache, client.auth)
            args.out_json.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            print_json(result)
            if args.download:
                video_url = client.extract_video_url(result)
                if not video_url:
                    raise GeminiGenError("No video URL found in result")
                client.download_video(video_url, args.download)
                print(f"downloaded={args.download}")
            return 0

        parser.error(f"unknown command: {args.command}")
        return 1
    except GeminiGenError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unhandled error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

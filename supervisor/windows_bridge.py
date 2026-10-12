"""Windows continuation guardian. UI stages run in the app-owned native helper.

`perform` in bridge.py still owns the journal, the readback, and the one-send
rule. This backend only supplies guard, route, snapshot, write, and send.
The helper path comes from AGENT_RADAR_NATIVE_HELPER, never from the request.
Helper stdout is one bounded JSON object of fixed fields. Composer text is not
accepted back; a sha256 of the canonical text is.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time

try:
    from supervisor.bridge import (
        BUNDLES, Blocked, Journal, canonical_text, perform, validate_snapshot,
    )
    from supervisor import platform_support as support
except ImportError:  # pragma: no cover - script layout
    from bridge import BUNDLES, Blocked, Journal, canonical_text, perform, validate_snapshot
    import platform_support as support

MAX_STAGE_BYTES = 4096
STAGE_TIMEOUT = {"snapshot": 6.0, "route": 8.0, "write": 8.0, "send": 6.0, "health": 3.0}
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ALLOWED = frozenset({
    "code", "identity", "busy", "input_count", "send_available", "send_enabled",
    "send_deferred", "empty", "digest", "stable_id", "title_only", "ambiguous",
    "used_enter", "attempted", "focused",
})
_FAILURES = frozenset({
    "target_unverified", "header_unverified", "sidebar_target_missing",
    "sidebar_target_ambiguous", "search_unavailable", "search_input_changed",
    "search_result_missing", "search_result_ambiguous", "window_ambiguous",
    "app_unavailable", "permission_required", "focus_changed", "app_ui_unavailable",
    "send_unconfirmed", "send_unavailable", "composer_unreadable", "input_failed",
    "input_changed", "already_running", "helper_invalid", "helper_unavailable",
})
# The helper says it refused before the composer was touched.
_PREWRITE = frozenset({
    "permission_required", "focus_changed", "user_active", "locked",
    "target_unverified", "state_changed", "user_stopped", "window_ambiguous",
    "app_unavailable",
})
_STABLE = (
    "id", "app_id", "title", "project", "source", "target", "status",
    "started_at", "navigation_key", "navigation_title", "user_stopped", "status_reason",
)
_UNMATCHED = "unmatched:"


def run_helper(argv, data, timeout):
    """Run the helper and return (returncode, stdout). Stderr is discarded.

    Reading stops at MAX_STAGE_BYTES + 1. A deadline kills the process.
    The raised Blocked code never contains helper output.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) > 32768:
        raise Blocked("invalid_request")
    flags = {}
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt" and creation:
        flags["creationflags"] = creation
    try:
        proc = subprocess.Popen(
            [str(part) for part in argv],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ,"AGENT_RADAR_BRIDGE_SUPERVISOR":"verified-python-supervisor"},
            **flags,
        )
    except OSError:
        raise Blocked("helper_unavailable")
    timer = threading.Timer(timeout, proc.kill)
    timer.daemon = True
    timer.start()
    output = b""
    try:
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            output = proc.stdout.read(MAX_STAGE_BYTES + 1)
        except OSError:
            output = b""
    finally:
        fired = timer.finished.is_set()
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        try:
            proc.stdout.close()
        except OSError:
            pass
    if fired:
        raise Blocked("helper_timeout")
    if len(output) > MAX_STAGE_BYTES:
        raise Blocked("helper_output")
    return proc.returncode, bytes(output)


class WindowsBackend:
    def __init__(self, request, helper=None, reader=None, identity=None,
                 idle=None, locked=None, runner=None, environ=None):
        if any(key in request for key in support.REQUEST_HELPER_KEYS):
            raise Blocked("invalid_request")
        self.request = request
        self.records_receipts = True
        self.did_write = False
        self.stage = "preflight"
        self.title = request.get("navigation_title") or request.get("title")
        self._written = None
        self._original = None
        self._title_unique = False
        self._identity_value = None
        self._seen_identity = False
        if helper is None:
            self.helper = support.native_helper_path(environ)
        else:
            self.helper = support.validate_helper_path(os.fspath(helper))
        self.reader = reader or _default_reader
        self.identity_check = identity if identity is not None else _default_identity
        self.idle_probe = idle if idle is not None else _default_idle
        self.locked_probe = locked if locked is not None else _default_locked
        self.runner = runner or run_helper

    def guard(self, fresh=False, after_write=False):
        locked = self.locked_probe()
        if locked is None or locked is True:
            raise Blocked("locked")
        idle = self.idle_probe()
        if idle is None or not isinstance(idle, (int, float)) or isinstance(idle, bool) or idle < 2:
            raise Blocked("user_active")
        if not fresh:
            return
        try:
            row = self.reader(self.request)
        except Blocked:
            raise
        except Exception:
            raise Blocked("state_unverified")
        self._require_row(row)
        self._title_unique = bool(row.get("title_unique") is True and row.get("title")==self.title and row.get("target")==self.request.get("target"))
        frozen = tuple(row.get(key) for key in _STABLE)
        if self._original is None:
            self._original = frozen
        elif frozen != self._original:
            raise Blocked("state_changed")

    def route(self):
        self._check_target_shape()
        self.guard(fresh=True)
        self.stage = "route"
        parsed = self._invoke("route")
        self._require_focus(parsed)
        if not _identity_ok(parsed, self.request.get("id")):
            raise Blocked("header_unverified")
        self.guard(fresh=True)

    def snapshot(self):
        parsed = self._invoke("snapshot")
        if self.stage in ("route", "write", "send") and parsed.get("focused") is not True:
            raise Blocked("focus_changed")
        value = _composer_value(parsed, self._written)
        send_available = parsed.get("send_available")
        send_enabled = parsed.get("send_enabled", send_available)
        if type(send_available) is not bool or type(send_enabled) is not bool:
            raise Blocked("helper_invalid")
        count = parsed.get("input_count")
        if type(count) is not int or isinstance(count, bool) or count < 0 or count > 50:
            raise Blocked("helper_invalid")
        if type(parsed.get("busy")) is not bool:
            raise Blocked("helper_invalid")
        return dict(
            identity=_identity_ok(parsed, self.request.get("id")),
            input_count=count, value=value, busy=parsed["busy"],
            send_exists=send_available, send_enabled=send_enabled,
            send_deferred=parsed.get("send_deferred") is True,
            send_count=1 if send_available else 0,
        )

    def write(self, text):
        self.guard(fresh=True)
        current = self.snapshot()
        validate_snapshot(current, None if self.request.get("overwrite_draft") is True else "")
        self.stage = "write"
        self.did_write = True
        parsed = self._invoke("write", text=text)
        if parsed.get("used_enter") is True:
            raise Blocked("send_unconfirmed")
        code = _code_of(parsed)
        if parsed.get("attempted") is False and code in _PREWRITE:
            self.did_write = False
        if code in _FAILURES:
            raise Blocked(code)
        self._require_focus(parsed)
        self._written = text
        self.guard(fresh=True, after_write=True)

    def send(self):
        self.guard(fresh=True, after_write=True)
        validate_snapshot(self.snapshot(), self._written)
        self.stage = "send"
        parsed = self._invoke("send")
        if parsed.get("used_enter") is True:
            raise Blocked("send_unconfirmed")
        self._require_focus(parsed)
        if _code_of(parsed) != "sent":
            raise Blocked("send_unconfirmed")
        # Sending ends the old round; confirmation belongs to the FIFO runtime.

    def inspect(self):
        if self.helper is None:
            raise Blocked("helper_unavailable")
        self.stage = "inspect"
        parsed = self._invoke("snapshot")
        code_given = _code_of(parsed)
        if code_given == "permission_required":
            raise Blocked("permission_required")
        current = dict(
            identity=_identity_ok(parsed, self.request.get("id")),
            input_count=parsed.get("input_count") if type(parsed.get("input_count")) is int else 0,
            value=_composer_value(parsed, None),
            busy=parsed.get("busy") is True,
            send_exists=parsed.get("send_available") is True,
            send_enabled=parsed.get("send_enabled", parsed.get("send_available")) is True,
            send_deferred=parsed.get("send_deferred") is True,
        )
        try:
            validate_snapshot(current, None)
            code = "ready" if current["send_exists"] or current["send_deferred"] else "send_unavailable"
        except Blocked as error:
            code = str(error)
        # The composer value stays in memory for the check above and is not returned.
        return dict(
            code=code, attempted=False, identity=current["identity"],
            input_count=current["input_count"], busy=current["busy"],
            send_exists=current["send_exists"], send_enabled=current["send_enabled"],
            send_deferred=current["send_deferred"],
            empty_composer=current["value"] == "",
        )

    def _require_row(self, row):
        if not isinstance(row, dict):
            raise Blocked("state_changed")
        if row.get("status") == "unknown" and row.get("lifecycle_pending"):
            raise Blocked("state_unverified")
        request = self.request
        app = request.get("app_id")
        expected = ({"completed"} if request.get("automation_mode") == "rage" else {"completed", "idle"}) \
            if request.get("kind") == "followup" else {"interrupted"}
        if row.get("status") not in expected or row.get("started_at") != request.get("started_at"):
            raise Blocked("state_changed")
        for field in ("project", "source", "target"):
            if field in request and row.get(field) != request[field]:
                raise Blocked("target_unverified")
        if app == "codex" and row.get("title") != self.title:
            raise Blocked("target_unverified")
        if row.get("user_stopped"):
            raise Blocked("user_stopped")
        try:
            current = self.identity_check(request)
        except Blocked:
            raise
        except Exception:
            raise Blocked("target_unverified")
        if not self._seen_identity:
            self._identity_value = current
            self._seen_identity = True
        elif current != self._identity_value:
            raise Blocked("target_unverified")
        if app == "grok" and isinstance(current, tuple) and len(current) >= 3 and row.get("project") != current[2]:
            raise Blocked("target_unverified")
        if app == "zcode" and isinstance(current, tuple) and len(current) >= 3 and row.get("project") != current[2]:
            raise Blocked("target_unverified")
        if app == "qoder-cn" and isinstance(current, tuple) and row.get("navigation_key") != current[0]:
            raise Blocked("target_unverified")
        reason = row.get("status_reason") or ""
        if not isinstance(reason, str):
            raise Blocked("state_unverified")
        if any(token in reason for token in (
                "用户主动", "用户取消", "用户停止", "主动停止", "手动停止",
                "user canceled", "user cancelled", "user stopped")):
            raise Blocked("user_stopped")
        restricted = ("认证", "访问权限", "上下文超过")
        if request.get("manual_retry") is not True:
            restricted += ("中止事件", "触发者")
        if any(token in reason for token in restricted):
            raise Blocked("manual_resolution")
        limited = any(token in reason for token in ("429", "配额", "频率"))
        if limited and not request.get("allow_rate_limit"):
            raise Blocked("manual_resolution")
        if request.get("allow_rate_limit") and not limited:
            raise Blocked("state_changed")
        if app == "autoclaw" and (
                row.get("navigation_key") != request.get("navigation_key") or
                row.get("navigation_title") != self.title):
            raise Blocked("target_unverified")

    def _check_target_shape(self):
        app = self.request.get("app_id")
        target = self.request.get("target", "")
        if app == "codex" and not re.fullmatch(r"codex://threads/[A-Za-z0-9_-]+", target or ""):
            raise Blocked("target_unverified")
        if app == "workbuddy" and not re.fullmatch(r"workbuddy://chat/[A-Za-z0-9_-]+", target or ""):
            raise Blocked("target_unverified")

    def _invoke(self, stage, text=None):
        if self.helper is None:
            raise Blocked("helper_unavailable")
        payload = _payload(self.request, stage, text)
        payload["supervisor_verified"]=True
        payload["title_unique"]=bool(self.request.get("app_id")=="codex" and self._title_unique)
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > 32768:
            raise Blocked("invalid_request")
        try:
            _code, output = self.runner([self.helper, "--native-bridge"], data, STAGE_TIMEOUT[stage])
        except Blocked:
            raise
        except Exception:
            raise Blocked("helper_unavailable")
        if not isinstance(output, (bytes, bytearray)) or len(output) > MAX_STAGE_BYTES:
            raise Blocked("helper_output")
        try:
            parsed = json.loads(bytes(output).decode("utf-8"))
        except (UnicodeError, ValueError):
            raise Blocked("helper_invalid")
        if not isinstance(parsed, dict) or set(parsed) - _ALLOWED:
            raise Blocked("helper_invalid")
        if parsed.get("used_enter") is True:
            raise Blocked("send_unconfirmed")
        stable = parsed.get("stable_id")
        if stable is not None and (not isinstance(stable, str) or len(stable) > 256 or "\n" in stable or "\x00" in stable):
            raise Blocked("helper_invalid")
        digest = parsed.get("digest")
        if digest is not None and (not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None):
            raise Blocked("helper_invalid")
        code = parsed.get("code")
        if code is not None and (not isinstance(code, str) or _CODE.fullmatch(code) is None):
            raise Blocked("helper_invalid")
        return parsed

    @staticmethod
    def _require_focus(parsed):
        if parsed.get("focused") is not True:
            raise Blocked("focus_changed")


def execute(request, journal_dir=None, environ=None):
    """One bridge request. Result codes only; exception text is not returned."""
    began = time.monotonic()
    backend = None
    result = {"code": "bridge_error", "attempted": False}
    try:
        if not isinstance(request, dict):
            raise Blocked("invalid_request")
        request = support.strip_helper_keys(request)
        if request.get("mode") == "judge":
            try:
                from supervisor.completion_judge import judge
            except ImportError:
                from completion_judge import judge
            try:
                result = judge(request)
            except Exception:
                result = {"code": "judgement_unknown", "attempted": False}
            return _public(result)
        if request.get("mode") == "health":
            helper = support.native_helper_path(environ)
            if helper is None:
                return {"code": "helper_unavailable", "attempted": False}
            probe = WindowsBackend(
                {"mode": "health", "app_id": "codex", "id": "health", "title": "health", "key": "health"},
                helper=helper, reader=lambda _request: None, identity=lambda _request: None,
                idle=lambda: 10, locked=lambda: False, environ=environ,
            )
            try:
                parsed = probe._invoke("health")
            except Blocked as error:
                return {"code": str(error), "attempted": False}
            code = _code_of(parsed)
            if code not in ("ready", "permission_required"):
                code = "permission_required"
            return {"code": code, "attempted": False}
        if request.get("app_id") not in BUNDLES or request.get("mode") not in ("send", "check", "inspect"):
            raise Blocked("unsupported")
        if not all(isinstance(request.get(key), str) and request[key] for key in ("key", "id", "title")):
            raise Blocked("invalid_request")
        if request["app_id"] == "openclaw":
            # Primary owns the native and WSL CLI resolver. Delegate to the
            # existing transport; do not invent a second sender here.
            try:
                from supervisor.openclaw_transport import OpenClawTransport
            except ImportError:
                from openclaw_transport import OpenClawTransport
            backend = OpenClawTransport(request, Blocked)
            result = backend.inspect() if request.get("mode") == "inspect" else backend.perform(Journal(journal_dir), _resolve)
        else:
            backend = WindowsBackend(request, environ=environ)
            result = backend.inspect() if request.get("mode") == "inspect" else perform(
                request, backend, Journal(journal_dir))
    except Blocked as error:
        result = {"code": str(error), "attempted": bool(backend and backend.did_write)}
        if error.retry_after is not None:
            result["retry_after"] = error.retry_after
    except Exception:
        result = {"code": "bridge_error", "attempted": bool(backend and getattr(backend, "did_write", False))}
    if isinstance(request, dict) and request.get("mode") == "inspect":
        return _public(result)
    operation_id = None
    if isinstance(request, dict):
        try:
            from supervisor import bridge as bridge_module
        except ImportError:
            import bridge as bridge_module
        try:
            operation_id = bridge_module._persist_receipts(request, result, backend, began)
        except Exception:
            operation_id = None
    if operation_id and isinstance(result, dict):
        result = dict(result)
        result["operation_id"] = operation_id
    return _public(result)


def _resolve(request):
    from supervisor.bridge import resolve_text
    return resolve_text(request)


def _public(result):
    if not isinstance(result, dict):
        return {"code": "bridge_error", "attempted": False}
    kept = {}
    for key in ("code", "attempted", "sent_at", "run_id", "retry_after", "diagnostic_summary",
                "identity", "operation_id", "input_count", "busy", "send_exists", "send_enabled",
                "send_deferred", "empty_composer"):
        if key not in result:
            continue
        value = result[key]
        if key == "code" and (not isinstance(value, str) or _CODE.fullmatch(value) is None):
            value = "bridge_error"
        if key in ("identity", "busy", "send_exists", "send_enabled", "send_deferred", "empty_composer", "attempted"):
            if type(value) is not bool:
                continue
        if key == "input_count" and (type(value) is not int or isinstance(value, bool)):
            continue
        if key in ("sent_at", "retry_after") and (isinstance(value, bool) or not isinstance(value, (int, float))):
            continue
        if key in ("run_id", "diagnostic_summary", "operation_id") and not isinstance(value, str):
            continue
        if isinstance(value, str) and len(value) > 300:
            continue
        kept[key] = value
    if "code" not in kept:
        kept["code"] = "bridge_error"
    if "attempted" not in kept:
        kept["attempted"] = False
    return kept


def _default_idle():
    if os.name != "nt":
        return 0.0
    return support.idle_seconds()


def _default_locked():
    if os.name != "nt":
        return True
    return support.workstation_locked()


def _default_reader(request):
    app = request.get("app_id")
    session_id = request.get("id")
    if app == "codex":
        try:
            from collector.codex_adapter import collect_session
        except ImportError:
            from codex_adapter import collect_session
        return collect_session(session_id)
    if app == "autoclaw":
        try:
            from collector.autoclaw_adapter import collect
        except ImportError:
            from autoclaw_adapter import collect
    elif app in ("grok", "qoder", "qoder-cn"):
        try:
            from collector.extended_adapters import collect
        except ImportError:
            from extended_adapters import collect
    else:
        try:
            from collector.desktop_adapters import collect
        except ImportError:
            from desktop_adapters import collect
    return next((row for row in collect() if row.get("id") == session_id), None)


def _default_identity(request):
    try:
        from supervisor.bridge import grok_target, qoder_target, zcode_target
    except ImportError:
        from bridge import grok_target, qoder_target, zcode_target
    app = request.get("app_id")
    if app == "grok":
        return grok_target(request)
    if app == "zcode":
        return zcode_target(request)
    if app == "qoder-cn":
        return qoder_target(request)
    return None


def _payload(request, stage, text):
    def text_field(name, limit):
        value = request.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > limit or "\x00" in value:
            raise Blocked("invalid_request")
        return value

    payload = {
        "stage": stage,
        "id": text_field("id", 256),
        "app_id": text_field("app_id", 64),
        "title": text_field("title", 400),
        "project": text_field("project", 1024),
        "navigation_key": text_field("navigation_key", 256),
        "target": text_field("target", 1024),
        "source": text_field("source", 64),
        "enter_fallback": False,
    }
    started = request.get("started_at")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        payload["started_at"] = started
    pid = request.get("pid")
    if type(pid) is int and not isinstance(pid, bool) and pid > 0:
        payload["pid"] = pid
    if stage == "write":
        if not isinstance(text, str) or len(text) > 2000:
            raise Blocked("text_invalid")
        payload["text"] = text
        payload["overwrite"] = request.get("overwrite_draft") is True
    return payload


def _identity_ok(parsed, expected_id):
    if parsed.get("identity") is not True or parsed.get("title_only") is True or parsed.get("ambiguous") is True:
        return False
    stable = parsed.get("stable_id")
    # A missing id is not filled in from the request. Title-only is not enough.
    return isinstance(stable, str) and stable == expected_id and bool(expected_id)


def _composer_value(parsed, written):
    if parsed.get("empty") is True:
        digest = parsed.get("digest")
        if digest is not None and digest != _EMPTY_DIGEST:
            raise Blocked("helper_invalid")
        return ""
    digest = parsed.get("digest")
    if not isinstance(digest, str):
        return None
    if digest == _EMPTY_DIGEST:
        return ""
    if isinstance(written, str) and digest == hashlib.sha256(canonical_text(written).encode()).hexdigest():
        return written
    return _UNMATCHED + digest


def _code_of(parsed):
    code = parsed.get("code")
    if isinstance(code, str) and _CODE.fullmatch(code):
        return code
    return ""

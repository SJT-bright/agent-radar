"""Read-only diagnostic contract for each app's main composer.

The contract classifies a caller-supplied observation. It does not focus a
window, walk an accessibility tree, write a draft, or send. ``grants_send``
stays false for every result, including a unique empty composer.

Version text is taken only from an appInfo mapping's
``CFBundleShortVersionString`` and ``CFBundleVersion``. Window titles, AX
values, placeholders, and any version-like key on the observation are ignored.

``bridge.py`` is intentionally unchanged. ``from_bridge_inspect`` and
``inspect_integration_plan`` describe how a later caller can adapt
``MacBackend.inspect()`` after it returns.
"""

import re

UNKNOWN = "unknown"
MISSING = "missing"
MULTIPLE = "multiple"
LOADING = "loading"
BUSY = "busy"
UNIQUE = "unique"
IDLE = "idle"

_VERSION_TOKEN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")
_VERSION_KEYS = ("CFBundleShortVersionString", "CFBundleVersion")

# Codes that bridge.inspect can return only after bind_window accepted one window.
_WINDOW_BOUND_CODES = frozenset({
    "ready", "send_unavailable", "already_running", "composer_unreadable",
    "target_unverified", "header_unverified", "sidebar_target_missing",
    "sidebar_target_ambiguous", "app_ui_unavailable",
})

READ_BUDGET = {
    "max_nodes": 6000,
    "max_depth": 55,
    "scan_seconds": 6,
}

# Structures below describe the checks already implemented in bridge.py.
# They are documentation for diagnosis, not a second sender.
_PROFILES = {
    "openclaw": {
        "bundle_id": "ai.openclaw.mac", "verified_versions": (), "navigation_seconds": 0.1,
        "composer": {"roles": ("LocalSessionRPC",), "hints": (),
                     "structure": "原会话 ID 与会话键通过只读本地索引核验；恢复走本地 Gateway RPC，不操作屏幕输入区"},
    },
    "codex": {
        "bundle_id": "com.openai.codex",
        "verified_versions": ("26.1007.30912",),
        "navigation_seconds": 1.5,
        "composer": {
            "roles": ("AXTextArea",),
            "hints": ("与 Codex 协作", "Work with Codex", "随心输入", "询问 ChatGPT"),
            "structure": "可读 DOM 类须含 ProseMirror，父级须含 _ComposerLayoutBody_；"
                         "类列表不可读时只接受 Group→Group→StaticText 的精确占位子树",
        },
    },
    "grok": {
        "bundle_id": "com.grokapp.desktop",
        "verified_versions": (),
        "navigation_seconds": 1.5,
        "composer": {
            "roles": ("AXTextArea", "AXTextField"),
            "hints": ("消息输入框", "随心输入"),
            "structure": "下半窗口中声明提示命中消息输入框或随心输入的输入区",
        },
    },
    "zcode": {
        "bundle_id": "dev.zcode.app",
        "verified_versions": (),
        "navigation_seconds": 2.0,
        "composer": {
            "roles": ("AXTextArea", "AXTextField"),
            "hints": ("提出后续修改要求", "继续输入以排队后续修改",
                      "Ask for follow-up changes", "Keep typing to queue follow-up changes"),
            "structure": "只接受上述后续修改提示；搜索框和历史正文不算主输入区",
        },
    },
    "autoclaw": {
        "bundle_id": "com.zhipuai.autoclaw",
        "verified_versions": (),
        "navigation_seconds": 1.5,
        "composer": {
            "roles": ("AXTextArea", "AXTextField"),
            "hints": ("填写你的目标，AutoClaw会持续工作至完成目标...",
                      "发送给 AutoClaw", "输入“@”使用技能",
                      "请输入你想让 AutoClaw 对文件做什么"),
            "structure": "只接受聊天输入区的声明提示，或「发送给 名称」；目标复核里的其他文本区不算",
        },
    },
    "workbuddy": {
        "bundle_id": "com.tencent.workbuddy.mac",
        "verified_versions": (),
        "navigation_seconds": 0.6,
        "composer": {
            "roles": ("AXTextArea", "AXTextField"),
            "hints": (),
            "structure": "下半窗口里排除搜索占位后的唯一文本区；没有单独的声明提示表",
        },
    },
    "workbuddy-ai": {
        "bundle_id": "com.workbuddy.workbuddy-ai",
        "verified_versions": (),
        "navigation_seconds": 1.5,
        "composer": {
            "roles": ("AXTextArea", "AXTextField"),
            "hints": (),
            "structure": "下半窗口里排除搜索占位后的唯一文本区；没有单独的声明提示表",
        },
    },
    "qoder": {
        "bundle_id": "com.qoder.app",
        "verified_versions": (),
        "navigation_seconds": 1.5,
        "composer": {
            "roles": ("AXTextArea", "AXTextField"),
            "hints": ("发送任务消息",),
            "structure": "声明提示为发送任务消息的唯一输入区",
        },
    },
    "qoder-cn": {
        "bundle_id": "com.qodercn.app",
        "verified_versions": (),
        "navigation_seconds": 1.5,
        "composer": {
            "roles": ("AXTextArea", "AXTextField"),
            "hints": ("发送任务消息",),
            "structure": "声明提示为发送任务消息的唯一输入区",
        },
    },
}

_LAYER_LABELS = (
    ("window", "窗口"),
    ("identity", "会话身份"),
    ("composer", "输入框"),
    ("send", "发送控件"),
)
_STATE_LABELS = {
    UNKNOWN: "未知",
    MISSING: "缺失",
    MULTIPLE: "多处命中",
    LOADING: "加载中",
    UNIQUE: "唯一命中",
}

INSPECT_INTEGRATION = (
    "不要修改 bridge.py。MacBackend.inspect() 返回后，用 bundle 的 appInfo "
    "调用 from_bridge_inspect(app_id, result, app_info)。版本只读 "
    "CFBundleShortVersionString 与 CFBundleVersion，不要从窗口标题、AX 值或 "
    "inspect 载荷里的 version 取版本。现有载荷的 send_exists 为假时无法区分"
    "零个和多个发送控件，应另传 send_count；identity 为假时也无法区分缺失和"
    "多处命中，应另传 identity_hits。loading 与 tree_incomplete 同样建议显式传入。"
    "返回值的 grants_send 恒为假，不能当作发送许可。"
)


def profiles():
    """Copy of the per-app contract. Callers cannot mutate the stored one."""
    return {app_id: _public_profile(app_id) for app_id in _PROFILES}


def read_app_info_version(app_info):
    """Version from appInfo only. Any other object is unknown and is not echoed."""
    if not isinstance(app_info, dict):
        return {"status": UNKNOWN, "source": "appInfo", "short": None, "build": None}
    short = _version_token(app_info.get("CFBundleShortVersionString"))
    build = _version_token(app_info.get("CFBundleVersion"))
    if short is None and build is None:
        return {"status": UNKNOWN, "source": "appInfo", "short": None, "build": None}
    return {"status": "present", "source": "appInfo", "short": short, "build": build}


def diagnose(app_id, observation=None, app_info=None):
    """Classify one read-only observation. Never authorizes a send."""
    observed = observation if isinstance(observation, dict) else {}
    version = read_app_info_version(app_info)
    version["compatibility"] = _compatibility(app_id, version)
    content = _composer_content(observed)
    window_state, window_detail = _window_state(observed)
    identity_state, identity_detail = _identity_state(observed)
    composer_state, composer_detail = _composer_state(observed, content)
    send_state, send_detail = _send_state(observed)
    if composer_state != UNIQUE:
        content = LOADING if composer_state == LOADING else None
    report = {
        "app_id": app_id if isinstance(app_id, str) else None,
        "known_app": isinstance(app_id, str) and app_id in _PROFILES,
        "version": version,
        "read_budget": dict(READ_BUDGET),
        "profile": _public_profile(app_id) if isinstance(app_id, str) and app_id in _PROFILES else None,
        "layers": {
            "window": {"state": window_state, "detail": window_detail},
            "identity": {"state": identity_state, "detail": identity_detail},
            "composer": {"state": composer_state, "detail": composer_detail},
            "send": {"state": send_state, "detail": send_detail},
        },
        "composer_content": content,
        "activity": _activity(observed, composer_state),
        "failed_layers": [],
        "grants_send": False,
        "attempted": False,
        "disposition": "diagnostic_only",
    }
    report["failed_layers"] = [name for name, _label in _LAYER_LABELS
                               if report["layers"][name]["state"] != UNIQUE]
    report["summary"] = _summary(report)
    return _seal(report)


def from_bridge_inspect(app_id, payload, app_info=None):
    """Adapt a ``MacBackend.inspect`` dict. Does not call the bridge.

    Version keys on ``payload`` are ignored. ``attempted`` on the payload
    cannot flip this report's ``attempted`` or ``grants_send``.
    """
    raw = payload if isinstance(payload, dict) else {}
    observation = {
        "window_count": raw.get("window_count"),
        "identity": raw.get("identity") if isinstance(raw.get("identity"), bool) else None,
        "identity_hits": raw.get("identity_hits"),
        "input_count": raw.get("input_count"),
        "busy": raw.get("busy") if isinstance(raw.get("busy"), bool) else None,
        "send_exists": raw.get("send_exists") if isinstance(raw.get("send_exists"), bool) else None,
        "send_count": raw.get("send_count"),
        "send_enabled": raw.get("send_enabled") if isinstance(raw.get("send_enabled"), bool) else None,
        "send_deferred": raw.get("send_deferred") is True,
        "empty_composer": raw.get("empty_composer") if isinstance(raw.get("empty_composer"), bool) else None,
        "loading": raw.get("loading") if isinstance(raw.get("loading"), bool) else None,
        "tree_incomplete": raw.get("tree_incomplete") is True or raw.get("code") == "tree_incomplete",
        "code": raw.get("code") if isinstance(raw.get("code"), str) else None,
    }
    report = diagnose(app_id, observation, app_info=app_info)
    report["bridge_code"] = observation["code"]
    report["integration"] = "bridge.inspect"
    return _seal(report)


def inspect_integration_plan():
    """How to connect this contract later. ``modify_bridge`` stays false."""
    return {
        "modify_bridge": False,
        "call_site": "MacBackend.inspect 返回之后、任何发送判断之前",
        "adapter": "from_bridge_inspect",
        "version_source": "appInfo",
        "version_keys": list(_VERSION_KEYS),
        "ignored_payload_keys": [
            "version", "app_version", "title", "value", "text",
            "CFBundleShortVersionString", "CFBundleVersion",
        ],
        "suggested_payload_additions": [
            "window_count", "identity_hits", "send_count", "loading", "tree_incomplete",
        ],
        "cannot_grant_send": True,
        "note": INSPECT_INTEGRATION,
    }


def _seal(report):
    report["grants_send"] = False
    report["attempted"] = False
    report["disposition"] = "diagnostic_only"
    return report


def _version_token(value):
    if not isinstance(value, str) or not _VERSION_TOKEN.fullmatch(value):
        return None
    return value


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _measured_count(observation, key):
    """Distinguish a missing count from a present but unusable one."""
    if key not in observation or observation[key] is None:
        return "absent", None
    count = _count(observation[key])
    if count is None:
        return "invalid", None
    return "ok", count


def _compatibility(app_id, version):
    profile = _PROFILES.get(app_id) if isinstance(app_id, str) else None
    if profile is None or version["status"] != "present" or version["short"] is None:
        return UNKNOWN
    verified = profile["verified_versions"]
    if not verified:
        return "unverified"
    return "verified" if version["short"] in verified else "unverified"


def _public_profile(app_id):
    profile = _PROFILES.get(app_id)
    if profile is None:
        return None
    composer = profile["composer"]
    return {
        "bundle_id": profile["bundle_id"],
        "verified_versions": list(profile["verified_versions"]),
        "navigation_seconds": profile["navigation_seconds"],
        "read_budget": dict(READ_BUDGET),
        "composer": {
            "roles": list(composer["roles"]),
            "hints": list(composer["hints"]),
            "structure": composer["structure"],
        },
    }


def _composer_content(observation):
    if observation.get("loading") is True:
        return LOADING
    if "value" in observation:
        value = observation.get("value")
        if value is None:
            return LOADING
        if isinstance(value, str):
            return "empty" if value == "" else "draft"
        return None
    empty = observation.get("empty_composer")
    status, count = _measured_count(observation, "input_count")
    if status != "ok" or count != 1:
        return None
    if empty is True:
        return "empty"
    if empty is False and observation.get("code") == "composer_unreadable":
        return LOADING
    if empty is False:
        return "draft"
    return None


def _composer_state(observation, content):
    if observation.get("tree_incomplete") is True or observation.get("code") == "tree_incomplete":
        return UNKNOWN, "budget"
    status, count = _measured_count(observation, "input_count")
    if status == "invalid":
        return UNKNOWN, "invalid"
    # Several matches stay ambiguous even while a renderer is still loading.
    if status == "ok" and count > 1:
        return MULTIPLE, "count"
    if observation.get("loading") is True:
        return LOADING, "flag"
    if status == "absent":
        return UNKNOWN, "absent"
    if count == 0:
        return MISSING, "count"
    if content == LOADING:
        return LOADING, "value"
    if content in ("empty", "draft"):
        return UNIQUE, content
    return UNKNOWN, "value"


def _window_state(observation):
    status, count = _measured_count(observation, "window_count")
    if status == "invalid":
        return UNKNOWN, "invalid"
    if status == "ok":
        if count == 0:
            return MISSING, "count"
        if count == 1:
            return UNIQUE, "count"
        return MULTIPLE, "count"
    code = observation.get("code")
    if code == "window_ambiguous":
        return MULTIPLE, "code"
    if code == "app_unavailable":
        return MISSING, "code"
    if code in _WINDOW_BOUND_CODES:
        return UNIQUE, "bound"
    return UNKNOWN, "absent"


def _identity_state(observation):
    status, hits = _measured_count(observation, "identity_hits")
    identity = observation.get("identity")
    if status == "invalid":
        return UNKNOWN, "invalid"
    if status == "ok":
        if hits == 0:
            return MISSING, "count"
        if hits > 1:
            return MULTIPLE, "count"
        if identity is False:
            return UNKNOWN, "mismatch"
        return UNIQUE, "count"
    code = observation.get("code")
    if code == "sidebar_target_ambiguous":
        return MULTIPLE, "code"
    if code == "sidebar_target_missing":
        return MISSING, "code"
    if identity is True:
        return UNIQUE, "flag"
    if identity is False:
        return UNKNOWN, "unverified"
    return UNKNOWN, "absent"


def _send_state(observation):
    deferred = observation.get("send_deferred") is True
    status, count = _measured_count(observation, "send_count")
    if status == "invalid":
        return UNKNOWN, "invalid"
    if status == "ok":
        if count == 0:
            return MISSING, "deferred" if deferred else "count"
        if count == 1:
            return UNIQUE, "deferred" if deferred else "count"
        return MULTIPLE, "count"
    exists = observation.get("send_exists")
    if exists is True:
        return UNIQUE, "flag"
    if deferred:
        return MISSING, "deferred"
    if exists is False or observation.get("code") == "send_unavailable":
        # send_exists false collapses zero buttons and several buttons.
        return UNKNOWN, "collapsed"
    return UNKNOWN, "absent"


def _activity(observation, composer_state):
    busy = observation.get("busy")
    if busy is True or (not isinstance(busy, bool) and observation.get("code") == "already_running"):
        return BUSY
    if composer_state == LOADING or observation.get("loading") is True:
        return LOADING
    if busy is False:
        return IDLE
    return UNKNOWN


def _summary(report):
    parts = []
    for name, label in _LAYER_LABELS:
        state = report["layers"][name]["state"]
        detail = report["layers"][name]["detail"]
        if state == UNIQUE:
            continue
        text = label + _STATE_LABELS[state]
        if name == "send" and detail == "deferred":
            text = "发送控件缺失（空框延后）"
        elif name == "identity" and state == MULTIPLE:
            text = "会话身份多处命中"
        parts.append(text)
    if report["layers"]["composer"]["state"] == UNIQUE:
        if report["composer_content"] == "empty":
            parts.insert(0, "输入框为空")
        elif report["composer_content"] == "draft":
            parts.insert(0, "输入框有草稿")
    if report["activity"] == BUSY:
        parts.append("忙碌")
    if not parts:
        content = report["composer_content"]
        if content == "empty":
            parts.append("输入框为空")
        elif content == "draft":
            parts.append("输入框有草稿")
        parts.append("只读诊断，不授予发送")
    else:
        parts.append("不授予发送")
    if report["version"]["compatibility"] == "unverified" and report["version"]["status"] == "present":
        parts.append("版本未在已核验名单")
    if not report["known_app"]:
        parts.append("软件未登记")
    return "；".join(parts)

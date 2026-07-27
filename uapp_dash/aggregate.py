"""集約: 各プロジェクトの `.agent-status/` を読み、表示用のフリートモデルを作る。

読み取り専用。派生状態（stalled/crashed）・claims の衝突・申告とエビデンスの不一致は
ここで計算する（書き手には計算させない）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import attention, claims as claims_mod, protocol as P
from .proc import alive_on_this_host, hostname
from .store import StatusStore, read_json, write_json_atomic

SCHEMA_FLEET = "uapp-dash/fleet/0"
MAX_EVENTS_PER_UNIT = 200
MAX_RECENT_EVENTS = 30
DEFAULT_SCAN_DEPTH = 3


def dash_home() -> Path:
    override = os.environ.get("UAPP_DASH_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "uapp-dash"
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "uapp-dash"


def registry_path() -> Path:
    return dash_home() / "registry.json"


def config_path() -> Path:
    return dash_home() / "config.json"


def load_registry() -> list[str]:
    data = read_json(registry_path()) or {}
    projects = data.get("projects")
    return [str(p) for p in projects] if isinstance(projects, list) else []


def register_project(project_root: Path) -> None:
    """`uapp-dash init` / `uapp-dash begin` から自動登録する（走査ルート設定に頼らないための本命経路）。"""
    project_root = Path(project_root).resolve()
    known = load_registry()
    if str(project_root) in known:
        return
    known.append(str(project_root))
    write_json_atomic(registry_path(), {"projects": known, "updatedAt": P.now_iso()})


def load_config() -> dict:
    config = read_json(config_path()) or {}
    roots = config.get("roots")
    return {
        "roots": [str(r) for r in roots] if isinstance(roots, list) else [],
        "scanDepth": int(config.get("scanDepth") or DEFAULT_SCAN_DEPTH),
    }


def scan_root(root: Path, depth: int) -> list[Path]:
    """深さ制限付きで `.agent-status` を探す（巨大な Library を掘らないため）。"""
    found: list[Path] = []
    root = Path(root)
    if not root.is_dir():
        return found

    def walk(current: Path, level: int) -> None:
        if (current / P.STATUS_DIR_NAME).is_dir():
            found.append(current)
        if level >= depth:
            return
        try:
            entries = list(current.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_dir() and not entry.name.startswith(".") and entry.name not in ("Library", "node_modules"):
                walk(entry, level + 1)

    walk(root, 0)
    return found


def discover_projects() -> list[Path]:
    seen: dict[str, Path] = {}
    for raw in load_registry():
        path = Path(raw)
        if (path / P.STATUS_DIR_NAME).is_dir():
            seen.setdefault(str(path.resolve()), path)
    config = load_config()
    for root in config["roots"]:
        for path in scan_root(Path(root), config["scanDepth"]):
            seen.setdefault(str(path.resolve()), path)
    return list(seen.values())


def _device_panel(units: list[dict], stores: StatusStore) -> list[dict]:
    """最新の evidence.device をシリアル単位でまとめる（負荷とポートの見張り）。"""
    latest: dict[str, dict] = {}
    for unit in units:
        for event in unit.get("_events", []):
            if event.get("kind") != "evidence.device":
                continue
            data = event.get("data") or {}
            serial = str(data.get("serial") or "unknown")
            current = latest.get(serial)
            if current is None or str(event.get("at") or "") >= str(current.get("at") or ""):
                load1 = data.get("load1")
                latest[serial] = {
                    "serial": serial,
                    "at": event.get("at"),
                    "load1": load1,
                    "uptimeSec": data.get("uptimeSec"),
                    "ports": data.get("ports") or [],
                    "warn": isinstance(load1, (int, float)) and load1 > P.DEVICE_LOAD_WARN,
                }
    return sorted(latest.values(), key=lambda d: d["serial"])


# 表示時にリンクになりうるフィールド。書き手は任意の文字列を入れられるので、
# `javascript:` のような実行可能スキームをここで落とす（ビューアー側にも allowlist がある）
LINK_KEYS = ("journeyReport", "reportPath", "artifactPath", "failureDir", "logPath", "xmlPath")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_SAFE_SCHEMES = ("http://", "https://", "file://")


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def safe_link(value):
    """リンクとして出して良い値だけ通す。危険なら None（呼び手は文字列として扱う）。

    **制御文字を含む値は無条件で拒否する**。`java&#9;script:` のようにタブ・改行を挟むと
    正規表現ではスキーム無し（相対パス）に見えるが、ブラウザの URL 解析はそれらを取り除いて
    `javascript:` として実行する。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    if _CONTROL_CHARS.search(value):
        return None
    text = value.strip()
    if _WINDOWS_DRIVE.match(text) or text.startswith("\\\\") or text.startswith("/"):
        return text                      # ローカルパス（絶対）
    if _SCHEME.match(text):
        return text if text.lower().startswith(_SAFE_SCHEMES) else None
    if ":" in text.split("/", 1)[0]:
        return None                      # 先頭要素にコロン＝解釈が割れる形は通さない
    return text                          # 相対パス


def _sanitize_links(event: dict) -> dict:
    data = event.get("data")
    if not isinstance(data, dict):
        return event
    cleaned = None
    for key in LINK_KEYS:
        if key in data and safe_link(data[key]) is None and data[key] is not None:
            cleaned = cleaned or dict(data)
            cleaned[key] = None
            cleaned[f"{key}Rejected"] = str(data[key])[:200]   # 何が来たかは残す（表示は文字列）
    return {**event, "data": cleaned} if cleaned else event


ACTIVITY_BUCKETS = 12
ACTIVITY_BUCKET_SEC = 300      # 5 分 × 12 = 直近 1 時間


def _activity_buckets(units: list[dict], *, now: datetime) -> list[int]:
    """直近 1 時間のイベント数を 5 分刻みで数える（古い順）。

    「稼働中」と書いてあっても本当に動いているかは件数の推移でしか分からない。
    スパークラインの元データとして配る。
    """
    buckets = [0] * ACTIVITY_BUCKETS
    for unit in units:
        for event in unit.get("_events", []):
            at, _ = P.parse_iso_safe(event.get("at"))
            if at is None:
                continue
            delta = (now - at).total_seconds()
            if delta < 0 or delta >= ACTIVITY_BUCKETS * ACTIVITY_BUCKET_SEC:
                continue
            buckets[ACTIVITY_BUCKETS - 1 - int(delta // ACTIVITY_BUCKET_SEC)] += 1
    return buckets


def _latest_evidence(events: list[dict]) -> dict | None:
    for event in reversed(events):
        kind = event.get("kind") or ""
        if kind.startswith("evidence."):
            data = event.get("data") or {}
            return {"kind": kind, "at": event.get("at"), "ok": P.evidence_ok(data),
                    "summary": P.summarize_evidence(kind, data)}
    return None


def _synthesize_unit(project_root: Path, unit_id: str, events: list[dict]) -> dict:
    """ジャーナルしか無い単位（依存ゼロのエミッタが書いたもの）を表示できる形にする。"""
    last_at = events[-1].get("at") if events else P.now_iso()
    return {
        "schema": P.SCHEMA_UNIT,
        "unitId": unit_id,
        "kind": "ambient",
        "project": {"path": str(project_root), "name": Path(project_root).name},
        "label": f"（スナップショット無し・{unit_id}）",
        "owner": {"agent": "tool", "host": unit_id.split("ambient-")[-1] if unit_id.startswith("ambient-") else ""},
        "state": "idle",
        "activity": "",
        "startedAt": events[0].get("at") if events else last_at,
        "lastHeartbeat": last_at,
        "ttlSec": P.DEFAULT_TTL_SEC,
        "tasks": [],
        "claims": [],
        "resources": [],
        "lastEvidence": None,
        "eventCount": len(events),
        "endedAt": None,
        "result": None,
    }


def build_project(project_root: Path, *, now: datetime | None = None) -> dict:
    now = now or P.now()
    store = StatusStore.for_project(project_root)
    units = store.list_units()
    for unit_id in store.orphan_journals():
        units.append(_synthesize_unit(store.project_root, unit_id,
                                      store.read_events(unit_id, limit=MAX_EVENTS_PER_UNIT)))
    enriched: list[dict] = []
    warnings: list[str] = []

    for unit in units:
        unit_id = unit.get("unitId")
        if not unit_id or not P.valid_unit_id(str(unit_id)):
            warnings.append(f"unitId が不正な記録を無視した: {unit_id!r}")
            continue
        events = [_sanitize_links(e) for e in store.read_events(unit_id, limit=MAX_EVENTS_PER_UNIT)]
        derived = attention.derive(unit, now=now)
        unit = dict(unit)
        unit["derived"] = derived
        unit["progress"] = attention.progress(unit)
        unit["mismatches"] = attention.detect_mismatch(unit, events)
        # スナップショットを更新しないエミッタ（NDJSON 追記だけ）にも対応するため、
        # 最新エビデンスはジャーナルからも導出して新しい方を採る
        latest = _latest_evidence(events)
        if latest and str(latest.get("at") or "") >= str((unit.get("lastEvidence") or {}).get("at") or ""):
            unit["lastEvidence"] = latest
        unit["_events"] = events
        unit["recentEvents"] = events[-MAX_RECENT_EVENTS:]
        warnings.extend(derived.get("warnings") or [])
        enriched.append(unit)

    resources = []
    for record in store.list_resources():
        holder = record.get("holder") or {}
        resources.append({**record, "holderAlive": alive_on_this_host(holder)})

    devices = _device_panel(enriched, store)
    buckets = _activity_buckets(enriched, now=now)
    for unit in enriched:
        unit.pop("_events", None)

    conflicts = claims_mod.find_conflicts(enriched)
    ordered = attention.sort_units(enriched, now=now)
    active = [u for u in ordered if (u.get("derived") or {}).get("state") not in ("done", "idle")]
    running = [u for u in ordered if (u.get("derived") or {}).get("state") == "running"]
    project = {
        "name": Path(project_root).name,
        "path": str(project_root),
        "projectId": hashlib.sha1(str(Path(project_root).resolve()).encode("utf-8")).hexdigest()[:8],
        "units": ordered,
        "activeCount": len(active),
        "runningCount": len(running),
        "conflicts": conflicts,
        "resources": resources,
        "devices": devices,
        "warnings": sorted(set(warnings)),
        # 直近 1 時間の動き（5 分 × 12）。数字だけだと「静かなのか死んだのか」が分からない
        "activityBuckets": buckets,
        "events60m": sum(buckets),
    }
    # 要注意の判定は**ここだけ**で行う。表示側にも同じ判定を書くと、件数と一覧が食い違う
    project["priorityItems"] = _priority_items(project, now=now)
    project["attentionCount"] = len(project["priorityItems"])
    for category in P.ATTENTION_CATEGORIES:
        project[f"{category}Count"] = len([i for i in project["priorityItems"] if i["category"] == category])
    project["lastActivityAt"] = max(
        [str(u.get("lastHeartbeat") or u.get("startedAt") or "") for u in ordered] or [""])
    return project


def _item(kind: str, category: str, rank_: int, title: str, reason: str, since: str | None,
          **extra) -> dict:
    return {"kind": kind, "category": category, "rank": rank_, "title": title,
            "reason": reason, "sinceAt": since or "", **extra}


def _priority_items(project: dict, *, now: datetime) -> list[dict]:
    """人が見るべきものを 1 本のリストに正規化する。

    単位だけでなく、編集領域の衝突・取り残された排他資源・デバイス過負荷・データの警告も
    同じ形にする（カードの中にしか出ないと、件数にも並び順にも反映されず見落とす）。
    """
    items: list[dict] = []
    for unit in project["units"]:
        derived = unit.get("derived") or {}
        category = derived.get("attentionCategory")
        mismatches = unit.get("mismatches") or []
        if not category and not mismatches:
            continue
        reason = (derived.get("reasons") or [None])[0] or (mismatches[0]["message"] if mismatches else "")
        items.append(_item(
            "unit", category or "watch", derived.get("attentionRank", 99),
            unit.get("label") or unit.get("unitId", ""), reason,
            unit.get("lastHeartbeat") or unit.get("startedAt"),
            unitId=unit.get("unitId"), state=derived.get("state"),
            terminal=bool(derived.get("terminal"))))

    for conflict in project["conflicts"]:
        hard = conflict.get("severity") == "conflict"
        items.append(_item(
            "conflict", "human" if hard else "watch", 1 if hard else 6,
            "編集領域の衝突" if hard else "YAML 資産の重なり注意",
            f"{conflict.get('reason', '')}: {' ↔ '.join(conflict.get('labels') or [])}",
            None, paths=conflict.get("paths") or []))

    for resource in project["resources"]:
        if resource.get("holderAlive") is False:
            holder = resource.get("holder") or {}
            items.append(_item(
                "orphanResource", "incident", 2, f"取り残し: {resource.get('resource')}",
                f"保持者のプロセスが居ない（{holder.get('label') or holder.get('unitId') or '不明'}）",
                resource.get("acquiredAt"), resource_=resource.get("resource")))

    for device in project["devices"]:
        if device.get("warn"):
            items.append(_item(
                "deviceOverload", "watch", 6, f"デバイス過負荷: {device.get('serial')}",
                f"load {device.get('load1')}（E2E がコールドスタート待ちで偽陽性失敗しうる）",
                device.get("at")))

    for warning in project["warnings"]:
        items.append(_item("dataWarning", "watch", 7, "記録の警告", warning, None))

    order = {name: index for index, name in enumerate(P.ATTENTION_CATEGORIES)}
    # 同じ種類なら、より上位の状態 → より長く放置されているものを先に
    items.sort(key=lambda i: (order.get(i["category"], 9), i["rank"], i["sinceAt"] or ""))
    return items


def build_fleet(projects: list[Path] | None = None, *, now: datetime | None = None) -> dict:
    now = now or P.now()
    roots = projects if projects is not None else discover_projects()
    built = []
    for root in roots:
        store = StatusStore.for_project(Path(root))
        if not store.exists():
            continue
        built.append(build_project(store.project_root, now=now))

    # 同名のプロジェクト（unity-nis が複数リポジトリにある等）を見分けられるようにする
    seen_names: dict[str, int] = {}
    for project in built:
        seen_names[project["name"]] = seen_names.get(project["name"], 0) + 1
    for project in built:
        if seen_names[project["name"]] > 1:
            parent = Path(project["path"]).parent.name
            project["displayName"] = f"{parent}/{project['name']}" if parent else project["name"]
        else:
            project["displayName"] = project["name"]

    order = {name: index for index, name in enumerate(P.ATTENTION_CATEGORIES)}

    def attention_key(project: dict) -> tuple:
        """並び順は `priorityItems` の先頭＝**そのプロジェクトで最も人手が要るもの**で決める。

        単位の状態だけで並べると、単位に紐づかない要注意項目（編集領域の衝突・取り残された
        排他資源・デバイス過負荷・記録の警告）が並び順に効かず、**Action Queue の順序と
        カード・ストリップの並びが食い違う**（衝突を抱えたプロジェクトがレビュー待ちより
        後ろに出る）。同じ判断を 2 か所でしない。
        """
        items = project["priorityItems"]
        if not items:
            return (len(P.ATTENTION_CATEGORIES), len(P.ATTENTION_ORDER), "")
        top = items[0]      # プロジェクト内で既に (category, rank, sinceAt) 順に並んでいる
        return (order.get(top["category"], 9), top["rank"], top["sinceAt"] or "")

    built.sort(key=lambda p: (*attention_key(p), p["name"]))

    # 表示側が並べ直さなくて済むよう、プロジェクト横断の優先リストもここで作る
    priority: list[dict] = []
    for project in built:
        for item in project["priorityItems"]:
            priority.append({**item, "project": project["displayName"],
                             "projectId": project["projectId"], "projectPath": project["path"]})
    priority.sort(key=lambda i: (order.get(i["category"], 9), i["rank"], i["sinceAt"] or ""))

    counts = {category: sum(p.get(f"{category}Count", 0) for p in built)
              for category in P.ATTENTION_CATEGORIES}
    fleet_buckets = [sum(values) for values in
                     zip(*[p["activityBuckets"] for p in built])] if built else [0] * ACTIVITY_BUCKETS
    all_devices = [d for p in built for d in p["devices"]]
    return {
        "schema": SCHEMA_FLEET,
        "generatedAt": P.to_iso(now),
        "host": hostname(),
        "projects": built,
        "priorityItems": priority,
        "summary": {
            "projects": len(built),
            "activeProjects": len([p for p in built if p["runningCount"]]),
            "units": sum(len(p["units"]) for p in built),
            "active": sum(p["activeCount"] for p in built),
            "running": sum(p["runningCount"] for p in built),
            "attention": sum(p["attentionCount"] for p in built),
            "human": counts["human"], "incident": counts["incident"], "watch": counts["watch"],
            # 画面下段のステータスバー用（資源・デバイス・衝突・直近の動き）
            "resourcesHeld": sum(len(p["resources"]) for p in built),
            "devices": len(all_devices),
            "devicesReady": len([d for d in all_devices if not d.get("warn")]),
            "conflicts": sum(len([c for c in p["conflicts"] if c["severity"] == "conflict"]) for p in built),
            "events60m": sum(p["events60m"] for p in built),
            "activityBuckets": fleet_buckets,
        },
    }


def fleet_json(fleet: dict) -> str:
    return json.dumps(fleet, ensure_ascii=False)

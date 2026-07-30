"""編集領域の宣言（claims）と重複警告（判断 D: 勧告のみ・ブロックしない）。

ファイルシステムを走査せず、パターン同士で保守的に判定する（過剰警告側に倒す）。
"""
from __future__ import annotations

import re

# マージ困難な YAML 資産。行単位の共存が成り立たないのでファイル丸ごと排他にする
YAML_ASSET_SUFFIXES = (".unity", ".prefab", ".asset")
# 宣言に関わらず暗黙で排他扱いにする領域（エディタ起動だけで書き換わる）
IMPLICIT_EXCLUSIVE = ("ProjectSettings/**", "Packages/manifest.json")

MODES = ("shared", "exclusive")


def normalize_path(path: str) -> str:
    text = str(path).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _translate(pattern: str) -> re.Pattern:
    out = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(ch))
        i += 1
    out.append("$")
    # Windows / macOS のファイルシステムは大小文字を区別しない。区別して比較すると
    # Assets/... と assets/... を別物と見なして衝突を見落とす（仕様は過剰警告側へ倒す）
    return re.compile("".join(out), re.IGNORECASE)


def matches(pattern: str, path: str) -> bool:
    return bool(_translate(normalize_path(pattern)).match(normalize_path(path)))


def static_prefix(pattern: str) -> str:
    """最初のワイルドカードより前の部分（ディレクトリ境界で切り詰める）。"""
    pattern = normalize_path(pattern)
    cut = len(pattern)
    for idx, ch in enumerate(pattern):
        if ch in "*?[":
            cut = idx
            break
    head = pattern[:cut]
    if cut < len(pattern):
        head = head[: head.rfind("/") + 1]
    return head


def _components(path: str) -> list[str]:
    return [part.lower() for part in normalize_path(path).split("/") if part]


def _is_ancestor(prefix: str, path: str) -> bool:
    a, b = _components(prefix), _components(path)
    return len(a) <= len(b) and b[: len(a)] == a


def patterns_overlap(a: str, b: str) -> bool:
    a, b = normalize_path(a), normalize_path(b)
    if a.lower() == b.lower():
        return True
    sa, sb = static_prefix(a), static_prefix(b)
    if not sa or not sb:
        return True  # 先頭が ** 等で範囲が読めない → 保守的に重なりとみなす
    if matches(a, sb.rstrip("/")) or matches(b, sa.rstrip("/")) or matches(a, sb) or matches(b, sa):
        return True
    has_wild_a, has_wild_b = sa != a, sb != b
    if has_wild_a and _is_ancestor(sa, sb):
        return True
    if has_wild_b and _is_ancestor(sb, sa):
        return True
    return False


def _is_yaml_asset(path: str) -> bool:
    return normalize_path(path).lower().endswith(YAML_ASSET_SUFFIXES)


def _in_implicit_exclusive(path: str) -> bool:
    return any(patterns_overlap(path, pattern) for pattern in IMPLICIT_EXCLUSIVE)


SEPARATOR = ";"


def _split_unescaped(text: str) -> list[str]:
    """エスケープされていない `;` で分割し、`\\;` はリテラルの `;` に戻す。"""
    parts: list[str] = []
    buffer: list[str] = []
    index = 0
    while index < len(text):
        ch = text[index]
        if ch == "\\" and index + 1 < len(text) and text[index + 1] == SEPARATOR:
            buffer.append(SEPARATOR)     # `\;` はリテラル
            index += 2
            continue
        if ch == SEPARATOR:
            parts.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(ch)
        index += 1
    parts.append("".join(buffer))
    return [p.strip() for p in parts if p.strip()]


def split_separators(raw) -> tuple[list, list[str]]:
    """**CLI 入力専用**: `;` で連結された値をほどく。戻り値は (ほどいた値, ほどいた元の値)。

    **隣り合うオプションで区切りが違うのが事故のもと**だった。`--tasks` は `;` 区切り、
    `--claims` は空白区切りなので、`--tasks "調査;実装"` を書いた直後に
    `--claims "a/**;b/**"` と書くのが自然に起きる。しかも間違えても
    「`;` を含む 1 本のパス」が claim になるだけで、**衝突検出が静かに無効化される**
    （導入先で 2 回とも間違えており、記録を見るまで気づけなかった）。

    ただし **`;` はファイル名に使える文字**（Windows でも POSIX でも不正ではない）なので、
    ここで勝手にほどくのは「取り違えのほうが桁違いに起きやすい」という運用判断にすぎない。
    そのため:

    - **CLI の入口でだけ呼ぶ**（`normalize_claims` は純粋に保つ。保存済みデータや
      ライブラリ利用者の宣言を、読み込むたびに黙って書き換えてはならない）
    - **`\\;` はリテラルの `;`** として扱う（本当に `;` を含むパスの逃げ道）
    - ほどいた事実は呼び手が警告できるよう返す（黙って直すと次も同じ書き方をする）
    """
    if not raw:
        return [], []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    expanded: list = []
    offenders: list[str] = []
    for item in raw:
        if isinstance(item, str):
            path, wrap = item, None
        elif isinstance(item, dict):
            path, wrap = str(item.get("path", "")), item
        else:
            expanded.append(item)
            continue
        if SEPARATOR not in path:
            expanded.append(item)
            continue
        parts = _split_unescaped(path)
        if len(parts) > 1:
            offenders.append(path)
        for part in parts:
            expanded.append(part if wrap is None else {**wrap, "path": part})
    return expanded, offenders


def normalize_claims(raw) -> list[dict]:
    """文字列/辞書の混在を正規化し、Unity 固有の昇格規則を適用する。"""
    if not raw:
        return []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    # **ここでは `;` をほどかない**（`;` は正当なファイル名文字。保存済みの宣言を
    # 読み込むたびに書き換えると、宣言した領域が黙って別物になる）。
    # 区切りの取り違えをほどくのは CLI の入口だけ（split_separators）
    result: list[dict] = []
    seen: set[str] = set()

    def add(path: str, mode: str, reason: str | None = None) -> None:
        path = normalize_path(path)
        if not path or path in seen:
            return
        seen.add(path)
        entry = {"path": path, "mode": mode}
        if reason:
            entry["promotedBy"] = reason
        result.append(entry)

    for item in raw:
        if isinstance(item, str):
            path, mode = item, "shared"
        else:
            path, mode = item.get("path", ""), item.get("mode", "shared")
        path = normalize_path(path)
        if not path:
            continue
        if mode not in MODES:
            mode = "shared"
        reason = None
        if _is_yaml_asset(path):
            mode, reason = "exclusive", "yaml-asset"
        elif _in_implicit_exclusive(path):
            mode, reason = "exclusive", "implicit-exclusive"
        add(path, mode, reason)
        if _is_yaml_asset(path):
            add(path + ".meta", "exclusive", "yaml-asset-meta")
    return result


def find_conflicts(units: list[dict]) -> list[dict]:
    """単位間で重なる claim を列挙する。重い判定ではないので毎回作り直す。

    同伴で追加した `.meta` も**判定には使う**（片方が本体・片方が .meta を宣言する
    ケースを見落とさないため）。表示上の重複だけを、本体パスに正規化して排除する。
    """
    conflicts: list[dict] = []
    seen: set[tuple] = set()
    active = [u for u in units if u.get("state") != "done"]
    for i, unit_a in enumerate(active):
        for unit_b in active[i + 1 :]:
            for claim_a in unit_a.get("claims") or []:
                for claim_b in unit_b.get("claims") or []:
                    path_a, path_b = claim_a.get("path", ""), claim_b.get("path", "")
                    if not path_a or not path_b or not patterns_overlap(path_a, path_b):
                        continue
                    exclusive = claim_a.get("mode") == "exclusive" or claim_b.get("mode") == "exclusive"
                    if exclusive:
                        severity, reason = "conflict", "排他宣言された領域が重なっている"
                    elif _assets_glob(path_a) and _assets_glob(path_b):
                        severity, reason = "yaml-risk", "重なりの中に YAML 資産（シーン/プレハブ）が入りうる"
                    else:
                        continue
                    key = (unit_a.get("unitId"), unit_b.get("unitId"), severity,
                           _canonical(path_a), _canonical(path_b))
                    if key in seen:
                        continue   # 本体と .meta で同じ衝突を二重に出さない
                    seen.add(key)
                    conflicts.append(
                        {
                            "severity": severity,
                            "reason": reason,
                            "units": [unit_a.get("unitId"), unit_b.get("unitId")],
                            "labels": [unit_a.get("label"), unit_b.get("label")],
                            "paths": [path_a, path_b],
                        }
                    )
    return conflicts


def _canonical(path: str) -> str:
    """表示上の重複排除キー。`.meta` は本体と同じ衝突として扱う。"""
    normalized = normalize_path(path).lower()
    return normalized[: -len(".meta")] if normalized.endswith(".meta") else normalized


def _assets_glob(path: str) -> bool:
    path = normalize_path(path)
    return path.lower().startswith("assets/") and any(ch in path for ch in "*?")

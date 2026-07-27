"""`.agent-status/` の入出力。

書き手は「自分の単位のファイルだけ」を原子的に置換し、ジャーナルには追記しかしない。
読み手はロックを取らず、壊れた JSON を読んだら一度だけ再試行する（判断 A）。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

try:                      # Windows
    import msvcrt
except ImportError:       # POSIX
    msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None

from . import protocol as P
from .proc import alive_on_this_host, hostname

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def slugify_resource(resource_id: str) -> str:
    """資源 ID をファイル名に落とす。衝突を避けるため元 ID のハッシュを付ける。"""
    head = _SLUG_UNSAFE.sub("_", resource_id)[:60]
    digest = hashlib.sha1(resource_id.encode("utf-8")).hexdigest()[:8]
    return f"{head}-{digest}"


REPLACE_RETRIES = 5
REPLACE_BACKOFF_SEC = 0.04
# 排他区間（資源の取得/解放・単位の退避）を待つ上限
LOCK_WAIT_SEC = 5.0

# release_resource の結果（「解放できなかった」を一括りにしない）
RELEASE_RELEASED = "released"
RELEASE_ABSENT = "absent"
RELEASE_NOT_OWNER = "not-owner"
RELEASE_BUSY = "busy"
# 呼び手にとって「もう自分が持っていない」と言える結果
RELEASE_SETTLED = (RELEASE_RELEASED, RELEASE_ABSENT, RELEASE_NOT_OWNER)


def _lock_fd(fd) -> None:
    if msvcrt is not None:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    elif fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:                                   # ロック機構が無い環境（想定外）
        raise OSError("この環境ではファイルロックを使えない")


def _unlock_fd(fd) -> None:
    try:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float = LOCK_WAIT_SEC):
    """OS のファイルロックで排他区間を作る。

    ファイル名の付け替えだけで排他を作ろうとすると、「取り残しトークンをどう回収するか」で
    必ず新しいレースが生まれる。OS のロックなら**プロセスが死んだ時点で必ず解放される**ので、
    取り残しが原理的に発生しない。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                _lock_fd(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"排他区間を取得できない（他プロセスが操作中）: {path}")
                time.sleep(0.02)
        try:
            yield
        finally:
            _unlock_fd(fd)
    finally:
        os.close(fd)


def write_json_atomic(path: Path, payload: dict) -> None:
    """同一ディレクトリの一時ファイル経由で置換する。

    Windows では読み手がファイルを開いている間の置換が共有違反で失敗し得る
    （Python の既定の open は FILE_SHARE_DELETE を立てない）。読み手はロックを取らない
    設計なので、**表示のために開かれただけで書き手が死ぬ**ことがないよう短く再試行する。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt >= REPLACE_RETRIES - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(REPLACE_BACKOFF_SEC * (attempt + 1))


def read_json(path: Path, retries: int = 1) -> dict | None:
    """壊れた JSON（書き込み途中の読み取り）は一度だけ待って再試行する。"""
    for attempt in range(retries + 1):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            if attempt >= retries:
                return None
            time.sleep(0.05)
    return None


def find_project_root(start: Path | None = None) -> Path:
    """`.agent-status` → `.git` の順に上位へ探し、無ければ起点を返す。"""
    cur = (start or Path.cwd()).resolve()
    for base in [cur, *cur.parents]:
        if (base / P.STATUS_DIR_NAME).is_dir():
            return base
    for base in [cur, *cur.parents]:
        if (base / ".git").exists():
            return base
    return cur


def resolve_status_dir(project: Path | None = None, *, env: dict | None = None) -> Path:
    """明示指定 > 環境変数 > プロジェクト探索 の順で `.agent-status` の位置を決める。"""
    env = os.environ if env is None else env
    if project is not None:
        project = Path(project)
        return project if project.name == P.STATUS_DIR_NAME else project / P.STATUS_DIR_NAME
    for key in ("UAPP_DASH_STATUS_DIR", "UAPP_E2E_STATUS_DIR"):
        value = env.get(key)
        if value:
            return Path(value)
    return find_project_root() / P.STATUS_DIR_NAME


class StatusStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    # --- 位置 ---------------------------------------------------------
    @classmethod
    def for_project(cls, project: Path | None = None, *, env: dict | None = None) -> "StatusStore":
        return cls(resolve_status_dir(project, env=env))

    @property
    def project_root(self) -> Path:
        return self.root.parent

    @property
    def units_dir(self) -> Path:
        return self.root / "units"

    @property
    def done_dir(self) -> Path:
        return self.units_dir / "done"

    @property
    def resources_dir(self) -> Path:
        return self.root / "resources"

    @property
    def locks_dir(self) -> Path:
        """ロックファイルの置き場。

        データと同じディレクトリに `*.lck` を置くと、`units/done/` や `resources/` に
        残り続けて「消してよいのか分からないゴミ」に見える（実運用で指摘された）。
        置き場を分けておけば、データ側は記録だけになる。
        """
        return self.root / ".locks"

    def lock_path(self, target: Path) -> Path:
        """`target` を守るためのロックファイルのパス（データ側には作らない）。"""
        target = Path(target)
        try:
            name = target.relative_to(self.root).as_posix()
        except ValueError:
            name = target.name
        return self.locks_dir / f"{_SLUG_UNSAFE.sub('_', name)}.lck"

    def exists(self) -> bool:
        return self.root.is_dir()

    def ensure(self) -> "StatusStore":
        self.units_dir.mkdir(parents=True, exist_ok=True)
        self.done_dir.mkdir(parents=True, exist_ok=True)
        self.resources_dir.mkdir(parents=True, exist_ok=True)
        return self

    def _unit_paths(self, unit_id: str) -> tuple[Path, Path]:
        if not P.valid_unit_id(unit_id):
            raise ValueError(f"不正な unitId: {unit_id!r}")
        return self.units_dir / f"{unit_id}.json", self.units_dir / f"{unit_id}.ndjson"

    def _find_unit_paths(self, unit_id: str) -> tuple[Path, Path]:
        """進行中→完了済みの順に探す。"""
        active_json, active_ndjson = self._unit_paths(unit_id)
        if active_json.exists():
            return active_json, active_ndjson
        done_json = self.done_dir / f"{unit_id}.json"
        if done_json.exists():
            return done_json, self.done_dir / f"{unit_id}.ndjson"
        return active_json, active_ndjson

    # --- 単位 ---------------------------------------------------------
    def read_unit(self, unit_id: str) -> dict | None:
        return read_json(self._find_unit_paths(unit_id)[0])

    def write_unit(self, unit: dict) -> Path:
        unit_id = unit["unitId"]
        path = self._find_unit_paths(unit_id)[0]
        write_json_atomic(path, unit)
        return path

    def list_units(self, *, include_done: bool = True, done_limit: int = 50) -> list[dict]:
        units: list[dict] = []
        if self.units_dir.is_dir():
            for path in sorted(self.units_dir.glob("*.json")):
                unit = read_json(path)
                if unit:
                    units.append(unit)
        if include_done and self.done_dir.is_dir():
            done_paths = sorted(self.done_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in done_paths[:done_limit]:
                unit = read_json(path)
                if unit:
                    units.append(unit)
        return units

    def orphan_journals(self) -> list[str]:
        """スナップショットを持たないジャーナルの単位 ID。

        ツール側のエミッタは「NDJSON へ 1 行追記するだけ」でも良い（ダッシュボードを
        必須依存にしないため）。その場合スナップショットが無いので、集約側が補う。
        """
        if not self.units_dir.is_dir():
            return []
        found = []
        for path in sorted(self.units_dir.glob("*.ndjson")):
            unit_id = path.stem
            if not P.valid_unit_id(unit_id):
                continue
            if not (self.units_dir / f"{unit_id}.json").exists():
                found.append(unit_id)
        return found

    def archive_unit(self, unit_id: str) -> None:
        """終了した単位を units/done/ へ移す（集約の既定読み取り対象から外す）。

        ジャーナルは**移動でなく追記マージ**する。退避の途中に外部の道具が
        （追記専用なので）新しい行を書くことがあり、単純な置換だとその行を失う。
        """
        self.done_dir.mkdir(parents=True, exist_ok=True)
        src_json, src_ndjson = self._unit_paths(unit_id)
        # ジャーナルを先に片付ける（スナップショットを先に移すと、その隙に届いたエビデンスが
        # 退避先へ書かれ、後からの移動で消える）。
        # **まず原子的なリネームで切り離してから**マージする: 外部のエミッタはパスを開いて
        # 追記するので、切り離した後の追記は新しいファイルに入り、読み落としも消失も起きない
        # （読み手は進行中と退避先の両方のジャーナルを読む）
        dest = self.done_dir / src_ndjson.name
        detached: list[Path] = []
        # 前回の退避が途中で落ちた残骸も一緒に回収する（放置すると未マージ分が読まれなくなる）
        detached.extend(sorted(self.units_dir.glob(f"{src_ndjson.name}.archiving*")))
        if src_ndjson.exists():
            # 名前は毎回一意にする（pid だけだと、同じプロセスの再試行や pid 再利用で
            # 未回収の残骸を上書きしてしまい、マージ前のイベントが復元不能になる）
            candidate = src_ndjson.with_name(
                f"{src_ndjson.name}.archiving{os.getpid()}-{secrets.token_hex(4)}")
            try:
                os.replace(src_ndjson, candidate)
                detached.append(candidate)
            except OSError:
                pass
        for source in detached:
            with file_lock(self.lock_path(dest)):
                text = source.read_text(encoding="utf-8", errors="replace")
                if text and not text.endswith("\n"):
                    text += "\n"
                with dest.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
            source.unlink(missing_ok=True)
        if src_json.exists():
            os.replace(src_json, self.done_dir / src_json.name)

    # --- イベント -----------------------------------------------------
    def append_event(self, unit_id: str, kind: str, data: dict, producer: str, *, seq: int | None = None) -> dict:
        path = self._find_unit_paths(unit_id)[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        if seq is None:
            seq = self.count_events(unit_id) + 1
        event = {
            "schema": P.SCHEMA_EVENT,
            "at": P.now_iso(),
            "unitId": unit_id,
            "seq": seq,
            "producer": producer,
            "kind": kind,
            "data": data or {},
        }
        line = json.dumps(event, ensure_ascii=False)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
        return event

    def count_events(self, unit_id: str) -> int:
        return len(self.read_events(unit_id, limit=100000))

    def read_events(self, unit_id: str, *, limit: int = 200) -> list[dict]:
        # 退避後に外部のエミッタが書いた行は進行中側に新しく作られる。両方読んでから
        # 時刻順に畳むことで、退避と追記が同時に起きても行を落とさない
        if not P.valid_unit_id(unit_id):
            return []
        lines: list[str] = []
        # `.archiving*` は退避の途中で落ちた残骸。次の退避で回収されるまでの間も読めるようにする
        leftovers = sorted(self.units_dir.glob(f"{unit_id}.ndjson.archiving*"))
        for path in (self.done_dir / f"{unit_id}.ndjson", *leftovers,
                     self.units_dir / f"{unit_id}.ndjson"):
            if not path.exists():
                continue
            try:
                lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                continue
        if not lines:
            return []
        events, seen = [], set()
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            if line in seen:
                continue  # 退避のやり直し等で同じ行が二重に入っても 1 件として扱う
            seen.add(line)
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 追記途中の欠けた行は捨てる（読み手は止めない）
        # 追記マージで前後が入れ替わることがあるため、時刻で並べ直す（同時刻は元の行順）
        events.sort(key=lambda e: str(e.get("at") or ""))
        return events

    # --- 排他資源 -----------------------------------------------------
    def resource_path(self, resource_id: str) -> Path:
        return self.resources_dir / f"{slugify_resource(resource_id)}.json"

    def read_resource(self, resource_id: str) -> dict | None:
        return read_json(self.resource_path(resource_id))

    def list_resources(self) -> list[dict]:
        if not self.resources_dir.is_dir():
            return []
        found = []
        for path in sorted(self.resources_dir.glob("*.json")):
            record = read_json(path)
            if record:
                found.append(record)
        return found

    def _resource_lock(self, path: Path):
        return file_lock(self.lock_path(path))

    def acquire_resource(self, resource_id: str, holder: dict) -> tuple[bool, dict | None, str | None]:
        """(取得できたか, 現在の保持者, 自分のロックID) を返す。保持者のプロセスが死んでいれば奪う。

        判定と書き込みは **OS のファイルロックで囲んだ排他区間**の中で行う。
        ファイルの有無や付け替えだけで排他を作ると、必ずどこかに「判定と書き込みの隙」が残る。
        """
        if not P.valid_resource_id(resource_id):
            raise ValueError(f"不正な資源 ID: {resource_id!r}（接頭辞は {P.RESOURCE_PREFIXES}）")
        self.resources_dir.mkdir(parents=True, exist_ok=True)
        path = self.resource_path(resource_id)
        lock_id = secrets.token_hex(8)
        record = {
            "schema": P.SCHEMA_UNIT,
            "resource": resource_id,
            "holder": holder,
            "acquiredAt": P.now_iso(),
            "lockId": lock_id,
        }
        try:
            with self._resource_lock(path):
                current = read_json(path) if path.exists() else None
                if current is not None:
                    alive = alive_on_this_host(current.get("holder") or {})
                    if alive is not False:
                        # 生存中、または生存を判定できない（他ホスト・pid 未記録）＝奪ってはいけない
                        return False, current.get("holder"), None
                    previous = current.get("holder")
                    write_json_atomic(path, {**record, "stolenFrom": previous})
                    return True, previous, lock_id
                write_json_atomic(path, record)
                return True, None, lock_id
        except TimeoutError:
            return False, (read_json(path) or {}).get("holder"), None

    def release_resource(self, resource_id: str, holder: dict, *, force: bool = False,
                         lock_id: str | None = None) -> str:
        """保持者が自分のときだけ解放する（排他区間の中で判定し、他人のロックには触れない）。

        結果は次のいずれか。**「解放できなかった」を一括りにしない**（呼び手が
        「もう自分のものではない」と「今は触れない」を区別できないと、再試行が空回りする）:

        - `released`  … 自分のロックを解放した
        - `absent`    … 記録が無い（既に解放済み。実質的に目的は達成されている）
        - `not-owner` … 別の保持者のロック（自分のものではないので触らない）
        - `busy`      … 他プロセスが操作中（時間をおいて再試行すべき）
        """
        path = self.resource_path(resource_id)
        if not path.exists():
            return RELEASE_ABSENT
        try:
            with self._resource_lock(path):
                if not path.exists():
                    return RELEASE_ABSENT
                current = read_json(path)
                if not (force or current is None or self._is_own_lock(current, holder, lock_id)):
                    return RELEASE_NOT_OWNER
                path.unlink(missing_ok=True)
                return RELEASE_RELEASED
        except TimeoutError:
            return RELEASE_BUSY        # 別者が操作中。次の解放試行に任せる

    @staticmethod
    def _is_own_lock(record: dict, holder: dict, lock_id: str | None) -> bool:
        """自分のロックか。**lockId か unitId の一致だけ**を認める。

        「同じホストなら同じ」といった緩和を入れると、無関係な取得者のロックを解放できてしまう。
        作業単位に紐付けずに取得した場合は acquire が返す lockId を使う（`--lock-id`）。
        """
        if lock_id:
            return record.get("lockId") == lock_id
        held_by = (record.get("holder") or {}).get("unitId")
        return bool(held_by) and held_by == holder.get("unitId")


def default_owner(
    agent: str | None = None,
    session: str | None = None,
    unit_id: str | None = None,
    pid: int | None = None,
) -> dict:
    """所有者情報。

    pid は **CLI 自身の pid を書かない**。CLI は 1 コマンドごとに終了するため、
    それを書くと集約が即座に「crashed」と誤断定する。明示指定か環境変数
    UAPP_DASH_PID（エージェント本体の持続プロセス）がある場合だけ記録し、
    無ければ pid 無し＝生存判定不能（stalled 止まり）とする。
    """
    if pid is None:
        env_pid = os.environ.get("UAPP_DASH_PID")
        if env_pid and env_pid.isdigit():
            pid = int(env_pid)
    owner = {
        "agent": agent or os.environ.get("UAPP_DASH_AGENT") or "unknown",
        "host": hostname(),
    }
    if pid:
        owner["pid"] = int(pid)
    if session or os.environ.get("UAPP_DASH_SESSION"):
        owner["session"] = session or os.environ.get("UAPP_DASH_SESSION")
    if unit_id:
        owner["unitId"] = unit_id
    return owner

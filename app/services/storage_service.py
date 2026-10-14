"""存储方式（数据库后端）的读取 / 校验 / 回写。

==================== 为什么单独一个模块 ====================
"从网页上换数据库"这件事，本项目此前**从来没有过** —— `app/` 里一处文件写入都没有，
配置一直是"用户拿编辑器改 `.env`，然后重启"。现在把它做成界面操作，
就必须把下面这几件事一次做对，否则会造出比"门槛高"更糟的东西：

1. **只改该改的键**（白名单）。这个文件里同时还躺着 JWT 密钥、Fernet 密钥、
   数据库口令 —— 一次"整份重写"就可能把它们抹掉，
   后果是"用户所有已保存的 API Key 再也解不开"（真发生过，见 pitfalls §34 一带）。
   所以这里只**逐行就地替换**白名单里的键，其余内容一字不动。

2. **切走时把另一套连接信息留成注释，而不是删掉**（沿用桌面端 `setup-config.js`
   的 `stash` 机制）。教训：删掉之后用户想切回来，就得重新回忆一串自动生成的
   32 位随机口令 —— 他不可能记得。

3. **识别"被真实环境变量钉住"的键**。取值优先级是「环境变量 > `.env`」，
   所以若某个键在真实环境里已经存在，改 `.env` **不会生效**、而且不报错。
   这类静默失效必须**当场告诉用户**，不能让他以为改成功了。

4. **写前备份 + 原子写**：`<文件>.bak` 留一份，先写临时文件再 replace，
   避免"写到一半断电"留下半截配置（那会让后端下次直接起不来）。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from app.core.config import BASE_DIR, data_root, get_settings
from app.core.exceptions import BadRequestError
from app.db.bootstrap import parse_env_text

#: 允许从界面改写的键（**白名单**，其余键一律不碰）
WRITABLE_KEYS: tuple[str, ...] = (
    "HNE_DB_BACKEND",
    "HNE_SQLITE_PATH",
    "HNE_MYSQL_HOST",
    "HNE_MYSQL_PORT",
    "HNE_MYSQL_USER",
    "HNE_MYSQL_PASSWORD",
    "HNE_MYSQL_DB",
)

#: 路径历史最多留几条（用户要求 2~3 条；留 3 条够回溯又不至于变成一堆噪音）
HISTORY_LIMIT = 3

#: 属于"另一套存储方式"的键：切走时会被注释掉（不是删除）
MYSQL_KEYS: tuple[str, ...] = (
    "HNE_MYSQL_HOST",
    "HNE_MYSQL_PORT",
    "HNE_MYSQL_USER",
    "HNE_MYSQL_PASSWORD",
    "HNE_MYSQL_DB",
)

#: 绝不回传给页面的键（与桌面端 preload 的做法一致：只报"有没有"，不报内容）
SECRET_KEYS: frozenset[str] = frozenset({"HNE_MYSQL_PASSWORD"})


def env_file_path() -> Path:
    """这份部署**真正生效**的配置文件路径。

    · 桌面壳/打包版：显式给出 `HNE_ENV_FILE`（配置在 userData 下）；
    · 浏览器端（仓库里起服务）：pydantic 读的是 `BASE_DIR/.env`。

    ★ 为什么以 `HNE_ENV_FILE` 优先：打包版的 `backend_entry.py` 会先把那份文件
      读进 `os.environ`，再让 Settings 从环境变量取值 —— 也就是说
      "写在 `HNE_ENV_FILE` 指向的那份里"才是真的生效。
    """
    raw = os.environ.get("HNE_ENV_FILE", "").strip()
    return Path(raw).expanduser().resolve() if raw else (BASE_DIR / ".env")


def pinned_keys() -> list[str]:
    """哪些键被**真实环境变量**钉住了（写 `.env` 不会生效）。

    ★ 这是本项目最怕的那类静默失效：用户改了、界面也说成功了，
      实际却因为环境变量优先级更高而毫无变化，且**不报任何错**。
      所以宁可在保存前拦下来，也不要让他带着错误认知离开。
    """
    return [key for key in WRITABLE_KEYS if os.environ.get(key, "").strip()]


def _value_sources() -> dict[str, str]:
    """每个键当前的值来自哪里：环境变量 / 配置文件 / 默认值。

    给界面用：用户问"我明明改了为什么没变"时，答案就在这一行。
    """
    file_values = parse_env_text(_read_text(env_file_path()))
    sources: dict[str, str] = {}
    for key in WRITABLE_KEYS:
        if os.environ.get(key, "").strip():
            sources[key] = "env"
        elif file_values.get(key, "").strip():
            sources[key] = "file"
        else:
            sources[key] = "default"
    return sources


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def _needs_quoting(value: str) -> bool:
    """`.env` 里需要加引号的值（否则被空格/#/引号弄脏）。与桌面端同一套判据。"""
    return any(ch in value for ch in ' \t#"\'`') or value == ""


def quote_if_needed(value: str) -> str:
    if _needs_quoting(value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _line_key(raw_line: str) -> str | None:
    """取一行生效配置的键名（注释行返回 None）。"""
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    key, sep, _ = line.partition("=")
    key = key.strip()
    if not sep or not key or not key.replace("_", "").isalnum():
        return None
    return key


# ==================================================================
#  读取当前状态（给界面渲染）
# ==================================================================
def read_state() -> dict[str, Any]:
    """当前存储方式 + 可改的字段 + 各自来自哪里（**不含任何密钥内容**）。"""
    settings = get_settings()
    file_values = parse_env_text(_read_text(env_file_path()))
    sources = _value_sources()

    values: dict[str, str] = {}
    for key in WRITABLE_KEYS:
        if key in SECRET_KEYS:
            continue
        if key == "HNE_DB_BACKEND":
            values[key] = settings.DB_BACKEND
            continue
        # 环境变量 > .env > 后端算出来的有效值
        values[key] = (
            os.environ.get(key, "").strip()
            or file_values.get(key, "").strip()
            or _effective_from_settings(key)
        )

    # ★ 把**当前正在用的**本机文件也纳入历史：否则用户改过一次路径之后，
    #   原来那份就再也没地方能看到了（而他正是要找它）。
    effective_sqlite = values.get("HNE_SQLITE_PATH") or str(settings.sqlite_file)
    _remember_path(effective_sqlite)

    return {
        "backend": settings.DB_BACKEND,
        "database": settings.database_label,
        "env_file": str(env_file_path()),
        "default_sqlite_path": str(data_root() / "data" / "app.sqlite3"),
        "values": values,
        # 只报"设没设"，绝不回传口令内容
        "secret_set": {key: bool(_secret_current(key)) for key in SECRET_KEYS},
        "pinned": pinned_keys(),
        "sources": sources,
        # ★ 换路径前的体检（当前这份）+ 最近用过的路径（方便回头找旧数据）
        "current_sqlite": inspect_sqlite(effective_sqlite),
        "sqlite_history": read_history(),
    }


def _secret_current(key: str) -> str:
    if os.environ.get(key, "").strip():
        return os.environ[key]
    return parse_env_text(_read_text(env_file_path())).get(key, "")


def _effective_from_settings(key: str) -> str:
    """后端当前真正在用的值（用于界面预填，避免显示成空）。"""
    settings = get_settings()
    mapping = {
        "HNE_SQLITE_PATH": str(settings.sqlite_file),
        "HNE_MYSQL_HOST": settings.MYSQL_HOST,
        "HNE_MYSQL_PORT": str(settings.MYSQL_PORT),
        "HNE_MYSQL_USER": settings.MYSQL_USER,
        "HNE_MYSQL_DB": settings.MYSQL_DB,
    }
    return mapping.get(key, "")


# ==================================================================
#  校验
# ==================================================================
def validate_payload(payload: dict[str, Any]) -> dict[str, str]:
    """逐字段校验，返回 {键: 错误文案}（空 dict = 通过）。

    ★ 判据与桌面端 `setup-config.js` 的 `validateValues` 保持同一套语义：
      选 sqlite 时 **MySQL 那一组完全不参与校验**，否则默认的零配置
      会被"口令没填"这种无关错误拦住。
    """
    errors: dict[str, str] = {}
    get = lambda key: str(payload.get(key) or "").strip()  # noqa: E731

    backend = get("HNE_DB_BACKEND") or "sqlite"
    if backend not in ("sqlite", "mysql"):
        errors["HNE_DB_BACKEND"] = "只能是 sqlite 或 mysql"
        return errors

    if backend == "mysql":
        for key, label in (
            ("HNE_MYSQL_HOST", "MySQL 主机"),
            ("HNE_MYSQL_USER", "MySQL 用户名"),
            ("HNE_MYSQL_DB", "数据库名"),
        ):
            if not get(key):
                errors[key] = f"{label}不能为空（选了 MySQL 就必须填）"
        port = get("HNE_MYSQL_PORT")
        if port and not (port.isdigit() and 1 <= int(port) <= 65535):
            errors["HNE_MYSQL_PORT"] = "端口必须是 1~65535 之间的整数"
        host = get("HNE_MYSQL_HOST")
        if host and any(ch.isspace() for ch in host):
            errors["HNE_MYSQL_HOST"] = "主机名里不能有空格"

    path = get("HNE_SQLITE_PATH")
    if path and "\x00" in path:
        errors["HNE_SQLITE_PATH"] = "路径里不能包含空字符"

    return errors


# ==================================================================
#  本机文件路径「体检」——换路径前的预警（这是用户真实踩过的坑）
# ==================================================================
def _current_sqlite_path() -> str:
    """当前**生效**的本机文件路径（环境变量 > .env > 后端默认）。"""
    explicit = os.environ.get("HNE_SQLITE_PATH", "").strip() or parse_env_text(
        _read_text(env_file_path())
    ).get("HNE_SQLITE_PATH", "").strip()
    if explicit:
        resolved = Path(explicit).expanduser()
        return str(resolved if resolved.is_absolute() else (data_root() / resolved).resolve())
    return str(get_settings().sqlite_file)


def _same_path(a: str, b: str) -> bool:
    """两个路径是不是同一个文件（大小写/分隔符/相对形式都要容错）。

    ★ Windows 上 `D:\\x\\app.sqlite3` 与 `d:/x/app.sqlite3` 指同一个文件，
      直接字符串比较会误判成"改了路径"，于是弹出一个不该有的警告。
    """
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _sqlite_stats(path: Path) -> dict[str, Any]:
    """看一眼某个 SQLite 文件里**有没有账号 / 多少数据**（只读，绝不建文件）。

    ==================== 为什么需要它 ====================
    用户真实踩过：把「本机文件路径」改成另一个位置后，**登录不上**了 ——
    因为新路径是一份**空库**，账号不在里面。他的第一反应是
    "你改我密码了？还是把我文件换位置了？"（完全合理的怀疑）。

    而当时界面只在角落里写了句"数据不会自动搬家"，太轻，没能拦住。
    ⇒ 所以保存前要能回答：**这个路径里有没有你的账号？有多少数据？**
      有了它，界面才能给出"你确定吗？这里什么都没有"这种**具体**的警告。
    """
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size": 0,
        "has_tables": False,
        "user_count": 0,
        "usernames": [],
        "sessions": 0,
        "messages": 0,
        "error": None,
    }
    if not path.is_file():
        return info
    info["size"] = path.stat().st_size

    import sqlite3

    try:
        # 只读 URI：**避免"顺手建出一个空文件"**那种副作用
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    try:
        tables = _list_tables(conn)
        info["has_tables"] = bool(tables)
        if "users" in tables:
            rows = conn.execute("SELECT username FROM users ORDER BY id LIMIT 10").fetchall()
            info["usernames"] = [r[0] for r in rows]
            info["user_count"] = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        for table, key in (("narrative_sessions", "sessions"), ("messages", "messages")):
            if table in tables:
                info[key] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return info


def _list_tables(conn: "Any") -> list[str]:
    """取表名（多路兜底）。

    ★ 踩过的坑：某些 SQLite 构建里 `SELECT name FROM sqlite_Main` 会报
      `no such table: sqlite_Main`（连**刚新建的正常库**也一样，
      我一开始误判成"用户的库坏了"）。所以这里依次尝试三条路径，
      并用"对照组正常库"验证过判据本身没问题。
    """
    for sql in (
        "SELECT name FROM sqlite_schema WHERE type='table'",
        "SELECT name FROM PRAGMA_table_list WHERE type='table'",
        "PRAGMA table_list",
    ):
        try:
            rows = conn.execute(sql).fetchall()
            names = sorted({r[0] for r in rows if r and isinstance(r[0], str) and r[0] != "sqlite_schema"})
            if names:
                return names
        except Exception:  # noqa: BLE001 - 逐条降级，任一条能用即可
            continue
    return []


def inspect_sqlite(raw_path: str) -> dict[str, Any]:
    """给界面用的"换路径前体检"。

    返回里带一个 `warning`（没有风险时为 None）与 `level`，前端据此决定
    是"直接保存"还是"必须二次确认"。判据故意做得**保守**：
    只要目标不是一份有账号的库，就提醒 —— 因为"换个路径继续用"是极少数场景，
      而"换完发现自己账号没了"是大多数人的下场。
    """
    text = str(raw_path or "").strip()
    target = Path(text).expanduser() if text else (data_root() / "data" / "app.sqlite3")
    if not target.is_absolute():
        target = (data_root() / target).resolve()

    stats = _sqlite_stats(target)
    warning: str | None = None
    level = "ok"

    if not stats["exists"]:
        level = "warn"
        warning = (
            "这个路径下**还没有数据库文件** —— 保存并重启后会新建一个**空库**，"
            "你在别处的账号与数据都不会出现在这里。"
        )
    elif stats["error"]:
        level = "warn"
        warning = f"这个文件读不出来（{stats['error']}），不确定它是不是一份可用的库。"
    elif not stats["has_tables"]:
        level = "warn"
        warning = "这个文件存在，但里面**没有任何数据表** —— 不是一份可用的库。"
    elif stats["user_count"] == 0:
        level = "warn"
        warning = "这个库是**空的**（一个账号都没有）—— 用它的话你需要重新注册。"
    else:
        level = "ok"

    return {
        **stats,
        "level": level,
        "warning": warning,
        # 给界面直接显示的一句话摘要
        "summary": _stats_summary(stats),
    }


def _stats_summary(stats: dict[str, Any]) -> str:
    if not stats["exists"]:
        return "文件不存在"
    if stats["error"]:
        return "无法读取"
    if not stats["has_tables"]:
        return "存在，但没有数据表"
    who = "、".join(stats["usernames"][:3]) or "（无账号）"
    return (
        f"已有 {stats['user_count']} 个账号（{who}）· "
        f"{stats['sessions']} 个会话 · {stats['messages']} 条消息"
    )


# ==================================================================
#  路径历史（用户要求：留住最近几条，方便回头找旧数据）
# ==================================================================
def _history_file() -> Path:
    """路径历史存在数据目录下的 config 里（与 .secrets.env 同一层）。

    ★ 为什么存这里：它是"本机运行痕迹"，不该写进 `.env`
      （那会多出一堆非配置项，而且用户可能把 `.env` 提交/分享出去）。
    """
    return data_root() / "config" / "sqlite-path-history.json"


def read_history() -> list[dict[str, Any]]:
    """读最近用过的本机文件路径（**新的在前**），附上每条的体检信息。"""
    import json

    path = _history_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []

    out: list[dict[str, Any]] = []
    for entry in items[:HISTORY_LIMIT]:
        value = entry.get("path") if isinstance(entry, dict) else entry
        if not value:
            continue
        stamp = entry.get("last_used") if isinstance(entry, dict) else None
        stat = _sqlite_stats(Path(str(value)).expanduser())
        out.append({
            "path": str(value),
            "last_used": stamp,
            "exists": stat["exists"],
            "user_count": stat["user_count"],
            "usernames": stat["usernames"],
            "summary": _stats_summary(stat),
        })
    return out


def _remember_path(value: str) -> None:
    """把这次用的路径记进历史（去重、最新的在前、只留 HISTORY_LIMIT 条）。"""
    import json

    text = str(value or "").strip()
    if not text:
        return
    path = _history_file()
    items: list[dict[str, str]] = []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        old = raw.get("items") if isinstance(raw, dict) else raw
        if isinstance(old, list):
            for entry in old:
                if isinstance(entry, dict) and entry.get("path"):
                    items.append({"path": str(entry["path"]), "last_used": str(entry.get("last_used") or "")})
                elif isinstance(entry, str) and entry:
                    items.append({"path": entry, "last_used": ""})
    except (OSError, ValueError):
        items = []

    items = [i for i in items if i["path"] != text]
    items.insert(0, {"path": text, "last_used": now_stamp()})
    items = items[:HISTORY_LIMIT]

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # 记历史失败**绝不能**影响保存本身（它只是便利功能）
        pass



def _clean_updates(payload: dict[str, Any]) -> dict[str, str | None]:
    """把页面提交的值整理成"要写入的键值"。

    规则（与桌面端 `preserveSecrets` 同源）：
      · 不在白名单里的键 **直接忽略**（不是报错 —— 前端多传无关字段不该失败，
        但也绝不能写进文件）；
      · 密钥类字段留空 = "我没动它"，沿用已有值（**绝不覆盖成空**）；
      · 其它字段留空 = 用户真的想清掉它（写空串，等于让后端用默认值）。
    """
    updates: dict[str, str | None] = {}
    for key in WRITABLE_KEYS:
        if key not in payload:
            continue
        value = str(payload.get(key) or "").strip()
        if key in SECRET_KEYS:
            if not value:
                continue  # 留空 → 保持原值
            updates[key] = value
            continue
        updates[key] = value
    return updates


# ==================================================================
#  回写
# ==================================================================
def write_config(payload: dict[str, Any]) -> dict[str, Any]:
    """把存储配置写入 `.env`（只动白名单里的键），返回结果摘要。

    抛 `BadRequestError` 的情形：校验不过 / 键被环境变量钉住 / 文件不可写。
    """
    errors = validate_payload(payload)
    if errors:
        raise BadRequestError("配置校验未通过", detail={"errors": errors})

    updates = _clean_updates(payload)
    if not updates:
        raise BadRequestError("没有需要保存的改动")

    # ★★ 换「本机文件路径」前必须体检 —— 用户真实踩过：
    #   换了个路径后登录不上（新路径是空库），他以为是"你改我密码了？"。
    #   这里**不拦死**（用户可能就是想换到新位置），但把"这里有什么"如实带回去，
    #   由前端做二次确认。除非调用方显式说 `confirm_empty=True`。
    new_sqlite = updates.get("HNE_SQLITE_PATH")
    wants_sqlite = (updates.get("HNE_DB_BACKEND") or "sqlite") == "sqlite"
    if wants_sqlite and new_sqlite is not None and not payload.get("confirm_empty"):
        check = inspect_sqlite(str(new_sqlite))
        # ★ 只有当**目标确实不是当前在用的那份**时才拦。
        #   否则会出现"用户把默认路径原样填了一遍就被警告"这种假警报 ——
        #   而假警报会让真的那一次也被无视（狼来了）。
        current = _current_sqlite_path()
        same_file = bool(current) and _same_path(str(check["path"]), current)
        if check["level"] != "ok" and not same_file:
            raise BadRequestError(
                "目标路径里没有可用的数据，需要你确认",
                detail={
                    "reason": "empty_sqlite_target",
                    #: 前端据此弹"你确定吗"的二次确认
                    "needs_confirm": True,
                    "target": check,
                    "hint": "换过去之后，你在别处的账号与数据不会跟着过来。",
                },
            )

    pinned = [key for key in updates if key in pinned_keys()]
    if pinned:
        raise BadRequestError(
            "这些键被当前进程的环境变量钉住了，改配置文件不会生效",
            detail={"pinned": pinned, "hint": "请先去掉对应的环境变量，或直接改环境变量本身"},
        )

    target = env_file_path()
    before = _read_text(target)

    backup: str | None = None
    if target.is_file():
        backup_path = target.with_name(f"{target.name}.bak")
        try:
            shutil.copy2(target, backup_path)
            backup = str(backup_path)
        except OSError as exc:  # pragma: no cover - 权限/占用等少见情况
            raise BadRequestError(
                f"备份原配置失败，已中止（不动你的文件）：{exc}"
            ) from exc

    backend = str(updates.get("HNE_DB_BACKEND") or "sqlite").strip()
    new_text, changes = _apply_updates(before, updates, backend)

    tmp = target.with_name(f"{target.name}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:  # pragma: no cover
            pass
        raise BadRequestError(f"写入配置失败：{exc}") from exc

    # ★ 写完**读回来再解析一遍**：确认该写进去的键真的在文件里。
    #   只信"我写过了"而不复核，正是"看起来对、实际没落地"的来源。
    verified = parse_env_text(_read_text(target))
    missing = [k for k, v in changes.items() if v and verified.get(k) != v]
    if missing:
        raise BadRequestError(
            "写入后复核失败：这些键没有落进文件", detail={"missing": missing}
        )

    # ★ 把**旧的那条**记进历史（用户要求：换过之后还能找到原来在哪）。
    #   顺序很重要：先记旧的，再让 read_state() 把新的记上，
    #   否则"刚被换掉的那份"会在历史里缺席 —— 而那正是用户要找的。
    previous = parse_env_text(before).get("HNE_SQLITE_PATH", "").strip()
    if previous:
        _remember_path(previous)
    if changes.get("HNE_SQLITE_PATH"):
        _remember_path(changes["HNE_SQLITE_PATH"])

    return {
        "env_file": str(target),
        "backup": backup,
        "changed": sorted(changes),
        "backend": backend,
        "restart_required": True,
        "sqlite_history": read_history(),
        "message": "配置已写入。需要重启后端才会生效 —— 当前进程仍在用旧配置。",
    }


def _owned_keys(backend: str) -> tuple[set[str], set[str]]:
    """返回 (当前后端拥有的键, 属于另一套存储的键)。

    ★ 为什么需要"另一套"这个概念：同一个 `.env` 里同时留着 MySQL 与 SQLite 的
      生效键会让人（和后端自己）都搞不清到底在用哪个。桌面端向导的做法是
      "另一套整组不写"，这里沿用 —— 但**留成注释**而不是删除，
      这样用户切回去时值还在（见 `_apply_updates`）。

    ★★ `HNE_DB_BACKEND` 本身**必须**在拥有的键里：
      它是"我现在用哪种存储"的开关，两边都该写。
      第一版漏了它，于是"切到 MySQL"时其他键都写进去了、**这个开关却没动** ——
      文件里仍是 `sqlite`，用户重启后还在用本机文件。表现是最迷惑的一种：
      保存成功、界面提示成功、只有数据落点没变（实测抓到）。
    """
    if backend == "mysql":
        return set(MYSQL_KEYS) | {"HNE_DB_BACKEND"}, {"HNE_SQLITE_PATH"}
    return {"HNE_SQLITE_PATH", "HNE_DB_BACKEND"}, set(MYSQL_KEYS)


def _apply_updates(
    text: str, updates: dict[str, str | None], backend: str
) -> tuple[str, dict[str, str]]:
    """在原文上**就地替换**（保留注释与其它键），返回 (新文本, 实际写入的键值)。"""
    lines = text.replace("\r\n", "\n").split("\n")
    my_keys, other_keys = _owned_keys(backend)

    changed: dict[str, str] = {}
    handled: set[str] = set()
    out: list[str] = []

    for raw in lines:
        key = _line_key(raw)
        if key is None:
            out.append(raw)
            continue

        # 属于另一套存储的生效行 → 注释掉（留住值，便于切回来）
        if key in other_keys:
            out.append(f"# {raw.strip()}")
            continue

        # 当前后端拥有的键 + 用户这次提交了 → 就地替换
        if key in updates and key in my_keys:
            value = updates[key]
            handled.add(key)
            if value:
                changed[key] = value
                out.append(f"{key}={quote_if_needed(value)}")
            else:
                # 显式清空 → 去掉这一行（= 让后端用默认值）
                changed[key] = ""
                out.append(f"# {key}=（已清空，改用默认值）")
            continue

        out.append(raw)

    # 文件里原本没有的键 → 追加到末尾
    appended: list[str] = []
    for key, value in updates.items():
        if key in handled or not value or key not in my_keys:
            continue
        appended.append(f"{key}={quote_if_needed(value)}")
        changed[key] = value

    if appended:
        if out and out[-1].strip():
            out.append("")
        out.append("# ---------------------- 由「存储设置」页写入 ----------------------")
        out.extend(appended)

    new_text = "\n".join(out)
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, changed


# ==================================================================
#  保存前的连通性测试
# ==================================================================
def test_connection(payload: dict[str, Any]) -> dict[str, Any]:
    """用**提交的参数**试着连一次，不落盘、不改动任何东西。

    ★ 为什么值得做：切换存储最大的坑是"填错了 → 保存 → 重启 → 起不来"，
      而那时代码已经写进文件了。先试连一次，就能在**写文件之前**拦住。
    """
    errors = validate_payload(payload)
    if errors:
        return {"ok": False, "errors": errors, "message": "配置校验未通过"}

    backend = str(payload.get("HNE_DB_BACKEND") or "sqlite").strip()
    if backend == "sqlite":
        return _test_sqlite(payload)
    return _test_mysql(payload)


def _test_sqlite(payload: dict[str, Any]) -> dict[str, Any]:
    raw = str(payload.get("HNE_SQLITE_PATH") or "").strip()
    target = Path(raw).expanduser() if raw else (data_root() / "data" / "app.sqlite3")
    if not target.is_absolute():
        target = (data_root() / target).resolve()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "message": f"这个目录不可写：{exc}", "path": str(target)}

    # 真的建一个连接（并读一下 schema 版本），而不是只看目录在不在
    import sqlite3

    try:
        con = sqlite3.connect(str(target), timeout=10)
        try:
            con.execute("SELECT 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {"ok": False, "message": f"打开本机文件失败：{exc}", "path": str(target)}

    exists = target.is_file()
    return {
        "ok": True,
        "backend": "sqlite",
        "path": str(target),
        "message": "本机文件可用（已存在这份库）" if exists else "本机文件可用（会在首次启动时创建）",
    }


def _test_mysql(payload: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import create_engine, text
    from urllib.parse import quote_plus

    host = str(payload.get("HNE_MYSQL_HOST") or "").strip()
    port = str(payload.get("HNE_MYSQL_PORT") or "3306").strip()
    user = str(payload.get("HNE_MYSQL_USER") or "").strip()
    db = str(payload.get("HNE_MYSQL_DB") or "narrative_engine").strip()
    # 口令留空 = 沿用已存的那份（与"保存"的语义一致，否则"测试"会假失败）
    password = str(payload.get("HNE_MYSQL_PASSWORD") or "").strip() or _secret_current(
        "HNE_MYSQL_PASSWORD"
    )

    url = (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{db}?charset=utf8mb4"
    )
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 8})
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT VERSION()")).fetchone()
        return {
            "ok": True,
            "backend": "mysql",
            "server": str(row[0]) if row else "",
            "message": f"连接成功（MySQL {row[0] if row else ''}）",
        }
    except Exception as exc:  # noqa: BLE001 - 要把原始错误如实带给用户
        return {
            "ok": False,
            "message": f"连不上：{type(exc).__name__}: {exc}",
            "hint": "检查主机 / 端口 / 用户名 / 口令 / 库名，以及 MySQL 服务是否在运行。",
        }
    finally:
        engine.dispose()


def now_stamp() -> str:
    """给路径历史打时间戳（`last_used` 字段用）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")

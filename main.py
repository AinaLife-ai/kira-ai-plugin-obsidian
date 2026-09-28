"""Obsidian 笔记工具插件（KiraAI 版）。

移植自 nori-core 的 nori_plugin_obsidian：经 Obsidian 社区插件
Local REST API（coddingtonbear）操作笔记库（vault），注册七个工具：
1. note_list    列出目录下笔记与文件夹（分页）
2. note_read    读笔记（content/metadata/structure 三模式）
3. note_write   全量写笔记（覆盖，不存在则创建）
4. note_append  追加到笔记末尾（不存在则创建）
5. note_patch   局部修改（标题/块引用/frontmatter 字段定位）
6. note_delete  删除笔记（不可逆）
7. note_search  搜索笔记（simple/dataview/jsonlogic）

frontmatter 解析、markdown 结构解析、搜索打分均由 Obsidian 侧完成，
本地零解析。工具返回值一律 str（喂给 LLM 继续推理）；失败也返回错误
描述文本而非抛异常，利于 LLM 自行调整参数重试。

前置条件：Obsidian 安装并启用 Local REST API 插件且保持运行，插件
配置填 base_url + api_key。base_url 未配置时工具降级为返回配置提示，
不影响主程序。

安全模型：仅与显式配置的 base_url 通信（Bearer 认证）；note_delete
不可逆，靠工具描述约束 LLM 删除前先向对方确认。
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any

from core.plugin import BasePlugin, logger, register
from core.chat import MessageChain
from core.chat.message_elements import Text

from .client import ObsidianClient

_ACCEPT_METADATA = "application/vnd.olrapi.note+json"
_ACCEPT_STRUCTURE = "application/vnd.olrapi.document-map+json"

# ---------------------------------------------------------------------------
# Study schedule extension (v1.1.0): Gantt Calendar compatible task helpers
# ---------------------------------------------------------------------------

_PRIO_EMOJI = {
    "highest": "🔺",
    "high": "⏫",
    "medium": "🔼",
    "low": "🔽",
    "lowest": "⏬",
}
_PRIO_NAMES = {v: k for k, v in _PRIO_EMOJI.items()}

_TASK_LINE_RE = re.compile(
    r"^- \[([ xX])\] (.+?)(?: (🔺|⏫|🔼|🔽|⏬))?"
    r"(?: .*?🛫 (\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?))?"
    r"(?: .*?📅 (\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?))?"
    r"(?: .*?✅ (\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?))?$"
)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _is_date_only(s: str) -> bool:
    return bool(s) and len(s.strip()) == 10


def _is_overdue(deadline: str | None, now: datetime) -> bool:
    dt = _parse_dt(deadline)
    if dt is None:
        return False
    if _is_date_only(deadline):
        return dt.date() < now.date()
    return dt < now


def _shift_date_str(s: str | None, days: int) -> str | None:
    dt = _parse_dt(s)
    if dt is None:
        return s
    nd = dt + timedelta(days=days)
    if _is_date_only(s):
        return nd.strftime("%Y-%m-%d")
    return nd.strftime("%Y-%m-%d %H:%M")


def _parse_task_line(line: str) -> dict | None:
    m = _TASK_LINE_RE.match(line.strip())
    if not m:
        return None
    done, desc, prio, start, due, comp = m.groups()
    return {
        "done": done.lower() == "x",
        "desc": desc.strip(),
        "prio": _PRIO_NAMES.get(prio, "normal") if prio else "normal",
        "start": start,
        "due": due,
        "comp": comp,
    }


def _make_task_line(desc: str, start: str, due: str, prio: str = "normal") -> str:
    parts = [f"- [ ] {desc.strip()}"]
    if prio and prio != "normal":
        parts.append(_PRIO_EMOJI.get(prio, ""))
    if start:
        parts.append(f"🛫 {start}")
    if due:
        parts.append(f"📅 {due}")
    return " ".join(p for p in parts if p)

_NOT_CONFIGURED = (
    "错误：Obsidian 笔记工具未配置。请在本插件配置中填写 base_url"
    "（Obsidian Local REST API 地址）和 api_key。"
)


def _as_error(result: Any) -> str | None:
    """client 返回 {"error": ...} 时转为带处置建议的错误文本。

    Returns:
        错误文本；result 非错误 dict 时返回 None（正常结果）。
    """
    if not (isinstance(result, dict) and isinstance(result.get("error"), str)):
        return None
    err = str(result["error"])
    if err.startswith("connection:") or err.startswith("timeout"):
        return (
            f"错误：Obsidian Local REST API 不可达或超时（{err}）。"
            "请确认 Obsidian 正在运行、Local REST API 插件已启用、"
            "base_url 配置正确。"
        )
    if err.startswith("HTTP 401"):
        return (
            f"错误：认证失败（{err}）。请检查插件配置的 api_key "
            "是否与 Local REST API 插件设置页一致。"
        )
    return f"错误：{err}"


def _truncate(text: str, limit: int) -> str:
    """超出上限截断并尾部标注。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[输出已截断]"


def _as_int(value: Any, default: int) -> int:
    """配置项安全转 int；None/空串/非法值落回默认（单字段容错，不拖垮整插件）。"""
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    """配置项安全转 float；None/空串/非法值落回默认（单字段容错）。"""
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _take_path_content(args: dict[str, Any]) -> tuple[str | None, str]:
    """提取并校验 path/content 公共参数。

    Returns:
        (path, content)；path 为 None 时 content 是错误文本（调用方直接返回）。
    """
    path = str(args.get("path", "") or "").strip().strip("/")
    if not path:
        return None, "错误：缺少 path 参数"
    content = args.get("content")
    if not isinstance(content, str):
        return None, "错误：缺少 content 参数（必须为字符串）"
    return path, content


class ObsidianPlugin(BasePlugin):
    """Obsidian 笔记工具插件。"""

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self._client: ObsidianClient | None = None
        self._max_output = 8000
        self._max_results = 8
        self._summary_len = 150
        # --- study schedule extension (v1.1.0) ---
        self.plan_file = str(self.plugin_cfg.get("plan_file", "") or "").strip()
        self.push_sid = str(self.plugin_cfg.get("push_sid", "") or "").strip()
        self.reminder_enabled = bool(self.plugin_cfg.get("reminder_enabled", False))
        self.remind_before_minutes = max(
            int(self.plugin_cfg.get("remind_before_minutes", 10) or 10), 0
        )
        self.report_time = str(self.plugin_cfg.get("report_time", "") or "").strip()
        self._study_task: asyncio.Task | None = None
        self._reminded: set[str] = set()
        self._reported_date: str = ""
        self._state_path = None

    async def initialize(self):
        """按插件配置构造客户端（支持热重载重入；失败降级为未配置）。"""
        try:
            base_url = str(self.plugin_cfg.get("base_url", "") or "").strip().rstrip("/")
            if not base_url:
                logger.info("Obsidian: 未配置 base_url（Local REST API 地址），笔记工具不可用")
                self._client = None
                return
            api_key = str(self.plugin_cfg.get("api_key", "") or "")
            if not api_key:
                logger.warning("Obsidian: 未配置 api_key，Local REST API 认证会失败（HTTP 401）")
            self._client = ObsidianClient(
                base_url,
                api_key,
                timeout=_as_float(self.plugin_cfg.get("timeout_seconds"), 15.0),
            )
            self._max_output = _as_int(self.plugin_cfg.get("max_output_chars"), 8000)
            self._max_results = _as_int(self.plugin_cfg.get("max_results"), 8)
            self._summary_len = _as_int(self.plugin_cfg.get("summary_max_length"), 150)
            logger.info("Obsidian: 笔记工具已就绪（base_url=%s，共 7 个工具）", base_url)
            if self.reminder_enabled and self.plan_file:
                self._state_path = Path(self.ctx.get_plugin_data_dir()) / "study_state.json"
                self._load_study_state()
                if self._study_task is None or self._study_task.done():
                    self._study_task = asyncio.create_task(self._study_loop())
                    logger.info("Obsidian: 学习监督定时任务已启动 (plan=%s)", self.plan_file)
        except Exception:
            logger.exception("Obsidian: 配置解析失败，笔记工具降级为不可用")
            self._client = None

    async def terminate(self):
        if self._study_task and not self._study_task.done():
            self._study_task.cancel()
            try:
                await self._study_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Obsidian: 学习监督任务退出异常")
        self._study_task = None
        self._client = None

    def _require_client(self) -> ObsidianClient | None:
        return self._client

    @register.tool(
        "note_list",
        "列出 Obsidian 笔记库指定目录下的笔记与文件夹（分页）。"
        "仅用于浏览 Obsidian 笔记库（vault），不是本地文件系统。"
        "需要浏览笔记目录结构时使用；查内容优先 note_search。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径（如 '01-architecture'；空=根目录）",
                },
                "page": {"type": "integer", "description": "页码（1 起，缺省 1）"},
                "page_size": {"type": "integer", "description": "每页条数（1-100，缺省 30）"},
            },
        },
    )
    async def note_list(self, event, *_, path: str = "", page: int = 1,
                        page_size: int = 30, **_kwargs) -> str:
        if self._client is None:
            return _NOT_CONFIGURED
        path = str(path or "").strip().strip("/")
        page = max(int(page or 1), 1)
        page_size = max(min(int(page_size or 30), 100), 1)
        api_path = f"/vault/{path}/" if path else "/vault/"
        result = await self._client.request("GET", api_path)
        err = _as_error(result)
        if err:
            return err
        files = result.get("files", []) if isinstance(result, dict) else result
        if not isinstance(files, list):
            return f"[{path or '/'}] 返回结构异常: {_truncate(str(files), 500)}"
        total = len(files)
        start = (page - 1) * page_size
        page_files = files[start : start + page_size]
        total_pages = max((total + page_size - 1) // page_size, 1)
        lines = [
            f"[{path or '/'} | 共 {total} 项，第 {page}/{total_pages} 页（每页 {page_size}）]"
        ]
        for item in page_files:
            if isinstance(item, dict):
                marker = "[目录]" if item.get("type") == "folder" else "[笔记]"
                lines.append(f"{marker} {item.get('path', item.get('title', '?'))}")
            else:
                lines.append(f"[笔记] {item}")
        if total > page * page_size:
            lines.append(f"（还有下一页，page={page + 1}）")
        return _truncate("\n".join(lines), self._max_output)

    @register.tool(
        "note_read",
        "读取 Obsidian 笔记库中的笔记。content（默认）返回 Markdown "
        "正文；metadata 返回含 frontmatter 的元数据 JSON；structure "
        "返回文档大纲 JSON。仅用于读 Obsidian 笔记，不用于本地文件。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "笔记路径，如 '01-architecture/design.md'",
                },
                "mode": {
                    "type": "string",
                    "description": "读取模式：content（默认）/ metadata / structure",
                },
            },
            "required": ["path"],
        },
    )
    async def note_read(self, event, *_, path: str = "", mode: str = "content",
                        **_kwargs) -> str:
        if self._client is None:
            return _NOT_CONFIGURED
        path = str(path or "").strip().strip("/")
        if not path:
            return "错误：缺少 path 参数"
        mode = str(mode or "content").strip()
        if mode not in ("content", "metadata", "structure"):
            return f"错误：不支持的 mode: {mode}（可选 content/metadata/structure）"
        accept = None
        if mode == "metadata":
            accept = _ACCEPT_METADATA
        elif mode == "structure":
            accept = _ACCEPT_STRUCTURE
        result = await self._client.request("GET", f"/vault/{path}", accept=accept)
        err = _as_error(result)
        if err:
            return err
        if mode == "content":
            content = (
                result if isinstance(result, str)
                else json.dumps(result, ensure_ascii=False)
            )
            return _truncate(f"[{path} | {len(content)} 字符]\n{content}", self._max_output)
        body = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return _truncate(f"[{path} | {mode}]\n{body}", self._max_output)

    @register.tool(
        "note_write",
        "创建或替换 Obsidian 笔记（会覆盖整篇内容！）。适合新写笔记"
        "或完全重写；已有笔记的局部修改优先 note_patch，日常记录"
        "追加用 note_append。仅用于写 Obsidian 笔记，不用于本地文件。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "笔记路径，如 'reports/test.md'（不存在则创建）",
                },
                "content": {"type": "string", "description": "完整笔记内容（Markdown）"},
            },
            "required": ["path", "content"],
        },
    )
    async def note_write(self, event, *_, path: str = "",
                         content: str | None = None, **_kwargs) -> str:
        if self._client is None:
            return _NOT_CONFIGURED
        path, content = _take_path_content({"path": path, "content": content})
        if path is None:
            return content
        result = await self._client.request("PUT", f"/vault/{path}", body=content)
        err = _as_error(result)
        if err:
            return err
        return f"已写入 {path}（{len(content)} 字符，全量覆盖）"

    @register.tool(
        "note_append",
        "追加内容到 Obsidian 笔记末尾（笔记不存在则自动创建）。"
        "适合日记、记录类追加，不动已有内容。仅用于 Obsidian 笔记。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "笔记路径，如 'reports/log.md'",
                },
                "content": {"type": "string", "description": "要追加的内容（Markdown）"},
            },
            "required": ["path", "content"],
        },
    )
    async def note_append(self, event, *_, path: str = "",
                          content: str | None = None, **_kwargs) -> str:
        if self._client is None:
            return _NOT_CONFIGURED
        path, content = _take_path_content({"path": path, "content": content})
        if path is None:
            return content
        result = await self._client.request("POST", f"/vault/{path}", body=content)
        err = _as_error(result)
        if err:
            return err
        return f"已追加到 {path}（{len(content)} 字符）"

    @register.tool(
        "note_patch",
        "局部修改 Obsidian 笔记的某个章节，无需重写整篇。可对标题、"
        "块引用或 frontmatter 字段做 append/prepend/replace。"
        "'修改某个标题''更新某个字段''替换某节'时优先本工具而非 "
        "note_write。文件必须已存在，新建先用 note_write。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "笔记路径，如 '01-architecture/design.md'",
                },
                "operation": {
                    "type": "string",
                    "description": "append（章节末尾追加）/ prepend（章节开头插入）/ replace（替换章节内容）",
                },
                "target_type": {
                    "type": "string",
                    "description": "heading（标题）/ block（块引用 ^id）/ frontmatter（YAML 字段）",
                },
                "target": {
                    "type": "string",
                    "description": (
                        "目标名称。标题：:: 分隔嵌套路径（最外层也要写，"
                        "如 '主标题::子标题'）；块引用：ID 不含 ^；"
                        "frontmatter：字段名"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "要应用的内容。heading/block 用 Markdown；"
                        "frontmatter 用 JSON 值（如 '\"value\"' 或 '[1,2]'）"
                    ),
                },
                "create_if_missing": {
                    "type": "boolean",
                    "description": "目标章节不存在时是否自动创建（默认 false，仅建章节不建文件）",
                },
                "target_scope": {
                    "type": "string",
                    "description": "content（默认，章节内容区域）/ marker（标题行/块标记本身，顶层标题后追加用 marker）",
                },
            },
            "required": ["path", "operation", "target_type", "target", "content"],
        },
    )
    async def note_patch(self, event, *_, path: str = "", operation: str = "",
                         target_type: str = "", target: str = "",
                         content: str | None = None,
                         create_if_missing: bool = False,
                         target_scope: str = "content", **_kwargs) -> str:
        if self._client is None:
            return _NOT_CONFIGURED
        path = str(path or "").strip().strip("/")
        if not path:
            return "错误：缺少 path 参数"
        operation = str(operation or "").strip()
        if operation not in ("append", "prepend", "replace"):
            return f"错误：不支持的操作 {operation}（可选 append/prepend/replace）"
        target_type = str(target_type or "").strip()
        if target_type not in ("heading", "block", "frontmatter"):
            return f"错误：不支持的目标类型 {target_type}（可选 heading/block/frontmatter）"
        target = str(target or "")
        if not target:
            return "错误：缺少 target 参数"
        if not isinstance(content, str):
            return "错误：缺少 content 参数（必须为字符串）"
        headers = {
            "Operation": operation,
            "Target-Type": target_type,
            "Target": urllib.parse.quote(target, safe=""),
        }
        if create_if_missing is True or str(create_if_missing).lower() == "true":
            headers["Create-Target-If-Missing"] = "true"
        target_scope = str(target_scope or "content").strip()
        if target_scope not in ("content", "marker"):
            return f"错误：不支持的 target_scope {target_scope}（可选 content/marker）"
        if target_scope != "content":
            headers["Target-Scope"] = target_scope
        result = await self._client.request(
            "PATCH",
            f"/vault/{path}",
            body=content,
            content_type="text/markdown",
            extra_headers=headers,
        )
        err = _as_error(result)
        if err:
            return err
        return f"已局部修改 {path}（{operation} {target_type} '{target}'）"

    @register.tool(
        "note_delete",
        "删除 Obsidian 笔记库中的笔记，不可逆！仅在对方明确要求删除"
        "时使用，删除前先确认路径正确。仅用于删除 Obsidian 笔记。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要删除的笔记路径，如 'reports/old-note.md'",
                },
            },
            "required": ["path"],
        },
    )
    async def note_delete(self, event, *_, path: str = "", **_kwargs) -> str:
        if self._client is None:
            return _NOT_CONFIGURED
        path = str(path or "").strip().strip("/")
        if not path:
            return "错误：缺少 path 参数"
        result = await self._client.request("DELETE", f"/vault/{path}")
        err = _as_error(result)
        if err:
            return err
        return f"已删除 {path}"

    @register.tool(
        "note_search",
        "搜索 Obsidian 笔记库，返回精简的 文件名|相关度|摘要 列表；"
        "看完整内容对感兴趣的笔记用 note_read。simple（默认）按"
        "关键词搜索；dataview 用 DQL 语句；jsonlogic 用 JSON 规则。"
        "仅用于搜索 Obsidian 笔记，不用于本地文件。",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询（simple=关键词 / dataview=DQL / jsonlogic=JSON 规则）",
                },
                "search_type": {
                    "type": "string",
                    "description": "搜索类型：simple（默认）/ dataview / jsonlogic",
                },
            },
            "required": ["query"],
        },
    )
    async def note_search(self, event, *_, query: str = "",
                          search_type: str = "simple", **_kwargs) -> str:
        if self._client is None:
            return _NOT_CONFIGURED
        query = str(query or "")
        if not query.strip():
            return "错误：缺少 query 参数"
        search_type = str(search_type or "simple").strip()
        if search_type == "simple":
            result = await self._client.request(
                "POST",
                f"/search/simple/?query={urllib.parse.quote(query, safe='')}",
            )
        elif search_type == "dataview":
            result = await self._client.request(
                "POST",
                "/search/",
                body=query,
                content_type="application/vnd.olrapi.dataview.dql+txt",
            )
        elif search_type == "jsonlogic":
            result = await self._client.request(
                "POST",
                "/search/",
                body=query,
                content_type="application/vnd.olrapi.jsonlogic+json",
            )
        else:
            return f"错误：不支持的搜索类型 {search_type}（可选 simple/dataview/jsonlogic）"
        err = _as_error(result)
        if err:
            return err
        if isinstance(result, list):
            return self._format_results(query, result, search_type)
        if isinstance(result, dict):
            preview = _truncate(str(result), 500)
            return (
                f"搜索 \"{query}\" 返回结构化数据（非列表），建议改用 simple 类型。"
                f"\n数据预览: {preview}"
            )
        return f"搜索 \"{query}\" 返回原始数据:\n{_truncate(str(result), 1000)}"

    def _format_results(self, query: str, results: list, search_type: str) -> str:
        """把搜索结果格式为 文件名|相关度|摘要 文本列表。"""
        if not results:
            return f"搜索 \"{query}\" 未找到相关笔记"
        if search_type == "simple":
            ordered = sorted(
                results,
                key=lambda x: (x.get("score", 0) or 0) if isinstance(x, dict) else 0,
                reverse=True,
            )
        else:
            ordered = results
        trimmed = ordered[: self._max_results]
        lines = [
            f"搜索 \"{query}\" 共找到 {len(results)} 条结果（显示前 {len(trimmed)} 条）:"
        ]
        for i, item in enumerate(trimmed, 1):
            if not isinstance(item, dict):
                lines.append(f"{i}. {item}")
                continue
            filename = item.get("filename", item.get("file", "未知文件"))
            if search_type == "simple":
                score = float(item.get("score", 0) or 0)
                matches = item.get("matches", [])
                if matches and isinstance(matches[0], dict):
                    ctx_text = str(matches[0].get("context", ""))
                    summary = ctx_text.strip().replace("\n", " ")[: self._summary_len]
                else:
                    summary = ""
                summary = summary or "（无摘要）"
                lines.append(f"{i}. {filename} | 相关度:{score:.2f} | {summary}")
            else:
                rv = item.get("result", "")
                if isinstance(rv, str):
                    summary = rv.strip().replace("\n", " ")[: self._summary_len]
                else:
                    summary = str(rv)[: self._summary_len]
                lines.append(f"{i}. {filename} | {summary}")
        lines.append("提示: 用 note_read 查看完整笔记内容")
        return _truncate("\n".join(lines), self._max_output)

    # ------------------------------------------------------------------
    # Study schedule extension (v1.1.0): 学习计划 / 打卡 / 复盘 + 定时提醒
    # ------------------------------------------------------------------

    async def _vault_read(self, path: str) -> str | None:
        """Read a vault file; empty string means file missing, None means error."""
        if self._client is None:
            return None
        r = await self._client.request("GET", f"/vault/{path}")
        if isinstance(r, dict) and r.get("error"):
            if "404" in str(r["error"]):
                return ""
            return None
        return str(r) if r is not None else ""

    async def _vault_write(self, path: str, content: str) -> str | None:
        """Write a vault file; returns error text or None on success."""
        if self._client is None:
            return "Obsidian 未配置"
        r = await self._client.request("PUT", f"/vault/{path}", body=content)
        if isinstance(r, dict) and r.get("error"):
            return str(r["error"])
        return None

    def _load_study_state(self):
        try:
            if self._state_path and self._state_path.exists():
                st = json.loads(self._state_path.read_text("utf-8"))
                self._reminded = set(st.get("reminded", []))
                self._reported_date = str(st.get("reported_date", ""))
        except Exception:
            logger.exception("Obsidian: 学习状态读取失败")

    def _save_study_state(self):
        try:
            if self._state_path:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                self._state_path.write_text(
                    json.dumps(
                        {"reminded": sorted(self._reminded), "reported_date": self._reported_date},
                        ensure_ascii=False,
                    ),
                    "utf-8",
                )
        except Exception:
            logger.exception("Obsidian: 学习状态保存失败")

    async def _study_notify(self, text: str):
        """Push a proactive notice to the configured sid."""
        if not self.push_sid:
            return
        try:
            await self.ctx.publish_notice(
                self.push_sid, MessageChain([Text(text)]), is_mentioned=True
            )
        except Exception:
            logger.exception("Obsidian: 学习提醒推送失败")

    async def _study_loop(self):
        while True:
            try:
                await self._study_tick()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Obsidian: 学习监督 tick 异常")
            await asyncio.sleep(60)

    async def _study_tick(self):
        if self._client is None:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        content = await self._vault_read(self.plan_file)
        if content is None:
            return
        # 1) remind tasks that are about to start
        if self.remind_before_minutes and content:
            window_end = now + timedelta(minutes=self.remind_before_minutes)
            for line in content.splitlines():
                t = _parse_task_line(line)
                if not t or t["done"]:
                    continue
                sd = _parse_dt(t["start"])
                if not sd or not (now <= sd <= window_end):
                    continue
                key = f"{today}|{line.strip()}"
                if key in self._reminded:
                    continue
                self._reminded.add(key)
                self._save_study_state()
                await self._study_notify(
                    f"⏰ 学习提醒：{t['desc']} 该开始了（{t['start']}）"
                )
        # 2) daily progress report at report_time
        if (
            self.report_time
            and now.strftime("%H:%M") == self.report_time
            and self._reported_date != today
        ):
            self._reported_date = today
            self._save_study_state()
            report = await self.study_report(None, plan_file=self.plan_file)
            await self._study_notify(report)

    @register.tool(
        name="study_schedule",
        description="把学习任务排进 Obsidian 计划笔记（Gantt Calendar Tasks 格式；同文件里今天已存在的任务行会被清理，避免重复堆积）。用户说'安排学习计划/排任务/制定学习日程'时调用。",
        params={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "string",
                    "description": "任务列表，每行一条，格式：描述 | 开始时间 | 截止时间 | 优先级。时间支持 YYYY-MM-DD 或 YYYY-MM-DD HH:mm；优先级可选 highest/high/medium/low/lowest（默认 normal）。示例：\n复习高数第三章 | 2026-09-28 09:00 | 2026-09-28 11:00 | high\n背单词50个 | 2026-09-28 14:00 | 2026-09-28 15:00 | medium",
                },
                "plan_file": {
                    "type": "string",
                    "description": "计划笔记文件名（默认为插件配置里的 plan_file）",
                },
            },
            "required": ["tasks"],
        },
    )
    async def study_schedule(self, event, *_, tasks: str = "", plan_file: str = "", **_kwargs) -> str:
        if self._client is None:
            return _NOT_CONFIGURED
        path = (plan_file or self.plan_file or "学习计划.md").strip().strip("/")
        if not tasks.strip():
            return "错误：缺少 tasks 参数"
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        content = await self._vault_read(path)
        if content is None:
            return "错误：读取计划文件失败，请检查 Obsidian 连接"
        # keep non-task lines and tasks whose due date is not today
        kept: list[str] = []
        removed = 0
        for line in content.splitlines():
            t = _parse_task_line(line)
            if t is None:
                kept.append(line)
                continue
            due_dt = _parse_dt(t["due"])
            if due_dt and due_dt.strftime("%Y-%m-%d") == today:
                removed += 1
                continue
            kept.append(line)
        while kept and kept[-1].strip() == "":
            kept.pop()
        # build new task lines
        new_lines: list[str] = []
        for raw in tasks.strip().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            parts = [p.strip() for p in re.split(r"\s*\|\s*", raw)]
            desc = parts[0]
            start = parts[1] if len(parts) > 1 else ""
            due = parts[2] if len(parts) > 2 else (start or today)
            prio = parts[3] if len(parts) > 3 else "normal"
            if not desc:
                continue
            if prio not in _PRIO_EMOJI:
                prio = "normal"
            if start and not _parse_dt(start):
                return f"错误：开始时间格式不对（{start}），应形如 2026-09-28 或 2026-09-28 18:00"
            if not _parse_dt(due):
                return f"错误：截止时间格式不对（{due}），应形如 2026-09-28 或 2026-09-28 18:00"
            new_lines.append(_make_task_line(desc, start, due, prio))
        if not new_lines:
            return "错误：没有解析出有效任务"
        updated = "\n".join(kept + new_lines).rstrip() + "\n"
        err = await self._vault_write(path, updated)
        if err:
            return f"写入失败：{err}"
        summary = "\n".join(l[5:] for l in new_lines)
        return (
            f"已安排 {len(new_lines)} 个学习任务到 {path}"
            f"（今日旧任务清理 {removed} 条）：\n{summary}"
        )

    @register.tool(
        name="study_checkin",
        description="学习打卡：把计划笔记里匹配关键词的未完成任务标记为已完成（[x] 并追加 ✅ 完成日期）。用户说'我学完了XX/打卡/标记完成'时调用。",
        params={
            "type": "object",
            "properties": {
                "task_keyword": {
                    "type": "string",
                    "description": "要标记完成的任务描述关键词（模糊匹配）",
                },
                "done_date": {
                    "type": "string",
                    "description": "完成日期 YYYY-MM-DD（默认今天）",
                },
                "plan_file": {
                    "type": "string",
                    "description": "计划笔记文件名（默认为插件配置里的 plan_file）",
                },
            },
            "required": ["task_keyword"],
        },
    )
    async def study_checkin(self, event, *_, task_keyword: str = "", done_date: str = "",
                            plan_file: str = "", **_kwargs) -> str:
        if self._client is None:
            return _NOT_CONFIGURED
        if not task_keyword.strip():
            return "错误：缺少 task_keyword 参数"
        path = (plan_file or self.plan_file or "学习计划.md").strip().strip("/")
        done = done_date or datetime.now().strftime("%Y-%m-%d")
        content = await self._vault_read(path)
        if content is None:
            return "错误：读取计划文件失败"
        if not content:
            return "计划文件为空，先 study_schedule 安排任务"
        kw = task_keyword.strip().lower()
        out: list[str] = []
        matched = False
        updated_line = ""
        candidates: list[str] = []
        for line in content.splitlines():
            t = _parse_task_line(line)
            if t is None:
                out.append(line)
                continue
            hit = kw in t["desc"].lower()
            if hit:
                candidates.append(line.strip())
            if t["done"]:
                out.append(line)
                continue
            if hit and not matched:
                base = re.sub(r"^- \[[ xX]\]", "- [x]", line, count=1).rstrip()
                if not re.search(r"✅ \d{4}-\d{2}-\d{2}", base):
                    base = f"{base} ✅ {done}"
                out.append(base)
                matched = True
                updated_line = base.strip()
                continue
            out.append(line)
        if not matched:
            tip = "\n".join(candidates[:5]) if candidates else "（没有相近任务）"
            return (
                f"没找到未完成的匹配任务：{task_keyword}\n"
                f"相近任务：\n{tip}\n"
                "试试更短的关键词，或先 study_schedule 安排任务"
            )
        err = await self._vault_write(path, "\n".join(out).rstrip() + "\n")
        if err:
            return f"写入失败：{err}"
        return f"✅ 打卡成功：{updated_line[5:]}"

    @register.tool(
        name="study_report",
        description="检查学习进度并动态调整：统计完成率、今日截止情况和过期任务，把过期未完成的任务顺延到明天。用户说'检查学习进度/今日复盘/学习状态'时调用。",
        params={
            "type": "object",
            "properties": {
                "plan_file": {
                    "type": "string",
                    "description": "计划笔记文件名（默认为插件配置里的 plan_file）",
                },
                "adjust": {
                    "type": "boolean",
                    "description": "是否把过期未完成任务顺延到明天（默认 true）",
                },
            },
        },
    )
    async def study_report(self, event, *_, plan_file: str = "", adjust: bool = True, **_kwargs) -> str:
        if self._client is None:
            return _NOT_CONFIGURED
        path = (plan_file or self.plan_file or "学习计划.md").strip().strip("/")
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        content = await self._vault_read(path)
        if content is None:
            return "错误：读取计划文件失败"
        if not content:
            return "计划文件为空，还没有任务"
        total = done = 0
        today_due = 0
        today_done = 0
        overdue = 0
        shifted = 0
        out: list[str] = []
        for line in content.splitlines():
            t = _parse_task_line(line)
            if t is None:
                out.append(line)
                continue
            total += 1
            due_dt = _parse_dt(t["due"])
            due_today = bool(due_dt and due_dt.strftime("%Y-%m-%d") == today)
            if due_today:
                today_due += 1
            if t["done"]:
                done += 1
                if due_today:
                    today_done += 1
                out.append(line)
                continue
            if _is_overdue(t["due"], now):
                overdue += 1
                if adjust:
                    new_due = _shift_date_str(t["due"], 1)
                    new_start = _shift_date_str(t["start"], 1) if t["start"] else ""
                    out.append(_make_task_line(t["desc"], new_start, new_due or "", t["prio"]))
                    shifted += 1
                    continue
            out.append(line)
        if shifted:
            err = await self._vault_write(path, "\n".join(out).rstrip() + "\n")
            if err:
                return f"统计完成但写回失败：{err}"
        rate = f"{done}/{total}（{done * 100 // total if total else 0}%）" if total else "0/0"
        lines = [
            f"📊 学习进度 {today}",
            f"任务总数：{total}　已完成：{rate}",
            f"今日截止：{today_due}（已完成 {today_done}）　过期未完成：{overdue}",
        ]
        if shifted:
            lines.append(f"↪️ 已把 {shifted} 个过期任务顺延到明天")
        if total and done == total:
            lines.append("🎉 全部完成，可以加量或休息")
        elif total and done >= total * 0.7:
            lines.append("👍 进度不错，保持节奏")
        elif overdue:
            lines.append("⚠️ 有任务积压，建议砍掉部分任务或降低强度")
        return "\n".join(lines)
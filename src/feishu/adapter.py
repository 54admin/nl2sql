"""飞书机器人通道适配器：旁路接入 orchestrator，不碰 HTTP/SSE 主链路。

命门：lark-oapi 的 ws 模块（lark_oapi.ws.client）有模块级全局 loop，
WsClient.start() 用它 run_until_complete 阻塞当前线程。若在 FastAPI 主线程
import 它，模块级 loop 会绑成正在运行的 FastAPI loop，跨线程 start() 必崩。

架构：
- WsClient 跑在独立 daemon 线程；线程内 new_event_loop + 覆盖 ws.client.loop
- 同步事件回调只做投递：run_coroutine_threadsafe(coro, main_loop) fire-and-forget
- _handle_incoming 及卡片流式全程在 main_loop，原生 asyncio
- 卡片用 CardKit streaming_mode（JSON 2.0）：创建卡片实体（body 仅 answer 元素）
  →发消息引用 card_id→每个工具步骤 card_element/create insert_before answer 插入
  独立元素（各带图标）→answer 元素 acontent 传【全量】文本打字机→done 关流式 +
  card.aupdate 全量替换（兜底：飞书 streaming 有平台超时，流式态可能丢过程/丢字，
  关流式后用 _tool_lines 重建确保过程+答案完整，summary 一并更新）

lark 全部延迟 import（放方法内）。复用：orchestrator/SessionManager/RedisClient/CancelToken。"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from uuid import uuid4

from src.core.types import CancelToken
from src.feishu import card
from src.logging import get_logger
from src.web.sse import ViewerMode

log = get_logger(__name__)

_BIND_KEY = "nl2sql:feishu:bind:{open_id}"
_SESSION_TTL = 3600


class CardStream:
    """一张流式卡片：每步过程独立元素 insert_before answer（各带图标），answer
    单元素 acontent 打字机。done 只关流式——流式态即最终态，每步独立图标已就位。

    顺序：每个过程元素 insert_before 到 answer 前，新元素紧贴 answer → 过程按
    到达顺序排列、answer 永远在末尾，与事件时序无关。
    """

    def __init__(self, lark_client, card_id: str, throttle_s: float):
        self._lark = lark_client
        self._card_id = card_id
        self._throttle = throttle_s
        self._answer = ""
        self._tool_lines: list[tuple[str, str]] = []   # [(icon_token, line)] 仅日志/诊断
        self._last_call_sig: str | None = None          # 重复调用去重（LLM 试错重发同 SQL）
        self._skip_next_result = False                   # 上一次 call 因重复跳过 → 对应 result 也跳过
        self._proc_seq = 0          # 过程元素 element_id 自增（保证唯一，避开 300301）
        self._seq = 0               # 卡片操作 sequence（严格递增，避开 300317）
        self._last_flush = 0.0
        self._flush_task: asyncio.Task | None = None

    def on_answer_delta(self, text: str) -> None:
        self._answer += text
        # 不流式 acontent 打字机：answer 仅在 on_done 全量 card.update 显示。
        # 否则流式 acontent（打字机）与全量 content 叠加 → 答案重复两遍。

    async def on_tool(self, token: str, line: str, *, rows: int | None = None) -> None:
        """一步过程：insert_before answer 插入独立元素（带 token 图标）。"""
        self._tool_lines.append((token, line))
        eid = f"proc_{self._proc_seq}"
        self._proc_seq += 1
        await self._add_element(card.proc_element(eid, token, line))
        log.info("飞书 tool：%s%s", line, f" → {rows} 行" if rows is not None else "")

    def on_tool_call(self, name: str, args) -> tuple[str, str] | None:
        """工具调用 → (图标, 友好文本)。重复调用（同 name+args）直接跳过不显示。"""
        sig = name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        if sig == self._last_call_sig:
            self._skip_next_result = True
            return None
        self._last_call_sig = sig
        self._skip_next_result = False
        return _tool_call_line(name, args)

    def on_tool_result(self, name: str, summary) -> tuple[str, str] | None:
        """工具结果 → (图标, 友好文本)。上一次 call 被去重跳过 → 对应 result 也跳过。"""
        if self._skip_next_result:
            self._skip_next_result = False
            return None
        return _tool_result_line(name, summary)

    async def on_done(self, answer: str | None = None) -> None:
        if answer:
            self._answer = answer
        log.info("飞书 done：answer=%d 字符，过程=%d 步", len(self._answer), len(self._tool_lines))
        # 全量替换：过程 + 答案 + summary + 关流式一次到位。
        # answer 仅此一份（on_answer_delta 不再 acontent 打字机，避免和全量 content 叠加重复）。
        ok = await self._update_card_full(card.build_final_card(self._tool_lines, self._answer))
        if not ok:   # 全量替换失败（飞书超时等）→ 退回流式态兜底：flush 答案 + 关流式并更新 summary
            await self._cancel_and_flush()
            await self._close_streaming(self._answer)

    async def on_clarify(self, question: str, options, sid: str) -> None:
        await self._cancel_and_flush()
        opts = " / ".join((o.get("label") if isinstance(o, dict) else str(o)) for o in (options or []))
        tip = f"\n\n_请直接回复你的选择（{opts}）_" if opts else ""
        prefix = (self._answer + "\n\n") if self._answer else ""
        await self._stream_text(card.ANSWER_EID, prefix + f"**需要确认**：{question}{tip}")
        await self._close_streaming()

    async def on_error(self, msg: str) -> None:
        self._answer = f"**错误**：{msg}"
        await self._cancel_and_flush()
        await self._close_streaming()

    # ---- 节流（streaming 不限 QPS，节流省 HTTP 往返）----
    def _schedule_flush(self) -> None:
        if self._flush_task and not self._flush_task.done():
            return
        delay = max(0.0, self._throttle - (time.monotonic() - self._last_flush))
        self._flush_task = asyncio.create_task(self._flush_after(delay))

    async def _flush_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._flush()
        except asyncio.CancelledError:
            pass

    async def _cancel_and_flush(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush()

    async def _flush(self) -> None:
        # answer 单元素流式（过程步骤已各自独立元素 insert_before，不拼这里）
        if not self._answer:
            return
        await self._stream_text(card.ANSWER_EID, self._answer)
        self._last_flush = time.monotonic()

    # ---- cardkit 调用（异步 a 前缀方法，直接 await）----
    async def _stream_text(self, element_id: str, content: str) -> None:
        """流式更新 answer 元素内容（acontent）：传全量，平台打字机。"""
        from lark_oapi.api.cardkit.v1 import ContentCardElementRequest, ContentCardElementRequestBody
        req = (ContentCardElementRequest.builder()
               .card_id(self._card_id).element_id(element_id)
               .request_body(ContentCardElementRequestBody.builder()
                             .content(content).sequence(self._next_seq()).build()).build())
        try:
            resp = await self._lark.cardkit.v1.card_element.acontent(req)
            if not resp.success():
                log.warning("飞书流式文本失败 card=%s eid=%s code=%s msg=%s",
                            self._card_id, element_id, resp.code, resp.msg)
        except Exception as e:
            log.warning("飞书流式文本异常/超时 card=%s eid=%s: %s", self._card_id, element_id, e)

    async def _close_streaming(self, answer: str | None = None) -> None:
        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody
        cfg: dict = {"streaming_mode": False}
        if answer:   # 关流式时一并更新 summary（"生成中..." → 答案摘要），避免卡片卡在生成中
            cfg["summary"] = {"content": card._summary_text(answer)}
        req = (SettingsCardRequest.builder().card_id(self._card_id)
               .request_body(SettingsCardRequestBody.builder()
                             .settings(json.dumps({"config": cfg}))
                             .sequence(self._next_seq()).build()).build())
        try:
            resp = await self._lark.cardkit.v1.card.asettings(req)
            if not resp.success():
                log.warning("飞书关流式失败 card=%s code=%s msg=%s", self._card_id, resp.code, resp.msg)
        except Exception as e:
            log.warning("飞书关流式异常/超时 card=%s: %s", self._card_id, e)

    async def _add_element(self, element: dict) -> None:
        """card_element/create：insert_before answer 插入独立过程元素（带图标）。
        elements 是 JSON 序列化的组件数组（JSON 2.0）；uuid 幂等防重复插入。"""
        from lark_oapi.api.cardkit.v1 import CreateCardElementRequest, CreateCardElementRequestBody
        req = (CreateCardElementRequest.builder().card_id(self._card_id)
               .request_body(CreateCardElementRequestBody.builder()
                             .type("insert_before").target_element_id(card.ANSWER_EID)
                             .uuid(uuid4().hex).sequence(self._next_seq())
                             .elements(json.dumps([element], ensure_ascii=False)).build()).build())
        try:
            resp = await self._lark.cardkit.v1.card_element.acreate(req)
            if not resp.success():
                log.warning("飞书插入过程元素失败 card=%s code=%s msg=%s",
                            self._card_id, resp.code, resp.msg)
        except Exception as e:
            log.warning("飞书插入过程元素异常/超时 card=%s: %s", self._card_id, e)

    async def _update_card_full(self, card_json: dict) -> bool:
        """全量替换卡片（card.update）：done 后，过程+答案+summary+关流式一次到位。
        返回是否成功（失败时调用方走流式态兜底）。"""
        from lark_oapi.api.cardkit.v1 import Card, UpdateCardRequest, UpdateCardRequestBody
        card_obj = Card.builder().type("card_json").data(
            json.dumps(card_json, ensure_ascii=False)).build()
        req = (UpdateCardRequest.builder().card_id(self._card_id)
               .request_body(UpdateCardRequestBody.builder().card(card_obj)
                             .sequence(self._next_seq()).build()).build())
        try:
            resp = await self._lark.cardkit.v1.card.aupdate(req)
            if not resp.success():
                log.warning("飞书全量更新卡片失败 card=%s code=%s msg=%s", self._card_id, resp.code, resp.msg)
                return False
            return True
        except Exception as e:
            log.warning("飞书全量更新异常/超时 card=%s: %s", self._card_id, e)
            return False

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq


class FeishuAdapter:
    def __init__(self, cfg, main_loop, orchestrator, session_mgr, redis):
        self._yml_cfg = cfg
        self._cfg = cfg
        self._main_loop = main_loop
        self._orch = orchestrator
        self._sessions = session_mgr
        self._redis = redis
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._thread: threading.Thread | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._lark_client = None

    async def start(self) -> None:
        self._cfg = await self._resolve_runtime_cfg()
        if not (self._cfg.enable and self._cfg.app_id and self._cfg.app_secret):
            log.info("飞书通道未启用（库/yml enable=false 或缺 app_id/secret）")
            return
        self._start_thread()

    def _start_thread(self) -> None:
        self._thread = threading.Thread(target=self._run_ws, name="feishu-ws", daemon=True)
        self._thread.start()

    async def reload(self) -> None:
        await asyncio.to_thread(self.stop)
        await self.start()

    async def _resolve_runtime_cfg(self):
        from sqlalchemy import select
        from src.config import FeishuConfig
        from src.storage.models import FeishuConfigRow
        from src.storage.pg_client import AsyncSessionFactory
        try:
            async with AsyncSessionFactory() as s:
                row = (await s.execute(select(FeishuConfigRow).where(
                    FeishuConfigRow.enabled.is_(True)).order_by(
                    FeishuConfigRow.version.desc()))).scalars().first()
            if row and row.app_id and row.app_secret:
                return FeishuConfig(enable=True, app_id=row.app_id, app_secret=row.app_secret,
                                    whitelist=row.whitelist or [],
                                    card_throttle_ms=row.card_throttle_ms or 300)
        except Exception as e:
            log.warning("读飞书库配置失败，用 yml 兜底: %s", e)
        return self._yml_cfg

    def stop(self) -> None:
        if self._ws_loop is not None:
            try:
                self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run_ws(self) -> None:
        import lark_oapi as lark
        from lark_oapi.ws import Client as WsClient
        from lark_oapi.ws import client as ws_mod
        self._ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._ws_loop)
        ws_mod.loop = self._ws_loop
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self._on_message_sync)
                   .register_p2_card_action_trigger(self._on_card_sync)
                   .build())
        self._lark_client = (lark.Client.builder()
                             .app_id(self._cfg.app_id).app_secret(self._cfg.app_secret)
                             .log_level(lark.LogLevel.INFO).timeout(15).build())  # 15s：防飞书 API hang 死锁 agent→卡片卡"生成中"
        self._ws = WsClient(self._cfg.app_id, self._cfg.app_secret,
                            event_handler=handler, log_level=lark.LogLevel.INFO,
                            auto_reconnect=True)
        log.info("飞书 ws 长连接启动 app_id=%s...", str(self._cfg.app_id)[:12])
        try:
            self._ws.start()
        except RuntimeError as e:
            # shutdown 时 stop(ws_loop) 会让 run_until_complete 抛 "Event loop stopped"——关停预期，不当异常
            if "stopped" in str(e):
                log.info("飞书 ws 长连接已关闭（shutdown）")
            else:
                log.exception("飞书 ws 长连接异常退出")
        except Exception:
            log.exception("飞书 ws 长连接异常退出")

    def _on_message_sync(self, data) -> None:
        try:
            msg = data.event.message
            if getattr(msg, "message_type", "") != "text":
                return
            open_id = data.event.sender.sender_id.open_id
            content = json.loads(msg.content) if getattr(msg, "content", None) else {}
            text = self._strip_at_mention((content.get("text") or "").strip())
            if not text:
                return
            if self._cfg.whitelist and open_id not in self._cfg.whitelist:
                asyncio.run_coroutine_threadsafe(
                    self._send_text(open_id, "未授权，联系管理员加白名单"), self._main_loop)
                return
            asyncio.run_coroutine_threadsafe(
                self._handle_incoming(open_id, text), self._main_loop)
        except Exception:
            log.exception("feishu on_message 解析异常")

    def _on_card_sync(self, data) -> None:
        try:
            value = getattr(data.event.action, "value", None) or {}
            sid, label = value.get("sid"), value.get("label")
            if not sid or not label:
                return
            open_id = data.event.operator.open_id
            asyncio.run_coroutine_threadsafe(
                self._handle_incoming(open_id, label, force_sid=sid), self._main_loop)
        except Exception:
            log.exception("feishu on_card 解析异常")

    @staticmethod
    def _strip_at_mention(text: str) -> str:
        return re.sub(r"@_user_\d+\s*", "", text).strip()

    async def _send_text(self, open_id: str, text: str) -> None:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = (CreateMessageRequest.builder().receive_id_type("open_id")
               .request_body(CreateMessageRequestBody.builder().receive_id(open_id)
                             .msg_type("text").content(json.dumps({"text": text})).build())
               .build())
        resp = await self._lark_client.im.v1.message.acreate(req)
        if not resp.success():
            log.warning("飞书发文本失败 open_id=%s code=%s", open_id, resp.code)

    async def _create_card(self, open_id: str) -> str | None:
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        card_json = json.dumps(card.build_streaming_card(), ensure_ascii=False)
        req = (CreateCardRequest.builder().request_body(
            CreateCardRequestBody.builder().type("card_json").data(card_json).build()).build())
        resp = await self._lark_client.cardkit.v1.card.acreate(req)
        if not resp.success():
            log.warning("飞书创建卡片实体失败 code=%s msg=%s", resp.code, resp.msg)
            return None
        card_id = resp.data.card_id
        mreq = (CreateMessageRequest.builder().receive_id_type("open_id")
                .request_body(CreateMessageRequestBody.builder().receive_id(open_id)
                              .msg_type("interactive")
                              .content(json.dumps({"type": "card", "data": {"card_id": card_id}}))
                              .build()).build())
        mresp = await self._lark_client.im.v1.message.acreate(mreq)
        if not mresp.success():
            log.warning("飞书发卡片消息失败 code=%s msg=%s", mresp.code, mresp.msg)
            return None
        return card_id

    async def _find_or_create_session(self, open_id: str) -> str:
        key = _BIND_KEY.format(open_id=open_id)
        sid = await self._redis.get(key)
        if sid and await self._sessions.get_session(sid):
            return sid
        sid = await self._sessions.create_session(open_id, "feishu", title=None)
        await self._redis.set(key, sid, ttl=_SESSION_TTL)
        return sid

    async def _handle_incoming(self, open_id: str, text: str,
                               *, force_sid: str | None = None) -> None:
        lock = self._user_locks.get(open_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[open_id] = lock
        async with lock:
            await self._process(open_id, text, force_sid)

    async def _process(self, open_id: str, text: str, force_sid: str | None) -> None:
        trace_id = uuid4().hex
        sid = force_sid or await self._find_or_create_session(open_id)
        try:
            await self._orch._sessions.fill_title_if_empty(sid, text[:20])
        except Exception:
            pass
        card_id = await self._create_card(open_id)
        if not card_id:
            return
        throttle = max(0.05, self._cfg.card_throttle_ms / 1000)
        stream = CardStream(self._lark_client, card_id, throttle)
        cancel = CancelToken()
        try:
            async for evt in self._orch.handle_message(
                    user_id=open_id, session_id=sid, text=text,
                    mode=ViewerMode.USER, trace_id=trace_id, cancel_token=cancel):
                t = evt.type
                if t == "answer_delta":
                    stream.on_answer_delta(evt.data.get("text", ""))
                elif t == "tool_call":
                    item = stream.on_tool_call(evt.data.get("name", ""), evt.data.get("args"))
                    if item:
                        await stream.on_tool(item[0], item[1])
                elif t == "tool_result":
                    item = stream.on_tool_result(evt.data.get("name", ""), evt.data.get("summary", ""))
                    if item:
                        await stream.on_tool(item[0], item[1], rows=_extract_rows(evt.data.get("summary")))
                elif t == "clarification_needed":
                    await stream.on_clarify(evt.data.get("question", ""), evt.data.get("options"), sid)
                    return
                elif t == "done":
                    await stream.on_done(evt.data.get("answer", ""))
                    return
                elif t == "error":
                    await stream.on_error(evt.data.get("message", "内部错误"))
                    return
                elif t == "cancelled":
                    await stream.on_error("已取消")
                    return
            await stream.on_done()
        except Exception as e:
            log.exception("飞书通道处理异常 open_id=%s", open_id)
            try:
                await stream.on_error(f"处理异常：{e}")
            except Exception:
                pass


def _tool_call_line(name, args) -> tuple[str, str]:
    """工具调用 → (图标token, 友好文本)。不同工具不同图标，不翻来覆去。"""
    if name == "execute_sql":
        sql = (args or {}).get("sql", "").strip()
        return ("code_outlined", f"**执行查询**\n\n```sql\n{sql}\n```")
    if name == "query_metadata":
        return ("doc-search_outlined", "**查询元数据**（表/字段）")
    if name == "knowledge_search":
        return ("doc_outlined", "**检索知识库**")
    if name == "do_attribution":
        return ("insert-chart_outlined", "**归因分析**")
    return ("setting_outlined", f"**调用 {name or '工具'}**")


def _tool_result_line(name, summary) -> tuple[str, str]:
    """工具结果 → (图标token, 友好文本)。不同工具/状态不同图标。"""
    if name == "execute_sql":
        rows = _extract_rows(summary)
        cols = _extract_cols(summary)
        # summary 不是合法 JSON 结果 → execute_sql 执行失败（SQL 报错），别显示"返回?行"
        if rows is None:
            return ("warning_outlined", "**查询失败**" + (f"：{summary[:60]}" if summary else ""))
        if rows == 0:
            return ("warning_outlined", "**查询完成**：无匹配数据（0 行）")
        return ("data-sheet_outlined", f"**查询完成**：返回 {rows} 行"
                + (f"（{cols} 列）" if cols else ""))
    if name == "query_metadata":
        return ("check_outlined", "**元数据已获取**")
    if name == "knowledge_search":
        return ("check_outlined", "**知识库检索完成**")
    if name == "do_attribution":
        return ("insert-chart_outlined", "**归因完成**")
    return ("check_outlined", f"**{name or '工具'} 完成**")


def _extract_cols(summary) -> int | None:
    if not summary or not isinstance(summary, str) or not summary.startswith("{"):
        return None
    try:
        cols = json.loads(summary).get("columns")
        return len(cols) if cols else None
    except Exception:
        return None


def _extract_rows(summary) -> int | None:
    """execute_sql 成功时 summary 是 JSON {"result_id":..,"rows":N,...}，提取 rows 供诊断（查 8→5 用）。"""
    if not summary or not isinstance(summary, str) or not summary.startswith("{"):
        return None
    try:
        return int(json.loads(summary).get("rows", -1))
    except Exception:
        return None

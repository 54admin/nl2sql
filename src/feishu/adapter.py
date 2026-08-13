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
    """一张流式卡片：『思考过程清单』+『答案』两个固定顶级元素。
    - 过程清单(PROC_EID)：on_tool 每步往里 acontent 追加一条 ✓（全程只更新这一个元素，
      不再每步 insert 折叠框——避免过程期一堆框堆叠）。飞书顶级 markdown 的 acontent 可靠。
    - 答案(ANSWER_EID)：acontent 全量打字机。
    - done：关流式 + build_final_card 全量重建，把全部步骤(+思考)折进一个 collapsible_panel，
      答案在面板外可见。流式态即"清单流水"，done 后即"折叠汇总"，无中间多框状态。
    """

    def __init__(self, lark_client, card_id: str, throttle_s: float):
        self._lark = lark_client
        self._card_id = card_id
        self._throttle = throttle_s
        self._answer = ""
        self._reasoning = ""    # 思考链全文（reasoning_delta 累积，done 时折进折叠面板）
        self._tool_lines: list[tuple[str, str]] = []   # [(icon_token, line)] 仅日志/诊断
        self._last_call_sig: str | None = None          # 重复调用去重（LLM 试错重发同 SQL）
        self._skip_next_result = False                   # 上一次 call 因重复跳过 → 对应 result 也跳过
        self._proc_titles: list[str] = []   # 流式态思考过程清单（短标题），实时 acontent 到 PROC_EID
        self._seq = 0               # 卡片操作 sequence（严格递增，避开 300317）
        self._last_flush = 0.0
        self._flush_task: asyncio.Task | None = None

    def on_answer_delta(self, text: str) -> None:
        self._answer += text
        self._schedule_flush()   # 流式打字机：节流 acontent 全量 _answer，平台逐字渲染

    def on_reasoning_delta(self, text: str) -> None:
        if not text:
            return
        self._reasoning += text
        # 不触发 flush：流式打字进折叠面板在真实链路不可靠（占位符"(思考中…)"不更新），
        # 思考留到 done 全量重建时折进"思考过程"面板

    async def on_tool(self, token: str, line: str, *, rows: int | None = None) -> None:
        """一步过程：往【唯一的思考过程清单】acontent 追加一条 ✓。
        全程只更新一个元素（不再每步 insert 一个折叠框），过程像流水往上长；done 后
        build_final_card 再把全部步骤(+思考)折进一个 collapsible_panel。"""
        line = f"`{time.strftime('%H:%M:%S')}` " + line   # 步骤时间戳，便于复盘每步几点执行
        self._tool_lines.append((token, line))
        # 流式清单标题：去 markdown 符号后取首行。飞书卡片一行能容纳远超 46 字，
        # 早先 [:46] 会把表名/关键信息砍在中段（如 vw_ods_ranking_with_…）。放宽到 120 字，
        # 绝大多数步骤一行显示完整；超长的尾部省略，不硬砍中段。
        first = re.sub(r"[`*#]", "", line.split("\n")[0]).strip()
        title = (first[:117] + "…") if len(first) > 120 else first
        title = title or "操作步骤"
        self._proc_titles.append(title)
        await self._stream_text(card.PROC_EID, card.progress_markdown(self._proc_titles))
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

    async def on_done(self, answer: str | None = None,
                      citations: list | None = None) -> None:
        # 防卡死铁律：无论后面成不成功，先 flush 末批 + 关流式 + 更新 summary（"生成中..."→答案摘要），
        # 这样即使全量重建失败卡片也不会停在"生成中"。然后有过程/思考才全量重建——把 11 步工具折进
        # collapsible_panel；纯答案回复保留打字机效果只关流式即可。重建失败则兜底 acontent 答案。
        if answer and answer.strip():
            self._answer = answer
        has_steps = bool(self._tool_lines)
        has_reasoning = bool(self._reasoning.strip())
        has_citations = bool(citations)
        log.info("飞书 done：answer=%d 字符，思考=%d 字符，过程=%d 步，参考=%d 条，%s",
                 len(self._answer), len(self._reasoning), len(self._tool_lines),
                 len(citations or []),
                 "折叠重建" if (has_steps or has_reasoning or has_citations) else "仅关流式(纯答案)")
        await self._cancel_and_flush()
        await self._close_streaming(self._answer)
        if has_steps or has_reasoning or has_citations:
            ok = await self._update_card_full(
                card.build_final_card(self._tool_lines, self._answer, self._reasoning,
                                      citations))
            if not ok:
                log.warning("飞书 done 全量重建失败，兜底 acontent 答案（过程保留流式态展开）")
                await self._stream_text(card.ANSWER_EID, self._answer)

    async def on_clarify(self, question: str, options, sid: str) -> None:
        await self._cancel_and_flush()
        opts = " / ".join((o.get("label") if isinstance(o, dict) else str(o)) for o in (options or []))
        tip = f"\n\n_请直接回复你的选择（{opts}）_" if opts else ""
        prefix = (self._answer + "\n\n") if self._answer else ""
        await self._stream_text(card.ANSWER_EID, prefix + f"**需要确认**：{question}{tip}")
        await self._close_streaming(question)

    async def on_error(self, msg: str) -> None:
        self._answer = f"**错误**：{msg}"
        await self._cancel_and_flush()
        await self._close_streaming(self._answer)

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
        # 只打字 answer（流式打字进折叠面板不可靠，已废弃）；过程步骤各自独立元素，思考留到 done 后折进面板
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
        # message_id 去重表：飞书 WS ACK 超时会补投同一条消息；按 message_id 去重，
        # TTL 内重复（含补投/手抖连发同一条）直接丢弃。不再用 per-user asyncio.Lock——
        # 那会把第二条消息整段队列到上一个 130s run 跑完才放行，反而更糟。
        self._seen_msg: dict[str, float] = {}
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
        from src.storage.db_client import AsyncSessionFactory
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
                   .register_p2_application_bot_menu_v6(self._on_menu_sync)
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
            # 去重：飞书 WS 在 ACK 超时（约30-60s，run 慢时极易触发）会补投同一条消息；
            # 按 message_id 去重，直接丢弃——根治"同一问题被投递N次、跑N个run"。
            msg_id = getattr(msg, "message_id", None)
            if msg_id and self._is_dup_msg(msg_id):
                log.info("飞书重复消息丢弃 msg_id=%s", msg_id)
                return
            open_id = data.event.sender.sender_id.open_id
            content = json.loads(msg.content) if getattr(msg, "content", None) else {}
            text = self._strip_at_mention((content.get("text") or "").strip())
            if not text:
                return
            # 回复目标：群聊(chat_type=group)回群里(chat_id)，私聊回 open_id。
            # 每人独立会话——session 仍按 open_id 绑定，只把回复发到对应会话。
            chat_type = getattr(msg, "chat_type", "p2p")
            chat_id = getattr(msg, "chat_id", "")
            reply_to = ("chat", chat_id) if (chat_type == "group" and chat_id) else ("open_id", open_id)
            if self._cfg.whitelist and open_id not in self._cfg.whitelist:
                asyncio.run_coroutine_threadsafe(
                    self._send_text(reply_to[1], "未授权，联系管理员加白名单",
                                    receive_id_type=reply_to[0]), self._main_loop)
                return
            asyncio.run_coroutine_threadsafe(
                self._handle_incoming(open_id, text, reply_to=reply_to), self._main_loop)
        except Exception:
            log.exception("feishu on_message 解析异常")

    def _on_card_sync(self, data) -> None:
        try:
            value = getattr(data.event.action, "value", None) or {}
            open_id = data.event.operator.open_id
            kind = value.get("kind")
            if kind == "switch":
                # 会话列表卡片按钮：切绑定，纯管理动作不触发 LLM 问数（不进 _process）
                sid = value.get("sid")
                if sid:
                    asyncio.run_coroutine_threadsafe(
                        self._switch_session(open_id, sid), self._main_loop)
                return
            # 默认（clarify 选项按钮）：label 当回答走原会话续上
            sid, label = value.get("sid"), value.get("label")
            if not sid or not label:
                return
            asyncio.run_coroutine_threadsafe(
                self._handle_incoming(open_id, label, force_sid=sid), self._main_loop)
        except Exception:
            log.exception("feishu on_card 解析异常")

    def _on_menu_sync(self, data) -> None:
        """飞书自定义菜单点击（application.bot.menu_v6）。注意 menu 事件的 open_id 在 operator.operator_id 下。"""
        try:
            op = data.event.operator
            open_id = getattr(getattr(op, "operator_id", None), "open_id", None)
            key = data.event.event_key
            if not open_id or not key:
                return
            if self._cfg.whitelist and open_id not in self._cfg.whitelist:
                asyncio.run_coroutine_threadsafe(
                    self._send_text(open_id, "未授权，联系管理员加白名单"), self._main_loop)
                return
            asyncio.run_coroutine_threadsafe(
                self._handle_menu(open_id, key), self._main_loop)
        except Exception:
            log.exception("feishu on_menu 解析异常")

    async def _handle_menu(self, open_id: str, key: str) -> None:
        """菜单分流：new_session 建新会话并切过去；list_sessions 发会话列表卡片。纯管理，不进 _process。
        菜单是纯管理动作（建会话/列表），无需 per-user 锁——并发由 orchestrator 会话忙闸门兜底。"""
        try:
            if key == "new_session":
                sid = await self._sessions.create_session(open_id, "feishu", title=None)
                await self._switch_session(open_id, sid)
            elif key == "list_sessions":
                await self._send_session_list(open_id)
            else:
                log.info("未知飞书菜单 key=%s，忽略", key)
        except Exception:
            log.exception("飞书菜单处理异常 open_id=%s key=%s", open_id, key)

    async def _switch_session(self, open_id: str, target_sid: str) -> None:
        """切当前会话绑定到 target_sid（新建/选历史共用）+ 发确认。
        必须更新 _BIND_KEY，否则下条消息又被 _find_or_create_session 弹回旧会话。"""
        sess = await self._sessions.get_session(target_sid)
        if not sess or sess.get("user_id") != open_id:
            await self._send_text(open_id, "会话不存在或无权访问")
            return
        await self._redis.set(_BIND_KEY.format(open_id=open_id), target_sid, ttl=_SESSION_TTL)
        title = sess.get("title") or "新会话"
        await self._send_text(open_id, f"✅ 已切换到会话：{title}（直接发消息开始提问）")

    async def _send_session_list(self, open_id: str) -> None:
        """发会话列表卡片：列历史会话 + 每个一个「进入」按钮（value kind=switch，点击切会话）。"""
        sessions = await self._sessions.list_sessions(open_id)
        current = await self._redis.get(_BIND_KEY.format(open_id=open_id))
        if not sessions:
            await self._send_text(open_id, "暂无历史会话。点「🆕 新会话」开始一个新的提问。")
            return
        await self._create_card(open_id, card.build_session_list_card(sessions, current))

    @staticmethod
    def _strip_at_mention(text: str) -> str:
        return re.sub(r"@_user_\d+\s*", "", text).strip()

    async def _send_text(self, receive_id: str, text: str, *, receive_id_type: str = "open_id") -> None:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = (CreateMessageRequest.builder().receive_id_type(receive_id_type)
               .request_body(CreateMessageRequestBody.builder().receive_id(receive_id)
                             .msg_type("text").content(json.dumps({"text": text})).build())
               .build())
        resp = await self._lark_client.im.v1.message.acreate(req)
        if not resp.success():
            log.warning("飞书发文本失败 %s=%s code=%s", receive_id_type, receive_id, resp.code)

    async def _create_card(self, receive_id: str, card_json: dict | None = None,
                           *, receive_id_type: str = "open_id") -> str | None:
        """建 CardKit 卡片实体 + 发 interactive 消息引用 card_id。
        默认发流式问答卡片；传 card_json 发静态卡片（如会话列表，streaming_mode=False）。
        receive_id_type/receive_id：回复目标——群聊传 ("chat", chat_id)，私聊默认 ("open_id", open_id)。"""
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        card_json = json.dumps(card_json or card.build_streaming_card(), ensure_ascii=False)
        req = (CreateCardRequest.builder().request_body(
            CreateCardRequestBody.builder().type("card_json").data(card_json).build()).build())
        resp = await self._lark_client.cardkit.v1.card.acreate(req)
        if not resp.success():
            log.warning("飞书创建卡片实体失败 code=%s msg=%s", resp.code, resp.msg)
            return None
        card_id = resp.data.card_id
        mreq = (CreateMessageRequest.builder().receive_id_type(receive_id_type)
                .request_body(CreateMessageRequestBody.builder().receive_id(receive_id)
                              .msg_type("interactive")
                              .content(json.dumps({"type": "card", "data": {"card_id": card_id}}))
                              .build()).build())
        mresp = await self._lark_client.im.v1.message.acreate(mreq)
        if not mresp.success():
            log.warning("飞书发卡片消息失败 %s=%s code=%s msg=%s", receive_id_type, receive_id, mresp.code, mresp.msg)
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
                               *, force_sid: str | None = None,
                               reply_to: tuple[str, str] | None = None) -> None:
        # 并发防护两层：(1) _on_message_sync 已按 message_id 去重补投；
        # (2) orchestrator 的会话忙时闸门保证同 session 不并发跑第二个 run。
        # 故此处不再持有 per-user 锁（旧实现 TOCTOU 竞态会失效 + 把消息队列 130s）。
        await self._process(open_id, text, force_sid, reply_to)

    def _is_dup_msg(self, msg_id: str, ttl: float = 600.0) -> bool:
        """message_id 去重：TTL 内重复→True。顺手清过期项，防字典无限增长。"""
        now = time.monotonic()
        stale = [k for k, t in self._seen_msg.items() if now - t > ttl]
        for k in stale:
            self._seen_msg.pop(k, None)
        if msg_id in self._seen_msg:
            return True
        self._seen_msg[msg_id] = now
        return False

    async def _process(self, open_id: str, text: str, force_sid: str | None,
                       reply_to: tuple[str, str] | None = None) -> None:
        trace_id = uuid4().hex
        sid = force_sid or await self._find_or_create_session(open_id)
        try:
            await self._orch._sessions.fill_title_if_empty(sid, text[:20])
        except Exception:
            pass
        rid_type, rid = reply_to or ("open_id", open_id)   # 群聊→chat_id，私聊→open_id
        card_id = await self._create_card(rid, receive_id_type=rid_type)
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
                elif t == "reasoning_delta":
                    stream.on_reasoning_delta(evt.data.get("text", ""))
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
                    await stream.on_done(evt.data.get("answer", ""),
                                         evt.data.get("citations") or [])
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
        q = (args or {}).get("query", "").strip()
        return ("doc_outlined", f"**检索知识库**：`{q}`" if q else "**检索知识库**")
    return ("setting_outlined", f"**调用 {name or '工具'}**")


def _tool_result_line(name, summary) -> tuple[str, str]:
    """工具结果 → (图标token, 友好文本)。token 留接口兼容（当前不渲染图标，card.py 已去 icon）。"""
    if name == "execute_sql":
        data = _parse_sql_summary(summary)
        if data is None:
            return ("warning_outlined", f"**❌ 查询失败**\n```\n{_short_sql_error(summary)}\n```")
        if data["rows"] == 0:
            return ("warning_outlined", "**查询完成**：无匹配数据（0 行）")
        cols = data["columns"]
        line = f"**查询完成**：返回 {data['rows']} 行" + (f"（{len(cols)} 列）" if cols else "")
        tbl = _preview_table(data["preview"][:3], cols)
        if tbl:
            line += "\n\n" + tbl
        return ("data-sheet_outlined", line)
    if name == "query_metadata":
        tables = _extract_tables(summary)
        if tables:
            names = "、".join(t for t in tables[:6] if t)
            more = f" 等 {len(tables)} 张" if len(tables) > 6 else ""
            return ("check_outlined", f"**元数据已获取**（{len(tables)} 张表）：{names}{more}")
        return ("check_outlined", "**元数据已获取**")
    if name == "knowledge_search":
        hits = _extract_kb_hits(summary)
        if hits is None:
            # summary 已是工具返回的失败描述（如"知识库检索失败：xxx"），整体加粗直接用，避免重复前缀
            return ("warning_outlined", f"**{summary or '知识库检索异常'}**")
        if not hits:
            return ("warning_outlined", "**知识库无匹配文档**")
        snippet = (hits[0] or "")[:80].replace("\n", " ")
        more = f"（共 {len(hits)} 段）" if len(hits) > 1 else ""
        return ("check_outlined", f"**知识库命中**{more}：{snippet}")
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


def _extract_kb_hits(summary) -> list[str] | None:
    """knowledge_search summary 是 JSON {"hits":[{"content":..,"doc_id":..}]}，提取 content 列表供展示。
    非 JSON（失败/无匹配兜底文本）返回 None。"""
    if not summary or not isinstance(summary, str) or not summary.startswith("{"):
        return None
    try:
        hits = json.loads(summary).get("hits")
        return [h.get("content", "") for h in hits] if hits else []
    except Exception:
        return None


def _parse_sql_summary(summary) -> dict | None:
    """execute_sql summary → {rows, columns, preview, result_id}。非 JSON（执行失败兜底文本）返回 None。"""
    if not summary or not isinstance(summary, str) or not summary.startswith("{"):
        return None
    try:
        d = json.loads(summary)
        return {"rows": d.get("rows", 0), "columns": d.get("columns") or [],
                "preview": d.get("preview") or [], "result_id": d.get("result_id")}
    except Exception:
        return None

def _short_sql_error(summary: str) -> str:
    """SQL 报错精简成单行（配代码块横向滚动展示）：去 pymysql 附的 [SQL:...] 重复（tool_call 已显示过 SQL）、
    字段校验拦截只留"不存在字段：xxx"，其余压缩空白、超长截断。原始 summary 照旧回灌 LLM 自愈，此处只管卡片展示。"""
    s = summary or ""
    s = re.sub(r"\[SQL:.*", "", s, flags=re.S).strip()
    if "不存在字段" in s:
        m = re.search(r"不存在字段：([^\n]+)", s)
        if m:
            return f"字段不存在：{m.group(1).strip()}（已要求用真实字段改写）"
    s = re.sub(r"\s+", " ", s)
    return s[:300]


def _preview_table(rows: list[dict], cols: list[str]) -> str:
    """前几行渲染成 markdown 表格（飞书 markdown 支持）。列取前 6、值截 30 字，避免过宽撑爆卡片。"""
    if not rows or not cols:
        return ""
    show = cols[:6]
    lines = ["| " + " | ".join(show) + " |",
             "| " + " | ".join("---" for _ in show) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c, ""))[:30] for c in show) + " |")
    return "\n".join(lines)


def _extract_tables(summary) -> list[str] | None:
    """query_metadata summary → table_name 列表。非 JSON 返回 None。"""
    if not summary or not isinstance(summary, str) or not summary.startswith("{"):
        return None
    try:
        return [t.get("table_name", "") for t in json.loads(summary).get("tables") or []]
    except Exception:
        return None

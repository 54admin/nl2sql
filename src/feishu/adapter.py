"""飞书机器人通道适配器：旁路接入 orchestrator，不碰 HTTP/SSE 主链路。

命门：lark-oapi 的 ws 模块（lark_oapi.ws.client）有模块级全局 loop，
WsClient.start() 用它 run_until_complete 阻塞当前线程。若在 FastAPI 主线程
import 它，模块级 loop 会绑成正在运行的 FastAPI loop，跨线程 start() 必崩。

架构：
- WsClient 跑在独立 daemon 线程；线程内 new_event_loop + 覆盖 ws.client.loop
- 同步事件回调只做投递：run_coroutine_threadsafe(coro, main_loop) fire-and-forget
- _handle_incoming 及卡片流式全程在 main_loop，原生 asyncio
- 卡片用 CardKit streaming_mode（JSON 2.0）：创建卡片实体（body = 执行过程清单 + 答案两元素）
  →清单就地 acontent：每个工具一行编号步骤（动作 · 结果 · 距上一步Ns，进行中/失败文字化，
  无 emoji、无 SQL、无思考行/意图行）→答案 acontent 全量打字机
  →终态四路（done/error/取消/澄清）整卡重建折叠：思考全文（截断护栏）+ 步骤折进默认收起面板。
  卡片写全过 _api_lock + 严格递增 seq（防 300317 乱序）。

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
    """一张流式卡片：『思考过程清单』+『答案』两个固定顶级元素（过程展示 v3，无 emoji）。
    - 清单(PROC_EID)：每个工具一行编号步骤——N. **动作** · 结果 · 距上一步Ns
      （完成；失败 · 失败；进行中 · 执行中；距上一步 = 上一步结果→本步结果，首步=距提问）。
      无 SQL、无思考行、无意图行、无 emoji（用户逐项拍板）——完整 SQL/思考全文只在
      done 后折进面板。finish/ask_user 不进清单。
    - 答案(ANSWER_EID)：acontent 全量打字机。
    - 终态四路（done/error/取消/澄清）统一整卡重建折叠；重建失败隔 1s 重试一次。
    - 所有卡片写过 _api_lock + 严格递增 seq（防 300317 乱序）；渲染带版本号防过期覆盖。
    """

    def __init__(self, lark_client, card_id: str, throttle_s: float):
        self._lark = lark_client
        self._card_id = card_id
        self._throttle = throttle_s
        self._answer = ""
        self._reasoning = ""    # 思考链全文（done 后截断折进面板，不进流式清单）
        self._tool_lines: list[tuple[str, str]] = []   # [(icon_token, line)] 折叠面板用完整行
        # 清单步骤：{head 动作名, done, short 结果摘要, gap 距上一步秒数(done), anchor 起算锚点}
        self._steps: list[dict] = []
        self._last_call_sig: str | None = None          # 重复调用去重（LLM 试错重发同 SQL）
        self._skip_next_result = False                   # 上一次 call 因重复跳过 → 对应 result 也跳过
        self._seq = 0               # 卡片操作 sequence（严格递增，避开 300317）
        # 串行锁：seq 分配与请求发出必须在锁内原子完成，防并发写后发先至触发 300317
        self._api_lock = asyncio.Lock()
        self._dirty = False         # 流式写失败标志（日志用；终态反正全量重建兜底）
        self._last_flush = 0.0
        self._flush_task: asyncio.Task | None = None
        self._version = 0          # 状态版本号：渲染前快照，锁内比对防过期覆盖
        self._finalized = False    # 终态后不再渲染清单
        self._bg: set[asyncio.Task] = set()   # fire-and-forget 任务引用（防被 GC 中途蒸发）
        self._run_start = time.monotonic()      # 首步「距上一步」锚点 = 距提问
        self._last_step_end = time.monotonic()  # 后续步骤「距上一步」锚点 = 上一步结果时刻

    # ---- 事件入口（_process 调） ----
    def on_answer_delta(self, text: str) -> None:
        self._answer += text
        self._schedule_flush()   # 流式打字机：节流 acontent 全量 _answer，平台逐字渲染

    def on_reasoning_delta(self, text: str) -> None:
        # 思考原文不进卡片：只累积，done 后截断折进面板
        if text:
            self._reasoning += text

    def on_notice(self, text: str) -> None:
        """异常路径提示行（如单轮超时被掐后重写）——按完成步骤插入并渲染，
        重写期间清单有可见反馈，不无声冻结。"""
        if not text or self._finalized:
            return
        self._steps.append({"head": text, "done": True, "short": "处理中", "gap": None,
                            "anchor": time.monotonic()})
        self._version += 1
        self._spawn(self._render_proc())

    def begin_tool(self, name: str, args) -> tuple[str, str, str] | None:
        """tool_call：去重 + 过滤控制流 → (icon, 面板完整行, 动作名)；None=不展示。"""
        if name in ("finish", "ask_user"):
            return None    # 收尾噪音；ask_user 的提问由 clarify 渲染到答案区
        sig = name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        if sig == self._last_call_sig:
            self._skip_next_result = True
            return None
        self._last_call_sig = sig
        self._skip_next_result = False
        token, line = _tool_call_line(name, args)
        return token, line, _step_title_call(name, args)

    def end_tool(self, name: str, summary) -> tuple[str, str, str] | None:
        """tool_result：上一次 call 被去重跳过 → 对应 result 也跳过。"""
        if name in ("finish", "ask_user"):
            return None
        if self._skip_next_result:
            self._skip_next_result = False
            return None
        token, line = _tool_result_line(name, summary)
        return token, line, _step_title_result(name, summary)

    def _pending_step(self, head: str) -> dict:
        """取/建清单末尾的进行中步骤（同头复用——SQL 打字机可能已先建）。"""
        if self._steps and not self._steps[-1]["done"] and self._steps[-1]["head"] == head:
            return self._steps[-1]
        st = {"head": head, "done": False, "short": "",
              "gap": None, "anchor": self._last_step_end}
        self._steps.append(st)
        return st

    async def on_tool_pending(self, token: str, full_line: str, head: str) -> None:
        """工具开跑：面板记完整行（含 SQL，done 后折进面板可见）；清单只挂编号步骤行
        （无 SQL、无意图行——用户拍板）。"""
        line = f"`{time.strftime('%H:%M:%S')}` " + full_line   # 步骤时间戳，便于复盘每步几点执行
        self._tool_lines.append((token, line))
        self._pending_step(head)
        self._version += 1
        await self._render_proc()
        log.info("飞书 tool 开始：%s", head)

    async def on_tool_done(self, token: str, full_line: str, short: str) -> None:
        """工具返回：面板补完整结果行；步骤转完成，标结果与「距上一步」秒数。"""
        line = f"`{time.strftime('%H:%M:%S')}` " + full_line
        self._tool_lines.append((token, line))
        st = next((s for s in reversed(self._steps) if not s["done"]), None)
        if st is None:   # 防御：call 事件被丢（异常路径）时结果行不失踪
            first = re.sub(r"[`*#]", "", (full_line or "").split("\n")[0]).strip()
            st = self._pending_step(first[:60] or "操作步骤")
        now = time.monotonic()
        st["done"] = True
        st["short"] = short
        st["gap"] = round(now - st["anchor"], 1)   # 上一步结果→本步结果：覆盖思考+生成+执行
        self._last_step_end = now
        self._version += 1
        await self._render_proc()
        log.info("飞书 tool 完成：%s · 距上一步 %.1fs", line.split("\n")[0], st["gap"])

    async def on_done(self, answer: str | None = None,
                      citations: list | None = None) -> None:
        # 防卡死铁律：先收尾清单 → flush 末批 → 关流式 + 换 summary（卡片不停在"生成中"）
        # → 整卡重建折叠（思考全文+步骤折进默认收起面板，答案在外，参考来源平铺）。
        # 重建失败重试一次仍败 → 兜底 acontent 答案 + 追加参考来源。
        if answer and answer.strip():
            self._answer = answer
        log.info("飞书 done：answer=%d 字符，思考=%d 字符，过程=%d 步，参考=%d 条%s",
                 len(self._answer), len(self._reasoning), len(self._tool_lines), len(citations or []),
                 "（本次有流式写失败，重建兜底）" if self._dirty else "")
        self._finalize()
        await self._cancel_and_flush()
        await self._close_streaming(self._answer)
        ok = await self._update_card_full(
            card.build_final_card(self._tool_lines, self._answer, citations,
                                  reasoning=self._reasoning))
        if ok:
            return
        log.warning("飞书 done 全量重建失败，兜底 acontent 答案（过程保留流式态展开）")
        await self._stream_text(card.ANSWER_EID, self._answer)
        if citations:
            await self._append_elements([card._citations_element(citations)])

    async def on_clarify(self, question: str, options, sid: str) -> None:
        # 澄清挂起也是终态：整卡折叠，问题+选项展示在答案区
        self._finalize()
        await self._cancel_and_flush()
        opts = " / ".join((o.get("label") if isinstance(o, dict) else str(o)) for o in (options or []))
        tip = f"\n\n_请直接回复你的选择（{opts}）_" if opts else ""
        prefix = (self._answer + "\n\n") if self._answer else ""
        text = prefix + f"**需要确认**：{question}{tip}"
        ok = await self._update_card_full(
            card.build_final_card(self._tool_lines, text, reasoning=self._reasoning))
        if not ok:   # 重建失败退回流式态：答案区写文本 + 关流式
            await self._stream_text(card.ANSWER_EID, text)
            await self._close_streaming(question)

    async def on_error(self, msg: str) -> None:
        # 错误也是终态：整卡折叠（过程收进面板），错误文案在答案区——流式通道被平台
        # 超时杀死时 acontent 会丢，整卡重建不依赖流式通道，保证错误一定上卡
        self._answer = f"**错误**：{msg}"
        self._finalize()
        await self._cancel_and_flush()
        ok = await self._update_card_full(
            card.build_final_card(self._tool_lines, self._answer, reasoning=self._reasoning))
        if not ok:
            await self._close_streaming(self._answer)

    # ---- 思考行 + 渲染（无定时器：思考行是静态文本，"会动"的只有 SQL 逐段长出） ----
    def _spawn(self, coro):
        """fire-and-forget 任务必须持引用——裸 task 可能被 GC 中途蒸发。完成后自动摘除。"""
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def _finalize(self) -> None:
        """终态：撤思考行、停节流任务；清单残留进行中步骤标「未完成」。"""
        self._finalized = True
        self._version += 1
        for st in self._steps:
            if not st["done"]:
                st["done"] = True
                st["short"] = "未完成"

    async def _render_proc(self) -> None:
        """渲染执行过程清单。进锁前快照版本号，锁内比对——过期渲染丢弃，防旧内容覆盖新内容。"""
        stamp = self._version
        content = card.progress_markdown(self._steps)
        async with self._api_lock:
            if stamp != self._version or self._finalized:
                return
            await self._send_element(card.PROC_EID, content)

    # ---- 节流（streaming 不限 QPS，节流省 HTTP 往返） ----
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
        if not self._answer:
            return
        await self._stream_text(card.ANSWER_EID, self._answer)
        self._last_flush = time.monotonic()

    # ---- cardkit 调用（全部过 _api_lock 串行，防 seq 乱序） ----
    async def _send_element(self, element_id: str, content: str) -> None:
        """acontent 更新元素（调用方必须已持 _api_lock）。失败置 _dirty。"""
        from lark_oapi.api.cardkit.v1 import ContentCardElementRequest, ContentCardElementRequestBody
        req = (ContentCardElementRequest.builder()
               .card_id(self._card_id).element_id(element_id)
               .request_body(ContentCardElementRequestBody.builder()
                             .content(content).sequence(self._next_seq()).build()).build())
        try:
            resp = await self._lark.cardkit.v1.card_element.acontent(req)
            if not resp.success():
                self._dirty = True
                log.warning("飞书流式文本失败 card=%s eid=%s code=%s msg=%s",
                            self._card_id, element_id, resp.code, resp.msg)
        except Exception as e:
            self._dirty = True
            log.warning("飞书流式文本异常/超时 card=%s eid=%s: %s", self._card_id, element_id, e)

    async def _stream_text(self, element_id: str, content: str) -> None:
        """流式更新元素内容（acontent）：传全量，平台打字机。"""
        async with self._api_lock:
            await self._send_element(element_id, content)

    async def _append_elements(self, elements: list[dict]) -> bool:
        """往卡片末尾追加元素（card_element/create type=append）。失败仅丢追加内容。"""
        from lark_oapi.api.cardkit.v1 import CreateCardElementRequest, CreateCardElementRequestBody
        async with self._api_lock:
            req = (CreateCardElementRequest.builder().card_id(self._card_id)
                   .request_body(CreateCardElementRequestBody.builder()
                                 .type("append")
                                 .elements(json.dumps(elements, ensure_ascii=False))
                                 .sequence(self._next_seq()).build()).build())
            try:
                resp = await self._lark.cardkit.v1.card_element.acreate(req)
                if not resp.success():
                    log.warning("飞书追加元素失败 card=%s code=%s msg=%s", self._card_id, resp.code, resp.msg)
                    return False
                return True
            except Exception as e:
                log.warning("飞书追加元素异常 card=%s: %s", self._card_id, e)
                return False

    async def _close_streaming(self, answer: str | None = None) -> None:
        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody
        async with self._api_lock:
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
        """全量替换卡片（card.update）：终态折叠重建用。失败隔 1s 重试 1 次（网关瞬时抖动）；
        两次失败返回 False（调用方走流式态兜底）。"""
        from lark_oapi.api.cardkit.v1 import Card, UpdateCardRequest, UpdateCardRequestBody
        card_obj = Card.builder().type("card_json").data(
            json.dumps(card_json, ensure_ascii=False)).build()
        for attempt in (1, 2):
            async with self._api_lock:
                req = (UpdateCardRequest.builder().card_id(self._card_id)
                       .request_body(UpdateCardRequestBody.builder().card(card_obj)
                                     .sequence(self._next_seq()).build()).build())
                try:
                    resp = await self._lark.cardkit.v1.card.aupdate(req)
                    if resp.success():
                        return True
                    log.warning("飞书全量更新卡片失败(%d/2) card=%s code=%s msg=%s",
                                attempt, self._card_id, resp.code, resp.msg)
                except Exception as e:
                    log.warning("飞书全量更新异常/超时(%d/2) card=%s: %s", attempt, self._card_id, e)
            if attempt == 1:
                await asyncio.sleep(1)
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
                elif t == "notice":
                    stream.on_notice(evt.data.get("text", ""))
                elif t == "tool_call":
                    item = stream.begin_tool(evt.data.get("name", ""), evt.data.get("args"))
                    if item:
                        await stream.on_tool_pending(*item)
                elif t == "tool_result":
                    item = stream.end_tool(evt.data.get("name", ""), evt.data.get("summary", ""))
                    if item:
                        await stream.on_tool_done(*item)
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


def _step_title_call(name, args) -> str:
    """工具调用 → 清单动作名（流式清单步骤行用；SQL/预览全文在步骤的 body 代码块）。"""
    if name == "execute_sql":
        return "执行查询"
    if name == "query_metadata":
        return "查询元数据"
    if name == "knowledge_search":
        q = (args or {}).get("query", "").strip()
        return f"检索知识库：{q[:30]}" if q else "检索知识库"
    if name == "get_sql_template":
        return "取 SQL 样板"
    return f"调用 {name or '工具'}"


def _step_title_result(name, summary) -> str:
    """工具结果 → 步骤行结果摘要（拼「N. 动作 · 摘要 · 距上一步Ns」）。"""
    if name == "execute_sql":
        data = _parse_sql_summary(summary)
        if data is None:
            return "失败"
        if data["rows"] == 0:
            return "无匹配数据"
        return f"返回 {data['rows']} 行"
    if name == "query_metadata":
        tables = _extract_tables(summary)
        return f"{len(tables)} 张表" if tables else "完成"
    if name == "knowledge_search":
        hits = _extract_kb_hits(summary)
        if hits is None or not hits:
            return "无匹配"
        return f"命中 {len(hits)} 段"
    return "完成"


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

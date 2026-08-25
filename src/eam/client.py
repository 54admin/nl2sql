"""EAM 文档管理客户端（只读同步源，调资产哨兵 EAM 平台）。

对接背景与接口实测记录见 docs/知识库管理设计.md 第 7 章（2026-08-25 逆向+实测）：
  网关:  {base_url}（默认 https://api.gw-greenenergy.com）
  鉴权:  Authorization: Basic <base64(ak:sk)>  且 loginToken 头【必须存在】（空值即可，
         缺失则服务端「系统异常」——实测踩坑）。ak/sk 平台签发长期凭证，走 yml 不入库不进前端。
  POST /annex/folder/tree       body {}                          → 全量目录树
  POST /annex/file/fileList     body {}                          → 全量文件清单（parentId 挂树还原路径）
  POST /annex/file/batch/downLoad  body {fileDownLoadList, fileName} → 文件流

只读红线：本客户端仅封装上述 3 个读接口；EAM 的上传/移动/删除等写接口一概不碰。
"""
from __future__ import annotations

import httpx

from src.config import EamConfig
from src.logging import get_logger
from src.ragflow.client import get_http_client

log = get_logger(__name__)

DOWNLOAD_TIMEOUT = 300.0   # 大文件下载独立超时（不复用默认 30s）


class EamError(RuntimeError):
    """EAM 调用失败（含未配置/HTTP错误/错误响应）。"""


class EamClient:
    def __init__(self, cfg: EamConfig):
        self._cfg = cfg

    # ---------- 底层 ----------
    def _require(self) -> EamConfig:
        if not self._cfg.ready:
            raise EamError("EAM 未配置：请在 yml eam 段填 base_url 与 auth_basic（base64(ak:sk)）。")
        return self._cfg

    def _headers(self) -> dict:
        # loginToken 空值头必须存在（实测：缺失 → 服务端「系统异常」）
        return {"Authorization": f"Basic {self._cfg.auth_basic}", "loginToken": ""}

    async def _post_json(self, path: str, body: dict) -> dict:
        cfg = self._require()
        url = f"{cfg.base_url.rstrip('/')}{path}"
        try:
            resp = await get_http_client().post(url, headers=self._headers(), json=body)
        except httpx.HTTPError as e:
            raise EamError(f"EAM 连接失败：{e}") from e
        if resp.status_code in (401, 403):
            raise EamError(f"EAM 鉴权失败（HTTP {resp.status_code}）: {resp.text[:150]}——检查 ak/sk 是否有效")
        if resp.status_code >= 400:
            raise EamError(f"EAM HTTP {resp.status_code}: {resp.text[:150]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise EamError(f"EAM 响应非 JSON: {resp.text[:150]}") from e
        if data.get("statusCode") != "1000":
            raise EamError(f"EAM 错误 {data.get('statusCode')}/{data.get('name', '')}: "
                           f"{data.get('message', '')[:150]}")
        return data

    # ---------- 三个只读能力 ----------
    async def tree(self) -> list[dict]:
        """全量目录树。节点：id / parentId / name / children。"""
        data = await self._post_json(self._cfg.tree_api, {})
        return data.get("data") or []

    async def files(self) -> list[dict]:
        """全量文件清单（实测 2030 条一次返回）。文件项含 parentId（挂树）/ fileId /
        name / fileType / size / version——fileType 用于前端格式过滤（RAGFlow 支持集）。"""
        data = await self._post_json(self._cfg.files_api, {})
        return data.get("data") or []

    async def download(self, doc_id: str, document_type: str = "file",
                       filename: str = "download") -> bytes:
        """下载文件内容（文件流）。documentType 实测传 "file"。"""
        cfg = self._require()
        url = f"{cfg.base_url.rstrip('/')}{cfg.download_api}"
        body = {"fileDownLoadList": [{"id": doc_id, "documentType": document_type}],
                "fileName": filename}
        try:
            resp = await get_http_client().post(
                url, headers=self._headers(), json=body, timeout=DOWNLOAD_TIMEOUT)
        except httpx.HTTPError as e:
            raise EamError(f"EAM 下载失败：{e}") from e
        ct = resp.headers.get("content-type", "")
        if resp.status_code >= 400 or "json" in ct:
            # 错误响应是 JSON（正常下载是文件流），读出错误信息
            raise EamError(f"EAM 下载错误 HTTP {resp.status_code}: {resp.text[:150]}")
        if not resp.content:
            raise EamError("EAM 下载内容为空")
        return resp.content

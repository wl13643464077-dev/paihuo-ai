"""RunningHub 数字人引擎(V13:基础版/省钱版).

工作流原理:LoadImage(照片,node12) + RH_LoadAudio(音频,node9) → RH_VideoSynthesis(node8)
→ 会说话的视频。调用:上传照片→上传音频→nodeInfoList 覆盖 node12/node9(+node8画质)→
发起任务→轮询 SUCCESS→取输出视频 URL。key/workflowId/画质在管理后台可配。
"""
import asyncio
import logging
import os

import httpx

from . import db, netfetch, secureconfig

log = logging.getLogger("runninghub")
BASE = "https://www.runninghub.cn"
DEFAULT_WORKFLOW = "1986772059139289089"


def conf():
    return (secureconfig.get_secret("runninghub_key"),
            db.get_setting("runninghub_workflow") or DEFAULT_WORKFLOW)


def ready() -> bool:
    return bool(secureconfig.get_secret("runninghub_key"))


class RHError(Exception):
    pass


async def _upload(cli, key, path, ftype) -> str:
    with open(path, "rb") as f:
        r = await cli.post(f"{BASE}/task/openapi/upload",
                           data={"apiKey": key, "fileType": ftype},
                           files={"file": (os.path.basename(path), f)})
    d = r.json()
    if d.get("code") != 0:
        raise RHError("RunningHub 素材上传失败，请稍后重试")
    return d["data"]["fileName"]


async def _create(cli, key, wf, img, aud):
    # 真实工作流(WanVideo InfiniteTalk):472=图片, 474/484=音频
    # instanceType=plus:更大显存机型(14B模型默认机型会 OOM)
    nodes = [{"nodeId": "472", "fieldName": "image", "fieldValue": img},
             {"nodeId": "474", "fieldName": "audio", "fieldValue": aud},
             {"nodeId": "484", "fieldName": "audio", "fieldValue": aud}]
    body = {"apiKey": key, "workflowId": wf, "nodeInfoList": nodes,
            "instanceType": await db.aget_setting("runninghub_instance") or "plus"}
    r = await cli.post(f"{BASE}/task/openapi/create", json=body)
    return r.json()


async def synth(photo_path: str, audio_path: str, progress) -> str:
    """照片+音频 → RunningHub 数字人视频 URL(高画质)."""
    key, wf = await db.arun(conf)
    if not key:
        raise RHError("未配置 RunningHub key(管理后台→数字人引擎)")
    async with httpx.AsyncClient(timeout=180) as cli:
        progress("start", "基础版(RunningHub):上传照片…")
        img = await _upload(cli, key, photo_path, "image")
        progress("start", "上传音频…")
        aud = await _upload(cli, key, audio_path, "audio")
        progress("start", "提交工作流(WanVideo InfiniteTalk 高清数字人)…")
        d = await _create(cli, key, wf, img, aud)
        # 队列满(同时只能跑1条)则排队等待重试,最多等 15 分钟
        for _ in range(30):
            if d.get("code") == 0:
                break
            if "QUEUE_MAXED" in str(d.get("msg", "")) + str(d.get("data", "")):
                progress("tool", "渲染队列忙,排队中…30秒后重试")
                await asyncio.sleep(30)
                d = await _create(cli, key, wf, img, aud)
            else:
                raise RHError("RunningHub 任务提交失败，请稍后重试")
        if d.get("code") != 0:
            raise RHError("RunningHub 任务提交失败，请稍后重试")
        tid = d["data"]["taskId"]
        progress("tool", f"任务已受理 {str(tid)[:14]}…,开始合成")
        for i in range(90):  # 最多 ~22 分钟
            await asyncio.sleep(15)
            rs = await cli.post(f"{BASE}/task/openapi/status",
                                json={"apiKey": key, "taskId": tid})
            st = (rs.json() or {}).get("data", "")
            if st == "SUCCESS":
                break
            if st == "FAILED":
                raise RHError("工作流执行失败")
            safe_state = st if st in {"RUNNING", "QUEUED", "WAITING"} else "排队"
            progress("tool", f"合成中({safe_state})…已等 {(i + 1) * 15}s")
        else:
            raise RHError("合成超时")
        ro = await cli.post(f"{BASE}/task/openapi/outputs",
                            json={"apiKey": key, "taskId": tid})
        od = ro.json()
        urls = []
        for o in (od.get("data") or []):
            u = o.get("fileUrl") or o.get("url") or (o if isinstance(o, str) else "")
            if isinstance(u, str) and u.startswith("http"):
                urls.append(u)
        # 工作流有音频+视频多个输出,只要视频(否则会误取到 flac 音频)
        vid = next((u for u in urls if any(x in u.lower()
                    for x in (".mp4", ".mov", ".webm", "video"))), None)
        if vid:
            try:
                await netfetch.guard_public_url(vid)
            except ValueError as exc:
                raise RHError("成片地址未通过公网安全校验") from exc
            return vid
        raise RHError("RunningHub 未返回可用成片地址，请稍后重试")


# ---------------- 文本驱动数字人（内容流水线 v3.1 · 03-digital-human） ----------------
# RunningHub 应用「数字人-文本驱动」：照片 + 口播文本 + 声音样本(<30 秒) + 情绪提示 → 本人声音口播视频。
# 不需要先配音，声音就是老板本人；节点与调用方式照搬 脚本/skill-scripts/generate_digital_human.py。
API_V2 = "https://www.runninghub.cn/openapi/v2"
TEXT_APP = "1961397507056144386"
TEXT_MAX_CHARS = 1000

EMOTION_PROMPTS = {
    "serious": "严肃警醒、审慎可信；开头有力度，正文冷静清晰，涉及赔付或损失时保持克制。",
    "caution": "开头警醒、有冲击力；主体专业坚定、清晰克制，关键风险和行动建议适度加重。",
    "analytical": "开头用有力的反问抓住注意；主体理性清晰、像和老板算账，关键结论坚定。",
    "upbeat": "开心、略兴奋、积极有感染力；开头明快抓人，正文清楚自然，重点句适度强调。",
    "engaging": "开头好奇、略兴奋，有感染力；主体积极明快、专业清晰，重点句适度强调。",
}
EMOTION_CUES = {
    "serious": ("赔付", "谁来赔", "保险", "事故", "伤亡", "人身伤害", "损失", "泄露", "诈骗", "违法", "违规", "罚款", "诉讼"),
    "caution": ("授权", "权限", "审批", "人审", "查重", "入库", "复核", "作废", "旧版", "旧报价", "密码", "责任", "拍板", "风险", "错误", "报错"),
    "analytical": ("财务", "利润", "报表", "月报", "口径", "算账", "成本", "返工", "提效", "增利"),
    "upbeat": ("好消息", "突破", "成功", "惊喜", "增长", "机会", "上线", "发布", "省事", "更快", "更省"),
}


def text_app_id() -> str:
    return db.get_setting("runninghub_text_app") or TEXT_APP


def choose_emotion(text: str) -> dict:
    """按口播正文选一个主情绪（风险/损失类不默认兴奋）。"""
    hits = {k: [c for c in cues if c in text] for k, cues in EMOTION_CUES.items()}
    if hits["serious"]:
        label = "serious"
    elif len(hits["caution"]) >= 2:
        label = "caution"
    elif hits["analytical"]:
        label = "analytical"
    elif hits["caution"]:
        label = "caution"
    elif hits["upbeat"]:
        label = "upbeat"
    else:
        label = "engaging"
    return {"label": label, "prompt": EMOTION_PROMPTS[label]}


def text_nodes(text: str, image_name: str, audio_name: str, ratio: str, emotion: str) -> list[dict]:
    return [
        {"nodeId": "126", "fieldName": "image", "fieldValue": image_name},
        {"nodeId": "127", "fieldName": "aspect_ratio", "fieldValue": ratio},
        {"nodeId": "184", "fieldName": "text", "fieldValue": text},
        {"nodeId": "217", "fieldName": "select", "fieldValue": "11"},
        {"nodeId": "185", "fieldName": "audio", "fieldValue": audio_name},
        {"nodeId": "249", "fieldName": "prompt", "fieldValue": emotion},
    ]


async def _v2(cli, key: str, path: str, *, json_body=None, file_path=None) -> dict:
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if file_path:
        # 照搬 generate_digital_human.py：上传必须带明确的文件类型，否则工作流的加载节点读不出来
        ext = os.path.splitext(file_path)[1].lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4"}.get(ext, "application/octet-stream")
        with open(file_path, "rb") as fh:
            r = await cli.post(f"{API_V2}{path}", headers=headers,
                               files={"file": (os.path.basename(file_path), fh.read(), mime)})
    else:
        r = await cli.post(f"{API_V2}{path}", headers=headers, json=json_body or {})
    try:
        d = r.json()
    except ValueError:
        raise RHError("RunningHub 返回异常，请稍后重试") from None
    if not isinstance(d, dict) or d.get("code") not in (None, 0, "0"):
        log.warning("runninghub v2 %s rejected code=%s", path, (d or {}).get("code") if isinstance(d, dict) else "?")
        raise RHError("RunningHub 拒绝了请求，请稍后重试")
    return d.get("data") if isinstance(d.get("data"), dict) else d


async def synth_text(photo_path: str, text: str, voice_sample_path: str, progress,
                     ratio: str = "3:4") -> str:
    """照片 + 口播文本 + 声音样本 → 本人声音的数字人视频 URL（文本驱动应用）。"""
    key = await db.arun(secureconfig.get_secret, "runninghub_key")
    if not key:
        raise RHError("未配置 RunningHub key(管理后台→数字人引擎)")
    text = " ".join(str(text or "").split())
    if not text:
        raise RHError("口播稿为空")
    if len(text) > TEXT_MAX_CHARS:
        raise RHError(f"口播稿超过 {TEXT_MAX_CHARS} 字，请精简")
    emotion = choose_emotion(text)
    app = await db.arun(text_app_id)
    async with httpx.AsyncClient(timeout=180) as cli:
        progress("start", "RunningHub 文本驱动：上传照片和声音样本…")
        img = (await _v2(cli, key, "/media/upload/binary", file_path=photo_path)).get("fileName")
        aud = (await _v2(cli, key, "/media/upload/binary", file_path=voice_sample_path)).get("fileName")
        if not img or not aud:
            raise RHError("RunningHub 素材上传失败，请稍后重试")
        progress("start", f"提交数字人（本人声音 · 情绪：{emotion['label']}）…")
        body = {"nodeInfoList": text_nodes(text, img, aud, ratio, emotion["prompt"]),
                "instanceType": await db.aget_setting("runninghub_instance") or "plus"}
        info = None
        for _ in range(30):  # 队列满时排队重试，最多 15 分钟
            try:
                info = await _v2(cli, key, f"/run/ai-app/{app}", json_body=body)
                break
            except RHError:
                progress("tool", "渲染队列忙,排队中…30秒后重试")
                await asyncio.sleep(30)
        if not info or not info.get("taskId"):
            raise RHError("RunningHub 任务提交失败，请稍后重试")
        tid = info["taskId"]
        log.info("runninghub text task submitted %s", tid)
        progress("tool", f"任务已受理 {str(tid)[:14]}…,开始合成（约 10-20 分钟）")
        failures = 0
        for i in range(120):  # 最多 30 分钟
            await asyncio.sleep(15)
            try:
                q = await _v2(cli, key, "/query", json_body={"taskId": tid})
                failures = 0
            except (RHError, httpx.HTTPError):
                failures += 1
                if failures >= 5:
                    raise RHError("RunningHub 查询多次失败，请稍后重试") from None
                continue
            st = str(q.get("status") or "").upper()
            if st == "SUCCESS":
                urls = [r.get("url") for r in (q.get("results") or [])
                        if isinstance(r, dict) and isinstance(r.get("url"), str)]
                vid = next((u for u in urls if any(x in u.lower() for x in (".mp4", ".mov", ".webm", "video"))),
                           urls[0] if len(urls) == 1 else None)
                if not vid:
                    raise RHError("RunningHub 未返回可用成片地址，请稍后重试")
                try:
                    await netfetch.guard_public_url(vid)
                except ValueError as exc:
                    raise RHError("成片地址未通过公网安全校验") from exc
                return vid
            if st in {"FAILED", "CANCELLED", "CANCELED"}:
                reason = " | ".join(str(q.get(k))[:200] for k in ("errorCode", "errorMessage", "failedReason", "promptTips")
                                    if q.get(k))
                log.warning("runninghub text task %s %s reason=%s", tid, st, reason or "(none)")
                raise RHError("数字人合成失败（RunningHub），点数已退回")
            safe = st if st in {"RUNNING", "QUEUED", "WAITING", "PENDING"} else "排队"
            progress("tool", f"合成中({safe})…已等 {(i + 1) * 15}s")
        raise RHError("合成超时")

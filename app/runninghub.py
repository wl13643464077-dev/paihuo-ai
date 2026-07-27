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
    key, wf = conf()
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

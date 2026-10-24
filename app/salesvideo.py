"""AI 带货视频（移植自纳米Work企业版「带货员」高阶通道）。

一张产品/门店照片 + 卖点 → 15 秒竖屏带货短片：
- 默认豆包 Seedance 2.5 图生视频，720p，模型原生生成口播与环境音；
- Seedance 拒绝「疑似真人」参考图时，同一分段自动改走可灵 v3 pro（有声）。
两条通道各只提交一次 POST，任务失败绝不自动重复提交同一模型（避免重复扣费）。
统一走管理后台配置的云雾网关（与数字人可灵通道同一个 base/key）。
"""
from __future__ import annotations

import asyncio
import base64
import re
import time

import httpx

from . import db, providers

SEEDANCE_MODEL = "doubao-seedance-2-5-260628"
KLING_MODEL = "kling-v3"
SEEDANCE_TASKS = "/api/v3/contents/generations/tasks"
KLING_TASKS = "/kling/v1/videos/image2video"
DURATION = 15
MAX_LINE_CHARS = 60
REAL_PERSON = re.compile(
    r"real person|真人|人脸|portrait rights|InputImageSensitiveContentDetected", re.I)
_MIME = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg", b"RIFF": "image/webp"}


class RealPersonRejected(providers.ProviderError):
    """Seedance 认为参考图是真人，改走可灵。"""


def prepare_image(data: bytes, max_side: int = 1280) -> bytes:
    """参考图统一转 JPEG、长边≤1280，避免把几 MB 原图塞进请求体。"""
    import io
    from PIL import Image, ImageOps
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.draft("RGB", (max_side, max_side))
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (OSError, ValueError) as exc:
        raise providers.ProviderError("参考图无法识别，请换一张 PNG/JPG") from exc
    image.thumbnail((max_side, max_side))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def image_data_uri(data: bytes) -> str:
    mime = next((m for magic, m in _MIME.items() if data.startswith(magic)), None)
    if not mime:
        raise providers.ProviderError("参考图只支持 PNG / JPG / WebP")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def build_prompt(params: dict, line: str) -> str:
    """把产品、卖点、场景和口播台词写成一条视频提示词；台词要求逐字说出。"""
    product = str(params.get("product") or "").strip()[:40]
    points = str(params.get("selling_points") or "").strip()[:160]
    scene = str(params.get("scene") or "").strip()[:80] or "明亮真实的门店/餐桌环境"
    return (
        f"竖屏 9:16 带货短视频，时长 {DURATION} 秒。主体：参考图中的「{product or '产品'}」，"
        f"保持产品外观、颜色、包装与参考图一致，不改形状不加 logo。场景：{scene}。"
        f"镜头：开头 2 秒特写制造食欲/质感冲击，中段缓慢推近展示细节"
        f"{('（突出：' + points + '）') if points else ''}，结尾稳定定格。"
        f"旁白用自然的普通话口语，逐字说出：「{line}」。"
        "画面里不要出现任何文字、字幕、水印、二维码、价格牌；不要变形、闪烁。"
    )


def seedance_request(prompt: str, image_uri: str) -> dict:
    return {"model": SEEDANCE_MODEL,
            "content": [{"type": "text", "text": prompt},
                        {"type": "image_url", "role": "first_frame",
                         "image_url": {"url": image_uri}}],
            "ratio": "adaptive", "duration": DURATION, "resolution": "720p",
            "generate_audio": True, "watermark": False}


def kling_request(prompt: str, image_uri: str) -> dict:
    return {"model_name": KLING_MODEL, "image": image_uri.split(",", 1)[1],
            "prompt": prompt[:2400],
            "negative_prompt": "画面文字，水印，变形，闪烁，人物面部扭曲",
            "duration": str(DURATION), "mode": "pro", "sound": "on"}


async def write_line(params: dict, brand_context: str = "") -> str:
    """老板没写台词时，让撰稿人写一句 15 秒口播（≤60 字，口语，不编价格）。"""
    given = re.sub(r"\s+", "", str(params.get("script") or ""))
    if given:
        return given[:MAX_LINE_CHARS]
    prompt = (
        (f"【本企业资料（确认版优先，不得编造价格/活动）】\n{brand_context[:1500]}\n\n"
         if brand_context else "")
        + f"给「{params.get('product') or '这款产品'}」写一句 15 秒带货短视频的口播台词，"
        f"卖点：{params.get('selling_points') or '（自己从产品名推断，不要编数据）'}。"
        "要求：35-55 个字，口语化，第一句就是让人想吃/想买的画面感，结尾一句轻号召"
        "（如「路过记得来尝」），不写价格、折扣、联系方式、网址。只输出台词本身。"
    )
    result = await providers.call_text(3, prompt, timeout=120, max_tokens=300)
    line = re.sub(r"\s+", "", str(result.get("text") or "")).strip("「」\"“”")
    if len(line) < 10:
        raise providers.ProviderError("台词生成失败，请手动填写一句口播")
    return line[:MAX_LINE_CHARS]


async def _post(base, key, path, body, timeout=120) -> dict:
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as cli:
        response = await cli.post(f"{base}{path}", json=body,
                                  headers={"Authorization": f"Bearer {key}"})
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        message = str((data.get("error") or {}).get("message") if isinstance(data.get("error"), dict)
                      else data.get("message") or data.get("error") or "")
        if REAL_PERSON.search(message):
            raise RealPersonRejected("参考图疑似真人")
        raise providers.ProviderError("带货视频任务提交失败，请稍后重试")
    return data


async def submit(prompt: str, image_uri: str, progress) -> tuple[str, str]:
    """先 Seedance；真人被拒才改可灵。返回 (通道, task_id)。"""
    base, key = await db.arun(providers.yunwu_conf)
    try:
        data = await _post(base, key, SEEDANCE_TASKS, seedance_request(prompt, image_uri))
        task_id = str(data.get("id") or data.get("task_id") or "")
        if not task_id:
            raise providers.ProviderError("Seedance 未返回任务编号")
        return "seedance", task_id
    except RealPersonRejected:
        progress("retry", "参考图里有真人，Seedance 不接，自动改走可灵 3.0 出片…")
    data = await _post(base, key, KLING_TASKS, kling_request(prompt, image_uri))
    task_id = str((data.get("data") or {}).get("task_id") or "")
    if not task_id:
        raise providers.ProviderError("可灵未返回任务编号")
    return "kling", task_id


async def poll(channel: str, task_id: str, progress, *, cancelled=None,
               timeout: int = 1500, interval: float = 12) -> str:
    """轮询直到成片，返回视频 URL；只查询，不重复提交。"""
    base, key = await db.arun(providers.yunwu_conf)
    path = SEEDANCE_TASKS if channel == "seedance" else KLING_TASKS
    started = time.time()
    async with httpx.AsyncClient(timeout=60, trust_env=False) as cli:
        while time.time() - started < timeout:
            await asyncio.sleep(interval)
            if cancelled:
                await cancelled()  # 调用方在已取消时自行抛出取消异常
            try:
                response = await cli.get(f"{base}{path}/{task_id}",
                                         headers={"Authorization": f"Bearer {key}"})
                data = response.json()
            except (httpx.HTTPError, ValueError):
                continue
            if channel == "seedance":
                status = str(data.get("status") or "").lower()
                url = (data.get("content") or {}).get("video_url")
                if status == "succeeded" and url:
                    return url
                if status in ("failed", "cancelled", "expired"):
                    raise providers.ProviderError("带货视频生成失败，请稍后重试")
            else:
                task = data.get("data") or {}
                status = str(task.get("task_status") or "")
                videos = (task.get("task_result") or {}).get("videos") or []
                if status == "succeed" and videos and videos[0].get("url"):
                    return videos[0]["url"]
                if status in ("failed", "error"):
                    raise providers.ProviderError("带货视频生成失败，请稍后重试")
            progress("tool", f"带货视频渲染中（{'Seedance 2.5' if channel == 'seedance' else '可灵 3.0'}）"
                             f"…已等 {int(time.time() - started)}s")
    raise providers.ProviderError("带货视频渲染超时（25分钟）")

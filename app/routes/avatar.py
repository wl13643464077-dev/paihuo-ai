"""数字人摄影棚的 HTTP 路由(第 3 期从 main.py 机械拆分，函数体未改)。

生成/克隆等业务在 app/avatar.py；启动段的 avatar.resume_pending 仍在 main.py。
上传白名单仍登记在 main.py。不 import main.py。
"""


import asyncio
import json
import logging
import os
import shutil
import tempfile
import time

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import auth, avatar, billing, db, features, llm, secureconfig
from ..engine import engine
from ..web_common import (
    TEN, _AVATAR_UPLOAD_MAX_BYTES, _assert_persistent_upload_capacity, _is_boss, _need_admin,
    _need_module, _page_result, _pagination, _persistent_upload_slot, _profile_id_for_tenant,
    _public_failure_for_view, _read_limited, _run_db_safely, _run_db_then_start_worker_safely,
    _start_billed_operation, _start_billing_operation_safely, _steps_for_view,
)


log = logging.getLogger("main")  # 与拆分前同名，日志检索口径不变
router = APIRouter()


# ---------------- V6:数字人摄影棚 ----------------


def _avatar_asset_name(raw, field: str, kinds: set[str], required: bool = True):
    """只接受当前租户已上传登记的 UUID 素材名，并在扣点前完成校验。"""
    from .. import providers as _providers
    name = (raw or "").strip()
    if not name:
        if required:
            raise HTTPException(400, f"{field} 必填")
        return None
    if name != os.path.basename(name) or not avatar.asset_belongs(name, kinds, TEN()):
        raise HTTPException(400, f"{field} 不是当前企业的有效已上传素材")
    try:
        avatar.asset_path(name, kinds, TEN())
    except _providers.ProviderError as e:
        raise HTTPException(400, str(e)) from e
    return name


def _prepare_avatar_clone_sample(raw_name, tid: int) -> str:
    """Validate and privately copy a voice sample under the asset registry lock."""
    sample_descriptor = -1
    sample_path = ""
    with avatar.asset_library_lock(tid):
        name = _avatar_asset_name(raw_name, "audio_name", {"voice"})
        source_path = avatar.asset_path(name, {"voice"}, tid)
        suffix = os.path.splitext(name)[1].lower()
        sample_descriptor, sample_path = tempfile.mkstemp(
            prefix=".avatar-clone-",
            suffix=suffix,
        )
        try:
            os.fchmod(sample_descriptor, 0o600)
            with os.fdopen(sample_descriptor, "wb") as target:
                sample_descriptor = -1
                with open(source_path, "rb") as source:
                    shutil.copyfileobj(source, target, length=1 << 20)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            if sample_descriptor >= 0:
                os.close(sample_descriptor)
            try:
                os.remove(sample_path)
            except OSError:
                pass
            raise
    return sample_path


def _cleanup_avatar_clone_sample(sample_path: str, tid: int) -> None:
    try:
        os.remove(sample_path)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("voice clone work sample cleanup failed tenant=%s", tid)


async def _prepare_avatar_clone_sample_safely(
    raw_name,
    tid: int,
) -> str:
    """Copy the clone sample without leaking it when the request is cancelled."""
    copy_task = asyncio.create_task(
        asyncio.to_thread(_prepare_avatar_clone_sample, raw_name, tid)
    )
    try:
        return await asyncio.shield(copy_task)
    except asyncio.CancelledError:
        sample_path = ""
        try:
            sample_path = await copy_task
        except BaseException:
            pass
        if sample_path:
            cleanup_task = asyncio.create_task(
                asyncio.to_thread(
                    _cleanup_avatar_clone_sample,
                    sample_path,
                    tid,
                )
            )
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
        raise


@router.get("/api/avatar/meta")
def avatar_meta():
    _need_module("avatar")
    eng = avatar.engine_name()
    return {"voices": avatar.cloned_voices() + avatar.VOICES,
            "engines": ([{"key": "basic", "label": "基础版·省钱(6点/条,不限时长)"}]
                        if avatar.rh_ready() else [])
                       + [{"key": "", "label": f"自动(当前:{'HeyGen·境外' if eng=='heygen' else '可灵'})"},
                          {"key": "heygen", "label": "HeyGen(会动·快 · 境外服务商)", "overseas": True},
                          {"key": "kling", "label": "可灵(对口型 · 国内)"}],
            "durations": [{"s": 15, "label": "15秒(快闪)"}, {"s": 30, "label": "30秒(标准)"},
                          {"s": 60, "label": "60秒(深度)"}],
            "public_base": avatar.public_base(),
            "engine": eng, "heygen_ready": bool(
                secureconfig.get_secret("heygen_key")
            ),
            "heygen_exhausted": bool(db.get_setting("heygen_exhausted")),
            "own_voice_ready": True,
            # 第3期:肖像/声音授权声明 + 境外传输告知 + 成片 AI 标识
            "consent": {"version": avatar.CONSENT_VERSION, "text": avatar.CONSENT_TEXT,
                        "overseas_text": avatar.OVERSEAS_TEXT},
            "ai_label": features.ai_label_for(TEN()),
            "link_video_enabled": features.is_enabled("linkgrab_video"),
            "link_video_hint": features.off_hint("linkgrab_video"),
            "engine_note": ("可灵引擎 · 照片对口型出片(系统音色/克隆音色/您的原声都支持)"
                            if eng == "kling" else
                            "HeyGen · Avatar IV 动作引擎(人物会动会说)")}


async def _avatar_script_from_link_work(body: dict, url: str, dur: int) -> dict:
    """已鉴权、已计费后的链接提取工作体。"""
    from .. import linkgrab
    style = (body.get("style") or "").strip()
    persona_txt = ""
    if body.get("profile_id"):
        p = await db.aone(
            "SELECT * FROM account_profile WHERE id=? AND tenant_id=? "
            "AND deleted_at IS NULL",
            (body["profile_id"], TEN()),
        )
        if p:
            per = db.jloads(p["persona_json"], {})
            persona_txt = ("\n改写要贴合这个人设(像TA本人说话):"
                           f"定位[{per.get('positioning','')}] 语气[{per.get('tone','')}] "
                           f"口头禅[{per.get('catchphrases','')}] 禁忌[{per.get('taboo','')}]\n")
    transcript = ""
    if linkgrab.is_video_link(url):
        try:
            transcript = await linkgrab.transcribe_link(url)
        except ValueError as exc:
            logging.getLogger("linkgrab").warning(
                "ASR fallback error_type=%s",
                type(exc).__name__,
            )
    if not transcript:
        try:
            transcript = await linkgrab.fetch_page_text(url)
        except Exception as exc:
            logging.getLogger("linkgrab").warning(
                "direct fetch fallback error_type=%s",
                type(exc).__name__,
            )
    rewrite_req = (f"任务:改写成一篇约 {dur} 秒(≈{dur*5}字)的中文口播稿。{persona_txt}\n"
                   f"要求:①开头3秒钩子;②口语化短句,适合真人出镜念;③保留核心信息点但换说法,"
                   f"不逐字抄袭;④结尾一句互动引导。{f'风格要求:{style}。' if style else ''}\n"
                   f"只输出口播稿正文,不要任何解释。")
    if transcript:
        # 已拿到原文/页面内容,直接用 DeepSeek 改写(快且便宜)
        from .. import providers as _p
        r = await _p.call_text(
            3,
            f"这是一条爆款内容的原文/页面信息:\n{transcript[:4000]}\n\n{rewrite_req}"
            f"\n注意:如果原文信息很少(只有标题描述),就围绕这个主题独立创作。",
            timeout=180,
            token="avatar:link",
        )
    else:
        prompt = (f"用 WebFetch 打开这个链接并读取内容:{url}\n"
                  f"(如是分享链接,尽力提取标题、文案、评论;打不开就用 WebSearch 搜该链接标题找同款内容)\n\n"
                  + rewrite_req)
        from .. import providers as _p
        research_brief = _p.sanitize_research_brief(
            f"打开并读取这个公开链接：{url}。提取页面或视频的公开标题、正文、描述与评论摘要；"
            "打不开时按链接标题寻找同一公开内容。不要改写，不要接收任何账号人设或企业资料。",
            limit=1200,
        )
        r = await _p.call_text(
            3, prompt, web=True, timeout=300, token="avatar:link",
            research_brief=research_brief,
        )
    script = (r["text"] or "").strip()
    if not script or len(script) < 30 or "无法" in script[:40] or "抱歉" in script[:20]:
        raise HTTPException(500, "这条链接提取不到内容(小红书/私密内容防抓严)。"
                                 "建议:①把视频的文案/标题复制过来直接粘到口播稿框改写;②换抖音公开链接试试")
    return {"script": script[:2000], "source_text": (transcript or "")[:3000]}


@router.post("/api/avatar/script-from-link")
async def avatar_script_from_link(body: dict):
    """爆款链接 → 提取文案 → 改写成口播稿（联网，走云雾能力网关）。"""
    await db.arun(_need_module, "avatar")
    raw_value = body.get("url", "")
    style_value = body.get("style", "")
    if not isinstance(raw_value, str) or len(raw_value) > 4000:
        raise HTTPException(400, "分享链接或文字最多 4000 个字符")
    if not isinstance(style_value, str) or len(style_value) > 200:
        raise HTTPException(400, "风格要求最多 200 个字符")
    raw = raw_value.strip()
    style = style_value.strip()
    import re as _re
    murl = _re.search(r"https?://[^\s,，、\u4e00-\u9fff]+", raw)
    url = murl.group(0).rstrip(")>].,;\'\"") if murl else ""
    if not url or len(url) > 2048:
        raise HTTPException(400, "没识别到链接:直接把分享文字整段粘进来也行(里面要含 http 链接)")
    try:
        dur = int(body.get("duration") or 30)
    except (TypeError, ValueError):
        raise HTTPException(400, "口播时长无效")
    if dur < 5 or dur > 120:
        raise HTTPException(400, "口播时长需在 5—120 秒之间")
    profile_id = await db.arun(
        _profile_id_for_tenant,
        body.get("profile_id"),
    )
    safe_body = {"style": style, "profile_id": profile_id}
    from .. import linkgrab
    # 第3期:视频平台链接转文字默认关闭，扣点前直接拒绝并提示上传自己的文件
    try:
        await db.arun(linkgrab.ensure_video_allowed, url)
    except features.FeatureDisabled as exc:
        raise HTTPException(403, str(exc)) from None
    try:  # 防 SSRF:先卡掉内网/本机地址,再扣费(别为一次被拦的请求收钱)
        await linkgrab._guard_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        billing_op = await _start_billing_operation_safely(
            billing.start_operation,
            "link_extract",
            tid=TEN(),
            note="爆款链接提取",
            cancel_reason="爆款链接提取请求中断自动退回",
        )
    except billing.InsufficientPoints as e:
        raise HTTPException(402, str(e))
    try:
        result = await _avatar_script_from_link_work(safe_body, url, dur)
    except BaseException as exc:
        try:
            await _run_db_safely(
                billing.fail_operation,
                billing_op,
                "爆款链接提取失败自动退回",
            )
        except Exception as refund_exc:
            logging.getLogger("billing").error(
                "link extraction refund failed op=%s error_type=%s",
                billing_op,
                type(refund_exc).__name__,
            )
        raise
    await _run_db_safely(billing.complete_operation, billing_op)
    return result


@router.post("/api/avatar/upload")
async def avatar_upload(file: UploadFile = File(...), kind: str = Form("photo"),
                        consent: str = Form("")):
    _need_module("avatar")
    # 第3期:照片/录音就是肖像和声音，上传前必须勾选授权声明(服务端校验+留痕)
    if kind in ("photo", "voice") and not avatar.consent_given(consent):
        raise HTTPException(400, avatar.CONSENT_MISSING)
    ext = (
        os.path.splitext(file.filename or "")[1].lower()
        or (".jpg" if kind == "photo" else ".mp3")
    )
    allowed = {"photo": (".jpg", ".jpeg", ".png", ".webp"),
               "voice": (".mp3", ".m4a", ".wav"),
               "video": (".mp4", ".mov")}
    if ext not in allowed.get(kind, ()):
        raise HTTPException(400, f"{kind} 不支持 {ext} 格式")
    max_bytes = _AVATAR_UPLOAD_MAX_BYTES
    declared_size = getattr(file, "size", None)
    try:
        declared_size = int(declared_size)
    except (TypeError, ValueError):
        declared_size = 0
    async with _persistent_upload_slot("avatar"):
        if declared_size > max_bytes:
            raise HTTPException(413, "文件超过30MB")
        _assert_persistent_upload_capacity(
            TEN(),
            max(1, declared_size),
            incoming_files=1,
        )
        data = await _read_limited(file, max_bytes, "文件超过30MB")
        _assert_persistent_upload_capacity(
            TEN(),
            len(data),
            incoming_files=1,
        )
        try:
            await asyncio.to_thread(
                avatar.validate_upload_media,
                data,
                ext,
                kind,
            )
            pub = await asyncio.to_thread(
                avatar.store_uploaded_asset,
                data,
                ext,
                kind,
                TEN(),
            )
        except avatar.InvalidAvatarMedia as exc:
            raise HTTPException(400, str(exc)) from exc
        except avatar.AssetQuotaExceeded as exc:
            raise HTTPException(413, str(exc)) from exc
    if kind in ("photo", "voice"):
        await db.arun(
            avatar.record_consent, TEN(), pub["name"], kind, "upload",
            user=auth.current(),
        )
    return {"name": pub["name"], "preview": f"/files/avatar-public/{pub['name']}"}


@router.get("/api/avatar/photos")
def avatar_photos():
    """照片卡槽:本租户存过的数字人照片,可反复选用、随意删除."""
    _need_module("avatar")
    return [{"name": p["name"], "preview": f"/files/avatar-public/{p['name']}", "ts": p.get("ts")}
            for p in avatar.saved_photos()
            if os.path.isfile(os.path.join(avatar.PUBLIC_DIR, os.path.basename(p["name"])))]


@router.delete("/api/avatar/photos/{name}")
def avatar_photo_delete(name: str):
    _need_module("avatar")
    if not avatar.photos_remove(os.path.basename(name)):
        raise HTTPException(404, "照片不存在或已删除")
    return {"ok": True}


@router.post("/api/avatar/clone")
async def avatar_clone(body: dict):
    """克隆声音:audio_name 为已上传(kind=voice)的样本文件名."""
    await db.arun(_need_module, "avatar")
    tid = TEN()
    # 第3期:克隆声音前必须勾选授权声明(服务端校验，样本通过校验后留痕)
    if not avatar.consent_given(body.get("consent")):
        raise HTTPException(400, avatar.CONSENT_MISSING)
    # Validation and the private copy share the asset lock, but the potentially
    # large copy/fsync runs on the default I/O executor rather than the loop or
    # the scarce DB executor.
    sample_path = await _prepare_avatar_clone_sample_safely(
        body.get("audio_name"),
        tid,
    )
    try:
        await db.arun(
            avatar.record_consent, tid, body.get("audio_name"), "voice",
            "voice_clone", user=auth.current(),
        )
    except BaseException:
        await asyncio.to_thread(_cleanup_avatar_clone_sample, sample_path, tid)
        raise

    try:
        op_key = await _start_billing_operation_safely(
            _start_billed_operation,
            "voice_clone",
            note="声音克隆",
            cancel_reason="声音克隆请求中断",
        )
    except BaseException:
        await asyncio.to_thread(_cleanup_avatar_clone_sample, sample_path, tid)
        raise
    try:
        try:
            voice = await avatar.clone_voice(
                sample_path, body.get("label") or "我的声音", save=False
            )
        finally:
            await asyncio.to_thread(
                _cleanup_avatar_clone_sample,
                sample_path,
                tid,
            )

        def claim(connection):
            row = connection.execute(
                "SELECT value FROM app_setting WHERE key=?",
                (f"cloned_voices:{tid}",),
            ).fetchone()
            voices = db.jloads(row["value"] if row else None, []) or []
            voices = [
                item for item in voices
                if isinstance(item, dict) and item.get("id") != voice["id"]
            ]
            voices.insert(0, voice)
            connection.execute(
                "INSERT INTO app_setting(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                "updated_at=excluded.updated_at",
                (
                    f"cloned_voices:{tid}",
                    json.dumps(voices[:10], ensure_ascii=False),
                    time.time(),
                ),
            )
            return True

        if not await _run_db_safely(
            billing.complete_operation_if_claimed,
            op_key,
            claim,
        ):
            raise RuntimeError("声音克隆本地结算状态冲突")
    except asyncio.CancelledError:
        try:
            await _run_db_safely(
                billing.fail_operation,
                op_key,
                "声音克隆请求中断",
            )
        except Exception as refund_exc:
            log.error(
                "voice clone cancellation refund failed op=%s error_type=%s",
                op_key,
                type(refund_exc).__name__,
            )
        raise
    except Exception as exc:
        try:
            settled = await _run_db_safely(
                billing.fail_operation,
                op_key,
                "声音克隆失败自动退回",
            )
        except Exception as settle_error:
            log.error(
                "voice clone refund failed op=%s error_type=%s",
                op_key,
                type(settle_error).__name__,
            )
            raise HTTPException(
                503, "声音克隆未完成，退点结算正在恢复，请稍后查看"
            ) from settle_error
        if not settled:
            raise HTTPException(503, "声音克隆结算状态待确认，请稍后查看") from exc
        raise HTTPException(500, "克隆失败，点数已退回，请重试") from exc
    return voice


@router.delete("/api/avatar/clone/{vid}")
def avatar_clone_delete(vid: str):
    _need_module("avatar")
    voices = [v for v in avatar.cloned_voices() if v["id"] != vid]
    avatar.save_cloned_voices(voices)
    return {"ok": True}


def _create_charged_avatar_job(params: dict, tid: int = None) -> int:
    """先落待计费工单，再把开工状态、余额与计费流水原子提交。"""
    tid = int(tid or TEN())
    action = avatar._charged_action(params)
    points = 0.0 if tid == 1 else float(
        (billing.prices().get(action) or {"points": 1})["points"])
    job_id = db.insert("avatar_job", {
        "params_json": json.dumps(params, ensure_ascii=False),
        "tenant_id": tid,
        "created_by": int((auth.current() or {}).get("id") or 0) or None,
        "status": "pending_charge",
        "billing_status": "pending",
        "billing_points": points,
    })

    def claim(connection):
        changed = connection.execute(
            "UPDATE avatar_job SET status='queued',billing_status='charged',"
            "updated_at=? "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (time.time(), job_id),
        )
        return changed.rowcount == 1

    try:
        charged = billing.charge_if_claimed(
            action,
            tid,
            claim,
            note=f"数字人工单 #{job_id}",
            points=points,
        )
    except billing.InsufficientPoints as exc:
        db.q(
            "DELETE FROM avatar_job "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (job_id,),
        )
        raise HTTPException(402, str(exc)) from exc
    except Exception:
        db.q(
            "DELETE FROM avatar_job "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (job_id,),
        )
        raise
    if not charged:
        db.q(
            "DELETE FROM avatar_job "
            "WHERE id=? AND status='pending_charge' AND billing_status='pending'",
            (job_id,),
        )
        raise HTTPException(409, "数字人任务已提交，请到任务中心查看")
    return job_id


def _start_avatar_job_worker(job_id: int):
    return asyncio.create_task(
        avatar.run_job(job_id, engine.broadcast)
    )


def _settle_unstarted_avatar_job(job_id: int) -> bool:
    return avatar.settle_failure(
        job_id,
        "数字人任务启动失败，系统已安全终止并退回本次点数",
    )


@router.post("/api/avatar/jobs")
async def avatar_job_create(body: dict):
    _need_module("avatar")
    script = (body.get("script") or "").strip()
    if not (body.get("photo_name") and script):
        raise HTTPException(400, "照片和口播稿必填")
    try:
        dur = int(body.get("duration") or 30)
    except (TypeError, ValueError):
        raise HTTPException(400, "视频时长无效")
    if dur not in {15, 30, 60}:
        raise HTTPException(400, "视频时长只能选择 15、30 或 60 秒")
    max_script_chars = {15: 120, 30: 240, 60: 480}[dur]
    if len(script) > max_script_chars:
        raise HTTPException(
            400,
            f"{dur} 秒口播稿最多 {max_script_chars} 个字符，请精简或选择更长时长",
        )
    tid = TEN()
    # 第3期:选 HeyGen(境外)必须明确同意；没同意的任务只用国内引擎
    try:
        overseas_ok = avatar.overseas_allowed(
            body.get("engine") or "", body.get("overseas_ok")
        )
    except avatar.ConsentRequired as exc:
        raise HTTPException(400, str(exc)) from None

    def create_job() -> int:
        all_voices = avatar.cloned_voices() + avatar.VOICES
        voice = next(
            (item for item in all_voices
             if item["id"] == body.get("voice_id")),
            avatar.VOICES[0],
        )
        # Validation and the charged job row form one asset-library critical
        # section. A concurrent delete can run only after the durable job
        # reference exists, at which point physical reclamation is prohibited.
        with avatar.asset_library_lock(tid):
            photo_name = _avatar_asset_name(
                body.get("photo_name"), "photo_name", {"photo"}
            )
            own_audio_name = _avatar_asset_name(
                body.get("own_audio_name"),
                "own_audio_name",
                {"voice"},
                required=False,
            )
            # 老素材(本期之前上传的)没有授权记录:本次必须勾选声明，勾了就补记
            try:
                avatar.require_consent(
                    tid, [photo_name, own_audio_name], body.get("consent"),
                    "avatar_job", user=auth.current(),
                    kinds={photo_name: "photo", own_audio_name or "": "voice"},
                )
            except avatar.ConsentRequired as exc:
                raise HTTPException(400, str(exc)) from None
            if overseas_ok:
                # 同意传输至境外服务商(HeyGen)也要留痕
                for name, kind in ((photo_name, "photo"), (own_audio_name, "voice")):
                    if name:
                        avatar.record_consent(tid, name, kind, "overseas_transfer",
                                              user=auth.current(), overseas=True)
            params = {
                "photo_name": photo_name,
                "script": script,
                "voice_id": voice["id"],
                "voice_label": voice["label"],
                "own_audio_name": own_audio_name,
                "engine": body.get("engine") or "",
                "duration": dur,
                "prompt": (body.get("prompt") or "").strip(),
                "domestic_only": not overseas_ok,
            }
            return _create_charged_avatar_job(params, tid)

    jid = await _run_db_then_start_worker_safely(
        create_job,
        start_worker=_start_avatar_job_worker,
        settle_unstarted=_settle_unstarted_avatar_job,
    )
    return {"job_id": jid}


@router.get("/api/avatar/jobs")
def avatar_jobs(limit: int | None = None, offset: int = 0):
    _need_module("avatar")
    page_limit, page_offset, paged = _pagination(limit, offset, 50)
    total = (
        int((db.one(
            "SELECT COUNT(*) AS n FROM avatar_job "
            "WHERE tenant_id=? AND deleted_at IS NULL",
            (TEN(),),
        ) or {}).get("n") or 0)
        if paged else 0
    )
    rows = db.q(
        "SELECT * FROM avatar_job WHERE tenant_id=? AND deleted_at IS NULL "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (TEN(), page_limit, page_offset),
    )
    for r in rows:
        r["params"] = db.jloads(r.pop("params_json"))
        r["steps"] = _steps_for_view(
            r.pop("steps_json"), _is_boss(), status=r.get("status")
        )
        retries = int(r.get("retry_count") or 0)
        r["free_retries_remaining"] = max(
            0, avatar.MAX_FREE_RETRIES - retries
        )
        r["retryable"] = bool(
            r.get("status") == "failed"
            and r.get("billing_status") in {"refunded", "included"}
            and r["free_retries_remaining"] > 0
        )
        r["error"] = _public_failure_for_view(
            r.get("status"),
            r.get("error"),
            _is_boss(),
        )
    return _page_result(rows, total, page_limit, page_offset) if paged else rows


@router.post("/api/avatar/jobs/{jid}/retry")
async def avatar_job_retry(jid: int):
    _need_module("avatar")
    row = await db.aone(
        "SELECT tenant_id,status FROM avatar_job "
        "WHERE id=? AND deleted_at IS NULL",
        (jid,),
    )
    if not row or row.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    if row.get("status") != "failed":
        raise HTTPException(409, "只有失败任务可以免费重试")
    prepared = await _run_db_then_start_worker_safely(
        avatar.prepare_retry,
        jid,
        TEN(),
        start_worker=lambda _prepared: _start_avatar_job_worker(jid),
        should_start=bool,
        settle_unstarted=(
            lambda _prepared: _settle_unstarted_avatar_job(jid)
        ),
    )
    if not prepared:
        current = await db.aone(
            "SELECT retry_count FROM avatar_job WHERE id=? AND tenant_id=?",
            (jid, TEN()),
        ) or {}
        if (current.get("retry_count") or 0) >= avatar.MAX_FREE_RETRIES:
            raise HTTPException(429, "该任务免费重试次数已用完，请新建任务")
        raise HTTPException(409, "这个任务已经不在失败状态了——多半是刚刚已被重试(正在排队执行)或已被删除。刷新看最新进度即可,不会重复扣点")
    engine.broadcast({"type": "avatar_update", "job_id": jid})
    current = await db.aone(
        "SELECT retry_count FROM avatar_job WHERE id=?", (jid,)
    ) or {}
    return {
        "ok": True,
        "job_id": jid,
        "free_retry": True,
        "retry_count": current.get("retry_count") or 0,
    }


@router.post("/api/avatar/jobs/{jid}/cancel")
def avatar_job_cancel(jid: int):
    _need_module("avatar")
    row = db.one(
        "SELECT tenant_id,status,billing_status FROM avatar_job "
        "WHERE id=? AND deleted_at IS NULL",
        (jid,),
    )
    if not row or row.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    if row["status"] not in ("queued", "running"):
        raise HTTPException(400, "该任务已经结束,不用取消")
    if not avatar.settle_failure(
            jid, "老板已取消", terminal_status="cancelled"):
        raise HTTPException(409, "这个任务的状态刚刚更新了(可能已被重试或删除),刷新页面看最新进度即可")
    llm.kill(f"avatar{jid}:")
    engine.broadcast({"type": "avatar_update", "job_id": jid})
    return {"ok": True}


@router.delete("/api/avatar/jobs/{jid}")
def avatar_job_delete(jid: int):
    _need_admin()
    _need_module("avatar")
    row = db.one(
        "SELECT tenant_id,status,billing_status FROM avatar_job "
        "WHERE id=? AND deleted_at IS NULL",
        (jid,),
    )
    if not row or row.get("tenant_id", 1) != TEN():
        raise HTTPException(404)
    if row.get("status") in ("pending_charge", "queued", "running"):
        if not avatar.settle_failure(
                jid, "删除在途任务", terminal_status="cancelled"):
            raise HTTPException(409, "任务状态刚刚发生变化，请刷新后再删除")
    llm.kill(f"avatar{jid}:")
    current = db.one(
        "SELECT status,billing_status FROM avatar_job "
        "WHERE id=? AND deleted_at IS NULL",
        (jid,),
    )
    if not current:
        raise HTTPException(404)
    if current["status"] in ("pending_charge", "queued", "running"):
        raise HTTPException(503, "这个数字人任务的退点还在处理中(约几秒),稍等片刻再删除")
    if (
        current["status"] in ("failed", "cancelled")
        and current["billing_status"] == "charged"
    ):
        raise HTTPException(503, "数字人任务退款尚未完成，请稍后重试删除")
    deleted_at = time.time()
    changed = db.execute(
        "UPDATE avatar_job SET deleted_at=?,deleted_by=?,delete_reason=?,"
        "updated_at=? WHERE id=? AND tenant_id=? AND deleted_at IS NULL "
        "AND status NOT IN ('pending_charge','queued','running')",
        (
            deleted_at,
            int((auth.current() or {}).get("id") or 0),
            "用户移入回收站",
            deleted_at,
            jid,
            TEN(),
        ),
    )
    if changed != 1:
        raise HTTPException(409, "任务状态刚刚发生变化，请刷新后再删除")
    engine.broadcast({"type": "avatar_update", "job_id": jid})
    return {"ok": True, "soft_deleted": True, "deleted_at": deleted_at}

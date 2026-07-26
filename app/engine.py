"""流水线引擎:显式状态机 + 持久化队列(PRD 7.2,产品的心脏).

工位状态机: queued → running → awaiting_review → done / cancelled
                         ↘ failed(自动重试2次后挂起)   rejected → 重跑new version
                         ↘ interrupted(老板打断,恢复后自动重跑)
上游修改后下游 done 置为 stale,引擎推进到 stale 工位时自动重算。
工单状态: running / awaiting_review / gate_blocked / failed / done / cancelled / paused
"""
import asyncio
import json
import logging
import time

from . import billing, db, gate, llm, providers
from .skills import registry

log = logging.getLogger("engine")

STATUS_CN = {
    "queued": "排队中", "running": "执行中", "awaiting_review": "等您审批",
    "gate_blocked": "被质检拦截", "failed": "已失败", "done": "已完成",
    "cancelled": "已终止", "paused": "已暂停", "rejected": "重做中",
    "stale": "待重算", "skipped": "已跳过", "interrupted": "被打断",
    "pending_charge": "待扣费",
}


def status_cn(value) -> str:
    """把内部状态枚举翻成老板能懂的词;未知值原样返回。"""
    return STATUS_CN.get(str(value or ""), str(value or "未知"))


MAX_RETRY = 2
LAST_IDX = 9
JOB_WORKING_STATUSES = (
    "pending_charge", "running", "awaiting_review", "gate_blocked", "paused",
)
JOB_EXECUTABLE_STATUSES = ("running", "awaiting_review", "gate_blocked")


class Engine:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.locks: dict = {}
        self.subscribers: dict = {}   # queue -> (租户id,是否root)，避免租户1普通成员被当平台方
        self._tid_cache: dict = {}     # (table, id) -> tenant_id 记忆,省得每条事件查库
        self._loop = None              # 引擎所在事件循环,供线程池路由跨线程安全唤醒

    # ---------- 跨线程安全投递 ----------
    def _dispatch(self, fn, *args):
        """asyncio.Queue 不是线程安全的:同步路由跑在线程池里,直接 put_nowait 会让
        等待中的 worker 不被唤醒(工单永久卡 running)或损坏订阅队列内部状态。
        因此非循环线程一律经 call_soon_threadsafe 投递回引擎循环执行。"""
        loop = self._loop
        if loop is None:
            fn(*args)                  # 引擎尚未启动:仍在单线程装配阶段,直调即可
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fn(*args)
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass                        # 循环已关闭(进程退出中),丢弃通知即可

    # ---------- 事件推送(SSE) ----------
    _EV_TABLE = {"meeting": ("meeting", "meeting_id"), "task": ("task", "task_id"),
                 "avatar": ("avatar_job", "job_id"), "pub": ("pub_task", "task_id")}

    def _tid_of(self, ev: dict):
        """按事件归属解析租户 id；解析不出时返回 None，仅平台 root 可接收。"""
        typ = ev.get("type") or ""
        if ev.get("tenant_id") is not None:
            return int(ev["tenant_id"])
        table, idkey = "job", "job_id"
        for pre, (t, k) in self._EV_TABLE.items():
            if typ.startswith(pre):
                table, idkey = t, k
                break
        rid = ev.get(idkey)
        if rid is None:
            return None
        ck = (table, rid)
        if ck not in self._tid_cache:
            if len(self._tid_cache) > 4000:
                self._tid_cache.clear()
            r = db.one(f"SELECT tenant_id FROM {table} WHERE id=?", (rid,))
            self._tid_cache[ck] = (r or {}).get("tenant_id") if r else None
        return self._tid_cache[ck]

    @staticmethod
    def _public_step(step: dict) -> dict:
        """普通成员只看到状态，不看到岗位、Skill、检索词或具体工作动作。"""
        step = step if isinstance(step, dict) else {}
        raw_kind = str(step.get("k") or "").lower()
        if raw_kind == "done":
            kind, label = "done", "任务处理已完成"
        elif raw_kind == "error":
            kind, label = "error", "任务处理遇到问题"
        elif raw_kind == "retry":
            kind, label = "retry", "正在重新处理"
        else:
            kind, label = "working", "员工正在处理任务"
        return {"k": kind, "l": label, "ts": step.get("ts") or time.time()}

    @classmethod
    def public_event(cls, ev: dict) -> dict:
        """把实时事件降级为对外契约；未知 ``*_step`` 同样默认去掉明细。"""
        typ = str(ev.get("type") or "")
        if typ in {"station_step", "task_step", "avatar_step", "employee_step"} \
                or typ.endswith("_step"):
            public = {
                key: ev[key]
                for key in (
                    "type", "job_id", "task_id", "tv_id", "idx", "n",
                    "tenant_id",
                )
                if key in ev
            }
            if typ == "tv_step":
                public["msg"] = cls._public_step(
                    {"k": "working", "ts": ev.get("ts")}
                )["l"]
            else:
                public["step"] = cls._public_step(ev.get("step") or {})
            return public
        return ev

    @classmethod
    def internal_event(cls, ev: dict) -> dict:
        """Boss keeps operational detail, but never receives raw error text."""
        typ = str(ev.get("type") or "")
        step = ev.get("step")
        if (
            (typ.endswith("_step") or typ in {
                "station_step", "task_step", "avatar_step", "employee_step",
            })
            and isinstance(step, dict)
            and str(step.get("k") or "").lower() == "error"
        ):
            safe = dict(ev)
            safe["step"] = cls._public_step(step)
            return safe
        return ev

    def broadcast(self, ev: dict):
        tid = self._tid_of(ev)
        for q_, subscriber in list(self.subscribers.items()):
            sub_tid, sub_root = subscriber if isinstance(subscriber, tuple) else (subscriber, False)
            if tid is None and not sub_root:
                continue   # 找不到归属绝不广播给普通账号，避免删除竞态/未知事件泄露
            if tid is not None and not sub_root and sub_tid != tid:
                continue
            payload = (
                self.internal_event(ev)
                if sub_root
                else self.public_event(ev)
            )
            # 事件塑形(含 _tid_of 的库查询)留在调用线程,只把入队动作投递回循环。
            self._dispatch(self._enqueue_event, q_, payload)

    @staticmethod
    def _enqueue_event(q_, payload):
        try:
            q_.put_nowait(payload)
        except asyncio.QueueFull:
            pass

    def touch(self, job_id):
        self.broadcast({"type": "job_update", "job_id": job_id, "ts": time.time()})

    def _recorder(self, job_id, idx, run_id=None):
        """工位步骤记录器:每一步实时 SSE 广播 + 落库(steps_json),供前端可视化.
        返回 (progress回调, steps列表)。idx 传 "gate" 表示质检关卡。"""
        steps = []
        st = {"save": 0.0, "cast": 0.0}

        def progress(kind, label=""):
            now = time.time()
            if kind == "typing" and steps and steps[-1]["k"] == "typing":
                steps[-1].update(l=label, ts=now)   # 打字进度原地更新,不刷屏
                if now - st["cast"] < 1:
                    return
            else:
                steps.append({"k": kind, "l": str(label)[:300], "ts": now})
            st["cast"] = now
            self.broadcast({"type": "station_step", "job_id": job_id, "idx": idx,
                            "n": len(steps), "step": steps[-1]})
            if run_id and (kind != "typing" or now - st["save"] > 3):
                # 进度回调常在事件循环上被 provider 协程调用;这里的落库是一次
                # 写操作,写锁竞争时会把整个循环冻住,必须投给 db 线程池。
                # 快照在提交前序列化(steps 随后还会被循环线程改),丢一帧无碍——
                # 下一次保存会整体重写。
                snapshot = json.dumps(steps, ensure_ascii=False)
                db.submit_write(db.update, "station_run", run_id,
                                {"steps_json": snapshot})
                st["save"] = now
        return progress, steps

    # ---------- 启动与恢复 ----------
    async def start(self):
        self._loop = asyncio.get_running_loop()

        def _recover():
            # 断点恢复:被重启打断的 running 工位重跑,进行中的工单重新入队
            for r in db.q("SELECT id FROM station_run WHERE status='running'"):
                db.update("station_run", r["id"], {"status": "rejected",
                                                   "review_comment": "服务重启,自动重跑"})
            running = [j["id"] for j in db.q(
                "SELECT id FROM job WHERE status IN ('running')")]
            # 兼容旧版本“先标失败、来不及退款”的窗口；CAS 保证多次启动也只退一次。
            for j in db.q("SELECT id FROM job WHERE status='failed' "
                          "AND billing_status='charged'"):
                self.settle_failure(j["id"], "服务启动补做失败结算")
            return running

        # 恢复扫描含批量写与退款事务,放 db 线程池,启动期就不阻塞事件循环。
        for job_id in await db.arun(_recover):
            self.notify(job_id)
        for _ in range(4):
            asyncio.create_task(self._worker())
        log.info("engine started")

    def notify(self, job_id: int):
        self._dispatch(self.queue.put_nowait, job_id)

    async def _worker(self):
        while True:
            job_id = await self.queue.get()
            lock = self.locks.setdefault(job_id, asyncio.Lock())
            if lock.locked():
                # 该工单正在推进,稍后重新入队,不丢通知
                asyncio.create_task(self._renotify(job_id))
                continue
            async with lock:
                try:
                    await self._advance(job_id)
                except Exception as e:
                    # 失败结算含退款事务(写),放 db 线程池执行。
                    await db.arun(
                        self.settle_failure,
                        job_id,
                        providers.public_failure_message(
                            e,
                            "内容流水线执行失败。未产出可用正文时整单点数自动退回(账单可查)；可复制 Brief 重新开单",
                        ),
                    )
                    log.error(
                        "advance job %s failed error_type=%s",
                        job_id,
                        type(e).__name__,
                    )
            # 工单进入终态后回收它的锁,别让 self.locks 随历史工单无限增长(内存泄漏)
            j = db.one("SELECT status FROM job WHERE id=?", (job_id,))
            if (not j or j["status"] in ("done", "cancelled", "failed")) and not lock.locked():
                self.locks.pop(job_id, None)

    async def _renotify(self, job_id):
        await asyncio.sleep(2)
        self.notify(job_id)

    # ---------- 推进状态机 ----------
    def _latest_run(self, job_id, idx):
        return db.one("SELECT * FROM station_run WHERE job_id=? AND station_idx=? "
                      "ORDER BY version DESC LIMIT 1", (job_id, idx))

    @staticmethod
    def _job_row_executable(row) -> bool:
        return bool(
            row
            and row["status"] in JOB_EXECUTABLE_STATUSES
            and row["billing_status"] == "charged"
        )

    def _needs_review(self, cfg, mode):
        if cfg["approval"] == registry.APPROVAL_FORCE:
            return True
        if mode == "fullauto":   # 完全托管跳过普通审批，但发布终审仍然强制停下。
            return False
        if mode == "autopilot":
            return False
        if mode == "manual":
            return True
        return cfg["approval"] in (registry.APPROVAL_PICK, registry.APPROVAL_REVIEW)

    def needs_review(self, station_idx: int, mode: str) -> bool:
        """给 API 返回本次工位的操作态；不向前端暴露内部审批配置。"""
        cfg = registry.BY_IDX.get(station_idx)
        return bool(cfg and self._needs_review(cfg, mode))

    def _has_usable_delivery(self, job_id: int) -> bool:
        """正文/文风工位已形成可用产出时，不按整单失败全额退费。"""
        return bool(db.one(
            "SELECT id FROM station_run WHERE job_id=? AND station_idx IN (3,4) "
            "AND status IN ('done','awaiting_review') "
            "AND output_json IS NOT NULL AND length(output_json)>2 LIMIT 1",
            (job_id,),
        ))

    def settle_failure(self, job_id: int, reason: str) -> bool:
        """收口工单与运行工位；无可用正文时按实际扣点金额幂等退款。"""
        from . import obs
        obs.count("job_settle_failure")
        job = db.one(
            "SELECT id,tenant_id,status,billing_status,billing_points FROM job WHERE id=?",
            (job_id,),
        )
        if not job or job["status"] in ("done", "cancelled"):
            return False
        reason = (reason or "执行失败")[:240]
        if self._has_usable_delivery(job_id) or job.get("billing_status") != "charged":
            with db.atomic() as c:
                now = time.time()
                cur = c.execute(
                    "UPDATE job SET status='failed',updated_at=? WHERE id=? "
                    "AND status NOT IN ('done','cancelled')",
                    (now, job_id),
                )
                if cur.rowcount == 1:
                    c.execute(
                        "UPDATE station_run "
                        "SET status='failed',review_comment=?,updated_at=? "
                        "WHERE job_id=? AND status IN ('running','queued')",
                        (reason, now, job_id),
                    )
            self.touch(job_id)
            return False

        amount = job.get("billing_points")
        if amount is None:
            amount = (billing.prices().get("content_job") or {"points": 18})["points"]

        def claim(c):
            cur = c.execute(
                "UPDATE job SET status='failed',billing_status='refunded',updated_at=? "
                "WHERE id=? AND billing_status='charged' "
                "AND status NOT IN ('done','cancelled')",
                (time.time(), job_id),
            )
            if cur.rowcount != 1:
                return False
            c.execute(
                "UPDATE station_run SET status='failed',review_comment=?,updated_at=? "
                "WHERE job_id=? AND status IN ('running','queued')",
                (reason, time.time(), job_id),
            )
            return True

        try:
            refunded = billing.refund_amount_if_claimed(
                int(job.get("tenant_id") or 1),
                float(amount or 0),
                claim,
                f"退回:内容流水线整单 · {reason[:120]}",
            )
        except Exception as exc:
            # 结算故障也不能留下“永远运行中”；保留 charged 供启动对账补退。
            log.error(
                "job %s refund settlement failed error_type=%s",
                job_id,
                type(exc).__name__,
            )
            with db.atomic() as c:
                now = time.time()
                cur = c.execute(
                    "UPDATE job SET status='failed',updated_at=? WHERE id=? "
                    "AND status NOT IN ('done','cancelled')",
                    (now, job_id),
                )
                if cur.rowcount == 1:
                    c.execute(
                        "UPDATE station_run "
                        "SET status='failed',review_comment=?,updated_at=? "
                        "WHERE job_id=? AND status IN ('running','queued')",
                        (reason, now, job_id),
                    )
            refunded = False
        self.touch(job_id)
        return refunded

    @staticmethod
    def _cancel_station_runs(c, job_id: int, reason: str,
                             clear_unfinished_output: bool = False):
        """在取消事务内收口尚未成为交付物的工位。

        ``done``/``awaiting_review`` 可能已经形成正文价值，必须保留给用户查看；
        其他在途版本不再允许 provider 返回后继续落库。退款时同时清掉未完成
        版本的临时输出，避免出现“退了整单点数却仍拿到正文”的窗口。
        """
        now = time.time()
        clear_sql = ",output_json=NULL" if clear_unfinished_output else ""
        c.execute(
            "UPDATE station_run SET status='cancelled',review_comment=?,updated_at=?"
            f"{clear_sql} WHERE job_id=? "
            "AND status IN ('queued','running','rejected','stale','interrupted')",
            (reason[:240], now, job_id),
        )

    def settle_cancel(self, job_id: int, reason: str = "老板取消工单") -> bool:
        """CAS 取消并按实际交付幂等结算。

        - 未完成、没有工位 3/4 可用正文：整单按原扣点金额退回；
        - 已有可用正文：取消但不退款，防止免费拿交付；
        - ``done``/``failed`` 不允许被改写为 cancelled；
        - 重复取消以及 worker/删除并发最多退款一次。
        """
        first = db.one(
            "SELECT id,tenant_id,status,billing_status,billing_points "
            "FROM job WHERE id=?",
            (job_id,),
        )
        if not first:
            raise ValueError("工单不存在")
        amount = first.get("billing_points")
        if amount is None:
            amount = (billing.prices().get("content_job") or {"points": 18})[
                "points"]
        outcome = {
            "cancelled": False,
            "invalid": None,
            "refunded": False,
        }

        def claim(c):
            row = c.execute(
                "SELECT id,tenant_id,status,billing_status,billing_points "
                "FROM job WHERE id=?",
                (job_id,),
            ).fetchone()
            if not row:
                outcome["invalid"] = "工单不存在"
                return False
            status = row["status"]
            billed = row["billing_status"]
            usable = bool(c.execute(
                "SELECT 1 FROM station_run "
                "WHERE job_id=? AND station_idx IN (3,4) "
                "AND status IN ('done','awaiting_review') "
                "AND output_json IS NOT NULL AND length(output_json)>2 LIMIT 1",
                (job_id,),
            ).fetchone())

            if status == "done":
                outcome["invalid"] = "已完成工单不能取消，也不会退款"
                return False
            if status == "failed":
                outcome["invalid"] = "失败工单已结算，不能取消；请新建工单"
                return False
            if status != "cancelled" and status not in JOB_WORKING_STATUSES:
                outcome["invalid"] = (
                    f"工单{status_cn(status)},不能取消；请新建工单")
                return False

            # 兼容升级前留下的 cancelled+charged：仅在确实没有可用交付时补退。
            if billed == "charged" and not usable:
                cur = c.execute(
                    "UPDATE job SET status='cancelled',billing_status='refunded',"
                    "updated_at=? WHERE id=? AND status=? "
                    "AND billing_status='charged'",
                    (time.time(), job_id, status),
                )
                if cur.rowcount != 1:
                    return False
                self._cancel_station_runs(
                    c, job_id, reason, clear_unfinished_output=True)
                outcome["cancelled"] = True
                outcome["refunded"] = True
                return True

            if status == "cancelled":
                outcome["cancelled"] = True
                return False

            cur = c.execute(
                "UPDATE job SET status='cancelled',updated_at=? "
                "WHERE id=? AND status=? AND billing_status=?",
                (time.time(), job_id, status, billed),
            )
            if cur.rowcount != 1:
                return False
            self._cancel_station_runs(c, job_id, reason)
            outcome["cancelled"] = True
            return False

        refunded = billing.refund_amount_if_claimed(
            int(first.get("tenant_id") or 1),
            float(amount or 0),
            claim,
            f"退回:内容流水线整单 · {reason[:120]}",
        )
        if outcome["invalid"]:
            raise ValueError(outcome["invalid"])
        if not outcome["cancelled"]:
            current = db.one(
                "SELECT status FROM job WHERE id=?", (job_id,))
            if not current:
                raise ValueError("工单不存在")
            if current["status"] != "cancelled":
                raise ValueError("工单状态刚刚更新了(可能已被他人操作或已推进),刷新页面按最新状态处理")
        self.touch(job_id)
        return bool(refunded or outcome["refunded"])

    async def _advance(self, job_id: int):
        while True:
            done = await self._advance_once(job_id)
            if done:
                return

    async def _advance_once(self, job_id: int) -> bool:
        """从工位0重扫描一遍,处理第一个可推进的工位。
        返回 True 表示本轮无需继续(等待用户/失败/全部完成)。"""
        job = db.one("SELECT * FROM job WHERE id=?", (job_id,))
        if not job or job["status"] in ("done", "cancelled", "failed", "paused"):
            return True
        brief = db.jloads(job["brief_json"])
        profile = None
        if job["profile_id"]:
            p = db.one("SELECT * FROM account_profile WHERE id=? AND tenant_id=?",
                       (job["profile_id"], job.get("tenant_id") or 1))
            if p:
                profile = {"name": p["name"], "persona": db.jloads(p["persona_json"])}

        idx = 0
        while idx <= LAST_IDX:
            cfg = registry.BY_IDX[idx]
            run = self._latest_run(job_id, idx)

            if run and run["status"] in ("done", "skipped"):
                idx += 1
                continue
            if run and run["status"] == "awaiting_review":
                changed = await db.aexecute(
                    "UPDATE job SET status='awaiting_review',current_idx=?,updated_at=? "
                    "WHERE id=? AND billing_status='charged' "
                    "AND status IN ('running','awaiting_review','gate_blocked')",
                    (idx, time.time(), job_id),
                )
                if changed:
                    self.touch(job_id)
                return True
            if run and run["status"] == "failed":
                await db.arun(
                    self.settle_failure,
                    job_id, run.get("review_comment") or f"工位{idx + 1}执行失败")
                return True

            # 需要跳过的可选工位(演绎师默认关闭)
            if cfg.get("optional") and not brief.get("enable_deck"):
                if not run:
                    def _skip_tx(station_idx=idx, skill=cfg["skill"]):
                        with db.atomic() as c:
                            current = c.execute(
                                "SELECT status,billing_status FROM job WHERE id=?",
                                (job_id,),
                            ).fetchone()
                            if not self._job_row_executable(current):
                                return False
                            now = time.time()
                            c.execute(
                                "INSERT INTO station_run"
                                "(job_id,station_idx,skill_id,status,created_at,updated_at) "
                                "VALUES(?,?,?,'skipped',?,?)",
                                (job_id, station_idx, skill, now, now),
                            )
                            return True
                    if not await db.arun(_skip_tx):
                        return True
                idx += 1
                continue

            # 发布工位前:质检关卡
            if idx == 8:
                g = db.jloads(job["gate_json"], {})
                if not g.get("passed") and not g.get("override"):
                    ok = await self._run_gate(job_id, brief)
                    if not ok:
                        return True

            # 执行(全新 or 重跑)
            revision_note, prev_output = None, None
            version = 1
            if run and run["status"] == "rejected":
                revision_note = run["review_comment"]
                prev_output = db.jloads(run["output_json"], {})
                version = run["version"] + 1
            elif run and run["status"] == "stale":
                version = run["version"] + 1
                revision_note = "上游产出已更新,请基于最新上游内容重做本工位。"
            elif run and run["status"] == "interrupted":
                version = run["version"] + 1
                revision_note = "此前执行被老板打断,请重新完整完成本工位。"

            res = await self._execute(job_id, idx, cfg, brief, profile, version,
                                      revision_note, prev_output)
            if res in ("cancelled", "deleted"):
                return True
            if res == "stale":
                return False  # 执行期间上游被打回,重扫描
            if res == "interrupted":
                return True   # 老板打断,工单已暂停,等恢复
            if not res:
                await db.arun(self.settle_failure, job_id, f"工位{idx + 1}重试后仍失败")
                return True

            run = self._latest_run(job_id, idx)
            if not run:  # 删除请求可能在 provider 返回后已清掉全部工位。
                return True
            if self._needs_review(cfg, job["mode"]):
                def _review_tx(run_id=run["id"], station_idx=idx):
                    with db.atomic() as c:
                        current_job = c.execute(
                            "SELECT status,billing_status FROM job WHERE id=?",
                            (job_id,),
                        ).fetchone()
                        current_run = c.execute(
                            "SELECT status FROM station_run WHERE id=?",
                            (run_id,),
                        ).fetchone()
                        if not (self._job_row_executable(current_job)
                                and current_run
                                and current_run["status"] == "running"):
                            return False
                        now = time.time()
                        c.execute(
                            "UPDATE station_run SET status='awaiting_review',"
                            "updated_at=? WHERE id=? AND status='running'",
                            (now, run_id),
                        )
                        c.execute(
                            "UPDATE job SET status='awaiting_review',current_idx=?,"
                            "updated_at=? WHERE id=? AND billing_status='charged' "
                            "AND status IN ('running','awaiting_review','gate_blocked')",
                            (station_idx, now, job_id),
                        )
                        return True
                transitioned = await db.arun(_review_tx)
                if not transitioned:
                    return True
                self.touch(job_id)
                from . import notify
                notify.push(self._job_tenant(job_id), "awaiting",
                            {"job_id": job_id, "title": self._job_title(job_id),
                             "station": f"工位{idx + 1}·{cfg['name']}"})
                return True
            # 自动放行:挑选类工位自动取推荐首选
            out = db.jloads(run["output_json"], {})
            if cfg["approval"] == registry.APPROVAL_PICK:
                out.setdefault("selected", 0)
            def _autopass_tx(run_id=run["id"], station_idx=idx, out=out):
                with db.atomic() as c:
                    current_job = c.execute(
                        "SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
                    current_run = c.execute(
                        "SELECT status FROM station_run WHERE id=?",
                        (run_id,),
                    ).fetchone()
                    if not (self._job_row_executable(current_job) and current_run):
                        return "terminal"
                    if current_run["status"] == "stale":
                        return "stale"
                    if current_run["status"] == "interrupted":
                        return "interrupted"
                    if current_run["status"] != "running":
                        return "terminal"
                    now = time.time()
                    c.execute(
                        "UPDATE station_run SET status='done',output_json=?,"
                        "updated_at=? WHERE id=? AND status='running'",
                        (json.dumps(out, ensure_ascii=False), now, run_id),
                    )
                    self._finalize_station_tx(
                        c, job_id, station_idx, out,
                        int(current_job["tenant_id"] or 1),
                    )
                    c.execute(
                        "UPDATE job SET status='running',current_idx=?,updated_at=? "
                        "WHERE id=? AND billing_status='charged' "
                        "AND status IN ('running','awaiting_review','gate_blocked')",
                        (min(station_idx + 1, LAST_IDX), now, job_id),
                    )
                    return "done"
            transition = await db.arun(_autopass_tx)
            if transition == "stale":
                return False
            if transition in ("interrupted", "terminal"):
                return True
            self.touch(job_id)
            return False  # 本工位完成,重扫描推进下一步

        # 全部工位完成
        title = self._job_title(job_id)

        def _complete_tx():
            with db.atomic() as c:
                current = c.execute(
                    "SELECT tenant_id,status,billing_status FROM job WHERE id=?",
                    (job_id,),
                ).fetchone()
                if not self._job_row_executable(current):
                    return False
                now = time.time()
                cur = c.execute(
                    "UPDATE job SET status='done',current_idx=?,updated_at=? "
                    "WHERE id=? AND billing_status='charged' "
                    "AND status IN ('running','awaiting_review','gate_blocked')",
                    (LAST_IDX, now, job_id),
                )
                if cur.rowcount != 1:
                    return False
                c.execute(
                    "INSERT INTO asset"
                    "(type,job_id,tenant_id,payload_json,created_at,updated_at) "
                    "VALUES('final',?,?,?,?,?)",
                    (
                        job_id,
                        int(current["tenant_id"] or 1),
                        json.dumps(
                            {"title": title,
                             "brief": brief.get("direction")},
                            ensure_ascii=False,
                        ),
                        now,
                        now,
                    ),
                )
                return True

        if not await db.arun(_complete_tx):
            return True
        from . import notify
        notify.push(self._job_tenant(job_id), "done", {"job_id": job_id, "title": title})
        await db.arun(self._distill_knowledge, job_id, title)
        self.touch(job_id)
        return True

    def _distill_knowledge(self, job_id, title):
        """V4:交付即沉淀——把复盘官的经验与回流选题写进公司知识库."""
        try:
            with db.atomic() as c:
                job = c.execute(
                    "SELECT tenant_id,status FROM job WHERE id=?", (job_id,)
                ).fetchone()
                if not job or job["status"] != "done":
                    return
                if c.execute(
                        "SELECT id FROM knowledge "
                        "WHERE job_id=? AND source='auto' "
                        "AND deleted_at IS NULL LIMIT 1",
                        (job_id,)).fetchone():
                    return
                retro = c.execute(
                    "SELECT output_json FROM station_run "
                    "WHERE job_id=? AND station_idx=? "
                    "ORDER BY version DESC LIMIT 1",
                    (job_id, LAST_IDX),
                ).fetchone()
                out = db.jloads(retro["output_json"], {}) if retro else {}
                parts = []
                report = (out.get("report") or "").strip()
                if report:
                    parts.append(report[:500])
                topics = [
                    topic.get("title", "")
                    for topic in out.get("next_topics") or []
                    if topic.get("title")
                ]
                if topics:
                    parts.append("回流选题:" + "、".join(topics))
                for tip in out.get("profile_updates") or []:
                    parts.append(f"经验:{tip}")
                now = time.time()
                c.execute(
                    "INSERT INTO knowledge"
                    "(title,content,tags_json,source,job_id,tenant_id,"
                    "created_at,updated_at) "
                    "VALUES(?,?,?,'auto',?,?,?,?)",
                    (
                        f"《{title}》交付复盘",
                        "\n".join(parts) or "(复盘官未产出要点)",
                        json.dumps(["自动沉淀"], ensure_ascii=False),
                        job_id,
                        int(job["tenant_id"] or 1),
                        now,
                        now,
                    ),
                )
        except Exception as exc:
            log.error(
                "distill knowledge for job %s failed error_type=%s",
                job_id,
                type(exc).__name__,
            )

    def _job_tenant(self, job_id) -> int:
        r = db.one("SELECT tenant_id FROM job WHERE id=?", (job_id,))
        return (r or {}).get("tenant_id") or 1

    def _job_title(self, job_id):
        for idx in (4, 3):
            r = self._latest_run(job_id, idx)
            if r and r["output_json"]:
                o = db.jloads(r["output_json"])
                tc = o.get("title_candidates") or []
                if tc:
                    return tc[o.get("selected_title", 0) if o.get("selected_title", 0) < len(tc) else 0]
        return "(未产出标题)"

    async def _run_gate(self, job_id, brief) -> bool:
        """质检;不通过则 gate_blocked 并返回 False."""
        self.broadcast({"type": "gate_running", "job_id": job_id})
        outputs = self.collect_outputs(job_id)
        title, body = "", ""
        o4, o3 = outputs.get(4) or {}, outputs.get(3) or {}
        body = o4.get("body") or o3.get("body") or ""
        tc = o4.get("title_candidates") or o3.get("title_candidates") or [""]
        title = tc[0]
        progress, steps = self._recorder(job_id, "gate")
        g = await gate.check(title, body, brief.get("platforms") or ["小红书"],
                             research=outputs.get(1), progress=progress,
                             token=f"job{job_id}:gate")
        g["steps"] = steps
        gate_cost = float(g.pop("cost_usd", 0) or 0)
        gate_tokens = int(g.pop("tokens", 0) or 0)
        def _gate_tx():
            with db.atomic() as c:
                job = c.execute(
                    "SELECT status,billing_status FROM job WHERE id=?",
                    (job_id,),
                ).fetchone()
                if job and job["status"] == "paused":
                    return "paused"
                if not self._job_row_executable(job):
                    return "gone"
                cur = c.execute(
                    "UPDATE job SET gate_json=?,cost_usd=cost_usd+?,"
                    "tokens=tokens+?,status=?,updated_at=? "
                    "WHERE id=? AND billing_status='charged' "
                    "AND status IN ('running','awaiting_review','gate_blocked')",
                    (
                        json.dumps(g, ensure_ascii=False),
                        gate_cost,
                        gate_tokens,
                        "running" if g["passed"] else "gate_blocked",
                        time.time(),
                        job_id,
                    ),
                )
                return "persisted" if cur.rowcount == 1 else "gone"

        outcome = await db.arun(_gate_tx)
        paused = outcome == "paused"
        persisted = outcome == "persisted"
        if paused:   # 质检期间被老板打断：不落结论，恢复后重检。
            self.touch(job_id)
            return False
        if not persisted:  # cancelled/deleted/refunded 的终态永远优先。
            return False
        self.touch(job_id)
        if not g["passed"]:
            from . import notify
            notify.push(self._job_tenant(job_id), "gate",
                        {"job_id": job_id, "title": self._job_title(job_id)})
        return g["passed"]

    async def _execute(self, job_id, idx, cfg, brief, profile, version,
                       revision_note, prev_output) -> bool:
        def _open_tx():
            with db.atomic() as c:
                job = c.execute(
                    "SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
                if not self._job_row_executable(job):
                    return ("deleted" if not job else "cancelled"), None, 1
                tenant = int(job["tenant_id"] or 1)
                now = time.time()
                cur = c.execute(
                    "INSERT INTO station_run"
                    "(job_id,station_idx,skill_id,version,status,created_at,updated_at) "
                    "VALUES(?,?,?,?, 'running',?,?)",
                    (job_id, idx, cfg["skill"], version, now, now),
                )
                rid = cur.lastrowid
                changed = c.execute(
                    "UPDATE job SET status='running',current_idx=?,updated_at=? "
                    "WHERE id=? AND billing_status='charged' "
                    "AND status IN ('running','awaiting_review','gate_blocked')",
                    (idx, now, job_id),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("工单状态已变化，停止创建工位")
                return None, rid, tenant

        early, run_id, tenant_id = await db.arun(_open_tx)
        if early:
            return early
        self.touch(job_id)
        progress, steps = self._recorder(job_id, idx, run_id)
        ctx = {
            "job_id": job_id, "tenant_id": tenant_id,
            "brief": brief, "profile": profile,
            "outputs": self.collect_outputs(job_id),
            "model": providers.text_model_for(idx),
            "version": version, "today": time.strftime("%Y-%m-%d"),
            "revision_note": revision_note, "prev_output": prev_output,
            "progress": progress, "token": f"job{job_id}:{idx}",
        }
        t0 = time.time()
        last_err = None
        if revision_note:
            progress("start", f"v{version} 携带老板意见重做:{revision_note[:120]}")
        else:
            progress("start", f"v{version} 开始执行(岗位:{cfg['name']} / Skill:{cfg['skill']})")
        for attempt in range(MAX_RETRY + 1):
            before = db.one(
                "SELECT status,billing_status FROM job WHERE id=?", (job_id,))
            if not self._job_row_executable(before):
                await db.aexecute(
                    "UPDATE station_run SET status='cancelled',"
                    "review_comment='工单已取消',updated_at=? "
                    "WHERE id=? AND status IN ('running','queued')",
                    (time.time(), run_id),
                )
                return "deleted" if not before else "cancelled"
            try:
                if attempt:
                    progress("retry", f"第 {attempt + 1} 次尝试…")
                r = await cfg["run"](ctx)

                # provider 可能不响应 kill；返回后先重新确认业务终态，再读取/落库结果。
                after_provider = db.one(
                    "SELECT status,billing_status FROM job WHERE id=?", (job_id,))
                if not self._job_row_executable(after_provider):
                    await db.aexecute(
                        "UPDATE station_run SET status='cancelled',output_json=NULL,"
                        "review_comment='工单已取消，在途结果已丢弃',updated_at=? "
                        "WHERE id=? AND status IN ('running','queued')",
                        (time.time(), run_id),
                    )
                    return "deleted" if not after_provider else "cancelled"

                progress("done", f"产出完成 · 用时 {int(time.time() - t0)}s · "
                                 f"{r['tokens']} tokens · ${r['cost_usd']:.3f}")
                def _save_tx(r=r, steps_snapshot=json.dumps(steps, ensure_ascii=False)):
                    with db.atomic() as c:
                        current_job = c.execute(
                            "SELECT status,billing_status FROM job WHERE id=?",
                            (job_id,),
                        ).fetchone()
                        current_run = c.execute(
                            "SELECT status FROM station_run WHERE id=?",
                            (run_id,),
                        ).fetchone()
                        if not current_job:
                            return "deleted"
                        if not self._job_row_executable(current_job):
                            if current_run and current_run["status"] in (
                                    "running", "queued"):
                                c.execute(
                                    "UPDATE station_run SET status='cancelled',"
                                    "output_json=NULL,"
                                    "review_comment='工单已取消，在途结果已丢弃',"
                                    "updated_at=? WHERE id=? "
                                    "AND status IN ('running','queued')",
                                    (time.time(), run_id),
                                )
                            return "cancelled"
                        if not current_run:
                            return "deleted"
                        if current_run["status"] in ("stale", "interrupted"):
                            return current_run["status"]
                        if current_run["status"] != "running":
                            return "terminal"
                        now = time.time()
                        c.execute(
                            "UPDATE station_run SET output_json=?,latency_ms=?,"
                            "steps_json=?,tokens=?,cost_usd=?,updated_at=? "
                            "WHERE id=? AND status='running'",
                            (
                                json.dumps(r["data"], ensure_ascii=False),
                                int((time.time() - t0) * 1000),
                                steps_snapshot,
                                r["tokens"],
                                r["cost_usd"],
                                now,
                                run_id,
                            ),
                        )
                        updated = c.execute(
                            "UPDATE job SET cost_usd=cost_usd+?,"
                            "tokens=tokens+?,updated_at=? "
                            "WHERE id=? AND billing_status='charged' "
                            "AND status IN "
                            "('running','awaiting_review','gate_blocked')",
                            (r["cost_usd"], r["tokens"], now, job_id),
                        )
                        return "success" if updated.rowcount == 1 else "cancelled"

                result_state = await db.arun(_save_tx)
                if result_state == "stale":
                    return "stale"  # 执行期间上游被打回,本次产出作废待重算
                if result_state == "interrupted":
                    return "interrupted"  # 收尾瞬间被打断,以打断为准
                if result_state in ("cancelled", "deleted", "terminal"):
                    return result_state
                return True
            except (llm.LLMError, KeyError, TypeError, ValueError) as e:
                jnow = db.one(
                    "SELECT status,billing_status FROM job WHERE id=?", (job_id,))
                if not jnow or jnow["status"] in (
                        "cancelled", "done", "failed") or (
                        jnow["billing_status"] != "charged"):
                    await db.aexecute(
                        "UPDATE station_run SET status='cancelled',output_json=NULL,"
                        "review_comment='工单已结束，停止重试',updated_at=? "
                        "WHERE id=? AND status IN ('running','queued')",
                        (time.time(), run_id),
                    )
                    return "deleted" if not jnow else "cancelled"
                if jnow and jnow["status"] == "paused":
                    progress("error", "⏸ 老板按下打断,本工位停工(恢复后自动重跑)")
                    await db.aexecute(
                        "UPDATE station_run SET status='interrupted',steps_json=?,"
                        "review_comment='老板打断',updated_at=? "
                        "WHERE id=? AND status='running'",
                        (json.dumps(steps, ensure_ascii=False),
                         time.time(), run_id),
                    )
                    self.touch(job_id)
                    return "interrupted"
                last_err = e
                progress(
                    "error",
                    f"第 {attempt + 1} 次执行失败，正在安全重试",
                )
                log.warning(
                    "job%s station%s attempt%s failed: error_type=%s",
                    job_id,
                    idx,
                    attempt + 1,
                    type(e).__name__,
                )
        progress("error", f"重试 {MAX_RETRY} 次后仍失败,工位挂起等老板处理")

        def _fail_tx(steps_snapshot=json.dumps(steps, ensure_ascii=False)):
            with db.atomic() as c:
                current_job = c.execute(
                    "SELECT status,billing_status FROM job WHERE id=?",
                    (job_id,),
                ).fetchone()
                if not current_job:
                    return "deleted"
                if not self._job_row_executable(current_job):
                    return "cancelled"
                cur = c.execute(
                    "UPDATE station_run SET status='failed',latency_ms=?,"
                    "steps_json=?,review_comment=?,updated_at=? "
                    "WHERE id=? AND status='running'",
                    (
                        int((time.time() - t0) * 1000),
                        steps_snapshot,
                        providers.public_failure_message(last_err),
                        time.time(),
                        run_id,
                    ),
                )
                return "failed" if cur.rowcount == 1 else "raced"

        outcome = await db.arun(_fail_tx)
        if outcome in ("deleted", "cancelled"):
            return outcome
        if outcome != "failed":
            return "cancelled"
        return False

    # ---------- 上游产出汇总(供下游 ctx 与前端) ----------
    def collect_outputs(self, job_id):
        outputs = {}
        for r in db.q("SELECT * FROM station_run WHERE job_id=? AND status IN ('done','awaiting_review') "
                      "ORDER BY station_idx, version", (job_id,)):
            outputs[r["station_idx"]] = db.jloads(r["output_json"], {})
        return outputs

    # ---------- 工位完成的副作用 ----------
    @staticmethod
    def _finalize_station_tx(c, job_id, idx, out, tenant_id):
        """把工位副作用纳入调用方事务，避免取消/删除后生成孤儿资产。"""
        now = time.time()
        if idx == 0:
            selected = out.get("selected", 0)
            for i, topic in enumerate(out.get("topics", [])):
                if i == selected:
                    continue
                c.execute(
                    "INSERT INTO asset"
                    "(type,job_id,tenant_id,payload_json,created_at,updated_at) "
                    "VALUES('topic',?,?,?,?,?)",
                    (
                        job_id,
                        tenant_id,
                        json.dumps(topic, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
        elif idx == 9:
            for topic in out.get("next_topics", []):
                c.execute(
                    "INSERT INTO asset"
                    "(type,job_id,tenant_id,payload_json,created_at,updated_at) "
                    "VALUES('topic',?,?,?,?,?)",
                    (
                        job_id,
                        tenant_id,
                        json.dumps(topic, ensure_ascii=False),
                        now,
                        now,
                    ),
                )

    def finalize_station(self, job_id, idx, out):
        ten = self._job_tenant(job_id)
        with db.atomic() as c:
            self._finalize_station_tx(c, job_id, idx, out, ten)

    def _mark_downstream_stale_tx(self, c, job_id, idx):
        """只把每个下游工位的最新版本置 stale，且与审批 CAS 同事务。"""
        c.execute(
            "UPDATE station_run SET status='stale',updated_at=? "
            "WHERE job_id=? AND station_idx>? "
            "AND status IN ('done','awaiting_review','running') "
            "AND version=("
            "SELECT MAX(s2.version) FROM station_run s2 "
            "WHERE s2.job_id=station_run.job_id "
            "AND s2.station_idx=station_run.station_idx)",
            (time.time(), job_id, idx),
        )

    # ---------- 用户动作 ----------
    def user_action(self, job_id, idx, action, payload=None):
        payload = payload or {}
        if action not in ("approve", "edit", "reject", "rerun"):
            raise ValueError(f"未知动作 {action}")

        with db.atomic() as c:
            job_row = c.execute(
                "SELECT id,tenant_id,status,billing_status FROM job WHERE id=?",
                (job_id,),
            ).fetchone()
            if not job_row:
                raise ValueError("工单不存在")
            if (job_row["billing_status"] != "charged"
                    or job_row["status"] not in JOB_EXECUTABLE_STATUSES):
                raise ValueError(
                    "该工单已取消、失败、完成或退款，不能继续审批/重跑；"
                    "请新建工单")

            run_row = c.execute(
                "SELECT * FROM station_run WHERE job_id=? AND station_idx=? "
                "ORDER BY version DESC LIMIT 1",
                (job_id, idx),
            ).fetchone()
            if not run_row:
                raise ValueError("工位尚未执行")
            run = dict(run_row)
            out = db.jloads(run["output_json"], {})
            now = time.time()

            if action in ("approve", "edit"):
                if run["status"] != "awaiting_review":
                    raise ValueError(
                        f"该工位{status_cn(run['status'])},现在不需要审批；请刷新页面看最新状态")
                edits = payload.get("edits") or {}
                out.update(edits)
                if "selected" in payload:
                    out["selected"] = payload["selected"]
                if "selected_title" in payload:
                    out["selected_title"] = payload["selected_title"]
                changed = c.execute(
                    "UPDATE station_run SET status='done',output_json=?,"
                    "review_comment=?,updated_at=? "
                    "WHERE id=? AND status='awaiting_review'",
                    (
                        json.dumps(out, ensure_ascii=False),
                        payload.get("comment"),
                        now,
                        run["id"],
                    ),
                )
                if changed.rowcount != 1:
                    raise ValueError("这个工位的状态刚刚更新了(可能已自动推进或被他人操作),刷新页面看最新状态")
                self._finalize_station_tx(
                    c,
                    job_id,
                    idx,
                    out,
                    int(job_row["tenant_id"] or 1),
                )
                self._mark_downstream_stale_tx(c, job_id, idx)
                target_idx = None
            elif action == "reject":
                if run["status"] != "awaiting_review":
                    raise ValueError(
                        f"该工位{status_cn(run['status'])},现在不能打回；请刷新页面看最新状态")
                comment = (payload.get("comment") or "").strip()
                if not comment:
                    raise ValueError("打回必须填写修改意见")
                changed = c.execute(
                    "UPDATE station_run SET status='rejected',review_comment=?,"
                    "updated_at=? WHERE id=? AND status='awaiting_review'",
                    (comment, now, run["id"]),
                )
                if changed.rowcount != 1:
                    raise ValueError("这个工位的状态刚刚更新了(可能已自动推进或被他人操作),刷新页面看最新状态")
                self._mark_downstream_stale_tx(c, job_id, idx)
                target_idx = idx
            else:  # rerun：仅允许仍在进行中的整单重跑已交付/待审批工位。
                if run["status"] not in ("done", "awaiting_review"):
                    raise ValueError(
                        f"该工位{status_cn(run['status'])},不能重跑；失败的工单请重新开单")
                comment = (payload.get("comment") or "").strip()
                changed = c.execute(
                    "UPDATE station_run SET status='rejected',review_comment=?,"
                    "updated_at=? WHERE id=? AND status IN ('done','awaiting_review')",
                    (comment or "老板要求重跑", now, run["id"]),
                )
                if changed.rowcount != 1:
                    raise ValueError("这个工位的状态刚刚更新了(可能已自动推进或被他人操作),刷新页面看最新状态")
                self._mark_downstream_stale_tx(c, job_id, idx)
                target_idx = idx

            if target_idx is None:
                job_changed = c.execute(
                    "UPDATE job SET status='running',updated_at=? "
                    "WHERE id=? AND status=? AND billing_status='charged'",
                    (now, job_id, job_row["status"]),
                )
            else:
                job_changed = c.execute(
                    "UPDATE job SET status='running',current_idx=?,updated_at=? "
                    "WHERE id=? AND status=? AND billing_status='charged'",
                    (target_idx, now, job_id, job_row["status"]),
                )
            if job_changed.rowcount != 1:
                raise ValueError(
                    "工单已取消、失败或退款；请新建工单")
        self.notify(job_id)
        self.touch(job_id)

    def _mark_downstream_stale(self, job_id, idx):
        with db.atomic() as c:
            self._mark_downstream_stale_tx(c, job_id, idx)

    # ---------- 打断 / 恢复 ----------
    def pause(self, job_id):
        """老板打断:立即终止该工单所有运行中的 LLM 调用,工单挂起等恢复."""
        with db.atomic() as c:
            job = c.execute(
                "SELECT status,billing_status FROM job WHERE id=?",
                (job_id,),
            ).fetchone()
            if not job:
                raise ValueError("工单不存在")
            if (job["billing_status"] != "charged"
                    or job["status"] not in JOB_EXECUTABLE_STATUSES):
                raise ValueError(f"工单{status_cn(job['status'])},不需要打断")
            now = time.time()
            changed = c.execute(
                "UPDATE job SET status='paused',updated_at=? "
                "WHERE id=? AND status=? AND billing_status='charged'",
                (now, job_id, job["status"]),
            )
            if changed.rowcount != 1:
                raise ValueError("工单状态刚刚更新了(可能已被他人操作或已推进),刷新页面按最新状态处理")
            c.execute(
                "UPDATE station_run SET status='interrupted',"
                "review_comment='老板打断',updated_at=? "
                "WHERE job_id=? AND status='running'",
                (now, job_id),
            )
        killed = llm.kill(f"job{job_id}:")
        log.info("job%s 被老板打断,终止了 %s 个进行中的调用", job_id, killed)
        self.touch(job_id)

    def resume(self, job_id):
        """恢复开工:被打断的工位自动重跑."""
        with db.atomic() as c:
            job = c.execute(
                "SELECT status,billing_status FROM job WHERE id=?",
                (job_id,),
            ).fetchone()
            if (not job or job["status"] != "paused"
                    or job["billing_status"] != "charged"):
                raise ValueError("工单不在可恢复的暂停状态")
            now = time.time()
            # 只改每个工位的最新版本，历史版本保留审计原貌。
            c.execute(
                "UPDATE station_run SET status='rejected',"
                "review_comment='此前执行被老板打断,恢复后重新完整完成本工位。',"
                "updated_at=? "
                "WHERE job_id=? AND status='interrupted' "
                "AND version=("
                "SELECT MAX(s2.version) FROM station_run s2 "
                "WHERE s2.job_id=station_run.job_id "
                "AND s2.station_idx=station_run.station_idx)",
                (now, job_id),
            )
            changed = c.execute(
                "UPDATE job SET status='running',updated_at=? "
                "WHERE id=? AND status='paused' AND billing_status='charged'",
                (now, job_id),
            )
            if changed.rowcount != 1:
                raise ValueError("工单状态刚刚更新了(可能已被他人操作或已推进),刷新页面按最新状态处理")
        self.notify(job_id)
        self.touch(job_id)

    def gate_action(self, job_id, action):
        with db.atomic() as c:
            job = c.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise ValueError("工单不存在")
            if (job["billing_status"] != "charged"
                    or job["status"] not in JOB_EXECUTABLE_STATUSES):
                raise ValueError(
                    "该工单已取消、失败、完成或退款；请新建工单")
            g = db.jloads(job["gate_json"], {})
            now = time.time()
            if action == "recheck":
                changed = c.execute(
                    "UPDATE job SET gate_json=NULL,status='running',updated_at=? "
                    "WHERE id=? AND status=? AND billing_status='charged'",
                    (now, job_id, job["status"]),
                )
            elif action == "override":
                g["override"] = True
                g["report"] = (
                    (g.get("report") or "")
                    + " |⚠️ 老板已知晓风险并选择继续"
                )
                changed = c.execute(
                    "UPDATE job SET gate_json=?,status='running',updated_at=? "
                    "WHERE id=? AND status=? AND billing_status='charged'",
                    (
                        json.dumps(g, ensure_ascii=False),
                        now,
                        job_id,
                        job["status"],
                    ),
                )
            else:
                raise ValueError("未知动作")
            if changed.rowcount != 1:
                raise ValueError("工单状态刚刚更新了(可能已被他人操作或已推进),刷新页面按最新状态处理")
        self.notify(job_id)
        self.touch(job_id)


engine = Engine()

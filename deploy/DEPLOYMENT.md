# 旧版发布文档（已停用）

本文件记录的不可变 release、固定 control-plane、离线 wheelhouse 和旧
`contentcrew.service` 发布链已经停用，不再是生产发布入口。仓库不再携带这套
旧发布脚本、systemd unit 或 Caddy 启动闸门；本文已停用，不能作为生产操作手册，
不要按本文历史命令部署、升级或恢复生产。

当前唯一的生产发布入口是：

[`deploy/simple/README.md`](simple/README.md)

它覆盖预检、在线备份、发布、健康检查、自动回滚和备份定时器配置。备份文件
校验、恢复演练、素材快照和异地同步的通用说明仍见：

[`deploy/BACKUP_RECOVERY.md`](BACKUP_RECOVERY.md)

服务器上可能仍留有旧 unit 或控制面时，应先按简易通道文档的迁移章节确认
`paihuo` 已稳定运行，再由有权限的运维人员按项目交接单执行精确清理。不要仅
因为本仓库删除了旧文件，就假定服务器上的旧 unit、发布目录或控制面已经消失。

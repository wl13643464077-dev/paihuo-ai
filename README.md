# 派活 PaiHuo

派活是一套面向企业的 AI 数字员工协作平台：老板下达任务，数字员工通过可审计的工作流执行，并交付可追溯成果。

线上产品：[https://paihuo.ai](https://paihuo.ai)

## 核心能力

- 数字员工按全局或员工维度自由选择文本、生图模型
- 统一云端模型与受控联网能力网关，不依赖本地账号登录态
- 全局任务中心集中查看进行中、待处理、完成和失败任务
- 线索雷达保留经过安全校验的原帖链接，方便回溯
- AI 会议按“提案 → 反向验证 → 决策 → 执行”强制收敛
- 多租户权限隔离；非管理员只看到数字员工的公开介绍
- 异步任务具备超时、看门狗、幂等重试和计费补偿

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
./run.sh
```

默认仅监听 `127.0.0.1:8899`。运行数据会写入 `data/`，该目录下的数据库、素材、备份和凭据均被 Git 忽略。

模型供应商凭据请在本地管理后台配置。不要把真实 API Key、Cookie、账号密码、生产数据库或用户素材提交到仓库。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q app deploy
node --check static/app.js
```

每个 Pull Request 都会自动执行同等检查。

## 协作方式

1. 从 `main` 创建 `feature/*` 或 `fix/*` 分支。
2. 完成修改并补充测试。
3. 提交 Pull Request，说明影响范围、验证结果和回滚方式。
4. CI、代码审查和安全审查通过后才能合并。
5. `main` 只代表通过评审的产品源码，不会自动部署生产。

生产发布由项目所有者从已审核提交构建不可变制品，完成备份、迁移、健康检查和回滚验证后，才会进入 `paihuo.ai`。

详细规则见 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [SECURITY.md](SECURITY.md)。

## 许可

当前仓库暂未附加开源许可证。代码可用于本项目协作与 Pull Request 评审；如需复制、分发或用于其他项目，请先取得项目所有者许可。

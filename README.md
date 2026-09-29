# Fund Signal

一个公开的、轻量级基金研究工作台：展示基金行情、策略信号、资讯摘要和历史回测。项目的目标是让个人能够复核数据与策略假设，而不是提供自动交易或荐股服务。

> 重要：本仓库已公开，但所有输出仅供学习、研究和自行验证。它不构成投资建议、要约、保证收益、资产管理或任何形式的交易指令。

## 功能

- 基金行情与估值展示
- 多策略信号计算与历史回测
- 市场资讯聚合与分类
- 账户登录、访客演示与会话管理
- 可选的 PostgreSQL 持久化
- 可选的 [xalpha](https://github.com/refraction-ray/xalpha) 交易流水组合分析：组合汇总与资金加权收益率（XIRR）

## 本地运行

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/OWNER/fund-signal-public.git
cd fund-signal-public
export FS_USER=admin
export FS_PASSWORD='replace-with-a-strong-password'
python3 server.py --port 8787
```

访问 `http://127.0.0.1:8787`。请使用随机强密码，且不要在 shell 历史、截图、日志或仓库中保存它。

### 账户注册

默认只能通过管理员生成的邀请码注册。若你明确要把自己部署的实例开放为自助注册服务，设置 `FS_ALLOW_PUBLIC_REGISTER=1` 后重启服务；这会允许任何访客创建账号。

```bash
export FS_ALLOW_PUBLIC_REGISTER=1
```

开放注册前请至少配置 HTTPS、限流/WAF、监控和可用的邮件或人工审核流程。公开 GitHub 仓库不等于应该把运行中的服务也无门槛开放；共享部署通常更适合默认使用邀请码。

## 可选：xalpha 组合分析

主服务不依赖 xalpha。要启用“组合分析”页面，请在同一个 Python 环境中安装可选依赖后重启服务：

```bash
pip install -r requirements-xalpha.txt
```

页面接受 CSV 格式 `date,fund,trade`。这是 xalpha 的原始流水约定：正数 `trade` 是申购金额，负数是赎回份额；不要把两种单位混用。导入只在本次请求内计算，不会由该功能写入应用存储，但 xalpha 可能会向第三方数据提供方请求基金信息。

xalpha 由 [refraction-ray/xalpha](https://github.com/refraction-ray/xalpha) 提供并采用 MIT License。本项目仅以可选依赖方式调用其公开 API，未复制其源代码；若未来复制或分发其代码，必须保留其完整许可证和版权声明。

## Docker

```bash
docker build -t fund-signal .
docker run --rm -p 8787:8787 \
  -e FS_USER=admin \
  -e FS_PASSWORD='replace-with-a-strong-password' \
  fund-signal
```

如需 PostgreSQL，请按 [docker-compose.prod.yml](docker-compose.prod.yml) 中的环境变量配置数据库。部署前请替换示例密码，并将数据库置于受限网络中。

## 公开仓库与安全警告

- 本公共版本不应包含生产配置、真实交易流水、账户、密钥、个人身份信息、内网地址或网络拓扑。提交前请检查 `git diff --cached`，并轮换任何曾经误提交的密钥。
- 应用的 API Key、Webhook、数据库密码和管理员密码属于敏感信息；仅通过受保护的环境变量、密钥管理服务或部署平台注入。不要把 `.env`、备份、日志或浏览器导出文件推送到 GitHub。
- 设置页不会回显已保存的 AI API Key；留空保存代表保留原密钥。仍建议为每个部署使用独立、可撤销的密钥。
- 默认账户名或示例密码绝不能直接用于公网部署。应启用 HTTPS、反向代理访问控制、最小权限数据库账户和定期备份；公开部署前也应完成依赖漏洞检查。
- 公开可见不等同于已授予第三方使用、复制或再分发代码的许可证。本仓库当前未附带本项目自身的许可证文本；维护者应在接受外部贡献或允许再分发前，明确选择并提交适用许可证。

## 数据与投资风险

- 行情、估值、净值、资讯及 xalpha 结果均来自第三方来源，可能延迟、缺失、变更、错误或受访问频率和服务条款限制。请自行取得必要授权并遵守各数据源条款。
- 回测使用历史数据，存在幸存者偏差、滑点、申赎费、税费、停牌、限购、分红、估值方法变化等未完全覆盖因素。过去表现不代表未来结果。
- 本项目不会代替你的风险评估。下单、申购、赎回、仓位与止损决策应由使用者独立完成；请先核对基金公告、交易规则和自身风险承受能力。
- 不要将券商、银行卡、身份证、完整持仓截图或其他个人数据输入本项目或提交 issue。公开 issue 与 commit 历史通常不可彻底删除。

## 开发检查

```bash
python3 -m unittest discover -s tests
python3 -m py_compile server.py fund_core.py xalpha_adapter.py
```

## 依赖与数据源

- 可选组合分析：[xalpha](https://github.com/refraction-ray/xalpha)
- 行情与资讯接口：见 `fund_core.py`。接口可用性与使用条款可能变化，部署者应自行验证。

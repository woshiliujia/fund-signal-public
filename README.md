# Fund Signal

一个用于展示基金行情、策略信号和资讯摘要的轻量级 Web 应用。项目仅供研究和学习使用，不构成任何投资建议。

## 功能

- 基金行情与估值展示
- 多策略信号计算与历史回测
- 市场资讯聚合与分类
- 账户登录、访客演示与会话管理
- 可选的 PostgreSQL 持久化

## 本地运行

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/OWNER/fund-signal-public.git
cd fund-signal-public
python3 server.py --port 8787
```

访问 `http://127.0.0.1:8787`。首次运行请通过环境变量设置管理员账号和密码：

```bash
export FS_USER=admin
export FS_PASSWORD='change-this-password'
```

## Docker

```bash
docker build -t fund-signal .
docker run --rm -p 8787:8787 \
  -e FS_USER=admin \
  -e FS_PASSWORD='change-this-password' \
  fund-signal
```

如需 PostgreSQL，请按 [docker-compose.prod.yml](docker-compose.prod.yml) 中的环境变量配置数据库；不要把真实密码、令牌、内网地址或个人信息提交到仓库。

## 安全与数据

- 本公共版本不包含生产配置、运行数据、账户、密钥、域名或网络拓扑。
- API 密钥仅应通过运行时环境变量或受保护的部署平台注入。
- 信号与回测结果仅用于研究，不保证准确性或收益。

## 许可证

请在使用或分发前确认依赖与数据源的各自许可证和使用条款。

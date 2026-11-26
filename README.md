# WorkBuddy2API 后台

可自托管的 HTTP 网关与管理面板。支持转发已授权的 WorkBuddy 原生桥接，或按模型分组连接你配置的 Chat Completions、Responses 和 Messages 服务。管理面板显示已配置的接口与匿名请求计数。

## 本机启动

需要 Python 3.11 或更新版本，只使用标准库。

1. 将 `gateway.example.toml` 复制成 `gateway.local.toml`。
2. 在运行环境中设置 `WB_GATEWAY_TOKEN`。选择至少 32 字符的随机值，或通过 `WB_GATEWAY_TOKEN_FILE` 指向已保护的本机文件。
3. 执行 `python gateway.py --config gateway.local.toml`。在浏览器打开 `http://127.0.0.1:8788`，输入网关密钥查看状态。

可用本机命令 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成独立的网关密钥。网关密钥与各上游凭据分别保存。每个上游的 `token_env` 只填写环境变量名称，`变量名_FILE` 也可读取外部密钥文件。

## 配置上游

- **WorkBuddy 原生桥接**。启用 `[native]`，填写已安装的 WorkBuddy 桥接实际提供的地址，路径为 `/workbaby/v1`。配置对应的本机服务凭据后，通过网关的同一路径使用会话、事件回放、取消和显式审批。
- **OpenAI 兼容或 Messages 服务**。启用一个或多个 `[groups.v1]` 至 `[groups.v10]`，配置该分组的上游 API 基址、模型 ID 和接口。Messages 服务可用 `auth = "x-api-key"`。独立鉴权的上游由部署者自行配置。
- 客户端把网关密钥放进 `Authorization: Bearer …`。比如 `GET /v1/models` 返回本地允许的模型，`POST /v1/chat/completions` 向该分组的上游发出一次请求。自托管容器或反代可显式设置 `listen.host` 和 `listen.port`，部署者需提供可信网络或 TLS。

源代码以 MIT 许可发布，保留上游 `ardeyouxipianyi/workbuddy2api-hub` 的许可文本。公开版携带该项目的流式请求适配器，运行时上游和凭据配置由部署者提供。

## 本机加密导出凭据

先执行 `python -m pip install -r requirements-backup.txt`。导出只读取部署者在上游 `token_env` 或 `[backup].credentials` 中登记、并在命令里逐个选定的凭据。本机命令交互读取备份口令，生成加密文件。

```text
python credential_backup.py export --config gateway.local.toml --credential WB_NATIVE_TOKEN --output account.wbgbackup
python credential_backup.py restore --input account.wbgbackup --directory restored-secrets
```

可以重复 `--credential` 选择多个凭据，例如同一账号的访问令牌和刷新令牌。恢复会新建当前用户的密钥目录，每个文件可用相应的 `变量名_FILE` 接入网关。网关管理密钥单独管理。备份口令和配置文件由部署者另行保存。

## 测试

`python -m unittest -v test_gateway test_credential_backup`

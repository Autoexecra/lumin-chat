# lumin-chat 许可证保护说明

## 1. 设计目标

本项目源码以 Apache License 2.0 开源发布。

同时，为了满足交付场景中的授权控制需求，项目提供**可选**的运行时许可证校验能力。该能力默认关闭，不影响开源版本的本地开发与学习使用。

## 2. 配置方式

在配置文件中增加：

```json
"license": {
  "enabled": true,
  "subject": "lumin-chat",
  "license_file": "/etc/lumin-chat/license.json",
  "secret_env": "LUMIN_CHAT_LICENSE_SECRET",
  "secret": ""
}
```

建议仅使用环境变量 `LUMIN_CHAT_LICENSE_SECRET` 提供签名密钥。

## 3. 许可证文件格式

许可证文件是一个 JSON 文档，包含 `payload` 与 `signature` 两部分。

示例：

```json
{
  "payload": {
    "subject": "lumin-chat",
    "issued_to": "demo-user",
    "expires_at": "2027-03-11T00:00:00Z",
    "hostnames": [
      "tl3588"
    ]
  },
  "signature": "<hmac-sha256>"
}
```

## 4. 校验规则

当前版本会校验：

- `subject` 必须匹配 `lumin-chat`
- `signature` 必须与 `payload` 对应
- `expires_at` 未过期
- 如果声明了 `hostnames`，则当前主机名必须命中

## 5. 生成方式

可使用以下脚本生成许可证：

```bash
export LUMIN_CHAT_LICENSE_SECRET="your-secret"
python3 scripts/generate_license.py \
  --expires-at 2027-03-11T00:00:00Z \
  --issued-to demo-user \
  --hostname tl3588 \
  --output /tmp/lumin-chat-license.json
```

## 6. 注意事项

- 这是面向交付流程的授权校验，不是对抗性防破解方案
- 请不要把真实签名密钥提交到 Git 仓库
- 生产环境建议将许可证文件部署到 `/etc/lumin-chat/license.json`
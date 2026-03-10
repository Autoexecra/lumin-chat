# lumin-chat Docker Ubuntu 测试报告

## 1. 测试环境

- 生成时间: 2026-03-10 15:46:35
- 目标主机: yanggan@tl3588:22
- 远端目录: /var/lib/lumin-chat
- 通过情况: 9/9

## 2. 总结

本次远端部署、项目冒烟与 Docker Ubuntu 非交互测试全部通过。

## 3. 详细结果

### 3.1 检查 RPM 安装状态 [通过]

命令：
```bash
rpm -q lumin-chat
```

退出码：0

标准输出：
```text
lumin-chat-0.1.0-1.noarch
```

标准错误：
```text
Warning: Permanently added '192.168.0.11' (ED25519) to the list of known hosts.
```

### 3.2 检查 Docker 版本 [通过]

命令：
```bash
docker --version
```

退出码：0

标准输出：
```text
Docker version 20.10.25-ce, build 911449ca24
```

标准错误：
```text
Warning: Permanently added '192.168.0.11' (ED25519) to the list of known hosts.
```

### 3.3 检查系统配置文件 [通过]

命令：
```bash
test -f /etc/lumin-chat/config.json && python3 - <<'PY'
import json
with open('/etc/lumin-chat/config.json', 'r', encoding='utf-8') as handle:
    payload = json.load(handle)
print(payload.get('app', {}).get('default_model_level', 'missing'))
PY
```

退出码：0

标准输出：
```text
1
```

标准错误：
```text
Warning: Permanently added '192.168.0.11' (ED25519) to the list of known hosts.
```

### 3.4 检查启动脚本帮助信息 [通过]

命令：
```bash
/usr/bin/lumin-chat --help
```

退出码：0

标准输出：
```text
usage: main.py [-h] [--config CONFIG] [--model-level MODEL_LEVEL]
               [--approval-mode {prompt,auto,read-only}]
               [--command-policy-mode {blacklist,whitelist}]
               [--workdir WORKDIR] [--session SESSION] [--show-thinking]
               [--hide-thinking]
               {chat,ask} ...

lumin-chat 终端代理

positional arguments:
  {chat,ask}
    chat                启动交互式对话
    ask                 执行一次非交互请求

options:
  -h, --help            show this help message and exit
  --config CONFIG       配置文件路径
  --model-level MODEL_LEVEL
                        要使用的模型级别
  --approval-mode {prompt,auto,read-only}
                        工具审批模式
  --command-policy-mode {blacklist,whitelist}
                        Shell 命令策略模式
  --workdir WORKDIR     初始工作目录
  --session SESSION     按会话 ID 或路径恢复历史会话
  --show-thinking       强制显示 thinking 流
  --hide-thinking       隐藏 thinking 流
```

标准错误：
```text
Warning: Permanently added '192.168.0.11' (ED25519) to the list of known hosts.
```

### 3.5 执行项目冒烟测试 [通过]

命令：
```bash
cd /var/lib/lumin-chat && python3 scripts/smoke_test.py
```

退出码：0

标准输出：
```text
smoke_test: ok
```

标准错误：
```text
Warning: Permanently added '192.168.0.11' (ED25519) to the list of known hosts.
```

### 3.6 拉取 Ubuntu 镜像 [通过]

命令：
```bash
docker pull ubuntu:latest
```

退出码：0

标准输出：
```text
latest: Pulling from library/ubuntu
Digest: sha256:d1e2e92c075e5ca139d51a140fff46f84315c0fdce203eab2807c7e495eff4f9
Status: Image is up to date for ubuntu:latest
docker.io/library/ubuntu:latest
```

标准错误：
```text
Warning: Permanently added '192.168.0.11' (ED25519) to the list of known hosts.
```

### 3.7 读取 Ubuntu 系统信息 [通过]

命令：
```bash
docker run --rm ubuntu:latest sh -lc 'cat /etc/os-release | sed -n "1,8p"'
```

退出码：0

标准输出：
```text
PRETTY_NAME="Ubuntu 24.04.4 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
VERSION="24.04.4 LTS (Noble Numbat)"
VERSION_CODENAME=noble
ID=ubuntu
ID_LIKE=debian
HOME_URL="https://www.ubuntu.com/"
```

标准错误：
```text
Warning: Permanently added '192.168.0.11' (ED25519) to the list of known hosts.
```

### 3.8 验证容器内文件写入 [通过]

命令：
```bash
docker run --rm ubuntu:latest sh -lc 'mkdir -p /tmp/lumin-chat && echo ready > /tmp/lumin-chat/status.txt && cat /tmp/lumin-chat/status.txt'
```

退出码：0

标准输出：
```text
ready
```

标准错误：
```text
Warning: Permanently added '192.168.0.11' (ED25519) to the list of known hosts.
```

### 3.9 验证容器内目录遍历 [通过]

命令：
```bash
docker run --rm ubuntu:latest sh -lc 'pwd && ls / | sed -n "1,20p"'
```

退出码：0

标准输出：
```text
/
bin
boot
dev
etc
home
lib
media
mnt
opt
proc
root
run
sbin
srv
sys
tmp
usr
var
```

标准错误：
```text
Warning: Permanently added '192.168.0.11' (ED25519) to the list of known hosts.
```

# lumin-chat RPM 打包与部署说明

## 1. 本地构建 RPM

在项目根目录执行：

- `python3 scripts/build_rpm.py`

默认产物输出到 `dist/rpm/`。

## 2. 直接部署到测试板

推荐命令：

- `python3 deploy.py --host tl3588 --port 22 --run-tests`

默认使用 RPM 打包模式，流程如下：

1. 本地构建 RPM。
2. 上传到目标主机临时目录。
3. 执行 `rpm -Uvh --replacepkgs --force` 安装。
4. 自动验证 `/etc/lumin-chat/config.json`、`/usr/bin/lumin-chat` 和 `/var/lib/lumin-chat`。
5. 生成中文测试报告。

## 3. 通过构建服务器构建后再部署

如果需要先在构建服务器编译，再部署到测试板，可执行：

- `python3 deploy.py --host tl3588 --port 22 --use-build-server --build-host <构建机> --build-port <端口> --run-tests`

脚本会：

1. 上传精简源码到构建服务器。
2. 在构建服务器执行 `compileall`。
3. 调用 `scripts/build_rpm.py` 生成 RPM。
4. 下载 RPM 到本地。
5. 上传并安装到测试板。
6. 自动执行回归测试。

## 4. 安装后目录布局

- 程序目录：`/var/lib/lumin-chat`
- 配置文件：`/etc/lumin-chat/config.json`
- 启动命令：`/usr/bin/lumin-chat`

## 5. 运行方式

安装成功后，直接执行：

- `lumin-chat --help`
- `lumin-chat chat`
- `lumin-chat ask "检查当前环境"`

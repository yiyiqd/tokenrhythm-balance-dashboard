# TokenRhythm 余额看板 (基元律动)

轻量级多账号余额监控看板，用于 [TokenRhythm](https://tokenrhythm.studio) 的 API 余额/用量查询。

纯 Python 标准库实现，**零第三方依赖**。单文件后端 + 单文件前端，部署即用。

## 功能

- 多账号批量查询余额、消费、调用次数、Token 用量
- 按模型聚合的消耗分布排行（需逐个 Key 查询）
- API Key 管理：列出 / 创建 / 删除 / 绑定完整 Key
- 自动后台刷新（默认 1800 秒），数据落盘缓存
- 凭据存本地文件（权限 0600），前端只拿展示字段，不接触凭据明文
- 支持三种凭据导入格式：Netscape cookie 文件（F12 导出）、整行 Cookie、纯 Token

## 快速开始

```bash
python3 server.py
# 或后台运行
nohup python3 server.py &
```

启动后浏览器打开 `http://<服务器IP>:9155`。

### 配置账号

在程序目录下编辑 `accounts.txt`，每行一个账号，支持格式：

```
备注名 sess_xxxxxxxxxxxxxxxx
sess_xxxxxxxxxxxxxxxx
tr_session=sess_xxx; tr_ref_device=yyy
```

也可以用网页端的「导入」功能批量添加（支持浏览器 F12 导出的 Netscape cookie 文件）。

## 项目结构

```
server.py      # 后端：HTTP 服务 + 上游 API 封装 + 数据缓存
index.html     # 前端：单文件页面（余额卡片 / 模型排行 / Key 管理）
accounts.txt   # 账号凭据（本地文件，请勿提交到仓库）
data.json      # 查询结果缓存（运行时自动生成）
keys_full.json # 本地保存的完整 Key（本地文件，请勿提交到仓库）
```

## 安全说明

- 所有凭据文件（`accounts.txt`、`keys_full.json`、cookie 文件）均为本地文件，写入时自动设置 `0600` 权限
- 建议将服务绑定在内网，或配合反向代理 + 认证使用
- 本工具仅调用 TokenRhythm 官方 API，不涉及任何破解或逆向

## 免责声明

本项目仅供个人学习与自用参考。使用本工具产生的任何后果（包括但不限于账号异常、资金损失）由使用者自行承担，与项目作者无关。

## License

[MIT](./LICENSE)

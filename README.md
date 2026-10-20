# TGChannelHub — Telegram 频道视频中转站

> **🌐 [English](README.en.md) | [中文](README.md)**

> 把 Telegram 频道变成你自己的**网页版视频站**：无需下载到本地，访客打开链接就能在线看/播放频道里的视频和图片。

**TGChannelHub** 是一个自托管的 Telegram 频道媒体代理。它以你的服务器为中转，实时从 Telegram 拉流，浏览器直接播放——**视频不落盘、支持拖动进度、微信直接看**。支持**多频道管理**，一个服务同时代理多个频道，每个频道独立分享链接。

基于 [tgfilestream](https://mau.dev/tulir/tgfilestream)（AGPL-3.0, Tulir Asokan）深度改造，从单频道 bot 重构为多频道可视化管理系统。

## ✨ 特性

| 特性 | 说明 |
|---|---|
| 🎬 实时流式中转 | 边拉边播，不占磁盘；HTTP Range 支持拖动进度 |
| 📚 多频道管理 | 网页管理页增删频道，一键复制每个频道的专属分享链接 |
| 🖼️ Telegram 相册聚合 | 同组媒体合并成一张卡片：首图大图 + 网格排列 |
| 📱 微信/手机可直接播 | 针对微信 X5 内核优化（playsinline、正确 Range） |
| 🔒 双密码体系 | 分享密码（访客）+ 管理密码（管理员）分开 |
| 🚀 并发加速 | 多线程分块下载 + DC 连接预热，启动快 |
| 🏷️ 标签浏览 | 高频 #标签栏 + 全量标签云 + caption 内 #标签可点，按系列筛选 |
| 🖼️ 图片灯箱 | 点击图片全屏查看，触摸/箭头/键盘滑动切换上/下一张 |
| 🛡 广告过滤 | 关键词 + 链接黑名单，命中消息整条隐藏（含媒体），管理页热配置 |
| 📤 上传中心 | 把 TG 群组当个人网盘：网页选文件/目录上传（并行加速、断点、监控） |
| 🚪 登录引导页 | 未登录时网页内引导填手机号/验证码/2FA，无需进容器敲命令 |

## 🚀 快速开始（推荐 Docker）

### 1. 准备 Telegram API 凭证

到 [my.telegram.org/apps](https://my.telegram.org/apps) 登录，创建一个应用，拿到：

- `api_id`（一串数字）
- `api_hash`（一串字母数字）

> ⚠️ 这是你的**账号凭证**，妥善保管，不要提交到公开仓库。

### 2. 获取 docker-compose.yml

```bash
git clone https://github.com/DYJ123-del/TGChannelHub.git
cd TGChannelHub
```

项目根目录已带 `docker-compose.yml`，按需修改：

```yaml
services:
  tgchannelhub:
    build: .
    container_name: tgchannelhub
    restart: unless-stopped
    ports:
      - "8081:8081"        # 对外端口，可改成 8080/56200 等
    volumes:
      - ./data:/app/data   # 所有数据落在这里（见下方"数据都存哪"）
    environment:
      - TZ=Asia/Shanghai
      # ---- Telegram API 凭证（必填）----
      - TG_API_ID=1234567
      - TG_API_HASH=abcdef0123456789abcdef0123456789
      # ---- 服务监听 ----
      - PORT=8081
      - HOST=0.0.0.0
      # ---- 公网地址（分享链接用它生成，反代后填 https://你的域名）----
      - PUBLIC_URL=http://192.168.1.100:8081
      # ---- 密码（部署后立刻改！）----
      - TG_UI_PASSWORD=请改成你的分享密码
      - TG_ADMIN_PASSWORD=请改成你的管理密码
      # ---- Telegram 会话文件（默认放 data 下）----
      - TG_SESSION_NAME=/app/data/tgchannelhub
      # ---- 代理（可选；直连 TG 超时/受限网络才需要）----
      # - TG_PROXY_HOST=192.168.1.2
      # - TG_PROXY_PORT=7890
      # - TG_PROXY_USER=user
      # - TG_PROXY_PASS=pass
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/', timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
```

> 仓库里已有可直接使用的 `docker-compose.yml`，拷贝改环境变量即可。没有代理需求的直接把 `TG_PROXY_*` 那几行删掉。

### 3. 启动 + 首次登录

```bash
docker compose up -d --build
```

启动后浏览器打开站点，**未登录时会自动跳到 `/setup` 登录引导页**：按步骤填手机号 → 收到的验证码 → （如开启两步验证）2FA 密码，登录完成自动放行，全程在网页内完成。

> 也可以继续用命令行登录（效果相同，二选一）：
> ```bash
> docker exec -it tgchannelhub python3 scripts/login.py send <你的手机号>
> docker exec -it tgchannelhub python3 scripts/login.py sign <收到的验证码>
> # 如果账号开了两步验证：
> docker exec -it tgchannelhub python3 scripts/login.py password <2FA密码>
> ```

登录完成后刷新网页即可。session 文件保存在 `data/` 目录，重启不丢。

## 📁 数据都存哪（重要）

所有数据落在 compose 挂载的 `./data` 目录，**备份/迁移只需打包这个目录**：

| 文件 | 作用 |
|---|---|
| `tgchannelhub.session` | Telegram 登录会话（账号"钥匙"，**千万别泄露**） |
| `channels.json` | 已代理的频道列表 |
| `passwords.json` | 你在管理页改过的密码（优先于环境变量） |
| `thumbs/` | 缩略图缓存 |
| `avatar_*.jpg` | 频道头像缓存 |

备份：`tar czf tgchannelhub-data.tar.gz data/`；迁移到新机器解压到新部署的 `data/` 即可。

## 🔗 访问方式

| 路径 | 说明 | 密码 |
|---|---|---|
| `/` | 管理入口（自动跳管理页） | 管理密码 |
| `/manage` | 管理页：增删频道 / 复制分享链接 / 改密码 | 管理密码 |
| `/c` | 频道选择页（多频道时展示所有） | 分享密码 |
| `/c/<频道>` | 单频道锁定页（只可看该频道） | 分享密码 |
| `/watch/<id>/<name>` | 视频/图片播放页 | 分享密码 |
| `/upload` | 上传中心（把文件传到你的 TG 群组/频道） | 管理密码 |
| `/adfilter` | 广告过滤配置（关键词 / 链接黑名单） | 管理密码 |

**典型用法**：你自己用 `/manage` 管理；把 `/c/<频道>` 链接分享给别人，他们输分享密码即可看。

## 🌐 反向代理（可选，公网访问）

局域网直接用 `http://<NAS-IP>:8081` 即可。公网建议反代（Lucky/Nginx/Caddy 均可）：

```
https://video.example.com → 容器 8081 端口
```

同时把 `PUBLIC_URL` 改成 `https://video.example.com`，分享链接会自动用新地址。

## 🧩 常用配置示例

**只代理一个频道，直连可通：**

```yaml
environment:
  - TG_API_ID=1234567
  - TG_API_HASH=abcdef...
  - PUBLIC_URL=http://192.168.1.100:8081
  - TG_UI_PASSWORD=my_share_pw
  - TG_ADMIN_PASSWORD=my_admin_pw
```

**代理多个频道：** 启动后在 `/manage` 页面点「加载我的频道」或手动输入 `t.me/xxx` 添加，无需改配置重启。

**需要代理才能连 TG：** 打开 `TG_PROXY_*` 四行配置（HTTP 代理格式；`TG_PROXY_TYPE` 支持 `http`/`socks5`，默认 http）。

## 📤 上传中心（个人网盘）

把 Telegram 群组当个人网盘：网页里把 NAS 文件直接传到你的群组/频道，再通过本项目的网页回看。

- **入口**：管理页「📤 上传中心」→ `/upload`（管理密码保护）
- **文件浏览器**：浏览 `UPLOAD_ROOT`（compose volume 挂载的目录）内文件，多选文件或整目录（递归）
- **目的地**：下拉加载你账号下所有群组/频道（`get_dialogs`），或手输 `@用户名` / chat_id（如个人网盘群组 ID）
- **队列**：并发文件数可调（1-16 同时传），暂停/继续，单任务取消
- **并行加速**：>10MB 文件按 512KB 分块 × 8 并发上传（`SaveBigFilePart` + `InputFileBig`），实测比串行快约 4 倍
- **实时进度**：进度条 + % + MB/s + 耗时，2 秒自动刷新
- **目录监控**：指定目录后新文件自动入队（mtime 稳定判定、按父目录自动打 `#标签`）
- 代理/凭据全部由 compose 环境变量注入；上传引擎复用主 client（不重新登录、无 session 冲突）

## 🛡 广告过滤

频道里混入的推广消息（引流广告、博彩推广等）可以按**关键词 + 链接黑名单**整条隐藏（含媒体）。

- **入口**：管理页「🛡 广告过滤」→ `/adfilter`（管理密码保护）
- **关键词**：caption 命中任一关键词即隐藏，不区分大小写；默认带一组实测词表（极搜/JISOU、内群/赞助群、博彩等）
- **链接黑名单**：caption 里含指定链接（如 `t.me/xxx`）即隐藏
- **热生效**：保存后 30 秒内自动生效，无需重启；词表存在 `data/ad_filter.json`，可随时改回
- **纯媒体消息**（无文字 caption）一律视为正常内容，不过滤

## 🛠 开发 / 本地运行

```bash
pip install -r requirements.txt -e .
python3 -m channelrelay
```

项目结构：

```
├── channelrelay/
│   ├── channel_ui.py        # Web UI 全部路由（列表/播放/管理/认证/广告过滤）
│   ├── setup_ui.py          # 登录引导页（/setup：手机号→验证码→2FA）
│   ├── telegram.py          # Telegram 客户端 + 代理注入
│   ├── paralleltransfer.py  # 并发分块下载器（Telethon 兼容补丁）
│   ├── config.py            # 环境变量配置
│   ├── util.py              # packed id 编解码 / 文件名
│   └── __main__.py          # 启动入口（含未登录重定向 middleware）
├── scripts/login.py         # 首次登录辅助（send/sign/password/verify）
└── docker-compose.yml       # 开箱即用的部署模板
```

## 📄 开源协议

**AGPL-3.0**。本项目是 [tgfilestream](https://mau.dev/tulir/tgfilestream)（Tulir Asokan）的衍生作品，按 AGPL-3.0 要求保留原作者版权声明，详见 [LICENSE](LICENSE)。

## ⚠️ 免责声明

本项目仅供学习与技术交流。请遵守 Telegram 服务条款与当地法律法规，**不要**用于传播侵权或违规内容。所有媒体均来自你授权访问的 Telegram 频道。

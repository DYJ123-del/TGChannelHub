# ChannelRelay — Telegram 频道视频中转

基于 [tgfilestream](https://mau.dev/tulir/tgfilestream) 深度改造的 **Telegram 频道视频网页中转**。访客通过浏览器直接观看/下载频道视频，NAS 实时从 Telegram 拉流，**不落盘**。

已从原版单频道 bot 模式重构为**多频道可视化管理系统**。

## 特性

- 🎬 **实时流式中转**：视频不落地，边拉边播（支持 HTTP Range 拖拽进度）
- 📚 **多频道管理**：管理页添加/删除频道，一键复制分享链接
- 🖼️ **相册聚合**：同组图片/视频按 Telegram 风格合并展示（首图大图 + 网格）
- 📱 **移动端优化**：微信内置浏览器（X5 内核）可直接播放
- 🔒 **双密码体系**：分享密码（访客看频道）+ 管理密码（管理页）
- 🚀 **多线程下载**：并发分块拉流，播放启动快（DC 连接预热）

## 快速开始（Docker）

```bash
git clone https://github.com/<your-username>/channelrelay.git
cd channelrelay

# 配置环境变量（见下），然后构建启动
docker compose up -d --build
```

## 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `TG_API_ID` / `TG_API_HASH` | Telegram API 凭证（[my.telegram.org](https://my.telegram.org/apps)） | **必填** |
| `TG_SESSION_NAME` | Telethon session 文件路径 | `/app/data/channelrelay` |
| `TG_PROXY_HOST` / `TG_PROXY_PORT` / `TG_PROXY_USER` / `TG_PROXY_PASS` | 代理（可选，直连 TG 超时场景必配） | 无 |
| `TG_UI_PASSWORD` | 分享密码（访客） | `1234` ⚠️ 部署后必须改 |
| `TG_ADMIN_PASSWORD` | 管理密码 | `123321` ⚠️ 部署后必须改 |
| `PUBLIC_URL` | 公网访问地址（用于生成分享链接） | `http://<host>:<port>` |
| `PORT` / `HOST` | 监听端口/地址 | `8081` / `0.0.0.0` |

> 密码可通过管理页「修改密码」持久化到 `/app/data/passwords.json`，优先于环境变量。

## 首次登录

1. 首次启动需登录 Telegram 账号（收藏夹/频道所属账号）。项目提供一个独立的登录脚本 `scripts/login.py`：
   ```bash
   docker run --rm -v $(pwd)/data:/app/data \
     -e TG_API_ID=... -e TG_API_HASH=... -e TG_PROXY_HOST=... \
     -e TG_SESSION_NAME=/app/data/channelrelay \
     --entrypoint python3 <image> /app/data/login.py
   ```
   （或在本机用 Telethon 交互登录后，把 session 文件放到挂载的 `data/` 目录）
2. 登录完成后正常启动服务。

## 访问方式

| 路径 | 说明 |
|---|---|
| `/` | 管理入口（302 → `/manage`） |
| `/manage` | 管理页：加频道/复制分享/改密码（管理密码） |
| `/c` | 频道选择页（分享密码，多频道时） |
| `/c/<频道>` | 锁定单频道（分享密码，只能看这一个） |
| `/watch/<id>/<name>` | 视频/图片播放页 |

## 反向代理（可选）

以 Lucky/Nginx 为例，把 `tg1.example.com:56200` 反代到容器 `8081` 端口即可获得公网访问。分享链接由 `PUBLIC_URL` 生成。

## 开发

```bash
# 本地运行
pip install -r requirements.txt -e .
python3 -m channelrelay
```

### 项目结构

```
channelrelay/
├── channel_ui.py        # Web UI 全部路由（列表/播放/管理/认证）
├── telegram.py          # Telegram 客户端 + 代理注入
├── paralleltransfer.py  # 并发分块下载器（Telethon 1.4x 兼容）
├── config.py            # 环境变量配置
├── util.py              # packed id 编解码 / 文件名
└── __main__.py          # 启动入口
```

## 开源协议

**AGPL-3.0**。本仓库是 [tgfilestream](https://mau.dev/tulir/tgfilestream)（作者 Tulir Asokan）的衍生作品，遵循 AGPL-3.0 要求保留原作者版权声明。见 [LICENSE](LICENSE)。

## 免责声明

本项目仅供学习与技术交流。请遵守 Telegram 服务条款及所在地区法律法规，不要用于侵权内容传播。
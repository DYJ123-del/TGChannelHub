FROM python:3.11-slim-bookworm
WORKDIR /app
COPY . /app
# 构建前校验：必须是含 channelrelay 包的源码（防止误用原版 tgfilestream）
RUN test -f /app/channelrelay/channel_ui.py || (echo "ERROR: 缺少 channel_ui.py，源码不完整" && exit 1) && \
    pip install --no-cache-dir -r /app/requirements.txt pysocks python-socks -i https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip install --no-cache-dir /app -i https://pypi.tuna.tsinghua.edu.cn/simple
WORKDIR /app
CMD ["python3", "-m", "channelrelay"]
FROM python:3.13-slim

ARG TARGETARCH
ARG BDPAN_VERSION=3.8.7
ARG BDPAN_SHA256_AMD64=53ba69b062ddacd4a25ea0bd9e112253a0594d2ee1911ca120ef73559f70eacf
ARG BDPAN_SHA256_ARM64=0b5be7541ae4d9a4da0974aaa3f84aab43374e9314294d27cfcf995b1bb08f1d

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) dir=linux;     sha="${BDPAN_SHA256_AMD64}" ;; \
      arm64) dir=linux-arm; sha="${BDPAN_SHA256_ARM64}" ;; \
      *) echo "unsupported arch: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL -o /tmp/bdpan.tar.gz \
      "https://issuecdn.baidupcs.com/issue/netdisk/ai-bdpan/${dir}/${BDPAN_VERSION}/bdpan-${BDPAN_VERSION}-linux-${TARGETARCH}.tar.gz"; \
    echo "${sha}  /tmp/bdpan.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/bdpan.tar.gz -C /tmp; \
    install -m 0755 /tmp/bdpan/bdpan /usr/local/bin/bdpan; \
    rm -rf /tmp/bdpan /tmp/bdpan.tar.gz; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /srv
COPY requirements.txt .
COPY shop/requirements.txt shop/requirements.txt
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt -r shop/requirements.txt

COPY app ./app
COPY web ./web
COPY templates ./templates
COPY shop ./shop

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/bdpan /downloads \
    && chown -R app:app /data /downloads

ENV BDPAN_CONFIG_DIR=/data/bdpan \
    BAIDU_EASY_ADDR=:8080 \
    BAIDU_EASY_DOWNLOAD_DIR=/downloads \
    BAIDU_EASY_TASKS_FILE=/data/tasks.json \
    SHOP_ADDR=:8081 \
    SHOP_RETRIES_FILE=/data/shop-retries.json \
    BAIDU_EASY_URL=http://127.0.0.1:8080 \
    PYTHONUNBUFFERED=1

VOLUME ["/data", "/downloads"]
EXPOSE 8080 8081

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

CMD ["/usr/local/bin/entrypoint.sh"]

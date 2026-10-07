FROM python:3.13-slim

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /srv
COPY requirements.txt ./shop/requirements.txt
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r shop/requirements.txt

COPY . ./shop

RUN useradd --create-home --uid 1000 app
USER app

ENV SHOP_ADDR=:8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "-m", "shop.app"]

# 番茄小说下载器（网页版）—— 纯网页服务，不需要虚拟桌面
FROM ubuntu:24.04

ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV DEBIAN_FRONTEND=noninteractive TZ=Asia/Shanghai LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH

# tesseract + 中文语言包：识别网页正文的混淆字体；Noto CJK：画参考字
RUN sed -i -E "s#https?://(archive|security)\.ubuntu\.com#http://${APT_MIRROR}#g" /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv tesseract-ocr tesseract-ocr-chi-sim fonts-noto-cjk ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
 && pip install --no-cache-dir -i "${PIP_INDEX}" -r /tmp/requirements.txt

COPY app /opt/app
VOLUME ["/data", "/downloads"]
EXPOSE 8084
CMD ["python", "/opt/app/app.py"]

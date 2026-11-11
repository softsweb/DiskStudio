FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates libicu-dev libssl3 xorriso \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir Pillow

ARG DIS_VERSION=v1.3.6
ARG DIS_SHA256=b559cb0c072b61ae497cd9e953bc0be1de6ed79003977ccf2430e5ac4a9ba980
ARG DIS_REPO=https://github.com/JiaFeiMiao-K-Cat/DiscImageStudio
# Presets file and calibration target come from this upstream commit (the v1.3.6 tag has fewer presets)
ARG DIS_REF=fb0a59836503384a425ad11a21a0f96febd0f60d

# Upstream engine (Linux CLI release) plus its built-in presets and calibration target
RUN mkdir -p /app/cli /app/repo/src/DiscImageStudio.Core/Presets /app/repo/src/DiscImageStudio.Core/Calibration/Patterns \
 && curl -fsSL -o /tmp/cli.tar.gz ${DIS_REPO}/releases/download/${DIS_VERSION}/DiscImageStudio-CLI-${DIS_VERSION}-linux-x64.tar.gz \
 && echo "${DIS_SHA256}  /tmp/cli.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/cli.tar.gz -C /app/cli \
 && rm /tmp/cli.tar.gz \
 && curl -fsSL -o /app/repo/src/DiscImageStudio.Core/Presets/built-in-disc-presets.json \
      https://raw.githubusercontent.com/JiaFeiMiao-K-Cat/DiscImageStudio/${DIS_REF}/src/DiscImageStudio.Core/Presets/built-in-disc-presets.json \
 && curl -fsSL -o /app/repo/src/DiscImageStudio.Core/Calibration/Patterns/target_slim.png \
      https://raw.githubusercontent.com/JiaFeiMiao-K-Cat/DiscImageStudio/${DIS_REF}/src/DiscImageStudio.Core/Calibration/Patterns/target_slim.png \
 && chmod +x /app/cli/disc-image-studio

COPY . /app/webui
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8765 DATA_DIR=/data DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
VOLUME /data
EXPOSE 8765
WORKDIR /app/webui
CMD ["python3", "server.py"]

FROM python:3.12-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends libmagic1 \
    && rm -rf /var/lib/apt/lists/*
ENV POETRY_VIRTUALENVS_CREATE=false PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir poetry
WORKDIR /app
COPY pyproject.toml poetry.lock* README.md ./
COPY src ./src

FROM base AS dev
# Tools for the image-mock system tests (loop devices, mkfs, FUSE drivers, device-mapper).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ntfs-3g exfatprogs exfat-fuse dosfstools e2fsprogs util-linux mount dmsetup fuse3 \
    smartmontools procps fdisk \
    && rm -rf /var/lib/apt/lists/*
RUN poetry install
WORKDIR /app
CMD ["bash"]

FROM base AS runtime
RUN poetry install --only main \
    && useradd --create-home drivecon
USER drivecon
ENTRYPOINT ["drivecon"]

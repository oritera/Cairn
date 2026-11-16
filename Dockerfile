FROM ghcr.io/astral-sh/uv:python3.13-trixie

COPY ./cairn/pyproject.toml /cairn/pyproject.toml
COPY ./cairn/uv.lock /cairn/uv.lock
WORKDIR /cairn
RUN uv sync --frozen --no-install-project -i https://mirrors.aliyun.com/pypi/simple/

COPY ./cairn /cairn
RUN uv sync --frozen -i https://mirrors.aliyun.com/pypi/simple/

# The host filesystem applies transparent file encryption whose policy covers .md, so every .md
# file that COPY just brought in is CIPHERTEXT -- the container's python cannot decode it and the
# dispatcher dies with UnicodeDecodeError while validating prompts. .tar.gz is not covered by that
# policy, so the same files travel in prompts-clean.tar.gz and are recreated here, inside the
# container's own filesystem, where the host driver is not in the path.
# Check with: docker run --rm --entrypoint sha256sum cairn-app /cairn/src/cairn/dispatcher/prompts/default/reason.md
COPY ./prompts-clean.tar.gz /tmp/prompts-clean.tar.gz
RUN tar xzf /tmp/prompts-clean.tar.gz -C /
RUN rm -rf /tmp/prompts-clean.tar.gz || true

ENV TZ=Asia/Shanghai
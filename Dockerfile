# Run the web UI on a Linux server, with agent code sandboxed by bubblewrap.
#
#   docker build -t agent-company .
#   docker run -p 8501:8501 --env-file .env \
#     --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
#     -v agent-company-projects:/app/projects agent-company
#
# Why the two --security-opt: Docker blocks the kernel features bubblewrap
# needs (new namespaces, mounts) by default. Without them, sandbox.py notices
# that bwrap does not work and refuses to run agent code (fail closed).
# The container itself still runs as a normal user, not root.

FROM python:3.12-slim

# bubblewrap: the sandbox for agent code (sandbox.py).
# git: DevOps commits and tags each product (git_ops.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap git \
    && rm -rf /var/lib/apt/lists/*

# A normal user: agent code never runs as root, even inside the sandbox.
RUN useradd --create-home app
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore keeps out .env, venv/, projects/ and other local files.
COPY . .
RUN mkdir -p projects workspace logs && chown -R app:app /app
USER app

EXPOSE 8501
# .streamlit/config.toml says "localhost only"; inside a container we must
# listen on all interfaces, or the published port reaches nothing.
CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]

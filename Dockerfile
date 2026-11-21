# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir -r /tmp/requirements.lock coverage==7.17.4 pytest-cov==7.1.0
COPY round3_sqlglot.tar.gz /tmp/round3_sqlglot.tar.gz
RUN echo '1f57574a55da6813c7b420afc7679f3470cd0337c1ac279a19a1fb615baed3b2  /tmp/round3_sqlglot.tar.gz' | sha256sum -c - \
    && mkdir -p /opt/sqlglot \
    && tar -xzf /tmp/round3_sqlglot.tar.gz -C /opt/sqlglot \
    && echo eb11aec4be8cb2122afe713a4816662c3f0fb2fa > /opt/sqlglot/.revision
ENV PYTHONDONTWRITEBYTECODE=1
COPY selector.py scheduler.py impact.py patch_diff.py equivalent_edits.py round3_*.py impact_index.json.gz /app/
CMD ["python", "/app/selector.py"]

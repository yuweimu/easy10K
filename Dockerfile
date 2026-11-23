FROM python:3.15.2-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY main.py line_item_chart.py line_item_storage.py report_metadata.py report_renderer.py digest_bs.py line-item-chart.txt schema.sql ./
COPY report_view ./report_view
COPY markdown ./markdown
COPY preprocessed ./preprocessed

# CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
CMD ["ls"]
